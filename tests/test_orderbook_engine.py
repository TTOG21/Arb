"""Order book walks and execution arithmetic, checked against hand calculations."""
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from arb.engine import AmountRule, FeeModel, LegSpec, MarketRules, execute_leg, max_fillable_start, run_chain
from arb.orderbook import BookSnapshot, integrity_problems, snapshot_skew_ms, to_levels, walk_base, walk_quote_budget
from arb.protocol import EvidenceLabel

NOW = datetime.now(timezone.utc)
FIXTURE = EvidenceLabel.HYPOTHETICAL


def book(symbol, bids, asks, sent=0.0, received=0.05):
    return BookSnapshot("x", symbol, to_levels(bids), to_levels(asks), "id", None, None, NOW, NOW, sent, received, "fixture", FIXTURE)


def rules(symbol, step, min_amount=None, min_cost=None, active=True):
    base, quote = symbol.split("/")
    return MarketRules("x", symbol, base, quote, active, AmountRule("step", D(step)), min_amount, min_cost)


FEE_GET = FeeModel(D("0.001"), "get", FIXTURE, FIXTURE, "fixture")
FEE_QUOTE = FeeModel(D("0.001"), "quote", FIXTURE, FIXTURE, "fixture")
LEGS = [
    LegSpec("x", "BTC/USDT", "buy", "USDT", "BTC"),
    LegSpec("x", "ETH/BTC", "buy", "BTC", "ETH"),
    LegSpec("x", "ETH/USDT", "sell", "ETH", "USDT"),
]
RULES = [rules("BTC/USDT", "0.00001"), rules("ETH/BTC", "0.0001"), rules("ETH/USDT", "0.0001")]
BOOKS = [
    book("BTC/USDT", [["49990", "1"]], [["50000", "1"]]),
    book("ETH/BTC", [["0.0499", "100"]], [["0.05", "100"]]),
    book("ETH/USDT", [["2600", "100"]], [["2601", "100"]]),
]


def test_walk_base_uses_depth_and_reports_vwap():
    walk = walk_base(to_levels([["100", "1"], ["101", "2"]]), D("2"))
    assert (walk.base, walk.quote, walk.worst_price, walk.levels_used, walk.complete) == (D(2), D(201), D(101), 2, True)
    assert walk.vwap == D("100.5")


def test_walk_stops_at_price_limit_and_reports_incomplete():
    walk = walk_base(to_levels([["100", "1"], ["101", "2"]]), D("2"), price_ok=lambda p: p <= D("100"))
    assert (walk.base, walk.complete) == (D(1), False)


def test_quote_budget_takes_partial_level():
    walk = walk_quote_budget(to_levels([["100", "1"], ["200", "1"]]), D("150"))
    assert (walk.base, walk.quote, walk.complete) == (D("1.25"), D(150), True)


@pytest.mark.parametrize(
    "bids, asks, expected",
    [
        ([["101", "1"]], [["100", "1"]], "crossed"),
        ([["99", "1"], ["100", "1"]], [["101", "1"]], "not strictly ordered"),
        ([], [["101", "1"]], "no bids"),
        ([["99", "0"]], [["101", "1"]], "non-positive"),
    ],
)
def test_integrity_problems(bids, asks, expected):
    assert any(expected in p for p in integrity_problems(book("A/B", bids, asks)))


def test_age_and_skew_are_upper_bounds_from_send_times():
    first, second = book("A/B", [["1", "1"]], [["2", "1"]], 10.0, 10.2), book("C/B", [["1", "1"]], [["2", "1"]], 10.3, 10.5)
    assert first.age_ms(11.0) == 1000
    assert snapshot_skew_ms([first, second]) == 500


def test_amount_rules_round_down():
    assert AmountRule("step", D("0.0001")).round_down(D("0.3992004")) == D("0.3992")
    assert AmountRule("significant_digits", D(3)).round_down(D("0.012345")) == D("0.0123")


def test_buy_with_fee_in_received_asset():
    fill = execute_leg(LEGS[0], RULES[0], BOOKS[0], D("0.001"), False, available=D(1000))
    assert (fill.order_amount, fill.spent, fill.received, fill.fee_asset) == (D("0.02"), D(1000), D("0.01998"), "BTC")


def test_buy_with_fee_in_quote_leaves_room_for_the_fee():
    # 1000 / 1.001 = 999.000999... buys 0.01998001... BTC, rounded down to 0.01998 at 50000.
    fill = execute_leg(LEGS[0], RULES[0], BOOKS[0], D("0.001"), True, available=D(1000))
    assert (fill.order_amount, fill.fee_amount, fill.spent, fill.residual) == (D("0.01998"), D("0.999"), D("999.999"), D("0.001"))


def test_sell_with_fee_in_spent_base():
    leg = LegSpec("x", "ETH/USDT", "sell", "ETH", "USDT")
    fill = execute_leg(leg, RULES[2], BOOKS[2], D("0.001"), True, available=D("1.001"))
    assert (fill.order_amount, fill.spent, fill.received) == (D(1), D("1.001"), D(2600))


def test_minimums_and_status_are_reported():
    strict = rules("BTC/USDT", "0.00001", min_amount=D("0.1"), min_cost=D("10000"), active=False)
    codes = {c for c, _ in execute_leg(LEGS[0], strict, BOOKS[0], D(0), False, available=D(1000)).problems}
    assert {"BELOW_MIN_AMOUNT", "BELOW_MIN_COST", "MARKET_INACTIVE"} <= codes
    tiny = execute_leg(LEGS[0], RULES[0], BOOKS[0], D(0), False, available=D("0.1"))
    assert tiny.has("ZERO_ORDER_AMOUNT")


def test_triangle_matches_hand_calculation():
    # 1000 USDT -> 0.02 BTC -> fee -> 0.01998 -> 0.3996 ETH -> fee -> 0.3992004
    # -> sell 0.3992 at 2600 = 1037.92 -> fee -> 1036.88208 USDT, ETH dust 0.0000004
    net = run_chain(LEGS, RULES, BOOKS, [FEE_GET] * 3, D(1000))
    gross = run_chain(LEGS, RULES, BOOKS, [FEE_GET] * 3, D(1000), zero_fees=True)
    assert net.final_amount == D("1036.88208")
    assert net.residuals == {"ETH": D("0.0000004")}
    assert gross.final_amount == D(1040)


def test_unknown_fee_is_never_zero():
    unknown = FeeModel(None, "get", EvidenceLabel.UNKNOWN, EvidenceLabel.UNKNOWN, "none")
    with pytest.raises(ValueError):
        run_chain(LEGS, RULES, BOOKS, [unknown] * 3, D(1000))


def test_max_fillable_start_respects_visible_depth():
    # Only 1 BTC is offered at 50000, so at most 50000 USDT fits the first leg.
    assert max_fillable_start(LEGS, RULES, BOOKS, [FEE_GET] * 3, D(80000), zero_fees=False) == D(50000)
