"""Sync quality summary helpers (Issue #12).

Standardizes the details JSONB in sync_runs with structured counts:
fetched, written, skipped, failed, unresolved, partial status.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class SyncQuality:
    """Quality summary for a sync run."""
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    unresolved: int = 0
    status: str = "success"  # success | partial | unavailable
    sources: dict[str, str] = field(default_factory=dict)  # source_name → ok|degraded|failed

    def classify(self) -> str:
        """Derive status from counts."""
        if self.failed > 0 and self.written > 0:
            self.status = "partial"
        elif self.failed > 0 and self.written == 0:
            self.status = "unavailable"
        elif self.written == 0 and self.fetched == 0:
            self.status = "unavailable"
        else:
            self.status = "success"
        return self.status

    def to_dict(self) -> dict:
        self.classify()
        return asdict(self)
