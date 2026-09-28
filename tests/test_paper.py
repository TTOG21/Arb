"""PAPER mode: simulated fills against fresh books, the paper portfolio and halts."""
import asyncio
from decimal import Decimal as D

from arb.protocol import EvidenceLabel
from conftest import TRIANGLE_BOOKS, VENUE, full_config, triangle_md


def paper_service(make_service, books, **config):
    service = make_service(triangle_md(books=books, label=EvidenceLabel.VERIFIED), full_config(mode="PAPER", **config))
    service.reset_paper()
    return service


def cycle(service):
    asyncio.run(service.screener.run_cycle())


def test_unchanged_books_fill_exactly_as_planned(make_service):
    service = paper_service(make_service, dict(TRIANGLE_BOOKS))
    cycle(service)
    [result] = service.journal.tail(5, ["PAPER_RESULT"])
    assert result["evidence_label"] == "PAPER"
    assert result["realized_net"] == result["planned_net"] == {"amount": "36.88208", "asset": "USDT"}
    balances = service.portfolio.as_dict()["balances"][VENUE]
    assert (balances["USDT"], balances["ETH"]) == ("2036.88208", "0.0000004")
    assert result["unrecovered"] == {"ETH": "0.0000004"}  # dust below the amount step stays unrecovered


def test_partial_fill_is_recovered_without_reusing_consumed_depth(make_service):
    fresh_eth = [TRIANGLE_BOOKS["ETH/USDT"], ([["2600", "0.2"], ["2590", "100"]], [["2601", "100"]])]
    service = paper_service(make_service, dict(TRIANGLE_BOOKS, **{"ETH/USDT": fresh_eth}))
    cycle(service)
    [result] = service.journal.tail(5, ["PAPER_RESULT"])
    # IOC at 2600 fills 0.2 ETH (519.48 after fee). Recovery sells 0.1992 ETH at 2590, the level that is
    # left after our own fill took the 0.2 at 2600 (515.412072 after fee).
    assert result["realized_net"] == {"amount": "34.892072", "asset": "USDT"}
    assert [f["role"] for f in result["fills"]] == ["planned_leg"] * 3 + ["recovery"]
    assert D(result["fills"][2]["fill_ratio"]) == D("0.2") / D("0.3992")


def test_incident_loss_halts_paper_mode(make_service):
    books = dict(TRIANGLE_BOOKS)
    books["BTC/USDT"] = [TRIANGLE_BOOKS["BTC/USDT"], ([["49000", "1"]], [["50000", "1"]])]
    books["ETH/BTC"] = [TRIANGLE_BOOKS["ETH/BTC"], ([["0.059", "100"]], [["0.06", "100"]])]
    service = paper_service(make_service, books, loss_per_incident_limit={"amount": "10", "asset": "USDT"})
    cycle(service)
    [result] = service.journal.tail(5, ["PAPER_RESULT"])
    # Leg 2 finds no ask at or below 0.05, so the 0.01998 BTC goes back to USDT at the 49000 bid.
    assert D(result["realized_net"]["amount"]) == D("0.01998") * D(49000) * D("0.999") - D(1000)
    status = service.screener.status()
    assert status["paper_halted"] and status["paper_halt_reason"].startswith("LIMIT_BREACHED")
    assert service.journal.tail(5, ["HALT"])[0]["trigger"] == "LIMIT_BREACHED"


def test_lost_connectivity_keeps_partial_results_and_halts(make_service):
    service = paper_service(make_service, dict(TRIANGLE_BOOKS))
    md = service.md
    original = md.fetch_order_book
    calls = {"n": 0}

    async def flaky(venue, symbol, depth):
        calls["n"] += 1
        if calls["n"] == 5:  # the 3 decision fetches succeed, then leg 1 fills, then leg 2 fails
            from arb.market_data import MarketDataError

            raise MarketDataError(venue, "NetworkError: fixture drop")
        return await original(venue, symbol, depth)

    md.fetch_order_book = flaky
    cycle(service)
    [result] = service.journal.tail(5, ["PAPER_RESULT"])
    assert [f["role"] for f in result["fills"]] == ["planned_leg", "recovery"]
    assert any(p.startswith("LOST_CONNECTIVITY") for p in result["problems"])
    assert service.screener.status()["paper_halt_reason"].startswith("LOST_CONNECTIVITY")


def test_research_mode_never_simulates(make_service):
    service = make_service(triangle_md(label=EvidenceLabel.VERIFIED), full_config())
    cycle(service)
    assert service.journal.tail(5, ["PAPER_RESULT"]) == []
    assert service.screener.last_results[0]["decision"] == "PAPER_CANDIDATE"
