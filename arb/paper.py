"""PAPER mode: simulated orders against fresh order books. Never real funds.

The decision is taken on one set of snapshots. The simulation then fetches new
books and fills IOC limit orders at the planned worst prices, so elapsed time,
vanished depth and partial fills show up in the result. Leftover inventory is
returned to the accounting asset at market when that trade is executable.
Paper fills are model outputs, not live execution evidence.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from arb.engine import ONE, LegFill, execute_leg
from arb.market_data import MarketDataError, MarketDataSource
from arb.orderbook import ZERO, BookSnapshot, consume, integrity_problems
from arb.protocol import PAPER_NOTE, StrategyType, dstr, iso, money, utc_now
from arb.roles import PaperPlan

NOT_EXECUTABLE = {"ZERO_ORDER_AMOUNT", "BELOW_MIN_AMOUNT", "BELOW_MIN_COST", "MARKET_INACTIVE"}


def _executable(fill: LegFill) -> bool:
    return fill.filled_base > 0 and not any(code in NOT_EXECUTABLE for code, _ in fill.problems)


class PaperPortfolio:
    """Simulated balances by venue and asset, persisted between restarts."""

    def __init__(self, path: Path):
        self._path = path
        self.balances: Optional[dict[str, dict[str, Decimal]]] = None
        self.seeded_at: Optional[str] = None
        self.seeded_from_config_version: Optional[int] = None
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.balances = {v: {a: Decimal(x) for a, x in held.items()} for v, held in raw["balances"].items()}
            self.seeded_at = raw.get("seeded_at")
            self.seeded_from_config_version = raw.get("seeded_from_config_version")

    def reset(self, balances: dict[str, dict[str, Decimal]], config_version: int) -> None:
        self.balances = {venue: dict(held) for venue, held in balances.items()}
        self.seeded_at = iso(utc_now())
        self.seeded_from_config_version = config_version
        self._save()

    def apply(self, fills: list[LegFill]) -> list[str]:
        """Book simulated fills. A negative balance is reported as a mismatch."""
        assert self.balances is not None
        problems = []
        for fill in fills:
            held = self.balances.setdefault(fill.spec.venue, {})
            held[fill.spec.from_asset] = held.get(fill.spec.from_asset, ZERO) - fill.spent
            held[fill.spec.to_asset] = held.get(fill.spec.to_asset, ZERO) + fill.received
            if held[fill.spec.from_asset] < 0:
                problems.append(f"{fill.spec.from_asset} on {fill.spec.venue} went negative.")
        self._save()
        return problems

    def as_dict(self) -> dict:
        return {
            "balances": None
            if self.balances is None
            else {venue: {asset: dstr(amount) for asset, amount in held.items()} for venue, held in self.balances.items()},
            "seeded_at": self.seeded_at,
            "seeded_from_config_version": self.seeded_from_config_version,
            "evidence_label": "HYPOTHETICAL",
            "note": "Simulated balances for PAPER mode. Never real funds.",
        }

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "balances": {v: {a: str(x) for a, x in held.items()} for v, held in (self.balances or {}).items()},
            "seeded_at": self.seeded_at,
            "seeded_from_config_version": self.seeded_from_config_version,
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)


@dataclass
class PaperOutcome:
    plan: PaperPlan
    fills: list[LegFill] = field(default_factory=list)  # planned legs as simulated
    recovery: list[LegFill] = field(default_factory=list)  # executable recovery trades
    realized_net: Decimal = ZERO
    unrecovered: dict[str, Decimal] = field(default_factory=dict)
    latency_ms: list[int] = field(default_factory=list)  # decision to fresh book, per planned leg
    problems: list[str] = field(default_factory=list)

    def executed(self) -> list[LegFill]:
        return [*self.fills, *self.recovery]

    def as_entry(self, opportunity_id: str, route: str) -> dict:
        asset = self.plan.accounting_asset
        return {
            "opportunity_id": opportunity_id,
            "strategy": self.plan.strategy.value,
            "route": route,
            "evidence_label": "PAPER",
            "note": PAPER_NOTE,
            "committed": money(self.plan.committed, asset),
            "planned_net": money(self.plan.planned_net, asset),
            "realized_net": money(self.realized_net, asset),
            "fills": [
                {**fill.ledger(), "role": "planned_leg", "fill_ratio": dstr(fill.fill_ratio)} for fill in self.fills
            ]
            + [{**fill.ledger(), "role": "recovery"} for fill in self.recovery],
            "unrecovered": {a: dstr(x) for a, x in self.unrecovered.items()},
            "unrecovered_valuation": "Excess inventory is valued at zero; a shortfall at the highest price seen.",
            "latency_ms_from_decision": self.latency_ms,
            "problems": self.problems,
        }


def _latency(book: BookSnapshot, plan: PaperPlan) -> int:
    return math.ceil(max(0.0, book.received_monotonic - plan.decision_monotonic) * 1000)


async def simulate(
    md: MarketDataSource, plan: PaperPlan, depth: int, balances: dict[str, dict[str, Decimal]]
) -> PaperOutcome:
    if plan.strategy is StrategyType.TRIANGULAR_SPOT:
        return await _simulate_triangle(md, plan, depth)
    return await _simulate_cross(md, plan, depth, balances)


async def _simulate_triangle(md: MarketDataSource, plan: PaperPlan, depth: int) -> PaperOutcome:
    out = PaperOutcome(plan)
    start = plan.accounting_asset
    positions: dict[str, Decimal] = {start: plan.committed}
    books: dict[int, BookSnapshot] = {}  # depleted by our own fills
    valid: dict[int, bool] = {}

    async def book(index: int) -> BookSnapshot:
        if index not in books:
            leg = plan.legs[index]
            books[index] = await md.fetch_order_book(leg.venue, leg.symbol, depth)
            valid[index] = not integrity_problems(books[index])
        return books[index]

    def take(index: int, fill: LegFill) -> None:
        books[index] = consume(books[index], "asks" if fill.spec.side == "buy" else "bids", fill.filled_base)

    for i, leg in enumerate(plan.legs):
        try:
            fresh = await book(i)
        except MarketDataError as exc:
            out.problems.append(f"LOST_CONNECTIVITY {leg.symbol}: {exc}")
            break
        out.latency_ms.append(_latency(fresh, plan))
        issues = integrity_problems(fresh)
        if issues:
            out.problems.append(f"BOOK_INVALID {leg.symbol}: {'; '.join(issues)}")
            break
        fee = plan.fees[i]
        fill = execute_leg(
            leg,
            plan.rules[i],
            fresh,
            fee.rate,
            fee.in_spent_asset(leg.side),
            available=positions.get(leg.from_asset, ZERO),
            target_base=plan.targets[i],
            price_limit=plan.price_limits[i],
        )
        out.fills.append(fill)
        take(i, fill)
        positions[leg.from_asset] = positions.get(leg.from_asset, ZERO) - fill.spent
        positions[leg.to_asset] = positions.get(leg.to_asset, ZERO) + fill.received
        if fill.filled_base <= 0:
            out.problems.append(f"NO_FILL {leg.symbol}: nothing filled within the price limit.")
            break

    # Return leftovers to the start asset through the market that links them to it.
    recovery_routes = {
        plan.legs[0].to_asset: (plan.legs[0].reversed(), 0),
        plan.legs[1].to_asset: (plan.legs[2], 2),
    }
    for asset, (spec, index) in recovery_routes.items():
        amount = positions.get(asset, ZERO)
        if amount <= 0:
            continue
        try:
            fresh = await book(index)
        except MarketDataError as exc:
            out.problems.append(f"LOST_CONNECTIVITY during recovery of {asset}: {exc}")
            continue
        if not valid[index]:
            continue
        fee = plan.fees[index]
        fill = execute_leg(spec, plan.rules[index], fresh, fee.rate, fee.in_spent_asset(spec.side), available=amount)
        if not _executable(fill):
            continue
        take(index, fill)
        out.recovery.append(fill)
        positions[asset] -= fill.spent
        positions[start] = positions.get(start, ZERO) + fill.received

    out.unrecovered = {a: x for a, x in positions.items() if a != start and x > 0}
    out.realized_net = positions.get(start, ZERO) - plan.committed
    return out


async def _simulate_cross(
    md: MarketDataSource, plan: PaperPlan, depth: int, balances: dict[str, dict[str, Decimal]]
) -> PaperOutcome:
    out = PaperOutcome(plan)
    buy_leg, sell_leg = plan.legs
    quote, base = buy_leg.from_asset, buy_leg.to_asset
    books = []
    for leg in plan.legs:
        try:
            fresh = await md.fetch_order_book(leg.venue, leg.symbol, depth)
        except MarketDataError as exc:
            out.problems.append(f"LOST_CONNECTIVITY {leg.symbol}@{leg.venue}: {exc}")
            return out  # neither leg is sent without both books
        books.append(fresh)
        out.latency_ms.append(_latency(fresh, plan))
    for fresh in books:
        issues = integrity_problems(fresh)
        if issues:
            out.problems.append(f"BOOK_INVALID {fresh.symbol}@{fresh.venue}: {'; '.join(issues)}")
    if out.problems:
        return out  # neither leg is sent when a book is invalid

    buy_fee, sell_fee = plan.fees
    buy = execute_leg(
        buy_leg,
        plan.rules[0],
        books[0],
        buy_fee.rate,
        buy_fee.in_spent_asset("buy"),
        available=balances.get(buy_leg.venue, {}).get(quote, ZERO),
        target_base=plan.targets[0],
        price_limit=plan.price_limits[0],
    )
    sell = execute_leg(
        sell_leg,
        plan.rules[1],
        books[1],
        sell_fee.rate,
        sell_fee.in_spent_asset("sell"),
        available=balances.get(sell_leg.venue, {}).get(base, ZERO),
        target_base=plan.targets[1],
        price_limit=plan.price_limits[1],
    )
    out.fills = [buy, sell]
    books = [consume(books[0], "asks", buy.filled_base), consume(books[1], "bids", sell.filled_base)]
    quote_flow = sell.received - buy.spent
    imbalance = buy.received - sell.spent  # > 0: extra base on the buy venue; < 0: short on the sell venue
    if imbalance > 0:
        fill = execute_leg(
            buy_leg.reversed(), plan.rules[0], books[0], buy_fee.rate, buy_fee.in_spent_asset("sell"), available=imbalance
        )
        if _executable(fill):
            out.recovery.append(fill)
            quote_flow += fill.received
            imbalance -= fill.spent
    elif imbalance < 0:
        target = -imbalance if sell_fee.in_spent_asset("buy") else -imbalance / (ONE - sell_fee.rate)
        fill = execute_leg(
            sell_leg.reversed(),
            plan.rules[1],
            books[1],
            sell_fee.rate,
            sell_fee.in_spent_asset("buy"),
            available=None,
            target_base=target,
        )
        if _executable(fill):
            out.recovery.append(fill)
            quote_flow -= fill.spent
            imbalance += fill.received
    prices = [p for p in (buy.vwap, sell.vwap, *(b.asks[0][0] for b in books if b.asks)) if p is not None]
    shortfall = imbalance * max(prices) if imbalance < 0 else ZERO
    out.unrecovered = {base: imbalance} if imbalance else {}
    out.realized_net = quote_flow + shortfall
    return out
