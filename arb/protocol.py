"""Shared vocabulary of the research protocol (P0).

The five roles (ATLAS, SCOUT, VECTOR, RELAY, AEGIS) run as deterministic checks
inside this process. They are not independent agents, so agreement between them
is not independent empirical validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Iterable, Optional

COLLABORATION_MODE = "DETERMINISTIC_PIPELINE"
COLLABORATION_NOTE = (
    "The five roles are deterministic checks in one process, not independent agents. "
    "Their agreement is not independent empirical validation."
)
PAPER_NOTE = "Paper fills are model outputs, not live execution evidence."


class Mode(str, Enum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"


class EvidenceLabel(str, Enum):
    VERIFIED = "VERIFIED"
    ESTIMATED = "ESTIMATED"
    HYPOTHETICAL = "HYPOTHETICAL"
    UNKNOWN = "UNKNOWN"


_STRENGTH = {
    EvidenceLabel.UNKNOWN: 0,
    EvidenceLabel.HYPOTHETICAL: 1,
    EvidenceLabel.ESTIMATED: 2,
    EvidenceLabel.VERIFIED: 3,
}


def weakest(labels: Iterable[EvidenceLabel]) -> EvidenceLabel:
    """A derived figure is only as strong as its weakest input."""
    return min(labels, key=_STRENGTH.__getitem__, default=EvidenceLabel.UNKNOWN)


class StrategyType(str, Enum):
    SPOT_ACROSS_EXCHANGES = "SPOT_ACROSS_EXCHANGES"
    TRIANGULAR_SPOT = "TRIANGULAR_SPOT"


class Severity(str, Enum):
    FAIL = "FAIL"  # evidence shows that a check fails
    MISSING = "MISSING"  # a required input is unknown or cannot be checked
    CONDITION = "CONDITION"  # an input exists but is weaker than required
    INFO = "INFO"


class ScoutResult(str, Enum):
    CANDIDATE = "CANDIDATE"
    NO_CANDIDATE = "NO_CANDIDATE"


class VectorResult(str, Enum):
    VALIDATED_FOR_PAPER = "VALIDATED_FOR_PAPER"
    INCONCLUSIVE = "INCONCLUSIVE"
    REJECTED = "REJECTED"


class RelayResult(str, Enum):
    FEASIBLE_FOR_PAPER = "FEASIBLE_FOR_PAPER"
    BLOCKED = "BLOCKED"
    NEEDS_ENGINEERING = "NEEDS_ENGINEERING"


class AegisVerdict(str, Enum):
    PASS_FOR_PAPER = "PASS_FOR_PAPER"
    CONDITIONAL_FOR_PAPER = "CONDITIONAL_FOR_PAPER"
    VETO = "VETO"


class FinalDecision(str, Enum):
    RESEARCH_ONLY = "RESEARCH_ONLY"
    PAPER_CANDIDATE = "PAPER_CANDIDATE"
    NO_TRADE = "NO_TRADE"
    # Never emitted: it needs agreed acceptance criteria evaluated on recorded
    # forward paper results, and that evaluation is not implemented yet.
    READY_FOR_REVIEW = "READY_FOR_REVIEW"


DECISION_RANK = {
    FinalDecision.PAPER_CANDIDATE: 2,
    FinalDecision.RESEARCH_ONLY: 1,
    FinalDecision.NO_TRADE: 0,
}


@dataclass(frozen=True)
class Finding:
    role: str
    code: str
    severity: Severity
    detail: str

    def as_dict(self) -> dict:
        return {"role": self.role, "code": self.code, "severity": self.severity.value, "detail": self.detail}


def any_severity(findings: Iterable[Finding], *severities: Severity) -> bool:
    return any(f.severity in severities for f in findings)


def dstr(value: Optional[Decimal]) -> Optional[str]:
    """Render a Decimal exactly, without exponent notation or trailing zeros."""
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def money(amount: Optional[Decimal], asset: str) -> Optional[dict]:
    return None if amount is None else {"amount": dstr(amount), "asset": asset}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: Optional[datetime]) -> Optional[str]:
    return None if moment is None else moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def display_time(moment: datetime, timezone_name: str) -> Optional[str]:
    """Local display time for the user; None when the tz database is unavailable."""
    try:
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo(timezone_name)).isoformat()
    except Exception:  # missing tzdata (e.g. Windows without the tzdata package)
        return None
