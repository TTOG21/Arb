"""Append-only decision journal and the registry of live opportunities.

The journal keeps decisions, rejected candidates, configuration changes, errors
and paper results, losing and inconclusive ones included. The registry gives a
persistent route one opportunity id and a versioned packet, so adjacent
snapshots of the same event are not counted as separate opportunities.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from arb.protocol import iso, utc_now
from arb.roles import Evaluation


class Journal:
    def __init__(self, path: Path):
        self._path = path

    def append(self, kind: str, **fields) -> dict:
        entry = {"ts": iso(utc_now()), "kind": kind, **fields}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")
        return entry

    def entries(self, kinds: Optional[Iterable[str]] = None) -> Iterable[dict]:
        if not self._path.exists():
            return
        wanted = None if kinds is None else set(kinds)
        with self._path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:  # a torn last line after a crash is skipped
                    continue
                if wanted is None or entry.get("kind") in wanted:
                    yield entry

    def tail(self, limit: int, kinds: Optional[Iterable[str]] = None) -> list[dict]:
        """Newest first."""
        recent = deque(self.entries(kinds), maxlen=limit)
        return list(reversed(recent))


@dataclass
class OpportunityRecord:
    opportunity_id: str
    key: str
    scope: str
    first_seen: str
    last_seen: str
    observations: int = 0
    packet_version: int = 0
    last_decision: Optional[str] = None
    packet: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)

    def brief(self) -> dict:
        return {
            "opportunity_id": self.opportunity_id,
            "route": self.summary.get("route"),
            "strategy": self.summary.get("strategy"),
            "decision": self.last_decision,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "observations": self.observations,
            "packet_version": self.packet_version,
        }


class OpportunityRegistry:
    def __init__(self) -> None:
        self._by_key: dict[str, OpportunityRecord] = {}
        self._by_id: dict[str, OpportunityRecord] = {}

    def observe(self, evaluation: Evaluation, config_version: int, scope: str) -> tuple[OpportunityRecord, bool]:
        """Record one evaluation. Returns the record and whether it is new or its decision changed."""
        now = iso(utc_now())
        record = self._by_key.get(evaluation.key)
        is_new = record is None
        if record is None:
            digest = hashlib.sha1(f"{evaluation.key}|{now}".encode()).hexdigest()[:16]
            record = OpportunityRecord(f"opp-{digest}", evaluation.key, scope, now, now)
            self._by_key[record.key] = record
            self._by_id[record.opportunity_id] = record
        record.observations += 1
        record.packet_version += 1
        record.last_seen = now
        record.packet = evaluation.finalize(record.opportunity_id, record.packet_version, config_version)
        record.summary = {
            **evaluation.summary,
            "opportunity_id": record.opportunity_id,
            "packet_version": record.packet_version,
            "observations": record.observations,
        }
        changed = is_new or record.last_decision != evaluation.decision.value
        record.last_decision = evaluation.decision.value
        return record, changed

    def close_unseen(self, scope: str, seen_keys: set[str]) -> list[OpportunityRecord]:
        closed = [r for r in self._by_key.values() if r.scope == scope and r.key not in seen_keys]
        for record in closed:
            del self._by_key[record.key]
            del self._by_id[record.opportunity_id]
        return closed

    def get(self, opportunity_id: str) -> Optional[OpportunityRecord]:
        return self._by_id.get(opportunity_id)

    def records(self) -> list[OpportunityRecord]:
        return list(self._by_id.values())
