"""Market data sources.

CcxtMarketData reads public venue data through ccxt's async REST client. It keeps
one client per venue, so markets load once instead of on every request.
StreamingMarketData keeps order books live over ccxt.pro websockets and uses the
REST source for everything else and as a fallback. Both only read public market
data: no account data, no order placement.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import statistics
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional, Protocol

from arb.config import DeskConfig
from arb.engine import AmountRule, FeeModel, MarketRules
from arb.orderbook import BookSnapshot, to_levels
from arb.protocol import EvidenceLabel, iso, utc_now, weakest

# ccxt precision modes (ccxt.DECIMAL_PLACES, ccxt.SIGNIFICANT_DIGITS, ccxt.TICK_SIZE).
DECIMAL_PLACES, SIGNIFICANT_DIGITS, TICK_SIZE = 2, 3, 4


class MarketDataError(Exception):
    def __init__(self, venue: str, message: str):
        super().__init__(f"{venue}: {message}")
        self.venue = venue


@dataclass(frozen=True)
class VenueMeta:
    precision_mode: Optional[int]
    default_fee_side: Optional[str]
    ioc_supported: Optional[bool]
    has_fetch_tickers: Optional[bool]
    # The venue rounds amounts with its own code (e.g. bitfinex), so the generic rule would be a guess.
    custom_amount_rounding: bool = False


class MarketDataSource(Protocol):
    evidence_label: EvidenceLabel
    source_name: str

    def monotonic(self) -> float: ...

    async def load_markets(self, venue: str) -> dict: ...

    async def fetch_tickers(self, venue: str) -> dict: ...

    async def fetch_order_book(self, venue: str, symbol: str, depth: int) -> BookSnapshot: ...

    def venue_meta(self, venue: str) -> VenueMeta: ...

    def status(self) -> dict: ...

    async def close(self) -> None: ...


def amount_rule(market: dict, precision_mode: Optional[int], custom_rounding: bool = False) -> Optional[AmountRule]:
    value = (market.get("precision") or {}).get("amount")
    if value is None or custom_rounding:
        return None
    value = Decimal(str(value))
    if precision_mode == TICK_SIZE and value > 0:
        return AmountRule("step", value)
    if precision_mode == DECIMAL_PLACES:
        return AmountRule("step", Decimal(1).scaleb(-int(value)))
    if precision_mode == SIGNIFICANT_DIGITS and value > 0:
        return AmountRule("significant_digits", value)
    return None


def market_rules(venue: str, market: dict, meta: VenueMeta) -> MarketRules:
    limits = market.get("limits") or {}

    def minimum(kind: str) -> Optional[Decimal]:
        value = (limits.get(kind) or {}).get("min")
        return None if value is None else Decimal(str(value))

    return MarketRules(
        venue=venue,
        symbol=market["symbol"],
        base=market["base"],
        quote=market["quote"],
        active=market.get("active"),
        amount_rule=amount_rule(market, meta.precision_mode, meta.custom_amount_rounding),
        min_amount=minimum("amount"),
        min_cost=minimum("cost"),
        market_id=market.get("id"),
    )


def fee_model(venue: str, market: dict, meta: VenueMeta, config: DeskConfig, data_label: EvidenceLabel) -> FeeModel:
    """The user's declared fee tier wins; otherwise ccxt defaults, which are not account-specific."""
    tier = (config.fee_tiers or {}).get(venue)
    if tier is not None:
        return FeeModel(tier.taker_rate, tier.fee_side, tier.evidence_label, tier.evidence_label, f"user-declared fee tier: {tier.source}")
    side = market.get("feeSide") or meta.default_fee_side
    side_label = weakest([EvidenceLabel.ESTIMATED, data_label]) if side else EvidenceLabel.UNKNOWN
    side_note = "" if side else "; fee asset unknown, assumed to be the received asset"
    taker = market.get("taker")
    if taker is None or market.get("percentage") is False:
        return FeeModel(None, side or "get", EvidenceLabel.UNKNOWN, side_label, f"no percentage taker fee in metadata for {market['symbol']}")
    return FeeModel(
        Decimal(str(taker)),
        side or "get",
        weakest([EvidenceLabel.ESTIMATED, data_label]),
        side_label,
        f"ccxt market metadata: default tier, not account-specific{side_note}",
    )


class CcxtMarketData:
    evidence_label = EvidenceLabel.VERIFIED
    source_name = "ccxt public REST"

    detail = (
        "Public REST order books, bulk tickers and market metadata. These are snapshots: legs are fetched one "
        "after another, so skew is measured and reported."
    )

    def __init__(self) -> None:
        self._clients: dict[str, Any] = {}
        self._markets: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._health: dict[str, dict] = {}
        self._round_trips: dict[str, deque] = {}

    def monotonic(self) -> float:
        return time.monotonic()

    def _client(self, venue: str) -> Any:
        import ccxt
        import ccxt.async_support as ccxt_async

        if venue not in ccxt.exchanges:
            raise MarketDataError(venue, "not a ccxt exchange id")
        client = self._clients.get(venue)
        if client is None:
            client = getattr(ccxt_async, venue)({"enableRateLimit": True})
            self._clients[venue] = client
        return client

    async def _call(self, venue: str, awaitable) -> Any:
        try:
            result = await awaitable
        except Exception as exc:  # ccxt raises many network and exchange error types
            self._health[venue] = {
                **self._health.get(venue, {}),
                "last_error": f"{type(exc).__name__}: {exc}"[:300],
                "last_error_at": iso(utc_now()),
            }
            raise MarketDataError(venue, f"{type(exc).__name__}: {exc}"[:300]) from exc
        self._health[venue] = {**self._health.get(venue, {}), "last_success_at": iso(utc_now())}
        return result

    async def load_markets(self, venue: str) -> dict:
        lock = self._locks.setdefault(venue, asyncio.Lock())
        async with lock:
            if venue not in self._markets:
                client = self._client(venue)
                self._markets[venue] = await self._call(venue, client.load_markets())
        return self._markets[venue]

    async def fetch_tickers(self, venue: str) -> dict:
        await self.load_markets(venue)
        return await self._call(venue, self._client(venue).fetch_tickers())

    async def fetch_order_book(self, venue: str, symbol: str, depth: int) -> BookSnapshot:
        await self.load_markets(venue)
        client = self._client(venue)
        sent_at, sent = datetime.now(timezone.utc), time.monotonic()
        raw = await self._call(venue, client.fetch_order_book(symbol, depth))
        received, received_at = time.monotonic(), datetime.now(timezone.utc)
        self._round_trips.setdefault(venue, deque(maxlen=20)).append(received - sent)
        timestamp = raw.get("timestamp")
        nonce = raw.get("nonce")
        return BookSnapshot(
            venue=venue,
            symbol=symbol,
            bids=to_levels(raw.get("bids") or []),
            asks=to_levels(raw.get("asks") or []),
            snapshot_id=uuid.uuid4().hex,
            exchange_nonce=None if nonce is None else str(nonce),
            event_time=datetime.fromtimestamp(timestamp / 1000, timezone.utc) if timestamp else None,
            request_sent_at=sent_at,
            received_at=received_at,
            sent_monotonic=sent,
            received_monotonic=received,
            source="ccxt public REST order book",
            evidence_label=EvidenceLabel.VERIFIED,
        )

    async def forward_order_book(self, venue: str, symbol: str, depth: int) -> BookSnapshot:
        """The book an order sent now would meet; the REST round trip is the latency."""
        return await self.fetch_order_book(venue, symbol, depth)

    def median_rtt_s(self, venue: str) -> Optional[float]:
        """Median measured REST order book round trip, or None before any measurement."""
        samples = self._round_trips.get(venue)
        return statistics.median(samples) if samples else None

    def venue_meta(self, venue: str) -> VenueMeta:
        from ccxt.async_support.base.exchange import Exchange as AsyncExchange

        client = self._client(venue)
        features = getattr(client, "features", None) or {}
        spot = features.get("spot") if isinstance(features, dict) else None
        create = spot.get("createOrder") if isinstance(spot, dict) else None
        tif = create.get("timeInForce") if isinstance(create, dict) else None
        trading = (client.fees or {}).get("trading") or {}
        return VenueMeta(
            precision_mode=client.precisionMode,
            default_fee_side=trading.get("feeSide"),
            ioc_supported=tif.get("IOC") if isinstance(tif, dict) else None,
            has_fetch_tickers=client.has.get("fetchTickers"),
            custom_amount_rounding=type(client).amount_to_precision is not AsyncExchange.amount_to_precision,
        )

    def status(self) -> dict:
        return {"source": self.source_name, "evidence_label": self.evidence_label.value, "venues": self._health}

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()


# Streaming ---------------------------------------------------------------------

FIRST_UPDATE_TIMEOUT_S = 10.0
MAX_STREAMS_PER_VENUE = 60
IDLE_EVICT_S = 900.0
RETRY_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)


def _default_pro_factory(venue: str) -> Any:
    import ccxt.pro as ccxtpro

    if venue not in ccxtpro.exchanges:
        return None
    return getattr(ccxtpro, venue)({"enableRateLimit": True})


def _rejected_depth(exc: Exception) -> bool:
    import ccxt

    return isinstance(exc, (ccxt.NotSupported, ccxt.BadRequest))


@dataclass
class _Stream:
    venue: str
    symbol: str
    limit: Optional[int]
    last_used: float
    task: Optional[asyncio.Task] = None
    book: Optional[dict] = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    failed: asyncio.Event = field(default_factory=asyncio.Event)
    last_update_monotonic: Optional[float] = None
    last_update_at: Optional[datetime] = None
    updates: int = 0
    rebuilds: int = 0
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None
    waited: bool = False  # the first-update wait already ran once; never block on it again


class StreamingMarketData:
    """Order books from ccxt.pro websocket streams; REST for everything else.

    Each symbol in use keeps one live subscription and a snapshot copies the current
    streamed book. Its age runs from the last update received, so a quiet market
    reads as older than it is: conservative, never optimistic. When a stream is not
    offered, fails, or has not delivered a first update in time, the REST snapshot
    is used instead and its source says so.
    """

    source_name = "ccxt.pro websocket order books (REST fallback)"
    detail = (
        "Order books stream over websockets through ccxt.pro, one live subscription per symbol in use. Markets and "
        "tickers come from REST, and REST snapshots replace any stream that fails or is not offered."
    )

    def __init__(
        self,
        rest: Any,
        pro_factory: Optional[Callable[[str], Any]] = None,
        first_update_timeout: float = FIRST_UPDATE_TIMEOUT_S,
        max_streams_per_venue: int = MAX_STREAMS_PER_VENUE,
        idle_evict_s: float = IDLE_EVICT_S,
        retry_backoff: tuple[float, ...] = RETRY_BACKOFF_S,
    ) -> None:
        self.rest = rest
        self.evidence_label = rest.evidence_label
        self._pro_factory = pro_factory or _default_pro_factory
        self._first_update_timeout = first_update_timeout
        self._max_streams = max_streams_per_venue
        self._idle_evict_s = idle_evict_s
        self._backoff = retry_backoff
        self._clients: dict[str, Any] = {}
        self._streams: dict[tuple[str, str], _Stream] = {}

    def monotonic(self) -> float:
        return self.rest.monotonic()

    async def load_markets(self, venue: str) -> dict:
        return await self.rest.load_markets(venue)

    async def fetch_tickers(self, venue: str) -> dict:
        return await self.rest.fetch_tickers(venue)

    def venue_meta(self, venue: str) -> VenueMeta:
        return self.rest.venue_meta(venue)

    def _client(self, venue: str) -> Any:
        if venue not in self._clients:
            self._clients[venue] = self._pro_factory(venue)  # None: the venue has no stream client
        return self._clients[venue]

    async def prepare_order_books(self, venue: str, symbols: list[str], depth: int) -> None:
        """Subscribe to every symbol at once and wait, bounded, for their first updates."""
        streams = [await self._stream(venue, symbol, depth) for symbol in symbols]
        await asyncio.gather(*(self._first_update(s) for s in streams if s is not None))

    async def _first_update(self, stream: _Stream) -> None:
        """Wait once, bounded, until a new stream delivers its first book or fails."""
        if stream.updates or stream.waited:
            return
        stream.waited = True
        outcomes = [asyncio.create_task(stream.ready.wait()), asyncio.create_task(stream.failed.wait())]
        _, pending = await asyncio.wait(outcomes, timeout=self._first_update_timeout, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

    async def fetch_order_book(self, venue: str, symbol: str, depth: int) -> BookSnapshot:
        stream = await self._stream(venue, symbol, depth)
        if stream is None:
            return await self.rest.fetch_order_book(venue, symbol, depth)
        await self._first_update(stream)
        if not stream.ready.is_set() or stream.book is None:
            return await self.rest.fetch_order_book(venue, symbol, depth)  # never serve a book being rebuilt
        stream.last_used = self.monotonic()
        return self._snapshot(stream, depth)

    async def forward_order_book(self, venue: str, symbol: str, depth: int) -> BookSnapshot:
        """The book an order sent now would meet, after the venue's measured REST round trip."""
        measure = getattr(self.rest, "median_rtt_s", None)
        delay = measure(venue) if measure else None
        if delay is None:
            return await self.rest.fetch_order_book(venue, symbol, depth)  # measures the round trip
        await asyncio.sleep(delay)
        return await self.fetch_order_book(venue, symbol, depth)

    async def _stream(self, venue: str, symbol: str, depth: int) -> Optional[_Stream]:
        key = (venue, symbol)
        stream = self._streams.get(key)
        if stream is not None and stream.task is not None and not stream.task.done():
            return stream
        client = self._client(venue)
        if client is None or not client.has.get("watchOrderBook"):
            return None
        await self._evict_idle(venue)
        if stream is None:
            if sum(1 for v, _ in self._streams if v == venue) >= self._max_streams:
                return None
            stream = _Stream(venue, symbol, depth, last_used=self.monotonic())
            self._streams[key] = stream
        stream.task = asyncio.create_task(self._watch(client, stream))
        return stream

    async def _watch(self, client: Any, stream: _Stream) -> None:
        failures = 0
        while True:
            try:
                book = await client.watch_order_book(stream.symbol, stream.limit)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # ccxt raises network, checksum and nonce errors here
                if stream.limit is not None and _rejected_depth(exc):
                    stream.limit = None  # the venue rejects this depth; use its default
                    continue
                stream.ready.clear()
                stream.failed.set()
                stream.book = None
                if stream.updates:
                    stream.rebuilds += 1
                stream.last_error = f"{type(exc).__name__}: {exc}"[:300]
                stream.last_error_at = iso(utc_now())
                await asyncio.sleep(self._backoff[min(failures, len(self._backoff) - 1)])
                failures += 1
                continue
            failures = 0
            stream.book = book
            stream.updates += 1
            stream.last_update_monotonic = self.monotonic()
            stream.last_update_at = datetime.now(timezone.utc)
            stream.ready.set()

    def _snapshot(self, stream: _Stream, depth: int) -> BookSnapshot:
        book = stream.book
        timestamp = book.get("timestamp")
        nonce = book.get("nonce")
        return BookSnapshot(
            venue=stream.venue,
            symbol=stream.symbol,
            bids=to_levels(book["bids"][:depth]),
            asks=to_levels(book["asks"][:depth]),
            snapshot_id=f"ws-{uuid.uuid4().hex}",
            exchange_nonce=None if nonce is None else str(nonce),
            event_time=datetime.fromtimestamp(timestamp / 1000, timezone.utc) if timestamp else None,
            request_sent_at=stream.last_update_at,
            received_at=stream.last_update_at,
            sent_monotonic=stream.last_update_monotonic,
            received_monotonic=stream.last_update_monotonic,
            source="ccxt.pro websocket order book",
            evidence_label=self.evidence_label,
            sequence_check=(
                "ccxt.pro raises on gaps where the venue sends sequence numbers, and the book is then rebuilt; "
                f"{stream.rebuilds} rebuilds so far."
            ),
            checksum=self._checksum_note(stream.venue),
            observed_monotonic=self.monotonic(),
        )

    def _checksum_note(self, venue: str) -> str:
        client = self._clients.get(venue)
        try:
            value = client.handle_option("watchOrderBook", "checksum", None)
        except Exception:
            value = None
        if value is True:
            return "Validated by ccxt.pro for this venue (watchOrderBook.checksum = True)."
        if value is False:
            return "Off in ccxt.pro for this venue (watchOrderBook.checksum = False, the ccxt default)."
        return "ccxt.pro has no checksum option for this venue."

    async def _stop(self, stream: _Stream) -> None:
        if stream.task is not None and not stream.task.done():
            stream.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stream.task
        client = self._clients.get(stream.venue)
        if client is not None and client.has.get("unWatchOrderBook"):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.un_watch_order_book(stream.symbol), 5)

    async def _evict_idle(self, venue: str) -> None:
        now = self.monotonic()
        for key, stream in list(self._streams.items()):
            if key[0] == venue and now - stream.last_used > self._idle_evict_s:
                await self._stop(stream)
                del self._streams[key]

    def status(self) -> dict:
        now = self.monotonic()
        streams: dict[str, dict] = {}
        for (venue, symbol), s in self._streams.items():
            streams.setdefault(venue, {})[symbol] = {
                "ready": s.ready.is_set(),
                "updates": s.updates,
                "rebuilds": s.rebuilds,
                "last_update_age_ms": None
                if s.last_update_monotonic is None
                else math.ceil(max(0.0, now - s.last_update_monotonic) * 1000),
                "depth_limit": s.limit,
                "last_error": s.last_error,
                "last_error_at": s.last_error_at,
            }
        return {**self.rest.status(), "source": self.source_name, "streams": streams}

    async def close(self) -> None:
        tasks = [s.task for s in self._streams.values() if s.task is not None and not s.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._streams.clear()
        for client in self._clients.values():
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
        self._clients.clear()
        await self.rest.close()
