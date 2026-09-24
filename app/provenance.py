"""Data source priority and conflict detection (Issue #18).

Defines precedence rules and logs conflicts when sources disagree.
"""
from __future__ import annotations

import logging
import re
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

#: Source label for an actual that was written and later *retracted* because it
#: turned out not to be the metric its column claims (Issue #62: every
#: ``eps_actual`` the Futu stage wrote before the field fix was 流动比率, the
#: current ratio).  The value itself is cleared; the label keeps a machine
#: readable reason on the row instead of a silently deleted one.
RETRACTED_ACTUAL_SOURCE = "futu_invalid_field"

#: Actual sources a provider is allowed to replace with its own value
#: (Issues #45, #62).  ``RETRACTED_ACTUAL_SOURCE`` is part of the set on purpose:
#: retracting a wrong actual must not make the row unwritable, otherwise the
#: corrected value could never repair it.
REPLACEABLE_ACTUAL_SOURCES = (
    "unknown",
    "algorithm",
    "longbridge",
    "futu",
    RETRACTED_ACTUAL_SOURCE,
)

# ── Numeric attribution labels (Issue #61) ─────────────────────────────────
#
# ``date_source``/``estimate_source``/``actual_source`` say *who* wrote a value;
# they never said *in what unit*.  The EPS/revenue pair of one row can therefore
# mix a quote-currency consensus estimate with a reporting-currency statement
# actual (TSM USD vs TWD, BABA USD vs CNY) or with a different per-share base
# (KTOS -0.00512 vs 5.540795), and every consumer subtracted them anyway.  These
# normalizers keep the provider's own words, and record ``unknown`` when the
# provider said nothing — never the listing's default currency.

#: Label stored when a provider does not declare the currency/basis.
UNKNOWN_ATTRIBUTION = "unknown"

_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")

#: Provider accounting-standard labels mapped to the basis stored on the row.
_BASIS_ALIASES = {
    "US_GAAP": "gaap",
    "USGAAP": "gaap",
    "GAAP": "gaap",
    "IFRS": "ifrs",
    "国际会计准则": "ifrs",
    "国际财务报告准则": "ifrs",
}


def normalize_currency(value) -> str:
    """Return a provider-declared ISO-4217 code, or ``unknown``.

    Never guesses: a missing value, or one that is not a three-letter code, is
    recorded as ``unknown`` so the read path refuses to compare the row's figures
    instead of assuming the listing's currency (Issue #61).
    """
    text = str(value or "").strip().upper()
    return text if _ISO_CURRENCY.match(text) else UNKNOWN_ATTRIBUTION


def normalize_basis(value) -> str:
    """Map a provider's accounting-standard label to ``gaap``/``ifrs``/``unknown``.

    Providers state the standard only sometimes (OpenD returns ``US_GAAP`` on the
    income statement and an empty string on the EPS statement), so an unstated
    basis stays ``unknown`` — the read path treats two unstated bases as
    unverified unless one provider produced both numbers.
    """
    text = str(value or "").strip()
    if not text:
        return UNKNOWN_ATTRIBUTION
    return _BASIS_ALIASES.get(text.upper(), _BASIS_ALIASES.get(text, UNKNOWN_ATTRIBUTION))


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
