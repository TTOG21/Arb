"""HTTP API, including the routes the original stubs exposed."""
import pytest
from fastapi.testclient import TestClient

from arb.protocol import EvidenceLabel
from conftest import VENUE, full_config, triangle_md
from main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(triangle_md(), tmp_path / "data")) as test_client:
        yield test_client


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_status_is_stopped_until_the_screener_really_runs(client):
    body = client.get("/screener/status").json()
    assert (body["status"], body["active_pairs"], body["cycles_completed"]) == ("stopped", 0, 0)


def test_trade_log_bounds_and_empty_research_log(client):
    assert client.get("/get_trade_log", params={"limit": -5}).status_code == 422
    assert client.get("/get_trade_log", params={"limit": 100000}).status_code == 422
    body = client.get("/get_trade_log").json()
    assert (body["limit"], body["logs"]) == (10, [])


def test_live_execution_cannot_be_authorized(client):
    response = client.put("/config", json={"live_execution_authorized": True})
    assert response.status_code == 422
    assert "not implemented" in response.text


def test_config_round_trip_bumps_the_version_once(client):
    assert client.put("/config", json=full_config()).json()["config_version"] == 2
    assert client.get("/config").json()["config"]["venues"] == [VENUE]
    assert client.put("/config", json=full_config()).json() == {"config_version": 2, "changed_fields": []}


def test_capability_check_is_honest(client):
    body = client.get("/desk/capabilities").json()
    assert body["collaboration_mode"] == "DETERMINISTIC_PIPELINE"
    assert body["tools"]["live_execution"]["available"] is False
    assert body["tools"]["account_data"]["available"] is False
    assert "READY_FOR_REVIEW" in body["decisions"]["never_emitted"]
    assert {"venues", "trade_size_limit", "maximum_book_age_ms"} <= {gap["field"] for gap in body["missing_inputs"]}


def test_find_triangular_without_inputs_scans_nothing(client):
    body = client.get("/find_triangular_arbitrage").json()
    assert (body["decision"], body["opportunity"]) == ("RESEARCH_ONLY", None)
    assert client.get("/find_cross_exchange_arbitrage").json()["opportunity"] is None


def test_find_triangular_and_read_the_packet(client):
    params = {"venue": VENUE, "start_asset": "usdt", "size": "1000"}
    body = client.get("/find_triangular_arbitrage", params=params).json()
    assert body["decision"] == "RESEARCH_ONLY"
    opportunity_id = body["opportunity"]["opportunity_id"]
    packet = client.get(f"/opportunities/{opportunity_id}").json()["packet"]
    assert packet["route"]["label"] == "USDT>BTC>ETH>USDT"
    assert [o["opportunity_id"] for o in client.get("/opportunities").json()["opportunities"]] == [opportunity_id]
    assert client.get("/opportunities/unknown").status_code == 404
    assert client.get("/find_triangular_arbitrage", params={**params, "size": "0"}).status_code == 422


def test_screener_needs_jobs_and_reports_its_real_state(client):
    assert client.post("/screener/start").status_code == 409
    client.put("/config", json=full_config())
    assert client.post("/screener/start").json()["status"] == "running"
    assert client.get("/screener/status").json()["status"] == "running"
    assert client.post("/screener/start").status_code == 409
    assert client.post("/screener/stop").json()["status"] == "stopped"
    kinds = {entry["kind"] for entry in client.get("/journal").json()["entries"]}
    assert {"CONFIG_CHANGED", "SCREENER_STARTED", "SCREENER_STOPPED"} <= kinds


def test_paper_trades_reach_the_trade_log(tmp_path):
    with TestClient(create_app(triangle_md(label=EvidenceLabel.VERIFIED), tmp_path / "data")) as client:
        assert client.post("/paper/reset").status_code == 409  # no balances declared yet
        client.put("/config", json=full_config(mode="PAPER"))
        assert client.post("/paper/reset").json()["balances"] == {VENUE: {"USDT": "2000"}}
        desk = client.app.state.desk
        client.portal.call(desk.screener.run_cycle)
        [log] = client.get("/get_trade_log").json()["logs"]
        assert (log["evidence_label"], log["realized_net"]) == ("PAPER", {"amount": "36.88208", "asset": "USDT"})
