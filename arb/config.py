"""Desk configuration: the initial settings template plus backend extensions.

Unknown values stay null. Amounts and limits are chosen by the user; nothing here
is a suggested allocation. Any change bumps config_version, which invalidates
earlier approvals.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arb.protocol import COLLABORATION_MODE, EvidenceLabel, Mode, StrategyType, iso, utc_now


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Money(_Strict):
    amount: Decimal = Field(ge=0)
    asset: str = Field(min_length=1)

    @field_validator("asset")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class FeeTier(_Strict):
    """Taker fee of the user's account on one venue, applied to every leg there."""

    taker_rate: Decimal = Field(ge=0, lt=Decimal("0.1"))
    # get/give: fee taken from the received/spent asset; base/quote: always that asset.
    fee_side: Literal["get", "give", "base", "quote"]
    evidence_label: EvidenceLabel
    source: str = Field(min_length=1)


class VenueEligibility(_Strict):
    eligible: bool
    evidence_label: EvidenceLabel
    source: str = Field(min_length=1)


class CostInputs(_Strict):
    adverse_movement_allowance_bps: Optional[Decimal] = Field(default=None, ge=0)
    model_uncertainty_allowance_bps: Optional[Decimal] = Field(default=None, ge=0)
    allowance_basis: Optional[str] = None
    # Rebalancing cost allocated to each cross-exchange trade, in its quote asset.
    rebalance_cost_per_trade: Optional[Money] = None
    rebalance_cost_source: Optional[str] = None


class TriangularJob(_Strict):
    venue: str
    start_asset: str
    size: Decimal = Field(gt=0)  # in start_asset units

    @field_validator("venue")
    @classmethod
    def _venue(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("start_asset")
    @classmethod
    def _asset(cls, value: str) -> str:
        return value.strip().upper()


class CrossExchangeJob(_Strict):
    symbol: str
    venues: list[str] = Field(min_length=2)
    size: Decimal = Field(gt=0)  # in base asset units

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("venues")
    @classmethod
    def _venues(cls, value: list[str]) -> list[str]:
        return [v.strip().lower() for v in value]


class ScanSettings(_Strict):
    interval_seconds: int = Field(default=60, ge=5)
    order_book_depth: int = Field(default=20, ge=5, le=500)
    max_candidates: int = Field(default=5, ge=1, le=5)
    triangular_jobs: list[TriangularJob] = []
    cross_exchange_jobs: list[CrossExchangeJob] = []


_NOT_IMPLEMENTED = {
    "live_execution_authorized": "Live execution is not implemented and cannot be authorized through this API.",
    "leverage_allowed": "Leverage is a separate research track that this backend does not implement.",
    "derivatives_allowed": "Derivatives are a separate research track that this backend does not implement.",
    "dex_allowed": "DEX routes are a separate research track that this backend does not implement.",
}


class DeskConfig(_Strict):
    # Fields of the initial settings template.
    mode: Mode = Mode.RESEARCH
    collaboration_mode: str = COLLABORATION_MODE
    language: str = "Greek"
    display_timezone: str = "Europe/Berlin"
    residence_country: Optional[str] = None
    reporting_currency: str = "EUR"
    available_capital: Optional[Money] = None
    venues: list[str] = []
    balances_by_venue: Optional[dict[str, dict[str, Decimal]]] = None
    fee_tiers: Optional[dict[str, FeeTier]] = None
    allowed_assets: list[str] = []
    allowed_strategies: list[StrategyType] = [StrategyType.SPOT_ACROSS_EXCHANGES, StrategyType.TRIANGULAR_SPOT]
    leverage_allowed: Literal[False] = False
    derivatives_allowed: Literal[False] = False
    dex_allowed: Literal[False] = False
    live_execution_authorized: Literal[False] = False
    trade_size_limit: Optional[Money] = None
    total_deployed_capital_limit: Optional[Money] = None
    venue_concentration_limit: Optional[Decimal] = Field(default=None, gt=0, le=1)
    inventory_exposure_limit: Optional[Money] = None
    loss_per_incident_limit: Optional[Money] = None
    daily_loss_limit: Optional[Money] = None
    minimum_conservative_net_amount: Optional[Money] = None
    minimum_conservative_net_bps: Optional[Decimal] = Field(default=None, ge=0)
    maximum_book_age_ms: Optional[int] = Field(default=None, gt=0)
    maximum_snapshot_skew_ms: Optional[int] = Field(default=None, gt=0)
    available_tools: list[str] = []
    validation_acceptance_criteria: Optional[Any] = None
    # Backend extensions, not part of the original template.
    venue_eligibility: Optional[dict[str, VenueEligibility]] = None
    verified_equivalent_assets: list[str] = []
    operational_reserves: Optional[Money] = None
    cost_inputs: CostInputs = CostInputs()
    scan: ScanSettings = ScanSettings()

    @field_validator(*_NOT_IMPLEMENTED, mode="before")
    @classmethod
    def _refuse_unimplemented(cls, value: Any, info) -> Any:
        if value is True:
            raise ValueError(_NOT_IMPLEMENTED[info.field_name])
        return value

    @field_validator("collaboration_mode")
    @classmethod
    def _collaboration(cls, value: str) -> str:
        if value not in (COLLABORATION_MODE, "TO_BE_VERIFIED"):
            raise ValueError(f"This backend runs the roles as {COLLABORATION_MODE}; other modes do not exist here.")
        return COLLABORATION_MODE

    @field_validator("venues")
    @classmethod
    def _lower_list(cls, value: list[str]) -> list[str]:
        return [v.strip().lower() for v in value]

    @field_validator("allowed_assets", "verified_equivalent_assets")
    @classmethod
    def _upper_list(cls, value: list[str]) -> list[str]:
        return [v.strip().upper() for v in value]

    @field_validator("fee_tiers", "venue_eligibility")
    @classmethod
    def _lower_keys(cls, value: Optional[dict]) -> Optional[dict]:
        return None if value is None else {k.strip().lower(): v for k, v in value.items()}

    @field_validator("balances_by_venue")
    @classmethod
    def _balances(cls, value: Optional[dict[str, dict[str, Decimal]]]) -> Optional[dict[str, dict[str, Decimal]]]:
        if value is None:
            return None
        cleaned: dict[str, dict[str, Decimal]] = {}
        for venue, assets in value.items():
            for asset, amount in assets.items():
                if amount < 0:
                    raise ValueError(f"Balance of {asset} on {venue} is negative.")
            cleaned[venue.strip().lower()] = {a.strip().upper(): amt for a, amt in assets.items()}
        return cleaned


def intake_gaps(config: DeskConfig) -> list[dict]:
    """ATLAS intake: the settings a paper candidate needs that are still missing."""
    gaps: list[dict] = []

    def need(field: str, why: str) -> None:
        gaps.append({"field": field, "why": why})

    if config.residence_country is None:
        need("residence_country", "Venue and product eligibility depends on the jurisdiction.")
    if not config.venues:
        need("venues", "Only venues you can actually access should be scanned.")
    for venue in config.venues:
        if venue not in (config.fee_tiers or {}):
            need(f"fee_tiers.{venue}", "Without your account fee tier, fees are only ccxt defaults (ESTIMATED).")
        if venue not in (config.venue_eligibility or {}):
            need(f"venue_eligibility.{venue}", "Eligibility must come from current official sources.")
    if config.available_capital is None:
        need("available_capital", "Capital explicitly available for this experiment.")
    if config.balances_by_venue is None:
        need("balances_by_venue", "Feasibility needs balances by venue and asset.")
    if not config.allowed_assets:
        need("allowed_assets", "The assets you allow the desk to consider.")
    for field in (
        "trade_size_limit",
        "total_deployed_capital_limit",
        "venue_concentration_limit",
        "inventory_exposure_limit",
        "loss_per_incident_limit",
        "daily_loss_limit",
    ):
        if getattr(config, field) is None:
            need(field, "Risk limits are chosen by you; the desk never invents them.")
    for field in ("minimum_conservative_net_amount", "minimum_conservative_net_bps"):
        if getattr(config, field) is None:
            need(field, "Acceptance threshold for conservative net economics.")
    for field in ("maximum_book_age_ms", "maximum_snapshot_skew_ms"):
        if getattr(config, field) is None:
            need(field, "Freshness and skew limits must come from your measured setup; no universal default exists.")
    costs = config.cost_inputs
    if costs.adverse_movement_allowance_bps is None or costs.model_uncertainty_allowance_bps is None:
        need("cost_inputs", "Adverse movement and model uncertainty allowances for conservative net.")
    elif not costs.allowance_basis:
        need("cost_inputs.allowance_basis", "Explain the basis of the allowances.")
    if config.validation_acceptance_criteria is None:
        need("validation_acceptance_criteria", "Define acceptance criteria before looking at evaluation results.")
    if not config.scan.triangular_jobs and not config.scan.cross_exchange_jobs:
        need("scan", "The screener needs at least one triangular or cross-exchange job.")
    return gaps


class ConfigStore:
    """Versioned configuration persisted as JSON."""

    def __init__(self, path: Path):
        self._path = path
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._config = DeskConfig.model_validate(raw["config"])
            self._version = int(raw["config_version"])
            self._updated_at: Optional[str] = raw.get("updated_at")
        else:
            self._config = DeskConfig()
            self._version = 1
            self._updated_at = None

    @property
    def config(self) -> DeskConfig:
        return self._config

    @property
    def version(self) -> int:
        return self._version

    @property
    def updated_at(self) -> Optional[str]:
        return self._updated_at

    def replace(self, new: DeskConfig) -> list[str]:
        """Store a new configuration and return the changed top-level fields."""
        old = self._config.model_dump(mode="json")
        fresh = new.model_dump(mode="json")
        changed = sorted(key for key in fresh if fresh[key] != old.get(key))
        if not changed:
            return []
        self._config = new
        self._version += 1
        self._updated_at = iso(utc_now())
        self._save()
        return changed

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "config_version": self._version,
            "updated_at": self._updated_at,
            "config": self._config.model_dump(mode="json"),
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
