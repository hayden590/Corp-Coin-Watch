"""Verdicts. Phase 1 uses safety only; phase 2 adds legitimacy checks,
VERIFIED, and the weighted backing/connection/chart/text scores.

Rule that holds in every phase: nothing overrides DANGER.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from safety import FAIL, UNKNOWN, SafetyReport

DANGER = "DANGER"
VERIFIED = "VERIFIED"
UNCONFIRMED = "UNCONFIRMED"
UNCHECKED = "UNCHECKED"  # safety APIs couldn't answer: treat as unsafe until re-checked


@dataclass
class Verdict:
    label: str
    reasons: list[str] = field(default_factory=list)

    @property
    def is_danger(self) -> bool:
        return self.label == DANGER


def verdict(safety: SafetyReport) -> Verdict:
    fails = [f"{c.name}: {c.detail}" for c in safety.checks if c.status == FAIL]
    if fails:
        return Verdict(DANGER, fails)
    if safety.overall == UNKNOWN:
        unknown = [f"{c.name}: {c.detail}" for c in safety.checks if c.status == UNKNOWN]
        return Verdict(UNCHECKED, unknown)
    return Verdict(UNCONFIRMED, ["safety checks passed; no official confirmation yet"])
