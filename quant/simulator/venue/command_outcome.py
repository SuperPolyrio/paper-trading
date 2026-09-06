"""Explicit unknown-request reconciliation outcomes."""

from __future__ import annotations

from enum import Enum


class ReconciliationOutcome(str, Enum):
    ACCEPTED_LIVE = "ACCEPTED_LIVE"
    MATCHED = "MATCHED"
    REJECTED = "REJECTED"
    NOT_FOUND = "NOT_FOUND"
