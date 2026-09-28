"""Execution arithmetic shared by VECTOR, RELAY and PAPER mode.

Every leg is modeled as a taker order walking visible depth. In a chain, the next
leg receives the rounded, fee-adjusted output of the preceding leg. Legs never
fill atomically: callers model what happens when a later leg fails.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Optional, Sequence

from arb.orderbook import ZERO, BookSnapshot, walk_base, walk_quote_budget
from arb.protocol import EvidenceLabel, dstr

ONE = Decimal(1)


@dataclass(frozen=True)
class AmountRule:
    """How a venue requires order amounts to be rounded. Always rounded down here."""

    kind: str  # "step" or "significant_digits"
    value: Decimal

    def round_down(self, amount: Decimal) -> Decimal:
        if amount <= 0:
            return ZERO
        if self.kind == "step":
            step = self.value
        else:
            step = ONE.scaleb(amount.adjusted() - int(self.value) + 1)
        return (amount / step).to_integral_value(rounding=ROUND_FLOOR) * step


@dataclass(frozen=True)
class MarketRules:
    venue: str
    symbol: str
    base: str
    quote: str
    active: Optional[bool]  # None: the venue metadata does not say
    amount_rule: Optional[AmountRule]
    min_amount: Optional[Decimal]
    min_cost: Optional[Decimal]
    market_id: Optional[str] = None

    def round_amount(self, amount: Decimal) -> Decimal:
        return amount if self.amount_rule is None else self.amount_rule.round_down(amount)


@dataclass(frozen=True)
class FeeModel:
    rate: Optional[Decimal]  # None: unknown, never treated as zero
    side: str  # get / give: received / spent asset; base / quote: always that asset
    label: EvidenceLabel
    side_label: EvidenceLabel
    source: str

    def in_spent_asset(self, trade_side: str) -> bool:
        if self.side == "give":
            return True
        if self.side == "quote":
            return trade_side == "buy"
        if self.side == "base":
            return trade_side == "sell"
        return False

    def as_dict(self) -> dict:
        return {
            "rate": dstr(self.rate),
            "fee_side": self.side,
            "evidence_label": self.label.value,
            "fee_side_evidence_label": self.side_label.value,
            "source": self.source,
        }


@dataclass(frozen=True)
class LegSpec:
    venue: str
    symbol: str
    side: str  # buy: spend quote, receive base; sell: spend base, receive quote
    from_asset: str
    to_asset: str

    def reversed(self) -> "LegSpec":
        side = "sell" if self.side == "buy" else "buy"
        return LegSpec(self.venue, self.symbol, side, self.to_asset, self.from_asset)

    def as_dict(self) -> dict:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side,
            "from_asset": self.from_asset,
            "to_asset": self.to_asset,
        }


@dataclass
class LegFill:
    spec: LegSpec
    input_amount: Optional[Decimal]  # from_asset available to the leg; None when unknown
    order_amount: Decimal  # rounded base amount of the order
    filled_base: Decimal
    quote_amount: Decimal
    vwap: Optional[Decimal]
    worst_price: Optional[Decimal]
    levels_used: int
    fee_rate: Decimal
    fee_amount: Decimal
    fee_asset: str
    spent: Decimal  # from_asset, including fees charged in it
    received: Decimal  # to_asset, net of fees charged in it
    residual: Decimal  # unspent from_asset
    price_limit: Optional[Decimal]
    problems: list[tuple[str, str]] = field(default_factory=list)

    def has(self, code: str) -> bool:
        return any(c == code for c, _ in self.problems)

    @property
    def fill_ratio(self) -> Optional[Decimal]:
        return self.filled_base / self.order_amount if self.order_amount > 0 else None

    def ledger(self) -> dict:
        return {
            **self.spec.as_dict(),
            "input": dstr(self.input_amount),
            "order_amount_base": dstr(self.order_amount),
            "filled_base": dstr(self.filled_base),
            "quote_amount": dstr(self.quote_amount),
            "vwap": dstr(self.vwap),
            "worst_price": dstr(self.worst_price),
            "levels_used": self.levels_used,
            "fee_rate": dstr(self.fee_rate),
            "fee": {"amount": dstr(self.fee_amount), "asset": self.fee_asset},
            "spent": {"amount": dstr(self.spent), "asset": self.spec.from_asset},
            "received": {"amount": dstr(self.received), "asset": self.spec.to_asset},
            "residual": {"amount": dstr(self.residual), "asset": self.spec.from_asset},
            "problems": [{"code": c, "detail": d} for c, d in self.problems],
        }


def execute_leg(
    spec: LegSpec,
    rules: MarketRules,
    book: BookSnapshot,
    fee_rate: Decimal,
    fee_in_spent: bool,
    *,
    available: Optional[Decimal],
    target_base: Optional[Decimal] = None,
    price_limit: Optional[Decimal] = None,
) -> LegFill:
    """Simulate one taker order.

    available caps what the leg may spend (None when balances are unknown; then
    target_base is required). target_base caps the base amount of the order.
    price_limit makes it an immediate-or-cancel limit order: only levels at or
    better than the limit fill, and the remainder is canceled.
    """
    if available is None and target_base is None:
        raise ValueError("either available or target_base is required")
    problems: list[tuple[str, str]] = []
    spend_factor = ONE + fee_rate if fee_in_spent else ONE

    if spec.side == "buy":
        price_ok = None if price_limit is None else (lambda p: p <= price_limit)
        if available is None:
            wanted = target_base
        else:
            affordable = walk_quote_budget(book.asks, available / spend_factor, price_ok)
            wanted = affordable.base if target_base is None else min(affordable.base, target_base)
        qty = rules.round_amount(wanted)
        walk = walk_base(book.asks, qty, price_ok)
        if available is None:
            depth_limited = not walk.complete
        else:
            depth_limited = not affordable.complete and (target_base is None or affordable.base < target_base)
        if fee_in_spent:
            fee_asset, fee_amount = spec.from_asset, walk.quote * fee_rate
            spent, received = walk.quote + fee_amount, walk.base
        else:
            fee_asset, fee_amount = spec.to_asset, walk.base * fee_rate
            spent, received = walk.quote, walk.base - fee_amount
    else:
        price_ok = None if price_limit is None else (lambda p: p >= price_limit)
        if available is None:
            wanted = target_base
        else:
            sellable = available / spend_factor
            wanted = sellable if target_base is None else min(sellable, target_base)
        qty = rules.round_amount(wanted)
        walk = walk_base(book.bids, qty, price_ok)
        depth_limited = not walk.complete
        if fee_in_spent:
            fee_asset, fee_amount = spec.from_asset, walk.base * fee_rate
            spent, received = walk.base + fee_amount, walk.quote
        else:
            fee_asset, fee_amount = spec.to_asset, walk.quote * fee_rate
            spent, received = walk.base, walk.quote - fee_amount

    if rules.active is False:
        problems.append(("MARKET_INACTIVE", f"{spec.symbol} is not active on {spec.venue}."))
    if rules.amount_rule is None:
        problems.append(("AMOUNT_RULE_UNKNOWN", f"No amount rounding rule for {spec.symbol} on {spec.venue}."))
    if qty <= 0:
        problems.append(("ZERO_ORDER_AMOUNT", f"The {spec.symbol} order rounds to zero."))
    if depth_limited:
        where = "within the price limit" if price_limit is not None else "in the visible book"
        problems.append(("DEPTH_INSUFFICIENT", f"Not enough {spec.symbol} depth {where}."))
    if walk.base > 0 and rules.min_amount is not None and walk.base < rules.min_amount:
        problems.append(("BELOW_MIN_AMOUNT", f"{dstr(walk.base)} is below the minimum amount {dstr(rules.min_amount)}."))
    if walk.base > 0 and rules.min_cost is not None and walk.quote < rules.min_cost:
        problems.append(("BELOW_MIN_COST", f"{dstr(walk.quote)} is below the minimum notional {dstr(rules.min_cost)}."))

    return LegFill(
        spec=spec,
        input_amount=available,
        order_amount=qty,
        filled_base=walk.base,
        quote_amount=walk.quote,
        vwap=walk.vwap,
        worst_price=walk.worst_price,
        levels_used=walk.levels_used,
        fee_rate=fee_rate,
        fee_amount=fee_amount,
        fee_asset=fee_asset,
        spent=spent,
        received=received,
        residual=ZERO if available is None else available - spent,
        price_limit=price_limit,
        problems=problems,
    )


@dataclass
class ChainResult:
    fills: list[LegFill]
    start_asset: str
    start_amount: Decimal
    final_asset: str
    final_amount: Decimal  # includes unspent final-asset leftovers from earlier legs
    residuals: dict[str, Decimal]  # leftovers in other assets

    def has(self, code: str) -> bool:
        return any(fill.has(code) for fill in self.fills)


def run_chain(
    legs: Sequence[LegSpec],
    rules: Sequence[MarketRules],
    books: Sequence[BookSnapshot],
    fees: Sequence[FeeModel],
    start_amount: Decimal,
    *,
    zero_fees: bool = False,
    targets: Optional[Sequence[Optional[Decimal]]] = None,
    price_limits: Optional[Sequence[Optional[Decimal]]] = None,
) -> ChainResult:
    holding = start_amount
    leftovers: dict[str, Decimal] = {}
    fills: list[LegFill] = []
    for i, spec in enumerate(legs):
        fee = fees[i]
        if not zero_fees and fee.rate is None:
            raise ValueError(f"unknown fee for {spec.symbol}; run with zero_fees for gross figures only")
        fill = execute_leg(
            spec,
            rules[i],
            books[i],
            ZERO if zero_fees else fee.rate,
            fee.in_spent_asset(spec.side),
            available=holding,
            target_base=None if targets is None else targets[i],
            price_limit=None if price_limits is None else price_limits[i],
        )
        fills.append(fill)
        if fill.residual:
            leftovers[spec.from_asset] = leftovers.get(spec.from_asset, ZERO) + fill.residual
        holding = fill.received
    final_asset = legs[-1].to_asset
    final_amount = holding + leftovers.pop(final_asset, ZERO)
    residuals = {asset: amount for asset, amount in leftovers.items() if amount}
    return ChainResult(fills, legs[0].from_asset, start_amount, final_asset, final_amount, residuals)


def max_fillable_start(
    legs: Sequence[LegSpec],
    rules: Sequence[MarketRules],
    books: Sequence[BookSnapshot],
    fees: Sequence[FeeModel],
    upper: Decimal,
    *,
    zero_fees: bool,
) -> Decimal:
    """Largest start amount up to `upper` whose legs all fit in the visible depth."""

    def fits(amount: Decimal) -> bool:
        return not run_chain(legs, rules, books, fees, amount, zero_fees=zero_fees).has("DEPTH_INSUFFICIENT")

    if fits(upper):
        return upper
    low, high = ZERO, upper
    for _ in range(60):
        mid = (low + high) / 2
        if fits(mid):
            low = mid
        else:
            high = mid
    return low.quantize(Decimal("1e-8"), rounding=ROUND_FLOOR) if low > Decimal("1e-8") else ZERO
