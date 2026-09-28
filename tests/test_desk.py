"""The five roles through the service, on fixture markets (HYPOTHETICAL numbers)."""
import asyncio
from decimal import Decimal as D

from arb.config import DeskConfig
from arb.packet import PACKET_FIELDS
from arb.protocol import EvidenceLabel
from conftest import TRIANGLE_BOOKS, VENUE, FakeMarketData, full_config, spot, triangle_markets, triangle_md

VERIFIED = EvidenceLabel.VERIFIED


def triangle(service, size="1000"):
    return asyncio.run(service.evaluate_triangular(VENUE, "USDT", D(size))).response


def flagged(response):
    return set(response["summary"]["blockers"]) | set(response["summary"]["conditions"])


def test_empty_config_is_research_only_and_lists_the_intake(make_service):
    response = triangle(make_service(triangle_md()))
    assert (response["decision"], response["execution_status"]) == ("RESEARCH_ONLY", "NO_TRADE")
    assert {"VENUES_NOT_CONFIGURED", "ALLOWANCES_NOT_CONFIGURED", "BALANCES_UNKNOWN", "TRADE_SIZE_LIMIT_NOT_CONFIGURED"} <= flagged(response)
    packet = response["opportunity"]
    assert packet["conditional_net"] == {"amount": "36.88208", "asset": "USDT"}
    assert packet["conservative_net"] is None and "conservative_net" in packet["null_reasons"]
    assert packet["evidence_labels"]["packet"] == "HYPOTHETICAL"


def test_complete_inputs_on_verified_data_make_a_paper_candidate(make_service):
    response = triangle(make_service(triangle_md(label=VERIFIED), full_config()))
    assert response["decision"] == "PAPER_CANDIDATE", response["summary"]
    packet = response["opportunity"]
    assert packet["vector_result"]["status"] == "VALIDATED_FOR_PAPER"
    assert packet["relay_result"]["status"] == "FEASIBLE_FOR_PAPER"
    assert packet["aegis_verdict"]["status"] == "PASS_FOR_PAPER"
    # 36.88208 minus 30 bps (20 adverse + 10 model) of the 1000 USDT committed.
    assert packet["conservative_net"] == {"amount": "33.88208", "asset": "USDT"}
    assert packet["expected_net"] is None
    assert set(PACKET_FIELDS) <= set(packet)
    assert all(packet[name] is None for name in packet["null_reasons"])
    assert [leg["time_in_force"] for leg in packet["execution_plan"]] == ["IOC"] * 3


def test_fixture_data_never_becomes_a_paper_candidate(make_service):
    response = triangle(make_service(triangle_md(), full_config()))
    assert response["decision"] == "RESEARCH_ONLY"
    assert "MARKET_DATA_NOT_VERIFIED" in response["summary"]["conditions"]


def test_sdk_default_fees_are_only_a_condition(make_service):
    response = triangle(make_service(triangle_md(label=VERIFIED), full_config(fee_tiers=None)))
    assert response["decision"] == "RESEARCH_ONLY"
    assert "FEE_NOT_VERIFIED" in response["summary"]["conditions"]


def test_unknown_fee_is_missing_never_zero(make_service):
    markets = triangle_markets(**{"ETH/USDT": spot("ETH/USDT", taker=None)})
    response = triangle(make_service(triangle_md(markets=markets, label=VERIFIED), full_config(fee_tiers=None)))
    assert response["decision"] == "RESEARCH_ONLY"
    assert "FEE_UNKNOWN" in response["summary"]["blockers"]
    assert response["opportunity"]["conditional_net"] is None
    assert response["opportunity"]["gross_capture"] == {"amount": "40", "asset": "USDT"}


def test_negative_after_fees_is_rejected(make_service):
    # Gross +2 USDT on 1000 turns into -1.003996 after three 0.1% fees.
    books = dict(TRIANGLE_BOOKS, **{"ETH/USDT": ([["2505", "100"]], [["2506", "100"]])})
    response = triangle(make_service(triangle_md(books=books, label=VERIFIED), full_config()))
    assert response["decision"] == "NO_TRADE"
    assert "NEGATIVE_AFTER_COSTS" in response["summary"]["blockers"]
    assert response["opportunity"]["conditional_net"] == {"amount": "-1.003996", "asset": "USDT"}
    assert response["opportunity"]["vector_result"]["status"] == "REJECTED"


def test_no_positive_top_of_book_edge_means_no_candidate(make_service):
    books = dict(TRIANGLE_BOOKS, **{"ETH/USDT": ([["2500", "100"]], [["2501", "100"]])})
    response = triangle(make_service(triangle_md(books=books)))
    assert (response["decision"], response["scout_result"]) == ("NO_TRADE", "NO_CANDIDATE")
    assert response["reason"].startswith("NO_POSITIVE_DISPLAYED_EDGE")


def test_stale_and_unsynchronized_books_are_vetoed(make_service):
    response = triangle(make_service(triangle_md(label=VERIFIED, fetch_seconds=3), full_config()))
    assert response["decision"] == "NO_TRADE"
    assert {"STALE_BOOK", "UNSYNCHRONIZED_BOOKS"} <= set(response["summary"]["blockers"])
    assert response["opportunity"]["aegis_verdict"]["status"] == "VETO"


def test_crossed_book_is_rejected_before_any_economics(make_service):
    books = dict(TRIANGLE_BOOKS, **{"ETH/BTC": ([["0.051", "100"]], [["0.05", "100"]])})
    response = triangle(make_service(triangle_md(books=books, label=VERIFIED), full_config()))
    assert response["decision"] == "NO_TRADE"
    assert "BOOK_INVALID" in response["summary"]["blockers"]
    assert response["opportunity"]["conditional_net"] is None


def test_stress_loss_above_the_incident_limit_is_vetoed(make_service):
    config = full_config(loss_per_incident_limit={"amount": "1", "asset": "USDT"})
    response = triangle(make_service(triangle_md(label=VERIFIED), config))
    assert response["decision"] == "NO_TRADE"
    assert "INCIDENT_LOSS_LIMIT_EXCEEDED" in response["summary"]["blockers"]
    worst = response["summary"]["stress_loss_scenario"]
    # Leg 2 fails: 0.01998 BTC goes back at the 49990 bid, pays 0.1%, and the 20 bps allowance applies.
    expected = D(1000) - D("0.01998") * D(49990) * D("0.999") * (1 - D("0.002"))
    assert worst["scenario"] == "LEG_2_FAILS_AFTER_LEG_1"
    assert D(worst["loss"]["amount"]) == expected


def test_limits_in_another_asset_are_not_assumed_convertible(make_service):
    config = full_config(trade_size_limit={"amount": "900", "asset": "EUR"})
    response = triangle(make_service(triangle_md(label=VERIFIED), config))
    assert response["decision"] == "RESEARCH_ONLY"
    assert "LIMIT_ASSET_MISMATCH" in response["summary"]["blockers"]


def test_size_is_clipped_to_the_declared_balance(make_service):
    config = full_config(balances_by_venue={VENUE: {"USDT": "500"}})
    packet = triangle(make_service(triangle_md(label=VERIFIED), config))["opportunity"]
    assert (packet["requested_size"]["amount"], packet["feasible_size"]["amount"]) == ("1000", "500")


def test_aegis_recomputation_agrees_with_vector(make_service):
    packet = triangle(make_service(triangle_md(label=VERIFIED), full_config()))["opportunity"]
    codes = {f["code"] for f in packet["aegis_verdict"]["findings"]}
    assert "RECOMPUTE_MISMATCH" not in codes


def test_repeated_observations_keep_one_versioned_opportunity(make_service):
    service = make_service(triangle_md())
    first, second = triangle(service), triangle(service)
    assert first["opportunity"]["opportunity_id"] == second["opportunity"]["opportunity_id"]
    assert second["opportunity"]["packet_version"] == 2
    assert len(service.journal.tail(50, ["OPPORTUNITY_OBSERVED"])) == 1


def test_config_change_bumps_the_version_and_is_journaled(make_service):
    service = make_service(triangle_md())
    assert service.store.version == 1
    service.update_config(DeskConfig.model_validate(full_config()))
    assert service.store.version == 2
    assert triangle(service)["opportunity"]["config_version"] == 2
    assert service.journal.tail(5, ["CONFIG_CHANGED"])[0]["config_version"] == 2


def test_market_data_errors_are_no_trade_and_journaled_once(make_service):
    md = triangle_md()
    md.offline.add(VENUE)
    service = make_service(md)
    for _ in range(3):
        response = triangle(service)
    assert (response["decision"], response["feasibility"]) == ("NO_TRADE", "UNVERIFIED")
    assert response["reason"].startswith("MARKET_DATA_UNAVAILABLE")
    assert len(service.journal.tail(50, ["ERROR"])) == 1


# --- spot across two venues ------------------------------------------------

CROSS_BOOKS = {
    "a": {"BTC/USDT": ([["49950", "1"]], [["50000", "1"]])},
    "b": {"BTC/USDT": ([["50600", "1"]], [["50650", "1"]])},
}


def cross_md(**kwargs):
    markets = {venue: {"BTC/USDT": spot("BTC/USDT", "0.00001")} for venue in CROSS_BOOKS}
    return FakeMarketData(markets, {venue: dict(books) for venue, books in CROSS_BOOKS.items()}, **kwargs)


def cross_config(**overrides):
    tier = {"taker_rate": "0.001", "fee_side": "get", "evidence_label": "VERIFIED", "source": "fixture"}
    config = full_config(
        venues=["a", "b"],
        allowed_assets=["BTC", "USDT"],
        balances_by_venue={"a": {"USDT": "10000"}, "b": {"BTC": "0.5"}},
        fee_tiers={"a": tier, "b": tier},
        venue_eligibility={v: {"eligible": True, "evidence_label": "VERIFIED", "source": "fixture"} for v in ("a", "b")},
        available_capital={"amount": "20000", "asset": "USDT"},
        trade_size_limit={"amount": "0.5", "asset": "BTC"},
        total_deployed_capital_limit={"amount": "15000", "asset": "USDT"},
        venue_concentration_limit="0.8",
        inventory_exposure_limit={"amount": "6000", "asset": "USDT"},
        verified_equivalent_assets=["BTC", "USDT"],
        cost_inputs={
            "adverse_movement_allowance_bps": "20",
            "model_uncertainty_allowance_bps": "10",
            "allowance_basis": "fixture",
            "rebalance_cost_per_trade": {"amount": "5", "asset": "USDT"},
            "rebalance_cost_source": "fixture withdrawal fees",
        },
        scan={"interval_seconds": 5, "cross_exchange_jobs": [{"symbol": "BTC/USDT", "venues": ["a", "b"], "size": "0.1"}]},
    )
    config.update(overrides)
    return config


def cross(service, size="0.1"):
    return asyncio.run(service.evaluate_cross("BTC/USDT", ["a", "b"], D(size))).response


def test_cross_exchange_candidate_matches_hand_calculation(make_service):
    response = cross(make_service(cross_md(label=VERIFIED), cross_config()))
    assert response["decision"] == "PAPER_CANDIDATE", response["summary"]
    packet = response["opportunity"]
    # Buy 0.1 BTC at 50000 (fee 0.0001 BTC); sell 0.1 BTC at 50600 (fee 5.06 USDT):
    # 5054.94 - 5000 - 0.0001 BTC valued at 50600 = 49.88; minus 5 USDT rebalancing = 44.88.
    assert packet["gross_capture"] == {"amount": "60", "asset": "USDT"}
    assert packet["cost_ledger"]["net_before_rebalancing"] == {"amount": "49.88", "asset": "USDT"}
    assert packet["conditional_net"] == {"amount": "44.88", "asset": "USDT"}
    # 30 bps of the 10060 USDT committed on both venues.
    assert packet["conservative_net"] == {"amount": "14.7", "asset": "USDT"}
    assert response["rejected_pairs"] == [
        {"buy": "b", "sell": "a", "displayed_spread": {"amount": "-700", "asset": "USDT"}}
    ]


def test_cross_exchange_needs_rebalancing_cost_and_asset_equivalence(make_service):
    config = cross_config(
        verified_equivalent_assets=[],
        cost_inputs={"adverse_movement_allowance_bps": "20", "model_uncertainty_allowance_bps": "10", "allowance_basis": "fixture"},
    )
    response = cross(make_service(cross_md(label=VERIFIED), config))
    assert response["decision"] == "RESEARCH_ONLY"
    assert {"REBALANCE_COST_UNKNOWN", "ASSET_EQUIVALENCE_UNVERIFIED"} <= set(response["summary"]["blockers"])
    assert response["opportunity"]["conditional_net"] is None


def test_cross_exchange_symbol_missing_on_one_venue(make_service):
    md = cross_md()
    md.markets["b"] = {}
    response = cross(make_service(md))
    assert response["reason"].startswith("SYMBOL_NOT_ON_TWO_VENUES")
