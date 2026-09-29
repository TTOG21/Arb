import os
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from arb.config import DeskConfig, intake_gaps
from arb.market_data import CcxtMarketData, MarketDataSource, StreamingMarketData
from arb.service import DeskService, ScreenerError

WEB_DIR = Path(__file__).resolve().parent / "arb" / "web"


def default_market_data() -> MarketDataSource:
    """ARB_MARKET_DATA=websocket (default) streams order books; =rest uses REST snapshots only."""
    mode = os.environ.get("ARB_MARKET_DATA", "websocket").strip().lower()
    if mode not in ("websocket", "rest"):
        raise ValueError("ARB_MARKET_DATA must be 'websocket' or 'rest'.")
    rest = CcxtMarketData()
    return rest if mode == "rest" else StreamingMarketData(rest)


def create_app(market_data: Optional[MarketDataSource] = None, data_dir: Optional[Path] = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        desk = DeskService(
            market_data or default_market_data(),
            data_dir or Path(os.environ.get("ARB_DATA_DIR", "data")),
        )
        app.state.desk = desk
        try:
            yield
        finally:
            await desk.close()

    app = FastAPI(title="Arbitrage Backend", lifespan=lifespan)

    # ---------------------------
    # 🔹 Enable CORS (frontend calls won't fail)
    # ---------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # change to ["http://localhost:3000"] in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def desk(request: Request) -> DeskService:
        return request.app.state.desk

    def nothing_to_scan(service: DeskService, strategy: str, needed: str) -> dict:
        return {
            "mode": service.config.mode.value,
            "strategy": strategy,
            "decision": "RESEARCH_ONLY",
            "execution_status": "NO_TRADE",
            "reason": f"Nothing to scan: pass {needed}, or add a matching scan job to the config.",
            "opportunity": None,
            "missing_inputs": intake_gaps(service.config),
        }

    # ---------------------------
    # 🔹 Health check
    # ---------------------------
    @app.get("/health")
    def health():
        return {"status": "ok"}

    # ---------------------------
    # 🔹 Dashboard (static page that calls this API)
    # ---------------------------
    app.mount("/ui", StaticFiles(directory=WEB_DIR), name="ui")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(WEB_DIR / "index.html")

    # ---------------------------
    # 🔹 Protocol: capability check and configuration
    # ---------------------------
    @app.get("/desk/capabilities")
    async def desk_capabilities(request: Request):
        return desk(request).capabilities()

    @app.get("/config")
    async def get_config(request: Request):
        service = desk(request)
        return {
            "config_version": service.store.version,
            "updated_at": service.store.updated_at,
            "config": service.config.model_dump(mode="json"),
        }

    @app.put("/config")
    async def put_config(config: DeskConfig, request: Request):
        return desk(request).update_config(config)

    # ---------------------------
    # 🔹 Screener endpoints
    # ---------------------------
    @app.post("/screener/start")
    async def screener_start(request: Request):
        try:
            status = await desk(request).screener.start()
        except ScreenerError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"message": "Screener started", **status}

    @app.post("/screener/stop")
    async def screener_stop(request: Request):
        return {"message": "Screener stopped", **await desk(request).screener.stop()}

    @app.get("/screener/status")
    async def screener_status(request: Request):
        return desk(request).screener.status()

    # ---------------------------
    # 🔹 Trade log and journal
    # ---------------------------
    @app.get("/get_trade_log")
    async def get_trade_log(request: Request, limit: int = Query(10, ge=0, le=500)):
        logs = desk(request).journal.tail(limit, ["PAPER_RESULT"])
        return {"limit": limit, "logs": logs, "note": "Only PAPER simulations appear here. No live trades are placed."}

    @app.get("/journal")
    async def get_journal(
        request: Request,
        limit: int = Query(50, ge=1, le=1000),
        kind: Optional[List[str]] = Query(None),
    ):
        return {"limit": limit, "entries": desk(request).journal.tail(limit, kind)}

    # ---------------------------
    # 🔹 Arbitrage research
    # ---------------------------
    @app.get("/find_triangular_arbitrage")
    async def find_triangular_arbitrage(
        request: Request,
        venue: Optional[str] = None,
        start_asset: Optional[str] = None,
        size: Optional[Decimal] = Query(None, gt=0),
    ):
        service = desk(request)
        jobs = service.config.scan.triangular_jobs
        job = jobs[0] if jobs else None
        venue = venue or (job.venue if job else None)
        start_asset = start_asset or (job.start_asset if job else None)
        size = size or (job.size if job else None)
        if venue is None or start_asset is None or size is None:
            return nothing_to_scan(service, "TRIANGULAR_SPOT", "venue, start_asset and size")
        return (await service.evaluate_triangular(venue, start_asset, size)).response

    @app.get("/find_cross_exchange_arbitrage")
    async def find_cross_exchange_arbitrage(
        request: Request,
        symbol: Optional[str] = None,
        venues: Optional[str] = Query(None, description="Comma-separated ccxt ids, e.g. kraken,bitstamp"),
        size: Optional[Decimal] = Query(None, gt=0, description="Base asset amount"),
    ):
        service = desk(request)
        jobs = service.config.scan.cross_exchange_jobs
        job = jobs[0] if jobs else None
        symbol = symbol or (job.symbol if job else None)
        venue_list = [v for v in venues.split(",") if v.strip()] if venues else (job.venues if job else None)
        size = size or (job.size if job else None)
        if symbol is None or not venue_list or size is None:
            return nothing_to_scan(service, "SPOT_ACROSS_EXCHANGES", "symbol, venues and size")
        return (await service.evaluate_cross(symbol, venue_list, size)).response

    @app.get("/opportunities")
    async def list_opportunities(request: Request):
        records = desk(request).registry.records()
        return {"opportunities": [record.brief() for record in records]}

    @app.get("/opportunities/{opportunity_id}")
    async def get_opportunity(opportunity_id: str, request: Request):
        record = desk(request).registry.get(opportunity_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown or closed opportunity.")
        return {"summary": record.summary, "packet": record.packet}

    # ---------------------------
    # 🔹 Paper portfolio (simulated balances, never real funds)
    # ---------------------------
    @app.get("/paper/portfolio")
    async def paper_portfolio(request: Request):
        return desk(request).portfolio.as_dict()

    @app.post("/paper/reset")
    async def paper_reset(request: Request):
        service = desk(request)
        if service.screener.running():
            raise HTTPException(status_code=409, detail="Stop the screener before resetting the paper portfolio.")
        try:
            return service.reset_paper()
        except ScreenerError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    return app


app = create_app()
