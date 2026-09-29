# Arbitrage Backend

A FastAPI research desk for crypto spot arbitrage. It implements the protocol of the
"Grok Crypto Arbitrage Masterprompts" (P0 to P5, the shared opportunity packet and the
initial settings template) as **deterministic code**, not as chat agents.

- Default mode is `RESEARCH`. `PAPER` simulates orders only. **There is no live order code.**
- `NO_TRADE` is a normal, successful answer. Nothing here promises profit.
- Unknown costs are never treated as zero. Unknown values stay `null`, with a reason.

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --reload            # API docs at http://127.0.0.1:8000/docs
```

If port 8000 is taken (on Windows this shows as `WinError 10013`), add `--port 8080` and open
`http://127.0.0.1:8080/docs`. The root URL redirects to the docs.

Config, journal and paper portfolio are stored in `./data` (override with `ARB_DATA_DIR`).
Market data comes from public exchange endpoints through ccxt; your network must reach them.

| `ARB_MARKET_DATA` | Order books |
|---|---|
| `websocket` (default) | Live streams through ccxt.pro, one subscription per symbol in use. REST serves markets and tickers, and replaces any stream that fails, is not offered, or has not delivered a first update within 10 s. |
| `rest` | REST snapshots only, fetched one leg after another. |

Every packet names each leg's source. A streamed book's age counts from its last update, so a
quiet market reads as older than it is. Integrity checks depend on the venue's ccxt.pro code:
`book_integrity` reports them per leg (for example, binance sequence checks are on, while kraken's
checksum check is off by ccxt default). In PAPER mode an order meets the streamed book after the
venue's measured REST round trip.

```bash
pip install -r requirements-dev.txt
pytest                               # fixture markets only, no network needed
```

## How the protocol maps to code

| Protocol | Code | Output |
|---|---|---|
| Shared rules (P0) | `arb/protocol.py`, `arb/config.py` | modes, evidence labels, config template |
| SCOUT (P2) | `roles.scout_setup`, `enumerate_triangles`, `rank_triangle_leads`, book integrity | `CANDIDATE` / `NO_CANDIDATE` |
| VECTOR (P3) | `roles._vector_triangle`, `roles._vector_cross`, `arb/engine.py` | `VALIDATED_FOR_PAPER` / `INCONCLUSIVE` / `REJECTED` |
| RELAY (P4) | `roles._relay_*`, stress scenarios, IOC order plan | `FEASIBLE_FOR_PAPER` / `BLOCKED` / `NEEDS_ENGINEERING` |
| AEGIS (P5) | `roles._aegis` (limits, freshness, fees, eligibility, recomputation) | `PASS_FOR_PAPER` / `CONDITIONAL_FOR_PAPER` / `VETO` |
| ATLAS (P1) | `roles._atlas`, journal, screener | `NO_TRADE` / `RESEARCH_ONLY` / `PAPER_CANDIDATE` |
| OPPORTUNITY_PACKET | `arb/packet.py` | every field present, `null_reasons` for the rest |

Every finding has a severity. Any `FAIL` gives `NO_TRADE`. Any `MISSING` or `CONDITION`
gives `RESEARCH_ONLY` and lists what to provide. Only a clean review gives
`PAPER_CANDIDATE`. `READY_FOR_REVIEW` is never emitted: it needs agreed acceptance
criteria evaluated on recorded forward paper results, which is not implemented.

The roles run in one process, so their agreement is not independent validation
(`collaboration_mode: DETERMINISTIC_PIPELINE`).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/desk/capabilities` | capability check and the settings still missing |
| GET / PUT | `/config` | versioned desk config (any change bumps `config_version`) |
| GET | `/find_triangular_arbitrage?venue=&start_asset=&size=` | one research pass on one venue |
| GET | `/find_cross_exchange_arbitrage?symbol=&venues=a,b&size=` | one pass across venues (`size` in base units) |
| GET | `/opportunities`, `/opportunities/{id}` | tracked opportunities and their full packets |
| POST | `/screener/start`, `/screener/stop` | background loop over the configured scan jobs |
| GET | `/screener/status` | the real state of that loop |
| GET | `/get_trade_log?limit=` | PAPER results only |
| GET | `/journal?limit=&kind=` | decisions, rejections, config changes, errors, halts |
| GET | `/paper/portfolio`, POST `/paper/reset` | simulated balances for PAPER mode |

## Minimal config for a triangular scan

`PUT /config` with the fields you know; leave the rest out (they stay `null`).

```json
{
  "residence_country": "DE",
  "venues": ["kraken"],
  "allowed_assets": ["USDT", "BTC", "ETH"],
  "balances_by_venue": {"kraken": {"USDT": "1000"}},
  "fee_tiers": {"kraken": {"taker_rate": "0.0026", "fee_side": "quote", "evidence_label": "VERIFIED", "source": "my account fee page, 2026-09-28"}},
  "venue_eligibility": {"kraken": {"eligible": true, "evidence_label": "VERIFIED", "source": "venue terms for my country"}},
  "venue_rules_verified": {"kraken": {"ioc_supported": true, "evidence_label": "VERIFIED", "source": "venue API docs, order types and minimums"}},
  "available_capital": {"amount": "1000", "asset": "USDT"},
  "trade_size_limit": {"amount": "200", "asset": "USDT"},
  "total_deployed_capital_limit": {"amount": "1000", "asset": "USDT"},
  "venue_concentration_limit": "1",
  "inventory_exposure_limit": {"amount": "200", "asset": "USDT"},
  "loss_per_incident_limit": {"amount": "5", "asset": "USDT"},
  "daily_loss_limit": {"amount": "20", "asset": "USDT"},
  "minimum_conservative_net_amount": {"amount": "0.5", "asset": "USDT"},
  "minimum_conservative_net_bps": "10",
  "maximum_book_age_ms": 1500,
  "maximum_snapshot_skew_ms": 1000,
  "cost_inputs": {"adverse_movement_allowance_bps": "10", "model_uncertainty_allowance_bps": "5", "allowance_basis": "why these numbers"},
  "validation_acceptance_criteria": {"hypothesis": "...", "minimum_paper_observations": 50},
  "scan": {"interval_seconds": 30, "triangular_jobs": [{"venue": "kraken", "start_asset": "USDT", "size": "200"}]}
}
```

The values above only show the format. They are not recommended limits or allocations, and
they are not facts about any venue: take the fee rate and the asset the fee is charged in
from your own account.

## Known limits

- Book age and skew are measured as upper bounds and checked against your limits. With REST
  snapshots the legs are fetched one after another; streams keep them close in time.
- Without your own fee tier, fees are ccxt defaults (`ESTIMATED`), which keeps results at `RESEARCH_ONLY`.
  The same holds for IOC support and order minimums until `venue_rules_verified` is declared, and for
  markets whose trading status the venue does not report.
- A stress scenario whose recovery does not fit the visible depth has an unknown loss, which blocks paper candidates.
- Limits are compared only in the same asset. USD, USDT, USDC and EUR are never assumed interchangeable.
- Cross-exchange results need a rebalancing cost and declared asset equivalence; transfers are not simulated.
- Paper fills are model outputs, not live execution evidence.
- CORS still allows every origin and the API has no authentication. Restrict both before exposing it.
