"""Regression tests for defects found in review. Each one failed before its fix."""
import asyncio
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from arb.config import DeskConfig
from arb.engine import max_fillable_start
from arb.protocol import EvidenceLabel
from conftest import TRIANGLE_BOOKS, VENUE, full_config, spot, triangle_markets, triangle_md
from main import create_app
from test_desk import CROSS_BOOKS, cross, cross_config, cross_md, triangle
from test_orderbook_engine import BOOKS, FEE_GET, LEGS, RULES

VERIFIED = EvidenceLabel.VERIFIED


def test_cross_stress_with_thin_rebuy_depth_is_not_a_paper_candidate(make_service):
    md = cross_md(label=VERIFIED)
    md.books["b"]["BTC/USDT"] = ([["50600", "1"]], [["50650", "0.01"]])  # cannot buy 0.1 BTC back
    response = cross(make_service(md, cross_config(loss_per_incident_limit={"amount": "25", "asset": "USDT"})))
    assert response["decision"] == "RESEARCH_ONLY"
    assert "RECOVERY_DEPTH_INSUFFICIENT" in response["summary"]["blockers"]
    [scenario] = [s for s in response["opportunity"]["stress_scenarios"] if s["scenario"] == "SELL_FILLS_BUY_FAILS"]
    assert scenario["loss"] is None and scenario["recovery_depth_sufficient"] is False


def test_daily_loss_in_another_asset_is_not_counted_as_zero(make_service):
    service = make_service(triangle_md(label=VERIFIED), full_config(daily_loss_limit={"amount": "100", "asset": "USD"}))
    service.journal.append("PAPER_RESULT", realized_net={"amount": "-500", "asset": "USDT"})
    response = triangle(service)
    assert response["decision"] == "RESEARCH_ONLY"
    assert "LIMIT_ASSET_MISMATCH" in response["summary"]["blockers"]


def test_zero_price_level_is_rejected_without_crashing(make_service):
    books = dict(
        TRIANGLE_BOOKS,
        **{"BTC/USDT": ([["49990", "1"]], [["0", "1"], ["50000", "1"]]), "ETH/BTC": ([["0.0521", "100"]], [["0.0522", "100"]])},
    )
    response = asyncio.run(make_service(triangle_md(books=books, label=VERIFIED), full_config()).evaluate_triangular(VENUE, "BTC", D("0.02"))).response
    assert response["decision"] == "NO_TRADE"
    assert "BOOK_INVALID" in response["summary"]["blockers"]


def test_one_failing_job_does_not_stop_the_others(make_service):
    jobs = [{"venue": VENUE, "start_asset": "USDT", "size": "1000"}, {"venue": VENUE, "start_asset": "USDT", "size": "500"}]
    service = make_service(triangle_md(), full_config(scan={"interval_seconds": 5, "triangular_jobs": jobs}))
    original = service.evaluate_triangular
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("fixture bug")
        return await original(*args, **kwargs)

    service.evaluate_triangular = flaky
    asyncio.run(service.screener.run_cycle())
    results = service.screener.last_results
    assert results[0]["reason"].startswith("INTERNAL_ERROR") and results[1]["decision"] == "RESEARCH_ONLY"
    assert service.journal.tail(5, ["ERROR"])[0]["error"].startswith("RuntimeError")


def test_paper_rebuy_is_limited_to_quote_held_on_the_sell_venue(make_service):
    md = cross_md(label=VERIFIED)
    md.books["a"]["BTC/USDT"] = [CROSS_BOOKS["a"]["BTC/USDT"], ([["49950", "1"]], [["50100", "1"]])]  # buy misses its limit
    service = make_service(md, cross_config(mode="PAPER"))
    service.reset_paper()
    asyncio.run(service.screener.run_cycle())
    assert service.journal.tail(5, ["HALT"]) == []
    assert D(service.portfolio.as_dict()["balances"]["b"]["USDT"]) >= 0


def test_paper_order_below_the_venue_minimum_is_rejected_not_filled(make_service):
    markets = triangle_markets(**{"ETH/BTC": spot("ETH/BTC", "0.0001", min_amount=0.1)})
    books = dict(TRIANGLE_BOOKS)
    books["BTC/USDT"] = [TRIANGLE_BOOKS["BTC/USDT"], ([["49990", "1"]], [["50000", "0.001"], ["50100", "1"]])]
    service = make_service(triangle_md(books=books, markets=markets, label=VERIFIED), full_config(mode="PAPER"))
    service.reset_paper()
    asyncio.run(service.screener.run_cycle())
    [result] = service.journal.tail(5, ["PAPER_RESULT"])
    # Leg 1 fills 0.001 of its 0.02 BTC order; the 0.0199 ETH follow-up is below the 0.1 minimum, so the
    # BTC goes back to USDT at the 49990 bid instead.
    assert any(problem.startswith("ORDER_REJECTED ETH/BTC") for problem in result["problems"])
    assert D(result["realized_net"]["amount"]) == D(950) + D("0.00099") * D(49990) * D("0.999") - D(1000)
    assert result["fills"][0]["order_amount_base"] == "0.02"


def test_evaluation_uses_one_config_snapshot(make_service):
    md = triangle_md(label=VERIFIED)
    service = make_service(md, full_config(mode="PAPER"))
    service.reset_paper()
    original = md.fetch_order_book

    async def change_config_midway(venue, symbol, depth):
        if len(md.fetches) == 0:
            service.update_config(DeskConfig.model_validate(full_config(mode="PAPER", allowed_assets=["USDT", "BTC"])))
        return await original(venue, symbol, depth)

    md.fetch_order_book = change_config_midway
    asyncio.run(service.screener.run_cycle())
    [observed] = service.journal.tail(5, ["OPPORTUNITY_OBSERVED"])
    assert observed["config_version"] == 2  # evaluated entirely under the config it started with
    assert service.journal.tail(5, ["PAPER_RESULT"]) == []  # the plan is stale under version 3
    assert service.journal.tail(5, ["PAPER_SKIPPED"])[0]["reason"].startswith("Config changed")


def test_sdk_only_venue_rules_and_unknown_market_status_are_conditions(make_service):
    response = triangle(make_service(triangle_md(label=VERIFIED), full_config(venue_rules_verified=None)))
    assert response["decision"] == "RESEARCH_ONLY"
    assert "VENUE_RULES_NOT_VERIFIED" in response["summary"]["conditions"]
    markets = {s: {**m, "active": None} for s, m in triangle_markets().items()}
    response = triangle(make_service(triangle_md(markets=markets, label=VERIFIED), full_config()))
    assert "MARKET_STATUS_UNKNOWN" in response["summary"]["conditions"]


def test_unknown_fees_are_shown_as_unknown_not_zero(make_service):
    markets = triangle_markets(**{"ETH/USDT": spot("ETH/USDT", taker=None)})
    packet = triangle(make_service(triangle_md(markets=markets), full_config(fee_tiers=None)))["opportunity"]
    assert {leg["expected_fee"]["amount"] for leg in packet["execution_plan"]} == {None}
    assert {leg["fee_amount"] for leg in packet["fees_by_leg"]} == {None}
    assert {row["fee_rate"] for row in packet["cost_ledger"]["legs"]} == {None}


def test_paper_cross_result_includes_the_allocated_rebalancing_cost(make_service):
    service = make_service(cross_md(label=VERIFIED), cross_config(mode="PAPER"))
    service.reset_paper()
    asyncio.run(service.screener.run_cycle())
    [result] = service.journal.tail(5, ["PAPER_RESULT"])
    assert result["planned_net"] == {"amount": "44.88", "asset": "USDT"}
    # 54.94 from the pair, 5.065 to buy back the 0.0001 BTC fee shortfall, 0.0000001 BTC left short at 50650,
    # and the 5 USDT rebalancing cost.
    assert D(result["realized_net"]["amount"]) == D("54.94") - D("5.065") - D("0.0000001") * D(50650) - D(5)


def test_max_fillable_start_handles_huge_requests():
    assert max_fillable_start(LEGS, RULES, BOOKS, [FEE_GET] * 3, D("1e24"), zero_fees=False) == D(50000)


def test_trade_log_accepts_limit_zero(tmp_path):
    with TestClient(create_app(triangle_md(), tmp_path / "data")) as client:
        assert client.get("/get_trade_log", params={"limit": 0}).json()["logs"] == []


def test_paper_start_is_refused_while_the_portfolio_is_negative(make_service):
    service = make_service(triangle_md(label=VERIFIED), full_config(mode="PAPER"))
    service.reset_paper()
    service.portfolio.balances[VENUE]["USDT"] = D("-1")
    with pytest.raises(Exception, match="negative"):
        asyncio.run(service.screener.start())


def test_venue_specific_amount_rounding_is_unknown_not_guessed():
    from arb.market_data import SIGNIFICANT_DIGITS, VenueMeta, market_rules

    market = spot("BTC/USD", "8")  # precision 8 means 8 significant digits under this mode
    generic = VenueMeta(SIGNIFICANT_DIGITS, None, True, True)
    custom = VenueMeta(SIGNIFICANT_DIGITS, None, True, True, custom_amount_rounding=True)
    assert market_rules("x", market, generic).amount_rule.kind == "significant_digits"
    assert market_rules("bitfinex", market, custom).amount_rule is None
