"""Fixture market data. Every number here is HYPOTHETICAL test data, not a market observation."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb.config import DeskConfig  # noqa: E402
from arb.market_data import TICK_SIZE, MarketDataError, VenueMeta  # noqa: E402
from arb.orderbook import BookSnapshot, to_levels  # noqa: E402
from arb.protocol import EvidenceLabel  # noqa: E402
from arb.service import DeskService  # noqa: E402

VENUE = "testex"


def spot(symbol: str, step: str = "0.0001", taker: Optional[float] = 0.001, active=True, min_amount=None, min_cost=None) -> dict:
    base, quote = symbol.split("/")
    return {
        "id": symbol.replace("/", ""),
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "type": "spot",
        "spot": True,
        "active": active,
        "taker": taker,
        "percentage": True,
        "precision": {"amount": float(step), "price": 0.01},
        "limits": {"amount": {"min": min_amount}, "cost": {"min": min_cost}},
    }


def triangle_markets(**overrides) -> dict:
    markets = {
        "BTC/USDT": spot("BTC/USDT", "0.00001"),
        "ETH/BTC": spot("ETH/BTC", "0.0001"),
        "ETH/USDT": spot("ETH/USDT", "0.0001"),
    }
    markets.update(overrides)
    return markets


# USDT > BTC > ETH > USDT returns 1040 USDT per 1000 before fees and 1036.88208 after
# 0.1% taker fees taken from the received asset (hand-checked in test_engine.py).
TRIANGLE_BOOKS = {
    "BTC/USDT": ([["49990", "1"]], [["50000", "1"]]),
    "ETH/BTC": ([["0.0499", "100"]], [["0.05", "100"]]),
    "ETH/USDT": ([["2600", "100"]], [["2601", "100"]]),
}


class FakeMarketData:
    source_name = "fixture"

    def __init__(self, markets: dict, books: dict, label=EvidenceLabel.HYPOTHETICAL, fetch_seconds=0.01, meta=None):
        self.evidence_label = label
        self.markets = markets  # venue -> symbol -> market
        self.books = books  # venue -> symbol -> (bids, asks) or a list of successive snapshots
        self.fetch_seconds = fetch_seconds
        self.meta = meta or {}
        self.offline: set[str] = set()
        self.clock = 1000.0
        self.fetches: list[tuple[str, str]] = []

    def monotonic(self) -> float:
        return self.clock

    def _check(self, venue: str) -> None:
        if venue in self.offline:
            raise MarketDataError(venue, "NetworkError: fixture venue offline")

    async def load_markets(self, venue: str) -> dict:
        self._check(venue)
        return self.markets[venue]

    def _current(self, venue: str, symbol: str):
        entry = self.books[venue][symbol]
        if isinstance(entry, list):
            return entry.pop(0) if len(entry) > 1 else entry[0]
        return entry

    async def fetch_tickers(self, venue: str) -> dict:
        self._check(venue)
        tickers = {}
        for symbol, entry in self.books[venue].items():
            bids, asks = entry[0] if isinstance(entry, list) else entry
            tickers[symbol] = {"bid": float(bids[0][0]) if bids else None, "ask": float(asks[0][0]) if asks else None}
        return tickers

    async def fetch_order_book(self, venue: str, symbol: str, depth: int) -> BookSnapshot:
        self._check(venue)
        bids, asks = self._current(venue, symbol)
        sent = self.clock
        self.clock += self.fetch_seconds
        now = datetime.now(timezone.utc)
        self.fetches.append((venue, symbol))
        return BookSnapshot(
            venue, symbol, to_levels(bids), to_levels(asks), f"fx-{len(self.fetches)}", None, None, now, now,
            sent, self.clock, "fixture order book", self.evidence_label,
        )  # fmt: skip

    def venue_meta(self, venue: str) -> VenueMeta:
        return self.meta.get(venue, VenueMeta(TICK_SIZE, "get", True, True))

    def status(self) -> dict:
        return {"source": self.source_name, "evidence_label": self.evidence_label.value, "venues": {}}

    async def close(self) -> None:
        pass


def full_config(**overrides) -> dict:
    """Every input a triangular paper candidate needs, for the fixture venue."""
    config = {
        "residence_country": "DE",
        "venues": [VENUE],
        "available_capital": {"amount": "5000", "asset": "USDT"},
        "balances_by_venue": {VENUE: {"USDT": "2000"}},
        "fee_tiers": {
            VENUE: {"taker_rate": "0.001", "fee_side": "get", "evidence_label": "VERIFIED", "source": "fixture account page"}
        },
        "venue_eligibility": {VENUE: {"eligible": True, "evidence_label": "VERIFIED", "source": "fixture terms"}},
        "venue_rules_verified": {VENUE: {"ioc_supported": True, "evidence_label": "VERIFIED", "source": "fixture docs"}},
        "allowed_assets": ["USDT", "BTC", "ETH"],
        "trade_size_limit": {"amount": "1000", "asset": "USDT"},
        "total_deployed_capital_limit": {"amount": "3000", "asset": "USDT"},
        "venue_concentration_limit": "1",
        "inventory_exposure_limit": {"amount": "1500", "asset": "USDT"},
        "loss_per_incident_limit": {"amount": "50", "asset": "USDT"},
        "daily_loss_limit": {"amount": "100", "asset": "USDT"},
        "minimum_conservative_net_amount": {"amount": "1", "asset": "USDT"},
        "minimum_conservative_net_bps": "10",
        "maximum_book_age_ms": 5000,
        "maximum_snapshot_skew_ms": 2000,
        "validation_acceptance_criteria": {"hypothesis": "fixture", "minimum_paper_observations": 30},
        "cost_inputs": {
            "adverse_movement_allowance_bps": "20",
            "model_uncertainty_allowance_bps": "10",
            "allowance_basis": "fixture assumption",
        },
        "scan": {"interval_seconds": 5, "triangular_jobs": [{"venue": VENUE, "start_asset": "USDT", "size": "1000"}]},
    }
    config.update(overrides)
    return config


@pytest.fixture
def make_service(tmp_path):
    def build(md, config: Optional[dict] = None) -> DeskService:
        service = DeskService(md, tmp_path / "data")
        if config is not None:
            service.update_config(DeskConfig.model_validate(config))
        return service

    return build


def triangle_md(books=None, markets=None, **kwargs) -> FakeMarketData:
    return FakeMarketData({VENUE: markets or triangle_markets()}, {VENUE: books or dict(TRIANGLE_BOOKS)}, **kwargs)
