"""Data source priority and conflict detection (Issue #18).

Defines precedence rules and logs conflicts when sources disagree.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from . import db

logger = logging.getLogger(__name__)

# Source precedence (higher index = higher priority)
SOURCE_PRIORITY = {
    "unknown": 0,
    "algorithm": 1,
    "longbridge": 2,
    "futu": 3,
    "kurumi": 4,
}


@dataclass
class FieldConflict:
    field: str
    current_value: str
    current_source: str
    proposed_value: str
    proposed_source: str
    decision: str  # "kept" | "replaced"


def should_replace(current_source: str, proposed_source: str) -> bool:
    """Return True if proposed source has higher priority."""
    return SOURCE_PRIORITY.get(proposed_source, 0) > SOURCE_PRIORITY.get(current_source, 0)


def detect_and_resolve_conflicts(earning_id: int, updates: dict[str, tuple[str, str]]) -> list[FieldConflict]:
    """Check field-level conflicts and return resolution decisions.

    updates: {field_name: (new_value, new_source)}
    Returns list of conflicts detected.
    """
    conflicts = []
    with db.db_cursor() as cur:
        cur.execute("SELECT * FROM earnings WHERE id=%s", (earning_id,))
        row = cur.fetchone()
        if not row:
            return conflicts

        for field, (new_value, new_source) in updates.items():
            current_value = str(row.get(field) or "")
            current_source = str(row.get(f"{field}_source") or "unknown") if f"{field}_source" in row else "unknown"

            if current_value and new_value and current_value != new_value:
                decision = "replaced" if should_replace(current_source, new_source) else "kept"
                conflicts.append(FieldConflict(
                    field=field,
                    current_value=current_value,
                    current_source=current_source,
                    proposed_value=new_value,
                    proposed_source=new_source,
                    decision=decision,
                ))
                if decision == "replaced":
                    logger.info("conflict resolved: earning=%d %s: %s(%s) → %s(%s)",
                                earning_id, field, current_value, current_source,
                                new_value, new_source)

    return conflicts


def get_provenance_summary() -> list[dict]:
    """Return provenance summary for admin diagnostics."""
    with db.db_cursor() as cur:
        cur.execute("""
            SELECT
                date_source, COUNT(*) as count
            FROM earnings
            WHERE date_source IS NOT NULL AND date_source != 'unknown'
            GROUP BY date_source ORDER BY count DESC
        """)
        date_sources = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT
                estimate_source, COUNT(*) as count
            FROM earnings
            WHERE estimate_source IS NOT NULL
            GROUP BY estimate_source ORDER BY count DESC
        """)
        estimate_sources = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT source, COUNT(*) as count
            FROM stock_names WHERE source != ''
            GROUP BY source ORDER BY count DESC
        """)
        name_sources = [dict(r) for r in cur.fetchall()]

    return {
        "date_sources": date_sources,
        "estimate_sources": estimate_sources,
        "name_sources": name_sources,
    }
