"""Order book snapshots, integrity checks and depth walks.

Prices and amounts are Decimals built from the text of each number, so float
artifacts never enter the accounting.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Callable, Optional, Sequence

from arb.protocol import EvidenceLabel

Level = tuple[Decimal, Decimal]  # (price, base amount)
ZERO = Decimal(0)


def to_levels(raw: Sequence[Sequence]) -> tuple[Level, ...]:
    """ccxt levels are [price, amount, ...]; extra fields are ignored."""
    return tuple((Decimal(str(level[0])), Decimal(str(level[1]))) for level in raw)


@dataclass(frozen=True)
class BookSnapshot:
    venue: str
    symbol: str
    bids: tuple[Level, ...]  # best (highest) first
    asks: tuple[Level, ...]  # best (lowest) first
    snapshot_id: str
    exchange_nonce: Optional[str]
    event_time: Optional[datetime]  # exchange timestamp, when the venue provides one
    request_sent_at: datetime
    received_at: datetime
    sent_monotonic: float
    received_monotonic: float
    source: str
    evidence_label: EvidenceLabel

    def age_ms(self, now_monotonic: float) -> int:
        """Upper bound on the book's age, measured on the local monotonic clock.

        The book cannot be older than the moment the request was sent, unless the
        venue served cached data, which REST snapshots cannot reveal.
        """
        return math.ceil(max(0.0, now_monotonic - self.sent_monotonic) * 1000)

    def request_window_ms(self) -> int:
        return math.ceil(max(0.0, self.received_monotonic - self.sent_monotonic) * 1000)


def snapshot_skew_ms(books: Sequence[BookSnapshot]) -> int:
    """Upper bound on the time spread between snapshots taken for one route."""
    if not books:
        return 0
    spread = max(b.received_monotonic for b in books) - min(b.sent_monotonic for b in books)
    return math.ceil(max(0.0, spread) * 1000)


def integrity_problems(book: BookSnapshot) -> list[str]:
    problems: list[str] = []
    for side, levels, descending in (("bids", book.bids, True), ("asks", book.asks, False)):
        if not levels:
            problems.append(f"no {side}")
            continue
        if any(price <= 0 or amount <= 0 for price, amount in levels):
            problems.append(f"non-positive price or amount in {side}")
        prices = [price for price, _ in levels]
        pairs = zip(prices, prices[1:])
        ordered = all(a > b for a, b in pairs) if descending else all(a < b for a, b in pairs)
        if not ordered:
            problems.append(f"{side} are not strictly ordered")
    if book.bids and book.asks and book.bids[0][0] >= book.asks[0][0]:
        problems.append("crossed or locked book: best bid >= best ask")
    return problems


def visible_base(levels: Sequence[Level]) -> Decimal:
    return sum((amount for _, amount in levels), ZERO)


def consume(book: BookSnapshot, side: str, base_amount: Decimal) -> BookSnapshot:
    """The book after our own fill took base_amount from the best levels of one side."""
    remaining = base_amount
    kept: list[Level] = []
    for price, amount in book.asks if side == "asks" else book.bids:
        if remaining > 0:
            take = min(amount, remaining)
            remaining -= take
            amount -= take
        if amount > 0:
            kept.append((price, amount))
    return replace(book, asks=tuple(kept)) if side == "asks" else replace(book, bids=tuple(kept))


@dataclass(frozen=True)
class Walk:
    base: Decimal  # base amount filled
    quote: Decimal  # quote amount paid or received
    worst_price: Optional[Decimal]
    levels_used: int
    complete: bool  # the requested amount was fully covered by visible depth

    @property
    def vwap(self) -> Optional[Decimal]:
        return self.quote / self.base if self.base > 0 else None


PriceCheck = Optional[Callable[[Decimal], bool]]


def walk_base(levels: Sequence[Level], base_amount: Decimal, price_ok: PriceCheck = None) -> Walk:
    """Fill a base amount against levels, best first, stopping at the price limit."""
    remaining = base_amount
    quote = ZERO
    worst: Optional[Decimal] = None
    used = 0
    for price, amount in levels:
        if remaining <= 0 or (price_ok is not None and not price_ok(price)):
            break
        take = min(amount, remaining)
        quote += take * price
        remaining -= take
        worst = price
        used += 1
    return Walk(base_amount - remaining, quote, worst, used, remaining <= 0)


def walk_quote_budget(asks: Sequence[Level], budget: Decimal, price_ok: PriceCheck = None) -> Walk:
    """How much base a quote budget buys, walking the asks."""
    remaining = budget
    base = ZERO
    worst: Optional[Decimal] = None
    used = 0
    for price, amount in asks:
        if remaining <= 0 or (price_ok is not None and not price_ok(price)):
            break
        cost = price * amount
        if cost <= remaining:
            base += amount
            remaining -= cost
        else:
            base += remaining / price
            remaining = ZERO
        worst = price
        used += 1
    return Walk(base, budget - remaining, worst, used, remaining <= 0)
