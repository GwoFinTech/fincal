"""Sync quality summary helpers (Issue #12).

Standardizes the details JSONB in sync_runs with structured counts:
fetched, written, skipped, failed, unresolved, partial status.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class SyncQuality:
    """Quality summary for a sync run."""
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    unresolved: int = 0
    #: Rows written without a fiscal identity (Issue #113).  A count, not a
    #: status input: the run still wrote what the provider gave it.
    unlabeled: int = 0
    #: Rows whose actual value the provider withdrew this run (Issue #111), and a
    #: sample of their display keys so the run's details name what went.
    retracted: int = 0
    retracted_sample: list[str] = field(default_factory=list)
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
