"""Desk service: fetches market data, runs the roles, keeps the journal, runs the screener."""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Awaitable, Optional, Sequence

from arb.config import ConfigStore, DeskConfig, intake_gaps
from arb.engine import ONE
from arb.journal import Journal, OpportunityRecord, OpportunityRegistry
from arb.market_data import MarketDataError, MarketDataSource, fee_model, market_rules
from arb.orderbook import ZERO
from arb.paper import PaperPortfolio, simulate
from arb.protocol import (
    COLLABORATION_MODE,
    COLLABORATION_NOTE,
    PAPER_NOTE,
    EvidenceLabel,
    FinalDecision,
    Mode,
    ScoutResult,
    StrategyType,
    dstr,
    iso,
    money,
    utc_now,
)
from arb.roles import (
    CrossRoute,
    DeskInputs,
    Evaluation,
    enumerate_triangles,
    evaluate_cross,
    evaluate_triangle,
    is_spot,
    rank_triangle_leads,
    scout_setup,
)

LIVE_EXECUTION_NOTE = "This backend has no live order code. Every result is research or a paper simulation."
MAX_LEADS_SHOWN = 10


class ScreenerError(Exception):
    pass


@dataclass
class Outcome:
    response: dict
    best: Optional[Evaluation] = None
    best_record: Optional[OpportunityRecord] = None
    error: Optional[str] = None
    config_version: Optional[int] = None  # the config the whole evaluation used


class DeskService:
    def __init__(self, market_data: MarketDataSource, data_dir: Path):
        self.md = market_data
        self.data_dir = data_dir
        self.store = ConfigStore(data_dir / "config.json")
        self.journal = Journal(data_dir / "journal.jsonl")
        self.registry = OpportunityRegistry()
        self.portfolio = PaperPortfolio(data_dir / "paper_portfolio.json")
        self.screener = Screener(self)
        self._last_quiet_event: dict[str, str] = {}  # scope -> last NO_CANDIDATE or ERROR, to avoid repeats

    @property
    def config(self) -> DeskConfig:
        return self.store.config

    async def close(self) -> None:
        await self.screener.stop()
        await self.md.close()

    # -- configuration and paper state -------------------------------------

    def update_config(self, new: DeskConfig) -> dict:
        changed = self.store.replace(new)
        if changed:
            self.journal.append(
                "CONFIG_CHANGED",
                config_version=self.store.version,
                changed_fields=changed,
                note="Earlier approvals are invalidated.",
            )
        return {"config_version": self.store.version, "changed_fields": changed}

    def reset_paper(self) -> dict:
        if self.config.balances_by_venue is None:
            raise ScreenerError("Set balances_by_venue in the config to seed the paper portfolio.")
        self.portfolio.reset(self.config.balances_by_venue, self.store.version)
        self.journal.append("PAPER_PORTFOLIO_SEEDED", config_version=self.store.version)
        return self.portfolio.as_dict()

    def paper_net_today(self) -> dict[str, Decimal]:
        """Net realized paper result per asset for the current UTC day."""
        today = datetime.now(timezone.utc).date().isoformat()
        totals: dict[str, Decimal] = {}
        for entry in self.journal.entries(["PAPER_RESULT"]):
            net = entry.get("realized_net") or {}
            if str(entry.get("ts", "")).startswith(today) and net.get("amount") is not None:
                totals[net["asset"]] = totals.get(net["asset"], ZERO) + Decimal(net["amount"])
        return totals

    def paper_loss_today(self) -> dict[str, Decimal]:
        return {asset: -net for asset, net in self.paper_net_today().items() if net < 0}

    def _inputs(self, config: DeskConfig, version: int, venues: Sequence[str]) -> DeskInputs:
        if config.mode is Mode.PAPER and self.portfolio.balances is not None:
            balances, label, source = self.portfolio.balances, EvidenceLabel.HYPOTHETICAL, "paper portfolio (simulated)"
        elif config.balances_by_venue is not None:
            balances, label, source = config.balances_by_venue, EvidenceLabel.ESTIMATED, "user-declared"
        else:
            balances, label, source = None, EvidenceLabel.UNKNOWN, "unknown"
        ioc: dict[str, Optional[bool]] = {}
        for venue in venues:
            try:
                ioc[venue] = self.md.venue_meta(venue).ioc_supported
            except MarketDataError:
                ioc[venue] = None
        return DeskInputs(
            config=config,
            config_version=version,
            data_label=self.md.evidence_label,
            balances=balances,
            balances_label=label,
            balances_source=source,
            paper_loss_today=self.paper_loss_today(),
            ioc_support=ioc,
            now_monotonic=self.md.monotonic(),
            evaluated_at=utc_now(),
        )

    # -- evaluations --------------------------------------------------------
    # Each evaluation reads the config once. A change that lands while books are
    # being fetched never mixes into it, and the result carries the version used.

    def _base(self, strategy: StrategyType, config: DeskConfig, version: int) -> dict:
        return {
            "mode": config.mode.value,
            "collaboration_mode": COLLABORATION_MODE,
            "strategy": strategy.value,
            "config_version": version,
            "execution_status": "NO_TRADE",
            "live_execution": LIVE_EXECUTION_NOTE,
            "market_data": {"source": self.md.source_name, "evidence_label": self.md.evidence_label.value},
        }

    async def evaluate_triangular(self, venue: str, start_asset: str, size: Decimal) -> Outcome:
        config, version = self.config, self.store.version
        venue, start = venue.strip().lower(), start_asset.strip().upper()
        scope = f"{StrategyType.TRIANGULAR_SPOT.value}|{venue}|{start}"
        base = self._base(StrategyType.TRIANGULAR_SPOT, config, version)
        base.update(venue=venue, start_asset=start, requested_size=money(size, start))
        setup = scout_setup(config, StrategyType.TRIANGULAR_SPOT, [venue], [start])
        depth = config.scan.order_book_depth
        try:
            markets = await self.md.load_markets(venue)
            triangles = enumerate_triangles(venue, markets, start, config.allowed_assets)
            base["routes_checked"] = len(triangles)
            if not triangles:
                return self._no_candidate(
                    base, scope, version, "NO_TRIANGULAR_ROUTE", f"No {start} > A > B > {start} cycle among active spot markets on {venue}."
                )
            meta = self.md.venue_meta(venue)
            if meta.has_fetch_tickers is False:
                return self._no_candidate(
                    base, scope, version, "BULK_TICKERS_UNAVAILABLE", f"{venue} offers no bulk tickers, so routes cannot be prefiltered."
                )
            leads = rank_triangle_leads(triangles, await self.md.fetch_tickers(venue))
            base["leads"] = [lead.as_dict() for lead in leads[:MAX_LEADS_SHOWN]]
            selected = [lead for lead in leads if lead.top_of_book_multiplier > ONE][: config.scan.max_candidates]
            if not selected:
                return self._no_candidate(
                    base,
                    scope,
                    version,
                    "NO_POSITIVE_DISPLAYED_EDGE",
                    f"None of the {len(leads)} priced routes shows a positive top-of-book gross edge, even before fees.",
                )
            evaluations = []
            for lead in selected:
                triangle = lead.triangle
                books = [await self.md.fetch_order_book(venue, leg.symbol, depth) for leg in triangle.legs]
                rules = [market_rules(venue, markets[leg.symbol], meta) for leg in triangle.legs]
                fees = [fee_model(venue, markets[leg.symbol], meta, config, self.md.evidence_label) for leg in triangle.legs]
                inputs = self._inputs(config, version, [venue])
                evaluations.append(evaluate_triangle(inputs, triangle, rules, fees, books, size, setup))
        except MarketDataError as exc:
            return self._data_error(base, scope, version, exc)
        return self._conclude(base, scope, version, evaluations)

    async def evaluate_cross(self, symbol: str, venues: Sequence[str], size: Decimal) -> Outcome:
        config, version = self.config, self.store.version
        symbol = symbol.strip().upper()
        venues = list(dict.fromkeys(v.strip().lower() for v in venues))
        scope = f"{StrategyType.SPOT_ACROSS_EXCHANGES.value}|{symbol}|{','.join(sorted(venues))}"
        base = self._base(StrategyType.SPOT_ACROSS_EXCHANGES, config, version)
        base.update(symbol=symbol, venues=venues)
        if symbol.count("/") != 1:
            return self._no_candidate(base, scope, version, "BAD_SYMBOL", "Use a spot symbol like BTC/USDT.")
        base_asset, quote_asset = symbol.split("/")
        base["requested_size"] = money(size, base_asset)
        if len(venues) < 2:
            return self._no_candidate(base, scope, version, "NEEDS_TWO_VENUES", "Cross-exchange research needs at least two venues.")
        setup = scout_setup(config, StrategyType.SPOT_ACROSS_EXCHANGES, venues, [base_asset, quote_asset])
        depth = config.scan.order_book_depth
        books, rules, fees, missing = {}, {}, {}, []
        try:
            for venue in venues:
                market = (await self.md.load_markets(venue)).get(symbol)
                if market is None or not is_spot(market) or market.get("active") is False:
                    missing.append(venue)
                    continue
                meta = self.md.venue_meta(venue)
                books[venue] = await self.md.fetch_order_book(venue, symbol, depth)
                rules[venue] = market_rules(venue, market, meta)
                fees[venue] = fee_model(venue, market, meta, config, self.md.evidence_label)
        except MarketDataError as exc:
            return self._data_error(base, scope, version, exc)
        base["symbol_unavailable_on"] = missing
        if len(books) < 2:
            return self._no_candidate(
                base, scope, version, "SYMBOL_NOT_ON_TWO_VENUES", f"{symbol} is an active spot market on fewer than two venues."
            )
        pairs, rejected = [], []
        for buy in books:
            for sell in books:
                if buy == sell:
                    continue
                asks, bids = books[buy].asks, books[sell].bids
                if not asks or not bids:
                    rejected.append({"buy": buy, "sell": sell, "reason": "empty book side"})
                    continue
                spread = bids[0][0] - asks[0][0]
                if spread > 0:
                    pairs.append((buy, sell))
                else:
                    rejected.append({"buy": buy, "sell": sell, "displayed_spread": money(spread, quote_asset)})
        base["rejected_pairs"] = rejected
        if not pairs:
            return self._no_candidate(
                base, scope, version, "NO_POSITIVE_DISPLAYED_SPREAD", "No venue's best bid is above another venue's best ask."
            )
        inputs = self._inputs(config, version, list(books))
        evaluations = [
            evaluate_cross(
                inputs,
                CrossRoute(symbol, base_asset, quote_asset, buy, sell),
                [rules[buy], rules[sell]],
                [fees[buy], fees[sell]],
                [books[buy], books[sell]],
                size,
                setup,
            )
            for buy, sell in pairs
        ]
        return self._conclude(base, scope, version, evaluations)

    def _close_unseen(self, scope: str, seen: set[str]) -> None:
        for record in self.registry.close_unseen(scope, seen):
            self.journal.append(
                "OPPORTUNITY_CLOSED",
                opportunity_id=record.opportunity_id,
                route=record.summary.get("route"),
                last_decision=record.last_decision,
                observations=record.observations,
                first_seen=record.first_seen,
                last_seen=record.last_seen,
            )

    def _conclude(self, base: dict, scope: str, version: int, evaluations: list[Evaluation]) -> Outcome:
        observed = []
        for evaluation in evaluations:
            record, changed = self.registry.observe(evaluation, version, scope)
            observed.append((evaluation, record))
            if changed:
                summary = evaluation.summary
                self.journal.append(
                    "OPPORTUNITY_OBSERVED",
                    opportunity_id=record.opportunity_id,
                    route=evaluation.route_label,
                    strategy=evaluation.strategy.value,
                    decision=evaluation.decision.value,
                    reason=summary["reason"],
                    conditional_net=summary["conditional_net"],
                    conservative_net=summary["conservative_net"],
                    blockers=summary["blockers"],
                    config_version=version,
                )
        self._close_unseen(scope, {e.key for e in evaluations})
        self._last_quiet_event.pop(scope, None)
        observed.sort(key=lambda pair: pair[0].rank, reverse=True)
        best, record = observed[0]
        response = {
            **base,
            "decision": best.decision.value,
            "reason": best.summary["reason"],
            "feasibility": "VERIFIED" if best.decision is FinalDecision.PAPER_CANDIDATE else "UNVERIFIED",
            "scout_result": ScoutResult.CANDIDATE.value,
            "summary": record.summary,
            "opportunity": record.packet,
            "candidates": [r.summary for _, r in observed],
        }
        return Outcome(response, best, record, config_version=version)

    def _quiet_event(self, scope: str, kind: str, marker: str, version: int, **fields) -> None:
        """Journal NO_CANDIDATE and ERROR events once per change, not every cycle."""
        if self._last_quiet_event.get(scope) != marker:
            self._last_quiet_event[scope] = marker
            self.journal.append(kind, scope=scope, config_version=version, **fields)

    def _empty(self, base: dict, reason: str) -> dict:
        return {
            **base,
            "decision": FinalDecision.NO_TRADE.value,
            "reason": reason,
            "feasibility": "UNVERIFIED",
            "scout_result": ScoutResult.NO_CANDIDATE.value,
            "summary": None,
            "opportunity": None,
            "candidates": [],
        }

    def _no_candidate(self, base: dict, scope: str, version: int, code: str, detail: str) -> Outcome:
        self._close_unseen(scope, set())
        self._quiet_event(scope, "NO_CANDIDATE", code, version, code=code, detail=detail)
        return Outcome(self._empty(base, f"{code}: {detail}"), config_version=version)

    def _data_error(self, base: dict, scope: str, version: int, exc: MarketDataError) -> Outcome:
        self._quiet_event(scope, "ERROR", str(exc), version, error=str(exc))
        return Outcome(self._empty(base, f"MARKET_DATA_UNAVAILABLE: {exc}"), error=str(exc), config_version=version)

    def internal_error(self, scope: str, exc: Exception) -> Outcome:
        """An unexpected error in one job. It is journaled and never stops the other jobs."""
        error = f"{type(exc).__name__}: {exc}"
        self._quiet_event(scope, "ERROR", error, self.store.version, error=error, note="Unexpected error; other jobs continue.")
        response = {"decision": FinalDecision.NO_TRADE.value, "reason": f"INTERNAL_ERROR: {error}", "execution_status": "NO_TRADE"}
        return Outcome(response, error=error)

    # -- capability check -----------------------------------------------------

    def capabilities(self) -> dict:
        config = self.config
        return {
            "collaboration_mode": COLLABORATION_MODE,
            "collaboration_note": COLLABORATION_NOTE,
            "mode": config.mode.value,
            "config_version": self.store.version,
            "tools": {
                "exchange_market_data": {
                    "available": True,
                    "source": self.md.source_name,
                    "evidence_label": self.md.evidence_label.value,
                    "detail": "Public REST order books, bulk tickers and market metadata. These are snapshots, not "
                    "streams: legs are fetched one after another, so skew is measured and reported.",
                    "reachability": self.md.status(),
                },
                "account_data": {
                    "available": False,
                    "detail": "No exchange account connection. Balances and fee tiers are user-declared in the config.",
                },
                "calculation": {"available": True, "detail": "Decimal arithmetic on every leg; no floats in the accounting."},
                "persistent_storage": {
                    "available": True,
                    "detail": f"Config, journal and paper portfolio are stored under {self.data_dir}.",
                },
                "simulation": {
                    "available": True,
                    "detail": "PAPER mode fills IOC orders against fresh books fetched after each decision. " + PAPER_NOTE,
                },
                "web_research": {"available": False, "detail": "Not part of this backend."},
                "live_execution": {"available": False, "detail": LIVE_EXECUTION_NOTE},
            },
            "scope": {
                "strategies": [s.value for s in StrategyType],
                "disabled_tracks": ["derivatives", "funding", "DEX routes", "bridges", "flash loans", "leverage"],
            },
            "decisions": {
                "emitted": [FinalDecision.NO_TRADE.value, FinalDecision.RESEARCH_ONLY.value, FinalDecision.PAPER_CANDIDATE.value],
                "never_emitted": {
                    FinalDecision.READY_FOR_REVIEW.value: "Needs agreed acceptance criteria evaluated on recorded forward "
                    "paper results, which this version does not implement."
                },
            },
            "missing_inputs": intake_gaps(config),
            "screener": self.screener.status(),
        }


class Screener:
    """Background scan loop. Its status always reflects the real task state."""

    def __init__(self, service: DeskService):
        self.service = service
        self._task: Optional[asyncio.Task] = None
        self.started_at: Optional[str] = None
        self.stopped_at: Optional[str] = None
        self.cycles = 0
        self.last_cycle_started_at: Optional[str] = None
        self.last_cycle_finished_at: Optional[str] = None
        self.last_results: list[dict] = []
        self.last_error: Optional[str] = None
        self.paper_halt_reason: Optional[str] = None

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        crashed = None
        if self._task is not None and self._task.done() and not self._task.cancelled():
            exc = self._task.exception()
            crashed = None if exc is None else f"{type(exc).__name__}: {exc}"
        config = self.service.config
        return {
            "status": "running" if self.running() else "stopped",
            "active_pairs": len(self.service.registry.records()),
            "mode": config.mode.value,
            "config_version": self.service.store.version,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "cycles_completed": self.cycles,
            "last_cycle_started_at": self.last_cycle_started_at,
            "last_cycle_finished_at": self.last_cycle_finished_at,
            "interval_seconds": config.scan.interval_seconds,
            "jobs": {"triangular": len(config.scan.triangular_jobs), "cross_exchange": len(config.scan.cross_exchange_jobs)},
            "last_results": self.last_results,
            "last_error": self.last_error,
            "crashed": crashed,
            "paper_halted": self.paper_halt_reason is not None,
            "paper_halt_reason": self.paper_halt_reason,
            "note": "Reflects the actual background task. Nothing is monitored while the status is stopped.",
        }

    async def start(self) -> dict:
        if self.running():
            raise ScreenerError("The screener is already running.")
        service = self.service
        config = service.config
        if not config.scan.triangular_jobs and not config.scan.cross_exchange_jobs:
            raise ScreenerError("No scan jobs configured. Add scan.triangular_jobs or scan.cross_exchange_jobs to the config.")
        if config.mode is Mode.PAPER:
            if service.portfolio.balances is None:
                service.reset_paper()
            negative = [
                f"{asset} on {venue}"
                for venue, held in service.portfolio.balances.items()
                for asset, amount in held.items()
                if amount < 0
            ]
            if negative:
                raise ScreenerError(
                    f"The paper portfolio has a negative balance ({', '.join(negative)}). Reset it with POST /paper/reset first."
                )
        self.paper_halt_reason = None
        self.last_error = None
        self.started_at, self.stopped_at = iso(utc_now()), None
        self._task = asyncio.create_task(self._run())
        service.journal.append("SCREENER_STARTED", mode=config.mode.value, config_version=service.store.version)
        return self.status()

    async def stop(self) -> dict:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self.stopped_at = iso(utc_now())
            self.service.journal.append("SCREENER_STOPPED", cycles_completed=self.cycles)
        return self.status()

    async def _run(self) -> None:
        while True:
            began = time.monotonic()
            await self.run_cycle()
            interval = self.service.config.scan.interval_seconds
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - began)))

    async def _guarded(self, scope: str, evaluation: Awaitable[Outcome]) -> Outcome:
        try:
            return await evaluation
        except Exception as exc:  # one job's bug or bad data must not stop the others
            return self.service.internal_error(scope, exc)

    async def run_cycle(self) -> None:
        service = self.service
        config = service.config
        self.last_cycle_started_at = iso(utc_now())
        jobs = [
            ("triangular", f"{job.venue}:{job.start_asset}", lambda job=job: service.evaluate_triangular(job.venue, job.start_asset, job.size))
            for job in config.scan.triangular_jobs
        ] + [
            ("cross_exchange", f"{job.symbol}:{','.join(job.venues)}", lambda job=job: service.evaluate_cross(job.symbol, job.venues, job.size))
            for job in config.scan.cross_exchange_jobs
        ]
        results, errors = [], []
        for kind, target, evaluate in jobs:
            outcome = await self._guarded(f"{kind}:{target}", evaluate())
            results.append(self._result(kind, target, outcome))
            errors += [outcome.error] if outcome.error else []
            if config.mode is Mode.PAPER:
                try:
                    await self._paper(outcome)
                except Exception as exc:  # never keep simulating after an unexplained failure
                    self._halt("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
        self.last_results = results
        self.last_error = errors[-1] if errors else None
        self.cycles += 1
        self.last_cycle_finished_at = iso(utc_now())

    @staticmethod
    def _result(kind: str, target: str, outcome: Outcome) -> dict:
        return {
            "job": kind,
            "target": target,
            "decision": outcome.response["decision"],
            "reason": outcome.response["reason"],
            "opportunity_id": outcome.best_record.opportunity_id if outcome.best_record else None,
        }

    def _halt(self, trigger: str, detail: str) -> None:
        if self.paper_halt_reason is None:
            self.paper_halt_reason = f"{trigger}: {detail}"
            self.service.journal.append("HALT", scope="paper", trigger=trigger, detail=detail)

    async def _paper(self, outcome: Outcome) -> None:
        """Simulate the best candidate when it is a PAPER_CANDIDATE. Never real orders."""
        service = self.service
        best = outcome.best
        if best is None or best.decision is not FinalDecision.PAPER_CANDIDATE or best.paper_plan is None:
            return
        if self.paper_halt_reason is not None or service.portfolio.balances is None:
            return
        plan = best.paper_plan
        if plan.config_version != service.store.version:
            service.journal.append(
                "PAPER_SKIPPED",
                opportunity_id=outcome.best_record.opportunity_id,
                reason=f"Config changed during the evaluation (version {plan.config_version} to "
                f"{service.store.version}); the plan is stale.",
            )
            return
        config = service.config
        result = await simulate(service.md, plan, config.scan.order_book_depth, service.portfolio.balances)
        problems = service.portfolio.apply(result.executed())
        entry = result.as_entry(outcome.best_record.opportunity_id, best.route_label)
        service.journal.append("PAPER_RESULT", config_version=plan.config_version, **entry)
        if problems:
            self._halt("BALANCE_MISMATCH", " ".join(problems))
        for trigger, prefix in (("LOST_CONNECTIVITY", "LOST_CONNECTIVITY"), ("STALE_OR_INVALID_BOOK", "BOOK_INVALID")):
            if any(problem.startswith(prefix) for problem in result.problems):
                self._halt(trigger, " ".join(result.problems))
        asset = plan.accounting_asset
        loss = -result.realized_net
        incident = config.loss_per_incident_limit
        if incident is not None and incident.asset == asset and loss > incident.amount:
            self._halt("LIMIT_BREACHED", f"Paper loss {dstr(loss)} {asset} exceeds loss_per_incident_limit {dstr(incident.amount)}.")
        daily = config.daily_loss_limit
        if daily is not None:
            losses = service.paper_loss_today()
            others = sorted(a for a in losses if a != daily.asset)
            if others:
                self._halt(
                    "LIMIT_UNCHECKABLE",
                    f"Paper losses today in {', '.join(others)} cannot be compared with daily_loss_limit in {daily.asset}.",
                )
            elif losses.get(daily.asset, ZERO) >= daily.amount:
                lost = losses[daily.asset]
                self._halt("LIMIT_BREACHED", f"Net paper loss today {dstr(lost)} {daily.asset} reached daily_loss_limit.")
