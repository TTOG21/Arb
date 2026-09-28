"""Websocket order books through a fake ccxt.pro client (no network)."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal as D

import ccxt
import pytest

from arb.config import DeskConfig
from arb.market_data import CcxtMarketData, StreamingMarketData
from arb.protocol import EvidenceLabel
from arb.service import DeskService
from conftest import TRIANGLE_BOOKS, VENUE, full_config, triangle_md

VERIFIED = EvidenceLabel.VERIFIED
TS = 1_790_000_000_000  # exchange timestamp in ms


def book(bids, asks, nonce=1):
    return {
        "bids": [[float(p), float(a)] for p, a in bids],
        "asks": [[float(p), float(a)] for p, a in asks],
        "timestamp": TS,
        "nonce": nonce,
    }


class FakePro:
    """Speaks the part of the ccxt.pro API the adapter uses."""

    def __init__(self, checksum=None, streams=True, reject_depth=False):
        self.has = {"watchOrderBook": streams, "unWatchOrderBook": True}
        self.queues: dict[str, asyncio.Queue] = {}
        self.depths: list = []
        self.unwatched: list[str] = []
        self.closed = False
        self._checksum = checksum
        self._reject_depth = reject_depth

    def push(self, symbol, item):
        self.queues.setdefault(symbol, asyncio.Queue()).put_nowait(item)

    def handle_option(self, method, option, default):
        return self._checksum

    async def watch_order_book(self, symbol, limit=None):
        self.depths.append(limit)
        if self._reject_depth and limit is not None:
            raise ccxt.NotSupported("fake: depth not accepted")
        item = await self.queues.setdefault(symbol, asyncio.Queue()).get()
        if isinstance(item, Exception):
            raise item
        return item

    async def un_watch_order_book(self, symbol):
        self.unwatched.append(symbol)

    async def close(self):
        self.closed = True


def streaming(pro, rest=None, **kwargs):
    rest = rest or triangle_md(label=VERIFIED)
    kwargs.setdefault("first_update_timeout", 0.2)
    kwargs.setdefault("retry_backoff", (0.01,))
    return StreamingMarketData(rest, pro_factory=lambda venue: pro, **kwargs), rest


def test_streamed_snapshot_carries_exchange_time_nonce_and_integrity_notes():
    async def run():
        pro = FakePro(checksum=True)
        md, rest = streaming(pro)
        pro.push("BTC/USDT", book([["49990", "1"]], [["50000", "1"]], nonce=42))
        snap = await md.fetch_order_book(VENUE, "BTC/USDT", 20)
        rest.clock += 2  # two seconds without a new update
        await md.close()
        return snap, md.monotonic()

    snap, now = asyncio.run(run())
    assert snap.source == "ccxt.pro websocket order book"
    assert snap.event_time == datetime.fromtimestamp(TS / 1000, timezone.utc)
    assert (snap.exchange_nonce, snap.bids[0], snap.asks[0]) == ("42", (D("49990"), D("1")), (D("50000"), D("1")))
    assert "checksum = True" in snap.checksum and "0 rebuilds" in snap.sequence_check
    assert snap.age_ms(now) == 2000  # age runs from the last update received


def test_no_first_update_in_time_falls_back_to_rest():
    async def run():
        md, _ = streaming(FakePro())
        snap = await md.fetch_order_book(VENUE, "BTC/USDT", 20)
        await md.close()
        return snap

    assert asyncio.run(run()).source == "fixture order book"


def test_stream_error_falls_back_until_the_book_is_rebuilt():
    async def run():
        pro = FakePro()
        md, _ = streaming(pro)
        pro.push("BTC/USDT", book([["49990", "1"]], [["50000", "1"]]))
        first = await md.fetch_order_book(VENUE, "BTC/USDT", 20)
        pro.push("BTC/USDT", ccxt.ChecksumError("fake checksum mismatch"))
        await asyncio.sleep(0.005)
        during = await md.fetch_order_book(VENUE, "BTC/USDT", 20)
        status = md.status()["streams"][VENUE]["BTC/USDT"]
        pro.push("BTC/USDT", book([["49991", "1"]], [["50001", "1"]], nonce=2))
        await asyncio.sleep(0.05)
        after = await md.fetch_order_book(VENUE, "BTC/USDT", 20)
        await md.close()
        return first, during, status, after

    first, during, status, after = asyncio.run(run())
    assert first.source.startswith("ccxt.pro") and during.source == "fixture order book"
    assert (status["ready"], status["rebuilds"]) == (False, 1) and status["last_error"].startswith("ChecksumError")
    assert after.source.startswith("ccxt.pro") and after.bids[0][0] == D("49991") and "1 rebuilds" in after.sequence_check


def test_failing_or_already_awaited_streams_do_not_block_again():
    async def run():
        pro = FakePro()
        md, rest = streaming(pro, first_update_timeout=5)
        pro.push("BTC/USDT", ccxt.NetworkError("fake: connection refused"))
        loop = asyncio.get_running_loop()
        started = loop.time()
        failing = await md.fetch_order_book(VENUE, "BTC/USDT", 20)  # stream failed at once: no 5 s wait
        md._first_update_timeout = 0.05
        await md.prepare_order_books(VENUE, ["ETH/USDT"], 20)  # silent stream: one bounded wait
        silent = await md.fetch_order_book(VENUE, "ETH/USDT", 20)  # already waited: no second wait
        elapsed = loop.time() - started
        await md.close()
        return failing, silent, elapsed

    failing, silent, elapsed = asyncio.run(run())
    assert failing.source == silent.source == "fixture order book"
    assert elapsed < 1


def test_rejected_depth_retries_with_the_venue_default():
    async def run():
        pro = FakePro(reject_depth=True)
        md, _ = streaming(pro)
        pro.push("BTC/USDT", book([["49990", "1"]], [["50000", "1"]]))
        snap = await md.fetch_order_book(VENUE, "BTC/USDT", 20)
        await md.close()
        return pro.depths, snap

    depths, snap = asyncio.run(run())
    assert depths[:2] == [20, None] and snap.source.startswith("ccxt.pro")


def test_venue_without_streams_uses_rest():
    async def run():
        md, _ = streaming(FakePro(streams=False))
        snap = await md.fetch_order_book(VENUE, "BTC/USDT", 20)
        await md.close()
        return snap

    assert asyncio.run(run()).source == "fixture order book"


def test_idle_streams_are_unsubscribed():
    async def run():
        pro = FakePro()
        md, rest = streaming(pro, idle_evict_s=10)
        pro.push("BTC/USDT", book([["49990", "1"]], [["50000", "1"]]))
        await md.fetch_order_book(VENUE, "BTC/USDT", 20)
        rest.clock += 11
        pro.push("ETH/USDT", book([["2600", "1"]], [["2601", "1"]]))
        await md.fetch_order_book(VENUE, "ETH/USDT", 20)
        watched = sorted(md.status()["streams"][VENUE])
        await md.close()
        return pro, watched

    pro, watched = asyncio.run(run())
    assert pro.unwatched == ["BTC/USDT"] and watched == ["ETH/USDT"] and pro.closed


def test_forward_book_waits_the_measured_round_trip():
    async def run():
        pro = FakePro()
        md, rest = streaming(pro)
        unmeasured = await md.forward_order_book(VENUE, "BTC/USDT", 20)  # no round trip measured yet
        rest.median_rtt_s = lambda venue: 0.01
        pro.push("BTC/USDT", book([["49990", "1"]], [["50000", "1"]]))
        measured = await md.forward_order_book(VENUE, "BTC/USDT", 20)
        await md.close()
        return unmeasured, measured

    unmeasured, measured = asyncio.run(run())
    assert unmeasured.source == "fixture order book" and measured.source.startswith("ccxt.pro")


def test_desk_evaluates_streamed_books(tmp_path):
    async def run():
        pro = FakePro(checksum=False)
        md, _ = streaming(pro)
        for symbol, (bids, asks) in TRIANGLE_BOOKS.items():
            pro.push(symbol, book(bids, asks))
        service = DeskService(md, tmp_path / "data")
        service.update_config(DeskConfig.model_validate(full_config()))
        outcome = await service.evaluate_triangular(VENUE, "USDT", D(1000))
        await md.close()
        return outcome.response

    response = asyncio.run(run())
    packet = response["opportunity"]
    assert response["decision"] == "PAPER_CANDIDATE", response["summary"]
    assert {ref["source"] for ref in packet["source_references"]} == {"ccxt.pro websocket order book"}
    assert all("checksum = False" in leg["checksum"] for leg in packet["book_integrity"])
    assert packet["conditional_net"] == {"amount": "36.88208", "asset": "USDT"}


@pytest.mark.parametrize("mode, expected", [("rest", CcxtMarketData), ("websocket", StreamingMarketData), (None, StreamingMarketData)])
def test_market_data_mode_switch(monkeypatch, mode, expected):
    from main import default_market_data

    if mode is None:
        monkeypatch.delenv("ARB_MARKET_DATA", raising=False)
    else:
        monkeypatch.setenv("ARB_MARKET_DATA", mode)
    assert isinstance(default_market_data(), expected)
    monkeypatch.setenv("ARB_MARKET_DATA", "carrier-pigeon")
    with pytest.raises(ValueError):
        default_market_data()
