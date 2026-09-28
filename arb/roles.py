"""The five protocol roles as deterministic checks (P1 to P5).

SCOUT finds candidates and checks data integrity. VECTOR does the profit
accounting. RELAY checks execution feasibility and writes the paper order plan.
AEGIS reviews risk and can veto. ATLAS issues the research decision. They run in
one process, so their agreement is not independent empirical validation.

Severity drives the decision: any FAIL means NO_TRADE, any MISSING or CONDITION
means RESEARCH_ONLY, and only a clean review becomes a PAPER_CANDIDATE.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional, Sequence

from arb.config import DeskConfig, Money
from arb.engine import ONE, ChainResult, FeeModel, LegFill, LegSpec, MarketRules, execute_leg, max_fillable_start, run_chain
from arb.orderbook import ZERO, BookSnapshot, integrity_problems, snapshot_skew_ms, visible_base, walk_quote_budget
from arb.packet import finalize_packet
from arb.protocol import (
    COLLABORATION_MODE,
    DECISION_RANK,
    AegisVerdict,
    EvidenceLabel,
    FinalDecision,
    Finding,
    Mode,
    RelayResult,
    ScoutResult,
    Severity,
    StrategyType,
    VectorResult,
    any_severity,
    display_time,
    dstr,
    iso,
    money,
    weakest,
)

BPS = Decimal(10000)
FAIL, MISSING, CONDITION, INFO = Severity.FAIL, Severity.MISSING, Severity.CONDITION, Severity.INFO
VERIFIED, ESTIMATED, UNKNOWN = EvidenceLabel.VERIFIED, EvidenceLabel.ESTIMATED, EvidenceLabel.UNKNOWN

RULE_PROBLEM_SEVERITY = {
    "MARKET_INACTIVE": FAIL,
    "ZERO_ORDER_AMOUNT": FAIL,
    "DEPTH_INSUFFICIENT": FAIL,
    "BELOW_MIN_AMOUNT": FAIL,
    "BELOW_MIN_COST": FAIL,
    "AMOUNT_RULE_UNKNOWN": MISSING,
}
EXPECTED_NET_REASON = "Outcome probabilities are not calibrated, so no probability weighted expectation is claimed."
HALT_TRIGGERS = [
    {"trigger": "STALE_OR_INVALID_BOOK", "action": "Stop new paper orders until a valid, fresh book is rebuilt."},
    {"trigger": "LOST_CONNECTIVITY", "action": "Stop new paper orders; research retries on the next cycle."},
    {"trigger": "EXCESSIVE_SKEW_OR_LATENCY", "action": "Stop new paper orders for the affected route."},
    {"trigger": "BALANCE_MISMATCH", "action": "Halt paper mode until the paper portfolio is reconciled or reset."},
    {"trigger": "LIMIT_BREACHED", "action": "Halt paper mode. Limits are never loosened automatically."},
]
INVALIDATION_CONDITIONS = [
    "Any change of config_version.",
    "Any leg book older than maximum_book_age_ms, or legs further apart than maximum_snapshot_skew_ms.",
    "A change in fee tier, balances, venue status or market rules.",
    "Paper or other evidence that contradicts the modeled fills.",
]
CLOCK_METHOD = (
    "Local monotonic clock. Each snapshot is bounded by its request send and response receive times; "
    "book_age_ms is an upper bound measured from the request send time. Venue side caching is not detectable "
    "from REST snapshots."
)


# ---------------------------------------------------------------------------
# Inputs and outputs
# ---------------------------------------------------------------------------


@dataclass
class DeskInputs:
    """Everything the roles may use for one evaluation."""

    config: DeskConfig
    config_version: int
    data_label: EvidenceLabel  # VERIFIED for live venue data, HYPOTHETICAL for fixtures
    balances: Optional[dict[str, dict[str, Decimal]]]  # declared (RESEARCH) or paper portfolio (PAPER)
    balances_label: EvidenceLabel
    balances_source: str
    paper_loss_today: dict[str, Decimal]  # realized paper losses today by asset, as positive amounts
    ioc_support: dict[str, Optional[bool]]  # per venue, from SDK metadata
    now_monotonic: float
    evaluated_at: datetime

    def balance(self, venue: str, asset: str) -> Optional[Decimal]:
        """None when balances are unknown; a declared portfolio without the asset holds zero."""
        if self.balances is None:
            return None
        return self.balances.get(venue, {}).get(asset, ZERO)


@dataclass
class PaperPlan:
    """What PAPER mode needs to replay a candidate against fresh books."""

    strategy: StrategyType
    legs: list[LegSpec]
    rules: list[MarketRules]
    fees: list[FeeModel]
    targets: list[Decimal]  # planned base amount per leg
    price_limits: list[Decimal]  # worst acceptable price per leg
    accounting_asset: str
    committed: Decimal  # triangular: start amount; cross: quote spent at the buy venue
    planned_net: Decimal
    decision_monotonic: float


@dataclass
class Evaluation:
    key: str
    strategy: StrategyType
    route_label: str
    decision: FinalDecision
    findings: list[Finding]
    packet_values: dict
    null_reasons: dict[str, str]
    summary: dict
    rank: tuple
    paper_plan: Optional[PaperPlan] = None

    def finalize(self, opportunity_id: str, packet_version: int, config_version: int) -> dict:
        values = dict(self.packet_values)
        values.update(opportunity_id=opportunity_id, packet_version=packet_version, config_version=config_version)
        return finalize_packet(values, self.null_reasons)


def _status(findings: Sequence[Finding], fail, missing, ok):
    if any_severity(findings, FAIL):
        return fail
    if any_severity(findings, MISSING):
        return missing
    return ok


def _role_result(status, findings: Sequence[Finding], **extra) -> dict:
    return {"status": status.value, "findings": [f.as_dict() for f in findings], **extra}


# ---------------------------------------------------------------------------
# SCOUT: candidate discovery and data integrity
# ---------------------------------------------------------------------------


def scout_setup(config: DeskConfig, strategy: StrategyType, venues: Sequence[str], assets: Sequence[str]) -> list[Finding]:
    findings: list[Finding] = []
    if strategy not in config.allowed_strategies:
        findings.append(Finding("SCOUT", "STRATEGY_NOT_ALLOWED", FAIL, f"{strategy.value} is not in allowed_strategies."))
    if not config.venues:
        findings.append(Finding("SCOUT", "VENUES_NOT_CONFIGURED", MISSING, "No accessible venues are configured."))
    for venue in venues:
        if config.venues and venue not in config.venues:
            findings.append(Finding("SCOUT", "VENUE_NOT_ALLOWED", FAIL, f"{venue} is not in the configured venues."))
    if not config.allowed_assets:
        findings.append(
            Finding("SCOUT", "ALLOWED_ASSETS_NOT_CONFIGURED", MISSING, "allowed_assets is empty, so every listed asset was scanned.")
        )
    for asset in assets:
        if config.allowed_assets and asset not in config.allowed_assets:
            findings.append(Finding("SCOUT", "ASSET_NOT_ALLOWED", FAIL, f"{asset} is not in allowed_assets."))
    return findings


def is_spot(market: dict) -> bool:
    return bool(market.get("spot") or market.get("type") == "spot")


@dataclass(frozen=True)
class Triangle:
    venue: str
    legs: tuple[LegSpec, LegSpec, LegSpec]

    @property
    def start_asset(self) -> str:
        return self.legs[0].from_asset

    @property
    def assets(self) -> list[str]:
        return [leg.from_asset for leg in self.legs]

    @property
    def label(self) -> str:
        return ">".join(self.assets + [self.start_asset])

    @property
    def key(self) -> str:
        legs = ",".join(f"{leg.symbol}:{leg.side}" for leg in self.legs)
        return f"{StrategyType.TRIANGULAR_SPOT.value}|{self.venue}|{self.label}|{legs}"


@dataclass(frozen=True)
class Lead:
    triangle: Triangle
    top_of_book_multiplier: Decimal

    def as_dict(self) -> dict:
        return {
            "route": self.triangle.label,
            "legs": [leg.as_dict() for leg in self.triangle.legs],
            "top_of_book_gross_bps": dstr((self.top_of_book_multiplier - ONE) * BPS),
            "evidence_label": ESTIMATED.value,
            "note": "Research lead from bulk tickers: top of book only, not synchronized, no depth.",
        }


def enumerate_triangles(venue: str, markets: dict, start_asset: str, allowed_assets: Sequence[str] = ()) -> list[Triangle]:
    """Every start > A > B > start cycle through active spot markets."""
    allowed = set(allowed_assets)
    edges: dict[str, dict[str, tuple[str, str]]] = {}
    for market in markets.values():
        if not is_spot(market) or market.get("active") is False:
            continue
        base, quote, symbol = market.get("base"), market.get("quote"), market.get("symbol")
        if not base or not quote or not symbol or base == quote:
            continue
        if allowed and (base not in allowed or quote not in allowed):
            continue
        edges.setdefault(quote, {}).setdefault(base, (symbol, "buy"))
        edges.setdefault(base, {}).setdefault(quote, (symbol, "sell"))
    triangles = []
    for mid, (symbol1, side1) in sorted(edges.get(start_asset, {}).items()):
        for last, (symbol2, side2) in sorted(edges.get(mid, {}).items()):
            back = edges.get(last, {}).get(start_asset)
            if last == start_asset or back is None:
                continue
            legs = (
                LegSpec(venue, symbol1, side1, start_asset, mid),
                LegSpec(venue, symbol2, side2, mid, last),
                LegSpec(venue, back[0], back[1], last, start_asset),
            )
            triangles.append(Triangle(venue, legs))
    return triangles


def rank_triangle_leads(triangles: Sequence[Triangle], tickers: dict) -> list[Lead]:
    """Top-of-book gross multiplier from bulk tickers. Leads only, never evidence."""
    leads = []
    for triangle in triangles:
        multiplier: Optional[Decimal] = ONE
        for leg in triangle.legs:
            ticker = tickers.get(leg.symbol) or {}
            price = ticker.get("ask") if leg.side == "buy" else ticker.get("bid")
            if price is None or price <= 0:
                multiplier = None
                break
            value = Decimal(str(price))
            multiplier = multiplier / value if leg.side == "buy" else multiplier * value
        if multiplier is not None:
            leads.append(Lead(triangle, multiplier))
    leads.sort(key=lambda lead: lead.top_of_book_multiplier, reverse=True)
    return leads


def _book_findings(books: Sequence[BookSnapshot], rules: Sequence[MarketRules]) -> tuple[list[Finding], list[list[str]]]:
    findings, problems = [], []
    for book in books:
        issues = integrity_problems(book)
        problems.append(issues)
        if issues:
            findings.append(Finding("SCOUT", "BOOK_INVALID", FAIL, f"{book.symbol} on {book.venue}: {'; '.join(issues)}."))
    for rule in rules:
        if rule.active is None:
            findings.append(
                Finding("SCOUT", "MARKET_STATUS_UNKNOWN", INFO, f"{rule.symbol} on {rule.venue}: venue metadata has no trading status.")
            )
    return findings, problems


# ---------------------------------------------------------------------------
# VECTOR: profit accounting
# ---------------------------------------------------------------------------


@dataclass
class VectorOut:
    status: VectorResult
    findings: list[Finding]
    requested: Decimal
    feasible: Decimal
    size_notes: list[str]
    asset: str
    gross: Optional[Decimal] = None
    net_before_unknown_costs: Optional[Decimal] = None
    conditional: Optional[Decimal] = None
    conservative: Optional[Decimal] = None
    denominator: Optional[Decimal] = None
    allowance_bps: Optional[Decimal] = None
    ladder: list[dict] = field(default_factory=list)
    breakeven_fee_bps_per_leg: Optional[Decimal] = None
    fills: list[LegFill] = field(default_factory=list)  # with fees when known, else gross
    fees_known: bool = False
    fee_sensitivity_per_bps: Optional[Decimal] = None  # net lost per extra bps of fee on every leg

    def bps(self, value: Optional[Decimal]) -> Optional[Decimal]:
        if value is None or not self.denominator:
            return None
        return value / self.denominator * BPS

    def result(self) -> dict:
        return _role_result(
            self.status,
            self.findings,
            bps_denominator={
                "amount": dstr(self.denominator),
                "asset": self.asset,
                "definition": "total capital committed by the candidate",
            },
            gross_bps=dstr(self.bps(self.gross)),
            conditional_bps=dstr(self.bps(self.conditional)),
            conservative_bps=dstr(self.bps(self.conservative)),
            size_ladder=self.ladder,
            breakeven_fee_bps_per_leg=dstr(self.breakeven_fee_bps_per_leg),
            sensitivity=self.sensitivity(),
        )

    def sensitivity(self) -> Optional[dict]:
        if self.fee_sensitivity_per_bps is None or not self.denominator:
            return None
        adverse = self.denominator / BPS
        return {
            "net_lost_per_bps_fee_on_every_leg": money(self.fee_sensitivity_per_bps, self.asset),
            "net_lost_per_bps_adverse_movement": money(adverse, self.asset),
            "biggest": "fees" if self.fee_sensitivity_per_bps >= adverse else "adverse_movement",
        }


def _economics_findings(
    config: DeskConfig,
    asset: str,
    denominator: Decimal,
    gross: Decimal,
    conditional: Optional[Decimal],
    upper_bound: Optional[Decimal],
    findings: list[Finding],
) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """Shared VECTOR checks. Returns (conservative net, total allowance in bps)."""
    if gross <= 0:
        findings.append(
            Finding("VECTOR", "NO_GROSS_EDGE", FAIL, f"Before fees the candidate returns {dstr(gross)} {asset} at this size.")
        )
    elif conditional is not None and conditional <= 0:
        findings.append(
            Finding("VECTOR", "NEGATIVE_AFTER_COSTS", FAIL, f"Conditional net is {dstr(conditional)} {asset} after modeled costs.")
        )
    elif conditional is None and upper_bound is not None and upper_bound <= 0:
        findings.append(
            Finding(
                "VECTOR",
                "NEGATIVE_BEFORE_UNKNOWN_COSTS",
                FAIL,
                f"Net is already {dstr(upper_bound)} {asset} before the costs that are still unknown.",
            )
        )
    costs = config.cost_inputs
    allowance: Optional[Decimal] = None
    if costs.adverse_movement_allowance_bps is None or costs.model_uncertainty_allowance_bps is None:
        findings.append(
            Finding(
                "VECTOR",
                "ALLOWANCES_NOT_CONFIGURED",
                MISSING,
                "Conservative net needs adverse movement and model uncertainty allowances in cost_inputs.",
            )
        )
    else:
        allowance = costs.adverse_movement_allowance_bps + costs.model_uncertainty_allowance_bps
        if not costs.allowance_basis:
            findings.append(Finding("VECTOR", "ALLOWANCE_BASIS_MISSING", CONDITION, "The allowances have no stated basis."))
    conservative: Optional[Decimal] = None
    if allowance is not None and conditional is not None:
        conservative = conditional - denominator * allowance / BPS
        if conditional > 0 and conservative <= 0:
            findings.append(
                Finding(
                    "VECTOR",
                    "NEGATIVE_CONSERVATIVE",
                    FAIL,
                    f"Conservative net is {dstr(conservative)} {asset}: the allowances erase the edge.",
                )
            )
    minimum = config.minimum_conservative_net_amount
    if minimum is None:
        findings.append(
            Finding("VECTOR", "MIN_NET_AMOUNT_NOT_CONFIGURED", MISSING, "minimum_conservative_net_amount is not set.")
        )
    elif minimum.asset != asset:
        findings.append(
            Finding(
                "VECTOR",
                "THRESHOLD_ASSET_MISMATCH",
                MISSING,
                f"minimum_conservative_net_amount is in {minimum.asset}, the candidate settles in {asset}, "
                "and no verified conversion is used.",
            )
        )
    elif conservative is not None and conservative < minimum.amount:
        findings.append(
            Finding(
                "VECTOR",
                "BELOW_MIN_NET_AMOUNT",
                FAIL,
                f"Conservative net {dstr(conservative)} {asset} is below the minimum {dstr(minimum.amount)} {asset}.",
            )
        )
    minimum_bps = config.minimum_conservative_net_bps
    if minimum_bps is None:
        findings.append(Finding("VECTOR", "MIN_NET_BPS_NOT_CONFIGURED", MISSING, "minimum_conservative_net_bps is not set."))
    elif conservative is not None and conservative / denominator * BPS < minimum_bps:
        findings.append(
            Finding(
                "VECTOR",
                "BELOW_MIN_NET_BPS",
                FAIL,
                f"Conservative net is {dstr(conservative / denominator * BPS)} bps, below {dstr(minimum_bps)} bps.",
            )
        )
    return conservative, allowance


def _vector_triangle(
    inp: DeskInputs,
    triangle: Triangle,
    rules: Sequence[MarketRules],
    fees: Sequence[FeeModel],
    books: Sequence[BookSnapshot],
    requested: Decimal,
) -> VectorOut:
    config = inp.config
    start = triangle.start_asset
    findings: list[Finding] = []
    notes: list[str] = []
    size = requested
    limit = config.trade_size_limit
    if limit is not None and limit.asset == start and size > limit.amount:
        size = limit.amount
        notes.append("Clipped to trade_size_limit.")
    balance = inp.balance(triangle.venue, start)
    if balance is not None and size > balance:
        size = balance
        notes.append(f"Clipped to the {inp.balances_source} balance of {start} on {triangle.venue}.")
    fees_known = all(fee.rate is not None for fee in fees)
    legs = triangle.legs
    if size > 0:
        depth_cap = max_fillable_start(legs, rules, books, fees, size, zero_fees=not fees_known)
        if depth_cap < size:
            size = depth_cap
            notes.append("Clipped to the size that fits the visible order book depth.")
    out = VectorOut(VectorResult.REJECTED, findings, requested, size, notes, start, fees_known=fees_known)
    if size <= 0:
        findings.append(Finding("VECTOR", "NO_FEASIBLE_SIZE", FAIL, "No positive size fits the balances, limits and depth."))
        return out

    gross_chain = run_chain(legs, rules, books, fees, size, zero_fees=True)
    net_chain = run_chain(legs, rules, books, fees, size) if fees_known else None
    out.denominator = size
    out.gross = gross_chain.final_amount - size
    out.fills = (net_chain or gross_chain).fills
    if net_chain is not None:
        out.conditional = net_chain.final_amount - size
    else:
        unknown = sorted({fee.source for fee in fees if fee.rate is None})
        findings.append(Finding("VECTOR", "FEE_UNKNOWN", MISSING, f"Fee rate unknown ({'; '.join(unknown)}); never treated as zero."))
    out.conservative, out.allowance_bps = _economics_findings(
        config, start, size, out.gross, out.conditional, None, findings
    )
    if fees_known:
        for fraction in (Decimal("0.25"), Decimal("0.5"), ONE):
            step = size * fraction
            chain = run_chain(legs, rules, books, fees, step)
            net = chain.final_amount - step
            out.ladder.append(
                {
                    "size": money(step, start),
                    "conditional_net": money(net, start),
                    "conditional_bps": dstr(net / step * BPS),
                    "problems": sorted({code for fill in chain.fills for code, _ in fill.problems}),
                }
            )
    if net_chain is not None:
        bumped = [replace(fee, rate=fee.rate + ONE / BPS) for fee in fees]
        out.fee_sensitivity_per_bps = net_chain.final_amount - run_chain(legs, rules, books, bumped, size).final_amount
    multiplier = gross_chain.final_amount / size
    if multiplier > 0:
        try:
            out.breakeven_fee_bps_per_leg = (ONE - multiplier ** (Decimal(-1) / Decimal(3))) * BPS
        except InvalidOperation:
            out.breakeven_fee_bps_per_leg = None
    out.status = _status(findings, VectorResult.REJECTED, VectorResult.INCONCLUSIVE, VectorResult.VALIDATED_FOR_PAPER)
    return out


# ---------------------------------------------------------------------------
# RELAY: execution feasibility, order plan and recovery
# ---------------------------------------------------------------------------


@dataclass
class RelayOut:
    status: RelayResult
    findings: list[Finding]
    plan: list[dict] = field(default_factory=list)
    stress: list[dict] = field(default_factory=list)
    worst_loss: Optional[Decimal] = None
    loss_is_lower_bound: bool = True
    max_exposure: Optional[Decimal] = None
    recovery_plan: Optional[dict] = None

    def result(self) -> dict:
        return _role_result(self.status, self.findings, order_plan=self.plan)


def _plan_leg(fill: LegFill, fee: FeeModel) -> dict:
    buy = fill.spec.side == "buy"
    return {
        **fill.spec.as_dict(),
        "order_type": "limit",
        "time_in_force": "IOC",
        "amount_base": dstr(fill.order_amount),
        "limit_price": dstr(fill.worst_price),
        "limit_meaning": "maximum acceptable purchase price" if buy else "minimum acceptable sale price",
        "expected_fee": {
            "amount": dstr(fill.fee_amount),
            "asset": fill.fee_asset,
            "rate": dstr(fill.fee_rate),
            "evidence_label": fee.label.value,
        },
        "balance_reservation": {"amount": dstr(fill.spent), "asset": fill.spec.from_asset},
        "cancel_condition": "Immediate or cancel: the unfilled remainder is canceled at once. A cancel never undoes filled quantity.",
        "fee_note": "A limit order that takes liquidity pays the taker fee, not the maker fee.",
    }


def _relay_common(inp: DeskInputs, fills: Sequence[LegFill], venues: Sequence[str]) -> list[Finding]:
    findings = [
        Finding("RELAY", code, RULE_PROBLEM_SEVERITY.get(code, MISSING), detail)
        for fill in fills
        for code, detail in fill.problems
    ]
    for venue in dict.fromkeys(venues):
        support = inp.ioc_support.get(venue)
        if support is None:
            findings.append(Finding("RELAY", "IOC_SUPPORT_UNKNOWN", MISSING, f"No metadata on immediate-or-cancel support for {venue}."))
        elif support is False:
            findings.append(
                Finding("RELAY", "IOC_NOT_SUPPORTED", MISSING, f"{venue} lacks IOC per SDK metadata; another order behavior needs engineering.")
            )
        else:
            findings.append(
                Finding(
                    "RELAY",
                    "IOC_FROM_SDK_METADATA",
                    INFO,
                    f"IOC support on {venue} comes from ccxt metadata (ESTIMATED). Confirm it in the venue documentation "
                    "before any live system.",
                )
            )
    if inp.balances is None:
        findings.append(
            Finding("RELAY", "BALANCES_UNKNOWN", MISSING, "Balances are unknown, so only public market research is possible.")
        )
    return findings


def _recovery_plan(policy: str) -> dict:
    return {
        "policy": policy,
        "authorization": "Any live system would need this policy explicitly authorized by the user. Here it only drives "
        "stress figures and paper simulation.",
        "halt_triggers": HALT_TRIGGERS,
    }


def _relay_triangle(
    inp: DeskInputs,
    triangle: Triangle,
    rules: Sequence[MarketRules],
    fees: Sequence[FeeModel],
    books: Sequence[BookSnapshot],
    vector: VectorOut,
) -> RelayOut:
    if not vector.fills:
        return RelayOut(
            RelayResult.BLOCKED, [Finding("RELAY", "NO_EXECUTABLE_PLAN", FAIL, "VECTOR found no feasible size to plan.")]
        )
    findings = _relay_common(inp, vector.fills, [triangle.venue])
    out = RelayOut(RelayResult.BLOCKED, findings, plan=[_plan_leg(f, fee) for f, fee in zip(vector.fills, fees)])
    out.recovery_plan = _recovery_plan(
        "If a later leg fails, return the held asset to the start asset through the market that links them in this "
        "triangle, at market (IOC without a price limit, taker fee), on the same venue."
    )
    if vector.fees_known:
        _stress_triangle(inp, triangle, rules, fees, books, vector, out)
    out.status = _status(findings, RelayResult.BLOCKED, RelayResult.NEEDS_ENGINEERING, RelayResult.FEASIBLE_FOR_PAPER)
    return out


def _adverse_bps(config: DeskConfig) -> Optional[Decimal]:
    return config.cost_inputs.adverse_movement_allowance_bps


def _stress_triangle(inp, triangle, rules, fees, books, vector: VectorOut, out: RelayOut) -> None:
    start = triangle.start_asset
    adverse = _adverse_bps(inp.config)
    haircut = ONE - (adverse or ZERO) / BPS
    fills = vector.fills
    start_leftover = fills[0].residual
    scenarios = (
        ("LEG_2_FAILS_AFTER_LEG_1", fills[0], [triangle.legs[0].reversed()], [rules[0]], [books[0]], [fees[0]]),
        ("LEG_3_FAILS_AFTER_LEG_2", fills[1], [triangle.legs[2]], [rules[2]], [books[2]], [fees[2]]),
    )
    losses, exposures = [], []
    for name, fill, legs, leg_rules, leg_books, leg_fees in scenarios:
        held = fill.received
        unwind = run_chain(legs, leg_rules, leg_books, leg_fees, held)
        result = unwind.final_amount * haircut + start_leftover - vector.feasible
        losses.append(max(ZERO, -result))
        exposures.append(unwind.final_amount)
        out.stress.append(
            {
                "scenario": name,
                "unmatched_exposure": money(held, fill.spec.to_asset),
                "exposure_value_now": money(unwind.final_amount, start),
                "recovery": f"{legs[0].side} {legs[0].symbol} at market",
                "adverse_movement_bps_applied": dstr(adverse),
                "net_result": money(result, start),
                "loss": money(max(ZERO, -result), start),
                "recovery_depth_sufficient": not unwind.has("DEPTH_INSUFFICIENT"),
                "time_exposed_ms": None,
                "evidence_label": weakest([inp.data_label, ESTIMATED]).value,
                "note": "Valued on the current snapshot. Later moves can make it worse; this is not a maximum possible loss.",
            }
        )
    out.worst_loss = max(losses)
    out.loss_is_lower_bound = adverse is None
    out.max_exposure = max(exposures)


# ---------------------------------------------------------------------------
# AEGIS: independent risk review with veto
# ---------------------------------------------------------------------------


@dataclass
class AegisOut:
    verdict: AegisVerdict
    findings: list[Finding]
    limits_checked: list[dict]

    def result(self) -> dict:
        return _role_result(self.verdict, self.findings, limits_checked=self.limits_checked)


def _check_limit(
    findings: list[Finding],
    checked: list[dict],
    name: str,
    limit: Optional[Money],
    observed: dict[str, Optional[Decimal]],
    what: str,
    breach_code: str,
    strict: bool = False,
) -> None:
    """Compare a limit only in its own asset. Different assets are never assumed interchangeable."""
    if limit is None:
        findings.append(Finding("AEGIS", f"{name.upper()}_NOT_CONFIGURED", MISSING, f"{name} is not set; the desk never invents limits."))
        checked.append({"limit": name, "status": "NOT_CONFIGURED"})
        return
    if limit.asset not in observed:
        findings.append(
            Finding(
                "AEGIS",
                "LIMIT_ASSET_MISMATCH",
                MISSING,
                f"{name} is in {limit.asset} but {what} is measured in {'/'.join(observed)}; no verified conversion is used.",
            )
        )
        checked.append({"limit": name, "status": "UNCHECKABLE_ASSET_MISMATCH"})
        return
    value = observed[limit.asset]
    if value is None:
        findings.append(Finding("AEGIS", "LIMIT_INPUT_UNKNOWN", MISSING, f"{what} is unknown, so {name} cannot be checked."))
        checked.append({"limit": name, "status": "UNKNOWN_INPUT"})
        return
    ok = value < limit.amount if strict else value <= limit.amount
    checked.append(
        {
            "limit": name,
            "limit_value": money(limit.amount, limit.asset),
            "observed": money(value, limit.asset),
            "status": "OK" if ok else "BREACHED",
        }
    )
    if not ok:
        findings.append(
            Finding("AEGIS", breach_code, FAIL, f"{what} {dstr(value)} {limit.asset} breaches {name} {dstr(limit.amount)} {limit.asset}.")
        )


def _unit_values(accounting_asset: str, books: Sequence[BookSnapshot], rules: Sequence[MarketRules]) -> dict[str, Decimal]:
    """Conservative liquidation value of one unit of each asset, from the candidate's own books."""
    values = {accounting_asset: ONE}
    for book, rule in zip(books, rules):
        if not book.bids or not book.asks:
            continue
        if rule.quote == accounting_asset:
            price = book.bids[0][0]
            values[rule.base] = min(values.get(rule.base, price), price)
        elif rule.base == accounting_asset:
            price = ONE / book.asks[0][0]
            values[rule.quote] = min(values.get(rule.quote, price), price)
    return values


def _concentration(inp: DeskInputs, venues, unit_values, asset, findings, checked) -> None:
    limit = inp.config.venue_concentration_limit
    if limit is None:
        findings.append(Finding("AEGIS", "VENUE_CONCENTRATION_LIMIT_NOT_CONFIGURED", MISSING, "venue_concentration_limit is not set."))
        checked.append({"limit": "venue_concentration_limit", "status": "NOT_CONFIGURED"})
        return
    if inp.balances is None:
        findings.append(Finding("AEGIS", "CONCENTRATION_UNCHECKABLE", MISSING, "Balances are unknown."))
        checked.append({"limit": "venue_concentration_limit", "status": "UNKNOWN_INPUT"})
        return
    per_venue: dict[str, Decimal] = {}
    for venue, holdings in inp.balances.items():
        for held_asset, amount in holdings.items():
            if amount <= 0:
                continue
            if held_asset not in unit_values:
                findings.append(
                    Finding(
                        "AEGIS",
                        "CONCENTRATION_UNCHECKABLE",
                        MISSING,
                        f"{held_asset} on {venue} cannot be valued in {asset} from this candidate's books.",
                    )
                )
                checked.append({"limit": "venue_concentration_limit", "status": "UNCHECKABLE"})
                return
            per_venue[venue] = per_venue.get(venue, ZERO) + amount * unit_values[held_asset]
    total = sum(per_venue.values(), ZERO)
    if total <= 0:
        findings.append(Finding("AEGIS", "CONCENTRATION_UNCHECKABLE", MISSING, "Declared balances are all zero."))
        checked.append({"limit": "venue_concentration_limit", "status": "UNKNOWN_INPUT"})
        return
    for venue in dict.fromkeys(venues):
        share = per_venue.get(venue, ZERO) / total
        ok = share <= limit
        checked.append(
            {"limit": "venue_concentration_limit", "venue": venue, "share": dstr(share), "limit_value": dstr(limit), "status": "OK" if ok else "BREACHED"}
        )
        if not ok:
            findings.append(
                Finding("AEGIS", "VENUE_CONCENTRATION_EXCEEDED", FAIL, f"{venue} holds {dstr(share)} of capital; the limit is {dstr(limit)}.")
            )


def _aegis(
    inp: DeskInputs,
    *,
    venues: Sequence[str],
    books: Sequence[BookSnapshot],
    rules: Sequence[MarketRules],
    fees: Sequence[FeeModel],
    asset: str,
    committed: Optional[Decimal],
    trade_size: dict[str, Optional[Decimal]],
    relay: RelayOut,
    recompute_problems: list[str],
    upstream: Sequence[Finding],
) -> AegisOut:
    config = inp.config
    findings: list[Finding] = []
    checked: list[dict] = []
    for problem in recompute_problems:
        findings.append(Finding("AEGIS", "RECOMPUTE_MISMATCH", FAIL, problem))
    if inp.data_label is not VERIFIED:
        findings.append(
            Finding("AEGIS", "MARKET_DATA_NOT_VERIFIED", CONDITION, f"Market data is {inp.data_label.value}, not live venue data.")
        )

    ages = [book.age_ms(inp.now_monotonic) for book in books]
    if config.maximum_book_age_ms is None:
        findings.append(Finding("AEGIS", "FRESHNESS_LIMIT_NOT_CONFIGURED", MISSING, "maximum_book_age_ms is not set."))
    elif max(ages) > config.maximum_book_age_ms:
        findings.append(
            Finding("AEGIS", "STALE_BOOK", FAIL, f"Oldest book is up to {max(ages)} ms old; the limit is {config.maximum_book_age_ms} ms.")
        )
    skew = snapshot_skew_ms(books)
    if config.maximum_snapshot_skew_ms is None:
        findings.append(Finding("AEGIS", "SKEW_LIMIT_NOT_CONFIGURED", MISSING, "maximum_snapshot_skew_ms is not set."))
    elif skew > config.maximum_snapshot_skew_ms:
        findings.append(
            Finding("AEGIS", "UNSYNCHRONIZED_BOOKS", FAIL, f"Snapshots span up to {skew} ms; the limit is {config.maximum_snapshot_skew_ms} ms.")
        )

    for venue, fee in {rule.venue: fee for rule, fee in zip(rules, fees)}.items():
        if fee.rate is not None and fee.label is not VERIFIED:
            findings.append(
                Finding("AEGIS", "FEE_NOT_VERIFIED", CONDITION, f"Fee on {venue} is {fee.label.value} ({fee.source}).")
            )
        if fee.rate is not None and fee.side_label is not VERIFIED:
            findings.append(
                Finding("AEGIS", "FEE_ASSET_NOT_VERIFIED", CONDITION, f"Which asset pays the fee on {venue} is {fee.side_label.value}.")
            )

    if config.residence_country is None:
        findings.append(Finding("AEGIS", "RESIDENCE_COUNTRY_UNKNOWN", MISSING, "Eligibility depends on the jurisdiction."))
    for venue in dict.fromkeys(venues):
        eligibility = (config.venue_eligibility or {}).get(venue)
        if eligibility is None:
            findings.append(Finding("AEGIS", "ELIGIBILITY_UNKNOWN", MISSING, f"Account eligibility on {venue} is not declared."))
        elif not eligibility.eligible:
            findings.append(Finding("AEGIS", "VENUE_NOT_ELIGIBLE", FAIL, f"{venue} is declared not eligible ({eligibility.source})."))
        elif eligibility.evidence_label is not VERIFIED:
            findings.append(
                Finding("AEGIS", "ELIGIBILITY_NOT_VERIFIED", CONDITION, f"Eligibility on {venue} is {eligibility.evidence_label.value}.")
            )

    _check_limit(findings, checked, "available_capital", config.available_capital, {asset: committed}, "Committed capital", "CAPITAL_EXCEEDED")
    _check_limit(findings, checked, "trade_size_limit", config.trade_size_limit, trade_size, "Trade size", "TRADE_SIZE_EXCEEDED")
    _check_limit(
        findings, checked, "total_deployed_capital_limit", config.total_deployed_capital_limit, {asset: committed},
        "Committed capital", "DEPLOYED_CAPITAL_EXCEEDED",
    )  # fmt: skip
    _check_limit(
        findings, checked, "loss_per_incident_limit", config.loss_per_incident_limit, {asset: relay.worst_loss},
        "Stress loss", "INCIDENT_LOSS_LIMIT_EXCEEDED",
    )  # fmt: skip
    _check_limit(
        findings, checked, "inventory_exposure_limit", config.inventory_exposure_limit, {asset: relay.max_exposure},
        "Unmatched exposure", "INVENTORY_EXPOSURE_EXCEEDED",
    )  # fmt: skip
    daily = config.daily_loss_limit
    _check_limit(
        findings, checked, "daily_loss_limit", daily,
        {daily.asset: inp.paper_loss_today.get(daily.asset, ZERO)} if daily else {},
        "Paper loss today", "DAILY_LOSS_LIMIT_REACHED", strict=True,
    )  # fmt: skip
    _concentration(inp, venues, _unit_values(asset, books, rules), asset, findings, checked)

    if config.validation_acceptance_criteria is None:
        findings.append(
            Finding("AEGIS", "ACCEPTANCE_CRITERIA_NOT_DEFINED", MISSING, "Define acceptance criteria before evaluating paper results.")
        )
    everything = [*upstream, *findings]
    if any_severity(everything, FAIL, MISSING):
        verdict = AegisVerdict.VETO
    elif any_severity(everything, CONDITION):
        verdict = AegisVerdict.CONDITIONAL_FOR_PAPER
    else:
        verdict = AegisVerdict.PASS_FOR_PAPER
    return AegisOut(verdict, findings, checked)


def _recompute_ledger(fills: Sequence[LegFill], chained: bool) -> list[str]:
    """Recompute each leg from its rendered ledger figures, as AEGIS would from the packet."""
    problems = []
    tolerance = Decimal("1e-18")
    previous: Optional[Decimal] = None
    for i, fill in enumerate(fills, start=1):
        row = fill.ledger()
        filled = Decimal(row["filled_base"])
        quote = Decimal(row["quote_amount"])
        rate = Decimal(row["fee_rate"])
        spent = Decimal(row["spent"]["amount"])
        received = Decimal(row["received"]["amount"])
        vwap = Decimal(row["vwap"]) if row["vwap"] is not None else None
        if vwap is not None and abs(filled * vwap - quote) > tolerance * max(ONE, quote):
            problems.append(f"Leg {i}: filled x vwap does not reproduce the quote amount.")
        buy = fill.spec.side == "buy"
        gross_in = quote if buy else filled
        gross_out = filled if buy else quote
        fee_in_spent = row["fee"]["asset"] == fill.spec.from_asset
        expected_spent = gross_in * (ONE + rate) if fee_in_spent else gross_in
        expected_received = gross_out if fee_in_spent else gross_out * (ONE - rate)
        if abs(spent - expected_spent) > tolerance * max(ONE, spent) or abs(received - expected_received) > tolerance * max(ONE, received):
            problems.append(f"Leg {i}: spent or received does not match the fill and fee rate.")
        if chained and previous is not None and row["input"] is not None and Decimal(row["input"]) != previous:
            problems.append(f"Leg {i}: input does not equal the previous leg's received amount.")
        previous = received
    return problems


# ---------------------------------------------------------------------------
# ATLAS: decision and summary
# ---------------------------------------------------------------------------


def _atlas(inp: DeskInputs, findings: Sequence[Finding]) -> tuple[FinalDecision, str, str]:
    fails = [f for f in findings if f.severity is FAIL]
    gaps = [f for f in findings if f.severity in (MISSING, CONDITION)]
    if fails:
        codes = ", ".join(dict.fromkeys(f.code for f in fails))
        return FinalDecision.NO_TRADE, f"Rejected: {codes}.", f"No action on this route. First rejection: {fails[0].detail}"
    if gaps:
        codes = ", ".join(dict.fromkeys(f.code for f in gaps))
        first = next((f for f in gaps if f.severity is MISSING), gaps[0])
        return FinalDecision.RESEARCH_ONLY, f"Inputs missing or unverified: {codes}.", f"Resolve first: {first.detail}"
    if inp.config.mode is Mode.PAPER:
        action = "The screener simulates this candidate against fresh order books in PAPER mode."
    else:
        action = "Set mode to PAPER and start the screener to record forward paper observations."
    return FinalDecision.PAPER_CANDIDATE, "All protocol checks passed on this snapshot and configuration.", action


def _summary(
    inp: DeskInputs,
    strategy: StrategyType,
    route: str,
    decision: FinalDecision,
    reason: str,
    next_action: str,
    findings: Sequence[Finding],
    values: dict,
    relay: RelayOut,
    evidence_quality: EvidenceLabel,
) -> dict:
    worst = None
    if relay.stress:
        worst = max(relay.stress, key=lambda s: Decimal(s["loss"]["amount"]))
    return {
        "mode": inp.config.mode.value,
        "decision": decision.value,
        "execution_status": "NO_TRADE",
        "reason": reason,
        "strategy": strategy.value,
        "route": route,
        "required_capital_by_venue": values.get("capital_by_venue"),
        "expected_net": None,
        "expected_net_reason": EXPECTED_NET_REASON,
        "conditional_net": values.get("conditional_net"),
        "conservative_net": values.get("conservative_net"),
        "assumptions": values.get("cost_assumptions"),
        "inventory_exposure": values.get("net_exposure"),
        "stress_loss_scenario": worst,
        "evidence_quality": evidence_quality.value,
        "blockers": list(dict.fromkeys(f.code for f in findings if f.severity in (FAIL, MISSING))),
        "conditions": list(dict.fromkeys(f.code for f in findings if f.severity is CONDITION)),
        "next_action": next_action,
        "evaluated_at": iso(inp.evaluated_at),
        "evaluated_at_display": display_time(inp.evaluated_at, inp.config.display_timezone),
    }


# ---------------------------------------------------------------------------
# Packet fields shared by both strategies
# ---------------------------------------------------------------------------


def _book_fields(inp: DeskInputs, books: Sequence[BookSnapshot], sides: Sequence[str], problems) -> dict:
    return {
        "source_references": [
            {
                "venue": b.venue,
                "symbol": b.symbol,
                "method": "fetch_order_book",
                "source": b.source,
                "snapshot_id": b.snapshot_id,
                "exchange_nonce": b.exchange_nonce,
            }
            for b in books
        ],
        "raw_data_references": [
            {
                "snapshot_id": b.snapshot_id,
                "best_bids": [[dstr(p), dstr(a)] for p, a in b.bids[:3]],
                "best_asks": [[dstr(p), dstr(a)] for p, a in b.asks[:3]],
            }
            for b in books
        ],
        "event_timestamps": [iso(b.event_time) for b in books],
        "receive_timestamps": [iso(b.received_at) for b in books],
        "book_age_ms": [b.age_ms(inp.now_monotonic) for b in books],
        "snapshot_skew_ms": snapshot_skew_ms(books),
        "freshness_limit_ms": inp.config.maximum_book_age_ms,
        "skew_limit_ms": inp.config.maximum_snapshot_skew_ms,
        "clock_uncertainty": {
            "method": CLOCK_METHOD,
            "request_window_ms": [b.request_window_ms() for b in books],
            "exchange_event_time_available": [b.event_time is not None for b in books],
        },
        "book_integrity": [
            {
                "venue": b.venue,
                "symbol": b.symbol,
                "valid": not issues,
                "problems": issues,
                "sequence_check": "Not available for REST snapshots.",
                "checksum": "Not available for REST snapshots.",
            }
            for b, issues in zip(books, problems)
        ],
        "book_depth_by_leg": [
            {
                "symbol": b.symbol,
                "side_used": "asks" if side == "buy" else "bids",
                "levels_visible": len(b.asks if side == "buy" else b.bids),
                "visible_base_amount": dstr(visible_base(b.asks if side == "buy" else b.bids)),
            }
            for b, side in zip(books, sides)
        ],
    }


def _fee_fields(fills: Sequence[LegFill], fees: Sequence[FeeModel], rules: Sequence[MarketRules]) -> dict:
    return {
        "fees_by_leg": [
            {**fee.as_dict(), "symbol": rule.symbol, "fee_amount": dstr(fill.fee_amount) if fill else None}
            for fill, fee, rule in zip(fills or [None] * len(fees), fees, rules)
        ],
        "fee_assets": [fill.fee_asset for fill in fills] if fills else None,
        "fee_tier_evidence": {rule.venue: {"label": fee.label.value, "source": fee.source} for rule, fee in zip(rules, fees)},
    }


def _eligibility(config: DeskConfig, venues: Sequence[str]) -> Optional[dict]:
    if config.venue_eligibility is None:
        return None
    return {
        venue: (config.venue_eligibility[venue].model_dump(mode="json") if venue in config.venue_eligibility else None)
        for venue in dict.fromkeys(venues)
    }


def _balances_view(inp: DeskInputs, venues: Sequence[str]) -> Optional[dict]:
    if inp.balances is None:
        return None
    return {
        "source": inp.balances_source,
        "evidence_label": inp.balances_label.value,
        "by_venue": {v: {a: dstr(x) for a, x in inp.balances.get(v, {}).items()} for v in dict.fromkeys(venues)},
    }


def _allowances(config: DeskConfig, denominator: str) -> dict:
    costs = config.cost_inputs
    return {
        "adverse_movement_bps": dstr(costs.adverse_movement_allowance_bps),
        "model_uncertainty_bps": dstr(costs.model_uncertainty_allowance_bps),
        "basis": costs.allowance_basis,
        "applied_to": denominator,
        "evidence_label": ESTIMATED.value if costs.allowance_basis else UNKNOWN.value,
    }


def _decide(inp, strategy, route, findings, values, relay, evidence_quality) -> tuple[FinalDecision, dict]:
    decision, reason, next_action = _atlas(inp, findings)
    values["unresolved_questions"] = [f.detail for f in findings if f.severity in (MISSING, CONDITION)]
    values["final_decision"] = decision.value
    values["invalidation_conditions"] = INVALIDATION_CONDITIONS
    summary = _summary(inp, strategy, route, decision, reason, next_action, findings, values, relay, evidence_quality)
    return decision, summary


def _common_values(inp: DeskInputs, strategy: StrategyType, route: dict) -> dict:
    return {
        "mode": inp.config.mode.value,
        "collaboration_mode": COLLABORATION_MODE,
        "strategy_type": strategy.value,
        "route": route,
        "reporting_currency": inp.config.reporting_currency,
        "operational_reserves": inp.config.operational_reserves.model_dump(mode="json") if inp.config.operational_reserves else None,
        "expected_net": None,
    }


COMMON_NULL_REASONS = {
    "expected_net": EXPECTED_NET_REASON,
    "freshness_limit_ms": "maximum_book_age_ms is not configured.",
    "skew_limit_ms": "maximum_snapshot_skew_ms is not configured.",
    "operational_reserves": "operational_reserves is not configured.",
    "account_eligibility": "venue_eligibility is not configured.",
    "available_balances": "Balances are unknown: no account connection and none declared.",
    "reserved_balances": "No executable plan.",
}


# ---------------------------------------------------------------------------
# Triangular spot inside one venue
# ---------------------------------------------------------------------------


def evaluate_triangle(
    inp: DeskInputs,
    triangle: Triangle,
    rules: Sequence[MarketRules],
    fees: Sequence[FeeModel],
    books: Sequence[BookSnapshot],
    requested: Decimal,
    setup_findings: Sequence[Finding],
) -> Evaluation:
    start = triangle.start_asset
    scout_findings, problems = _book_findings(books, rules)
    scout_findings = [*setup_findings, *scout_findings]
    books_ok = not any(problems)

    if books_ok:
        vector = _vector_triangle(inp, triangle, rules, fees, books, requested)
        relay = _relay_triangle(inp, triangle, rules, fees, books, vector)
    else:
        vector = VectorOut(VectorResult.REJECTED, [], requested, ZERO, ["Not evaluated: invalid order book."], start)
        relay = RelayOut(RelayResult.BLOCKED, [])
    fills = vector.fills
    aegis = _aegis(
        inp,
        venues=[triangle.venue],
        books=books,
        rules=rules,
        fees=fees,
        asset=start,
        committed=vector.feasible if books_ok else None,
        trade_size={start: vector.feasible if books_ok else None},
        relay=relay,
        recompute_problems=_recompute_ledger(fills, chained=True) if fills else [],
        upstream=[*scout_findings, *vector.findings, *relay.findings],
    )
    findings = [*scout_findings, *vector.findings, *relay.findings, *aegis.findings]

    fee_labels = [fee.label for fee in fees]
    values = _common_values(
        inp,
        StrategyType.TRIANGULAR_SPOT,
        {"label": triangle.label, "venue": triangle.venue, "legs": [leg.as_dict() for leg in triangle.legs]},
    )
    values.update(_book_fields(inp, books, [leg.side for leg in triangle.legs], problems))
    values.update(_fee_fields(fills, fees, rules))
    values.update(
        asset_identifiers={
            "venue": triangle.venue,
            "assets": triangle.assets,
            "markets": [{"symbol": r.symbol, "market_id": r.market_id, "base": r.base, "quote": r.quote} for r in rules],
        },
        evidence_labels={
            "packet": inp.data_label.value,
            "order_books": inp.data_label.value,
            "book_age_ms": weakest([inp.data_label, VERIFIED]).value,
            "market_rules": weakest([inp.data_label, ESTIMATED]).value,
            "fees": weakest(fee_labels).value,
            "balances": inp.balances_label.value,
            "gross_capture": inp.data_label.value,
            "conditional_net": weakest([inp.data_label, *fee_labels]).value if vector.conditional is not None else UNKNOWN.value,
            "conservative_net": weakest([inp.data_label, ESTIMATED, *fee_labels]).value if vector.conservative is not None else UNKNOWN.value,
            "expected_net": UNKNOWN.value,
            "stress_scenarios": weakest([inp.data_label, ESTIMATED]).value,
        },
        requested_size=money(requested, start),
        feasible_size=({**money(vector.feasible, start), "notes": vector.size_notes} if books_ok else None),
        rounded_quantities=[{"symbol": f.spec.symbol, "amount_base": dstr(f.order_amount)} for f in fills] or None,
        account_eligibility=_eligibility(inp.config, [triangle.venue]),
        available_balances=_balances_view(inp, [triangle.venue]),
        reserved_balances=[{"venue": f.spec.venue, **money(f.spent, f.spec.from_asset)} for f in fills] or None,
        conversions=None,
        gross_capture=money(vector.gross, start),
        conditional_net=money(vector.conditional, start),
        conservative_net=money(vector.conservative, start),
        cost_ledger=(
            {
                "legs": [f.ledger() for f in fills],
                "start": money(vector.feasible, start),
                "final_in_start_asset": money(vector.conditional + vector.feasible, start) if vector.conditional is not None else None,
                "residuals_valued_at_zero": {a: dstr(x) for a, x in _residuals(fills, start).items()},
                "fees_applied": vector.fees_known,
            }
            if fills
            else None
        ),
        cost_assumptions=[
            "Every leg is a taker order walking the visible book; depth beyond the fetched levels is ignored.",
            "Each leg receives the rounded, fee adjusted output of the previous leg.",
            "Residual dust from rounding is valued at zero.",
            "A venue-wide fee tier applies to every market on the venue; per-market fee exceptions are not modeled.",
            "Results are in the start asset; conversion to the reporting currency is not modeled.",
            "No rebalancing is needed: the route starts and ends in the same asset on one venue.",
        ],
        uncertainty_allowances=_allowances(inp.config, "start amount"),
        capital_by_venue={triangle.venue: money(vector.feasible, start)} if books_ok else None,
        total_committed_capital=money(vector.feasible, start) if books_ok else None,
        inventory_before={triangle.venue: {start: dstr(vector.feasible)}} if books_ok else None,
        inventory_after=(
            {triangle.venue: {start: dstr(vector.conditional + vector.feasible), **{a: dstr(x) for a, x in _residuals(fills, start).items()}}}
            if vector.conditional is not None
            else None
        ),
        net_exposure=(
            {start: dstr(vector.conditional), **{a: dstr(x) for a, x in _residuals(fills, start).items()}}
            if vector.conditional is not None
            else None
        ),
        residual_exposure=(
            {"assets": {a: dstr(x) for a, x in _residuals(fills, start).items()}, "valued_at": "zero (conservative)"}
            if vector.conditional is not None
            else None
        ),
        rebalance_plan={"required": False, "reason": "The route returns to the start asset on the same venue."},
        rebalance_cost=money(ZERO, start),
        rebalance_feasibility="NOT_REQUIRED",
        execution_plan=relay.plan or None,
        fill_assumptions=[
            "Legs are submitted one after another. They never fill atomically.",
            "Each order is an IOC limit at the worst price reached in the snapshot walk.",
            "Paper fills are simulated against a fresh book fetched after the decision.",
        ],
        recovery_plan=relay.recovery_plan,
        stress_scenarios=relay.stress or None,
        scout_result=_role_result(ScoutResult.CANDIDATE, scout_findings),
        vector_result=vector.result(),
        relay_result=relay.result(),
        aegis_verdict=aegis.result(),
    )
    reasons = {
        **COMMON_NULL_REASONS,
        "conversions": "The route itself is the conversion; no external conversion is used.",
        "conditional_net": "A fee rate is unknown or no size was feasible.",
        "conservative_net": "Allowances are not configured, a fee is unknown or no size was feasible.",
        "stress_scenarios": "Stress needs known fees and a feasible plan.",
        "execution_plan": "No feasible plan.",
        "feasible_size": "Invalid order book.",
    }
    evidence = weakest([inp.data_label, *fee_labels])
    decision, summary = _decide(inp, StrategyType.TRIANGULAR_SPOT, triangle.label, findings, values, relay, evidence)
    plan = None
    if decision is FinalDecision.PAPER_CANDIDATE:
        plan = PaperPlan(
            StrategyType.TRIANGULAR_SPOT,
            list(triangle.legs),
            list(rules),
            list(fees),
            [f.order_amount for f in fills],
            [f.worst_price for f in fills],
            start,
            vector.feasible,
            vector.conditional,
            inp.now_monotonic,
        )
    rank = (DECISION_RANK[decision], _rank_value(vector))
    return Evaluation(triangle.key, StrategyType.TRIANGULAR_SPOT, triangle.label, decision, findings, values, reasons, summary, rank, plan)


def _residuals(fills: Sequence[LegFill], start: str) -> dict[str, Decimal]:
    leftovers: dict[str, Decimal] = {}
    for fill in fills:
        if fill.spec.from_asset != start and fill.residual:
            leftovers[fill.spec.from_asset] = leftovers.get(fill.spec.from_asset, ZERO) + fill.residual
    return leftovers


def _rank_value(vector: VectorOut) -> Decimal:
    for value in (vector.conservative, vector.conditional, vector.gross):
        bps = vector.bps(value)
        if bps is not None:
            return bps
    return Decimal("-Infinity")


# ---------------------------------------------------------------------------
# Spot across two venues with prefunded balances
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrossRoute:
    symbol: str
    base: str
    quote: str
    buy_venue: str
    sell_venue: str

    @property
    def legs(self) -> tuple[LegSpec, LegSpec]:
        return (
            LegSpec(self.buy_venue, self.symbol, "buy", self.quote, self.base),
            LegSpec(self.sell_venue, self.symbol, "sell", self.base, self.quote),
        )

    @property
    def label(self) -> str:
        return f"BUY {self.symbol}@{self.buy_venue} > SELL {self.symbol}@{self.sell_venue}"

    @property
    def key(self) -> str:
        return f"{StrategyType.SPOT_ACROSS_EXCHANGES.value}|{self.symbol}|{self.buy_venue}>{self.sell_venue}"


def _round_both(amount: Decimal, first: MarketRules, second: MarketRules) -> Decimal:
    for _ in range(5):
        rounded = second.round_amount(first.round_amount(amount))
        if rounded == amount:
            break
        amount = rounded
    return amount


def _vector_cross(
    inp: DeskInputs,
    route: CrossRoute,
    rules: Sequence[MarketRules],
    fees: Sequence[FeeModel],
    books: Sequence[BookSnapshot],
    requested: Decimal,
) -> tuple[VectorOut, Optional[LegFill], Optional[LegFill]]:
    config = inp.config
    buy_spec, sell_spec = route.legs
    buy_book, sell_book = books
    fee_buy, fee_sell = fees
    findings: list[Finding] = []
    notes: list[str] = []
    fees_known = fee_buy.rate is not None and fee_sell.rate is not None
    rate_buy, rate_sell = fee_buy.rate or ZERO, fee_sell.rate or ZERO
    buy_fee_spent, sell_fee_spent = fee_buy.in_spent_asset("buy"), fee_sell.in_spent_asset("sell")
    q = requested

    limit = config.trade_size_limit
    if limit is not None and limit.asset == route.base and q > limit.amount:
        q = limit.amount
        notes.append("Clipped to trade_size_limit.")
    elif limit is not None and limit.asset == route.quote:
        affordable = walk_quote_budget(buy_book.asks, limit.amount / (ONE + rate_buy if buy_fee_spent else ONE)).base
        if q > affordable:
            q = affordable
            notes.append("Clipped to trade_size_limit.")
    cash = inp.balance(route.buy_venue, route.quote)
    if cash is not None:
        affordable = walk_quote_budget(buy_book.asks, cash / (ONE + rate_buy if buy_fee_spent else ONE)).base
        if q > affordable:
            q = affordable
            notes.append(f"Clipped to the {route.quote} balance on {route.buy_venue}.")
    inventory = inp.balance(route.sell_venue, route.base)
    if inventory is not None:
        sellable = inventory / (ONE + rate_sell if sell_fee_spent else ONE)
        if q > sellable:
            q = sellable
            notes.append(f"Clipped to the {route.base} inventory on {route.sell_venue}.")
    depth = min(visible_base(buy_book.asks), visible_base(sell_book.bids))
    if q > depth:
        q = depth
        notes.append("Clipped to the visible order book depth.")
    q = _round_both(q, rules[0], rules[1])
    out = VectorOut(VectorResult.REJECTED, findings, requested, q, notes, route.quote, fees_known=fees_known)
    if q <= 0:
        findings.append(Finding("VECTOR", "NO_FEASIBLE_SIZE", FAIL, "No positive matched size fits the balances, limits and depth."))
        return out, None, None

    buy_gross = execute_leg(buy_spec, rules[0], buy_book, ZERO, False, available=None, target_base=q)
    sell_gross = execute_leg(sell_spec, rules[1], sell_book, ZERO, False, available=None, target_base=q)
    out.gross = sell_gross.quote_amount - buy_gross.quote_amount
    buy_fill, sell_fill = buy_gross, sell_gross
    net_before: Optional[Decimal] = None
    if fees_known:
        buy_fill = execute_leg(buy_spec, rules[0], buy_book, rate_buy, buy_fee_spent, available=None, target_base=q)
        sell_fill = execute_leg(sell_spec, rules[1], sell_book, rate_sell, sell_fee_spent, available=None, target_base=q)
        base_change = buy_fill.received - sell_fill.spent
        # A base shortfall caused by fees is valued at the higher price, as if bought back.
        price = max(buy_fill.vwap, sell_fill.vwap) if base_change < 0 else min(buy_fill.vwap, sell_fill.vwap)
        net_before = sell_fill.received - buy_fill.spent + base_change * price
        bump = ONE / BPS
        buy_bumped = execute_leg(buy_spec, rules[0], buy_book, rate_buy + bump, buy_fee_spent, available=None, target_base=q)
        sell_bumped = execute_leg(sell_spec, rules[1], sell_book, rate_sell + bump, sell_fee_spent, available=None, target_base=q)
        bumped_change = buy_bumped.received - sell_bumped.spent
        bumped_price = max(buy_bumped.vwap, sell_bumped.vwap) if bumped_change < 0 else min(buy_bumped.vwap, sell_bumped.vwap)
        out.fee_sensitivity_per_bps = net_before - (sell_bumped.received - buy_bumped.spent + bumped_change * bumped_price)
    else:
        unknown = sorted({fee.source for fee in fees if fee.rate is None})
        findings.append(Finding("VECTOR", "FEE_UNKNOWN", MISSING, f"Fee rate unknown ({'; '.join(unknown)}); never treated as zero."))
    out.fills = [buy_fill, sell_fill]
    out.net_before_unknown_costs = net_before

    rebalance = config.cost_inputs.rebalance_cost_per_trade
    rebalance_cost: Optional[Decimal] = None
    if rebalance is None:
        findings.append(
            Finding("VECTOR", "REBALANCE_COST_UNKNOWN", MISSING, "Restoring inventory between venues has a cost that is not configured.")
        )
    elif rebalance.asset != route.quote:
        findings.append(
            Finding("VECTOR", "REBALANCE_COST_ASSET_MISMATCH", MISSING, f"rebalance_cost_per_trade is in {rebalance.asset}, not {route.quote}.")
        )
    else:
        rebalance_cost = rebalance.amount
        if not config.cost_inputs.rebalance_cost_source:
            findings.append(Finding("VECTOR", "REBALANCE_COST_SOURCE_MISSING", CONDITION, "The rebalancing cost has no stated source."))
    if net_before is not None and rebalance_cost is not None:
        out.conditional = net_before - rebalance_cost
    # Committed capital: quote spent at the buy venue plus the inventory sold at the sell venue.
    out.denominator = buy_fill.spent + sell_fill.quote_amount
    out.conservative, out.allowance_bps = _economics_findings(
        config, route.quote, out.denominator, out.gross, out.conditional, net_before, findings
    )
    if out.gross > 0 and buy_gross.vwap:
        ratio = sell_gross.vwap / buy_gross.vwap
        out.breakeven_fee_bps_per_leg = (ONE - (ONE / ratio).sqrt()) * BPS
    out.status = _status(findings, VectorResult.REJECTED, VectorResult.INCONCLUSIVE, VectorResult.VALIDATED_FOR_PAPER)
    return out, buy_fill, sell_fill


def _relay_cross(inp, route: CrossRoute, rules, fees, books, vector: VectorOut) -> RelayOut:
    if not vector.fills:
        return RelayOut(RelayResult.BLOCKED, [Finding("RELAY", "NO_EXECUTABLE_PLAN", FAIL, "VECTOR found no feasible size to plan.")])
    findings = _relay_common(inp, vector.fills, [route.buy_venue, route.sell_venue])
    findings.append(
        Finding(
            "RELAY",
            "NOT_ATOMIC",
            INFO,
            "Two venues cannot fill atomically. Fill or kill on one venue does not make the pair atomic, and parallel "
            "submission does not guarantee matched fills.",
        )
    )
    out = RelayOut(RelayResult.BLOCKED, findings, plan=[_plan_leg(f, fee) for f, fee in zip(vector.fills, fees)])
    out.recovery_plan = _recovery_plan(
        "If only the buy fills, sell the extra base at market on the buy venue. If only the sell fills, buy the base "
        "back at market on the sell venue to restore inventory."
    )
    if vector.fees_known:
        buy_fill, sell_fill = vector.fills
        adverse = _adverse_bps(inp.config)
        shift = (adverse or ZERO) / BPS
        rate_buy, rate_sell = fees[0].rate, fees[1].rate
        unwind = execute_leg(
            route.legs[0].reversed(), rules[0], books[0], rate_buy, fees[0].in_spent_asset("sell"), available=buy_fill.received
        )
        rebuy_fee_received = not fees[1].in_spent_asset("buy")
        target = sell_fill.spent / (ONE - rate_sell) if rebuy_fee_received else sell_fill.spent
        rebuy = execute_leg(
            route.legs[1].reversed(), rules[1], books[1], rate_sell, fees[1].in_spent_asset("buy"), available=None, target_base=target
        )
        results = (
            ("BUY_FILLS_SELL_FAILS", buy_fill.received, route.base, unwind, unwind.received * (ONE - shift) - buy_fill.spent, buy_fill.spent),
            ("SELL_FILLS_BUY_FAILS", sell_fill.spent, route.base, rebuy, sell_fill.received - rebuy.spent * (ONE + shift), rebuy.spent),
        )
        losses, exposures = [], []
        for name, held, asset, recovery, result, exposure in results:
            losses.append(max(ZERO, -result))
            exposures.append(exposure)
            out.stress.append(
                {
                    "scenario": name,
                    "unmatched_exposure": money(held, asset),
                    "exposure_value_now": money(exposure, route.quote),
                    "recovery": f"{recovery.spec.side} {recovery.spec.symbol} at market on {recovery.spec.venue}",
                    "adverse_movement_bps_applied": dstr(adverse),
                    "net_result": money(result, route.quote),
                    "loss": money(max(ZERO, -result), route.quote),
                    "recovery_depth_sufficient": not recovery.has("DEPTH_INSUFFICIENT"),
                    "time_exposed_ms": None,
                    "evidence_label": weakest([inp.data_label, ESTIMATED]).value,
                    "note": "Valued on the current snapshots. Later moves can make it worse; this is not a maximum possible loss.",
                }
            )
        out.worst_loss = max(losses)
        out.loss_is_lower_bound = adverse is None
        out.max_exposure = max(exposures)
    out.status = _status(findings, RelayResult.BLOCKED, RelayResult.NEEDS_ENGINEERING, RelayResult.FEASIBLE_FOR_PAPER)
    return out


def cross_setup_findings(config: DeskConfig, route: CrossRoute) -> list[Finding]:
    findings = []
    for asset in (route.base, route.quote):
        if asset not in config.verified_equivalent_assets:
            findings.append(
                Finding(
                    "AEGIS",
                    "ASSET_EQUIVALENCE_UNVERIFIED",
                    MISSING,
                    f"{asset} on {route.buy_venue} and {route.sell_venue} is not declared equivalent (contract, network, redemption).",
                )
            )
    return findings


def evaluate_cross(
    inp: DeskInputs,
    route: CrossRoute,
    rules: Sequence[MarketRules],
    fees: Sequence[FeeModel],
    books: Sequence[BookSnapshot],
    requested: Decimal,
    setup_findings: Sequence[Finding],
) -> Evaluation:
    scout_findings, problems = _book_findings(books, rules)
    scout_findings = [*setup_findings, *scout_findings]
    books_ok = not any(problems)
    buy_fill = sell_fill = None
    if books_ok:
        vector, buy_fill, sell_fill = _vector_cross(inp, route, rules, fees, books, requested)
        relay = _relay_cross(inp, route, rules, fees, books, vector)
    else:
        vector = VectorOut(VectorResult.REJECTED, [], requested, ZERO, ["Not evaluated: invalid order book."], route.quote)
        relay = RelayOut(RelayResult.BLOCKED, [])
    equivalence = cross_setup_findings(inp.config, route)
    committed = vector.denominator if vector.fills else None
    aegis = _aegis(
        inp,
        venues=[route.buy_venue, route.sell_venue],
        books=books,
        rules=rules,
        fees=fees,
        asset=route.quote,
        committed=committed,
        trade_size={route.base: vector.feasible if books_ok else None, route.quote: buy_fill.spent if buy_fill else None},
        relay=relay,
        recompute_problems=_recompute_ledger(vector.fills, chained=False) if vector.fills else [],
        upstream=[*scout_findings, *vector.findings, *relay.findings, *equivalence],
    )
    aegis.findings[:0] = equivalence
    findings = [*scout_findings, *vector.findings, *relay.findings, *aegis.findings]
    fills = vector.fills
    fee_labels = [fee.label for fee in fees]
    base, quote = route.base, route.quote

    values = _common_values(
        inp,
        StrategyType.SPOT_ACROSS_EXCHANGES,
        {"label": route.label, "legs": [leg.as_dict() for leg in route.legs]},
    )
    values.update(_book_fields(inp, books, ["buy", "sell"], problems))
    values.update(_fee_fields(fills, fees, rules))
    rebalance = inp.config.cost_inputs.rebalance_cost_per_trade
    values.update(
        asset_identifiers={
            "symbol": route.symbol,
            "base": base,
            "quote": quote,
            "markets": [{"venue": r.venue, "symbol": r.symbol, "market_id": r.market_id} for r in rules],
            "declared_equivalent": [a for a in (base, quote) if a in inp.config.verified_equivalent_assets],
        },
        evidence_labels={
            "packet": inp.data_label.value,
            "order_books": inp.data_label.value,
            "book_age_ms": weakest([inp.data_label, VERIFIED]).value,
            "market_rules": weakest([inp.data_label, ESTIMATED]).value,
            "fees": weakest(fee_labels).value,
            "balances": inp.balances_label.value,
            "asset_equivalence": ESTIMATED.value if not equivalence else UNKNOWN.value,
            "gross_capture": inp.data_label.value,
            "conditional_net": weakest([inp.data_label, ESTIMATED, *fee_labels]).value if vector.conditional is not None else UNKNOWN.value,
            "conservative_net": weakest([inp.data_label, ESTIMATED, *fee_labels]).value if vector.conservative is not None else UNKNOWN.value,
            "expected_net": UNKNOWN.value,
            "stress_scenarios": weakest([inp.data_label, ESTIMATED]).value,
        },
        requested_size=money(requested, base),
        feasible_size=({**money(vector.feasible, base), "notes": vector.size_notes} if books_ok else None),
        rounded_quantities=[{"venue": f.spec.venue, "amount_base": dstr(f.order_amount)} for f in fills] or None,
        account_eligibility=_eligibility(inp.config, [route.buy_venue, route.sell_venue]),
        available_balances=_balances_view(inp, [route.buy_venue, route.sell_venue]),
        reserved_balances=[{"venue": f.spec.venue, **money(f.spent, f.spec.from_asset)} for f in fills] or None,
        conversions=None,
        gross_capture=money(vector.gross, quote),
        conditional_net=money(vector.conditional, quote),
        conservative_net=money(vector.conservative, quote),
        cost_ledger=(
            {
                "legs": [f.ledger() for f in fills],
                "net_before_rebalancing": money(vector.net_before_unknown_costs, quote),
                "rebalance_cost_per_trade": rebalance.model_dump(mode="json") if rebalance else None,
                "fees_applied": vector.fees_known,
            }
            if fills
            else None
        ),
        cost_assumptions=[
            "Both legs are taker orders walking the visible books; depth beyond the fetched levels is ignored.",
            "The same matched base quantity is bought on one venue and sold from existing inventory on the other.",
            "A base shortfall caused by fees is valued at the higher of the two prices, as if bought back.",
            "Prefunded trading does not lock the spread over time: inventory must be restored, and that costs money.",
            "Results are in the quote asset; conversion to the reporting currency is not modeled.",
        ],
        uncertainty_allowances=_allowances(inp.config, "total committed capital on both venues"),
        capital_by_venue=(
            {route.buy_venue: money(buy_fill.spent, quote), route.sell_venue: money(sell_fill.spent, base)} if fills else None
        ),
        total_committed_capital=money(committed, quote),
        inventory_before=_balances_view(inp, [route.buy_venue, route.sell_venue]),
        inventory_after=(
            {
                route.buy_venue: {quote: dstr(-buy_fill.spent), base: dstr(buy_fill.received)},
                route.sell_venue: {base: dstr(-sell_fill.spent), quote: dstr(sell_fill.received)},
                "note": "Changes per venue. Inventory drifts toward the buy venue until rebalanced.",
            }
            if fills
            else None
        ),
        net_exposure=(
            {base: dstr(buy_fill.received - sell_fill.spent), quote: dstr(sell_fill.received - buy_fill.spent)} if fills else None
        ),
        residual_exposure=(
            {
                "venue_inventory_shift": {route.buy_venue: dstr(buy_fill.received), route.sell_venue: dstr(-sell_fill.spent)},
                "asset": base,
                "note": "The pair adds little net price exposure, but the prefunded inventory on both venues stays fully exposed.",
            }
            if fills
            else None
        ),
        rebalance_plan=(
            {
                "required": True,
                "transfers": [
                    {"asset": base, "from": route.buy_venue, "to": route.sell_venue, "amount": dstr(buy_fill.received)},
                    {"asset": quote, "from": route.sell_venue, "to": route.buy_venue, "amount": dstr(sell_fill.received)},
                ],
                "note": "Deposits and withdrawals can be suspended. Networks, fees and timing must be verified per asset.",
            }
            if fills
            else None
        ),
        rebalance_cost=rebalance.model_dump(mode="json") if rebalance else None,
        rebalance_feasibility="USER_DECLARED_COST" if rebalance else "UNKNOWN",
        execution_plan=relay.plan or None,
        fill_assumptions=[
            "Both legs are IOC limit orders at the worst price reached in the snapshot walk.",
            "The two venues are not filled atomically.",
            "Paper fills are simulated against fresh books fetched after the decision.",
        ],
        recovery_plan=relay.recovery_plan,
        stress_scenarios=relay.stress or None,
        scout_result=_role_result(ScoutResult.CANDIDATE, scout_findings),
        vector_result=vector.result(),
        relay_result=relay.result(),
        aegis_verdict=aegis.result(),
    )
    reasons = {
        **COMMON_NULL_REASONS,
        "conversions": "Both venues quote the same symbol; no conversion is used.",
        "conditional_net": "A fee rate or the rebalancing cost is unknown, or no size was feasible.",
        "conservative_net": "Allowances or other costs are not configured, or no size was feasible.",
        "rebalance_cost": "rebalance_cost_per_trade is not configured.",
        "stress_scenarios": "Stress needs known fees and a feasible plan.",
        "execution_plan": "No feasible plan.",
        "total_committed_capital": "No feasible plan.",
        "feasible_size": "Invalid order book.",
        "inventory_before": "Balances are unknown.",
    }
    evidence = weakest([inp.data_label, *fee_labels])
    decision, summary = _decide(inp, StrategyType.SPOT_ACROSS_EXCHANGES, route.label, findings, values, relay, evidence)
    plan = None
    if decision is FinalDecision.PAPER_CANDIDATE:
        plan = PaperPlan(
            StrategyType.SPOT_ACROSS_EXCHANGES,
            list(route.legs),
            list(rules),
            list(fees),
            [f.order_amount for f in fills],
            [f.worst_price for f in fills],
            quote,
            buy_fill.spent,
            vector.net_before_unknown_costs,  # transfers are not simulated, so compare before rebalancing
            inp.now_monotonic,
        )
    rank = (DECISION_RANK[decision], _rank_value(vector))
    return Evaluation(route.key, StrategyType.SPOT_ACROSS_EXCHANGES, route.label, decision, findings, values, reasons, summary, rank, plan)
