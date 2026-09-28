"""The shared OPPORTUNITY_PACKET specification.

Every field is always present. Unsupported or unmeasured values stay null, and
null_reasons says why.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import ConfigDict, create_model

PACKET_FIELDS = (
    "opportunity_id", "config_version", "packet_version", "mode", "collaboration_mode",
    "strategy_type", "route", "asset_identifiers", "reporting_currency",
    "source_references", "raw_data_references", "evidence_labels",
    "event_timestamps", "receive_timestamps", "book_age_ms", "snapshot_skew_ms",
    "freshness_limit_ms", "skew_limit_ms", "clock_uncertainty", "book_integrity",
    "book_depth_by_leg", "requested_size", "feasible_size", "rounded_quantities",
    "account_eligibility", "available_balances", "reserved_balances",
    "fees_by_leg", "fee_assets", "fee_tier_evidence", "conversions",
    "gross_capture", "conditional_net", "expected_net", "conservative_net",
    "cost_ledger", "cost_assumptions", "uncertainty_allowances",
    "capital_by_venue", "total_committed_capital", "operational_reserves",
    "inventory_before", "inventory_after", "net_exposure", "residual_exposure",
    "rebalance_plan", "rebalance_cost", "rebalance_feasibility",
    "execution_plan", "fill_assumptions", "recovery_plan", "stress_scenarios",
    "scout_result", "vector_result", "relay_result", "aegis_verdict",
    "unresolved_questions", "final_decision", "invalidation_conditions",
)  # fmt: skip

OpportunityPacket = create_model(
    "OpportunityPacket",
    __config__=ConfigDict(extra="forbid"),
    null_reasons=(dict[str, str], {}),
    **{name: (Optional[Any], None) for name in PACKET_FIELDS},
)


def finalize_packet(values: dict, reasons: dict[str, str]) -> dict:
    unknown = set(values) - set(PACKET_FIELDS)
    if unknown:
        raise ValueError(f"not packet fields: {sorted(unknown)}")
    packet = {name: values.get(name) for name in PACKET_FIELDS}
    packet["null_reasons"] = {
        name: reasons.get(name, "Not measured by this backend.") for name in PACKET_FIELDS if packet[name] is None
    }
    return OpportunityPacket.model_validate(packet).model_dump()
