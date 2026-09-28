"""Market data sources.

CcxtMarketData reads public venue data through ccxt's async REST client. It keeps
one client per venue, so markets load once instead of on every request. It only
reads markets, tickers and order books: no account data, no order placement.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Protocol

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

    def __init__(self) -> None:
        self._clients: dict[str, Any] = {}
        self._markets: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._health: dict[str, dict] = {}

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
