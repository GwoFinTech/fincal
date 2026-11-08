#!/usr/bin/env python3
"""Full sync of earnings data from Longbridge finance-calendar into fincal DB.
Covers wide date ranges and uses pagination to get all records.
Uses batch inserts for performance.

Longbridge event sequences (Issue #75)
--------------------------------------
One company has several event sequences in the same calendar, told apart by
``ext.financial_report.period_type``:

============  ==========================================================
``qf``/``3q``  earnings release ("业绩公布").  ``period`` is the fiscal
               quarter of the *report* (1–4), so it is the only sequence
               whose ``period`` may become a fiscal identity.
``saf``        half-year report disclosure ("半年报").  ``period`` is not
               the quarter — a half-year event is published as ``period=4``
               — and it normally follows the ``qf`` release of the same
               period by 0–2 days.
``af``         annual report disclosure ("年报").  Same caveat as ``saf``;
               for many non-US names this *is* the annual result event,
               with no separate ``qf/4`` event to pair with.
============  ==========================================================

Reading ``period`` for ``saf``/``af`` is what labelled 394 production rows
"Q4" on an August date (a half-year event, rendered as "same fiscal year Q4
before Q3").  The quarter of a disclosure therefore comes from its sequence
(``saf`` → Q2, ``af`` → Q4) and its fiscal year from the event's own date,
never from the disclosure's ``period``/``fiscal_year`` fields.

One row per fiscal period (Issue #50 follow-up)
-----------------------------------------------
The persistent identity of a row is the fiscal period, so a release and a
disclosure of the *same* period cannot both be rows.  The release owns the
period (its figures are the quarter's), which is recorded as the row's
``report_type``: ``qf``/``3q`` → ``Q``, ``saf`` → ``H``, ``af`` → ``A``
(:data:`app.fiscal.PERIOD_TYPE_REPORT_TYPES`).  Consequently:

* a disclosure whose period already has a release event is not written
  (:func:`fiscal_period_for_event` returns ``skip``) — regardless of how many
  days separate the two events;
* a disclosure event that is a period's *only* event still is that period's row
  (``report_type`` = ``H``/``A``), subject to the usual ordering guard;
* a release arriving later takes the period over from the disclosure row
  (:func:`app.fiscal.reschedule_confirmed_rows`, ``kind='takeover'``), and an
  incoming disclosure never re-dates a stored release row.
"""
import re
import subprocess
import json
import logging
import sys
import os

import psycopg2.errors
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor
from app.symbol import from_lb_calendar_info, normalize
from app.config import stage_timeout
from app.sync_audit import check_cancelled, SyncCancelledError, HeartbeatThrottle
from app.sync_quality import SyncQuality
from app import fiscal
from app.provenance import UNKNOWN_ATTRIBUTION, normalize_currency

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 200

# ── Revision stamps (Issue #103) ────────────────────────────────────────────
#: Columns a client can see.  ``updated_at`` may only move when one of them
#: changes: the flush used to write ``updated_at = NOW()`` on every re-delivery,
#: so one weekly run stamped 25,875 unchanged rows with a single batch time and
#: the 497 events of the feed collapsed into 12 ``SEQUENCE`` values.
#:
#: Provider provenance (``date_source``/``date_status``/``estimate_source``/
#: ``actual_source``) is deliberately *absent*: both sync stages rewrite it on
#: every run, so counting it would restore exactly the churn being fixed.
_REVISION_COLUMNS = (
    "company_name", "fiscal_year", "fiscal_quarter",
    "eps_estimate", "eps_actual", "revenue_estimate", "revenue_actual",
    "before_after", "estimate_currency", "actual_currency",
    "estimate_basis", "actual_basis", "is_predicted",
)

#: Issue #73: ``estimate_as_of``/``actual_as_of`` say when *that* number changed,
#: so each side follows only its own values and labels.
_ESTIMATE_STAMP_COLUMNS = (
    "eps_estimate", "revenue_estimate", "estimate_currency", "estimate_basis",
)
_ACTUAL_STAMP_COLUMNS = (
    "eps_actual", "revenue_actual", "actual_currency", "actual_basis",
)

#: How a re-delivered Longbridge row updates a stored one, column by column.
#: Rendered into the conflict clause below; :func:`fiscal.revision_predicate`
#: reuses these very expressions, so "what a write changes" and "what bumps the
#: stamp" cannot drift apart (Issue #103).
_LONGBRIDGE_UPSERT: dict[str, str] = {
    "company_name": "EXCLUDED.company_name",
    "fiscal_year": "CASE WHEN earnings.fiscal_year IS NULL THEN EXCLUDED.fiscal_year ELSE earnings.fiscal_year END",
    "fiscal_quarter": "CASE WHEN earnings.fiscal_quarter IS NULL THEN EXCLUDED.fiscal_quarter ELSE earnings.fiscal_quarter END",
    "eps_estimate": "COALESCE(EXCLUDED.eps_estimate, earnings.eps_estimate)",
    "eps_actual": "COALESCE(EXCLUDED.eps_actual, earnings.eps_actual)",
    "revenue_estimate": "COALESCE(EXCLUDED.revenue_estimate, earnings.revenue_estimate)",
    "revenue_actual": "COALESCE(EXCLUDED.revenue_actual, earnings.revenue_actual)",
    "before_after": "COALESCE(EXCLUDED.before_after, earnings.before_after)",
    # Issue #61 / #89: a label is evidence, so it is never wiped and never
    # downgraded.  It is written when the row carries none (or only the
    # ``unknown`` marker) and the new value is a real declaration; the estimate
    # side only accepts it for an estimate Longbridge owns
    # (``estimate_source`` longbridge/unattributed) — the same rule the one-time
    # backfill uses, because a predictor-owned estimate is not Longbridge's
    # figure and must not inherit the calendar's currency.
    "estimate_currency": (
        "CASE\n"
        "                WHEN earnings.estimate_currency IS NOT NULL\n"
        "                     AND LOWER(earnings.estimate_currency) <> 'unknown'\n"
        "                    THEN earnings.estimate_currency\n"
        "                WHEN LOWER(COALESCE(earnings.estimate_source, '')) IN ('', 'longbridge')\n"
        "                     AND EXCLUDED.estimate_currency IS NOT NULL\n"
        "                     AND LOWER(EXCLUDED.estimate_currency) <> 'unknown'\n"
        "                    THEN EXCLUDED.estimate_currency\n"
        "                ELSE earnings.estimate_currency END"
    ),
    "estimate_basis": "COALESCE(earnings.estimate_basis, EXCLUDED.estimate_basis)",
    # Issue #89/#91: the label describes *this* provider's number, so it may
    # never outlive the value it was written for.  A declaration from the writer
    # always wins; a re-sync by the same writer keeps what is stored; a
    # *different* writer that declares nothing resets the label to `unknown`
    # instead of leaving Futu's TWD/EUR/CNY sitting on a USD-converted
    # Longbridge figure.
    "actual_currency": (
        "CASE\n"
        "                WHEN EXCLUDED.eps_actual IS NULL AND EXCLUDED.revenue_actual IS NULL\n"
        "                    THEN earnings.actual_currency\n"
        "                WHEN EXCLUDED.actual_currency IS NOT NULL\n"
        "                     AND LOWER(EXCLUDED.actual_currency) <> 'unknown'\n"
        "                    THEN EXCLUDED.actual_currency\n"
        "                WHEN LOWER(COALESCE(earnings.actual_source, '')) = 'longbridge'\n"
        "                    THEN earnings.actual_currency\n"
        "                ELSE 'unknown' END"
    ),
    "actual_basis": (
        "CASE\n"
        "                WHEN EXCLUDED.eps_actual IS NULL AND EXCLUDED.revenue_actual IS NULL\n"
        "                    THEN earnings.actual_basis\n"
        "                WHEN EXCLUDED.actual_basis IS NOT NULL\n"
        "                     AND LOWER(EXCLUDED.actual_basis) <> 'unknown'\n"
        "                    THEN EXCLUDED.actual_basis\n"
        "                WHEN LOWER(COALESCE(earnings.actual_source, '')) = 'longbridge'\n"
        "                    THEN earnings.actual_basis\n"
        "                ELSE 'unknown' END"
    ),
    "is_predicted": fiscal.IDENTITY_PROMOTION_VALUE,
}

#: The INSERT's parameter layout: one VALUES field per batch tuple, in the order
#: :func:`sync_earnings` builds them, with the type each field must be read as.
#: The cast is not cosmetic: in ``INSERT ... SELECT`` the target column does not
#: coerce the select list, and a column whose values are all NULL in a batch (a
#: calendar page where no event carries an actual) arrives as ``unknown`` and is
#: read as text — which a ``numeric`` target rejects.
_BATCH_VALUE_CASTS = {
    "symbol": "text", "market": "text", "company_name": "text",
    "report_date": "date", "report_type": "text",
    "fiscal_year": "integer", "fiscal_quarter": "integer",
    "eps_estimate": "numeric", "eps_actual": "numeric",
    "revenue_estimate": "numeric", "revenue_actual": "numeric",
    "before_after": "text", "estimate_currency": "text",
    "actual_currency": "text", "estimate_basis": "text", "actual_basis": "text",
}
#: Clear the actual figures an event explicitly withdrew (Issue #111).  Runs before
#: the provenance UPDATE of the same batch, so ``date_status`` is recomputed from
#: what is actually stored: a row that loses its last actual goes back to
#: ``scheduled`` in the same commit.  Only rows that still hold the value are
#: touched, and a predicted row is never a target.
_RETRACTION_SQL = """
    UPDATE earnings AS e SET
        eps_actual = CASE WHEN v.drop_eps THEN NULL ELSE e.eps_actual END,
        revenue_actual = CASE WHEN v.drop_revenue THEN NULL ELSE e.revenue_actual END,
        actual_as_of = CASE WHEN v.drop_eps AND v.drop_revenue THEN NULL ELSE e.actual_as_of END,
        actual_currency = CASE WHEN v.drop_eps AND v.drop_revenue THEN NULL ELSE e.actual_currency END,
        actual_basis = CASE WHEN v.drop_eps AND v.drop_revenue THEN NULL ELSE e.actual_basis END,
        actual_source = CASE WHEN v.drop_eps AND v.drop_revenue THEN NULL ELSE e.actual_source END,
        updated_at = NOW()
      FROM (VALUES %s) AS v(symbol, market, report_date, report_type, drop_eps, drop_revenue)
     WHERE e.symbol = v.symbol AND e.market = v.market
       AND e.report_date = (v.report_date)::date
       AND e.report_type = v.report_type
       AND e.is_predicted = FALSE
       -- Only the value's own supplier may withdraw it (#91): Longbridge serving a
       -- blank for an event whose actual came from Futu says nothing about Futu's
       -- figure — the first version of this cleared one Futu-written US actual.
       AND (e.actual_source IS NULL OR e.actual_source = 'longbridge')
       AND ((v.drop_eps AND e.eps_actual IS NOT NULL)
            OR (v.drop_revenue AND e.revenue_actual IS NOT NULL))
"""

_BATCH_VALUE_COLUMNS = tuple(_BATCH_VALUE_CASTS)


@dataclass
class FlushStats:
    """Outcome of one batch upsert (Issue #52).

    ``rows`` is what the batch actually committed; ``moves`` are the fiscal
    periods re-dated onto the provider's date, ``skipped`` the periods whose
    provider date was already owned by another row (a data conflict to
    reconcile, not a failure).
    """

    rows: int = 0
    moves: int = 0
    skipped: int = 0
    #: Incoming half-year/annual disclosures dropped because the period's release
    #: event already owns it (Issue #50 follow-up).
    dropped_disclosures: int = 0
    #: Periods where an incoming release replaced a stored disclosure row.
    takeovers: int = 0
    #: Rows skipped because another confirmed row already holds the same fiscal
    #: identity at a different (report_date, report_type) — Issue #87.
    identity_skipped: int = 0
    #: Actual figures the provider withdrew and this batch cleared (Issue #111).
    retracted: int = 0
    #: The display keys of the rows that really lost a value, so a run's details name
    #: what went rather than listing every event that carried a blank.
    retracted_keys: list[str] = field(default_factory=list)



@dataclass
class SyncStats:
    """Entry counts for one Longbridge run.

    The counts are accumulated as batches commit, so a run that dies mid-way —
    or loses single batches to a data conflict — still reports what it really
    wrote.  Before Issue #52 the failure path hard-coded ``fetched=0,
    written=0`` and recorded ~0 for a run that had already committed ~10.8k rows.
    """

    fetched: int = 0
    written: int = 0
    rescheduled: int = 0
    skipped: int = 0
    failed_batches: int = 0
    dropped_disclosures: int = 0
    takeovers: int = 0
    #: Calendar pages (day buckets) the provider returned, and the events inside
    #: them.  Both are counted even when an event cannot be resolved, so a
    #: provider payload change reads as "25k events, 0 resolved" instead of
    #: "0 fetched, success" — the shape of the 2026-10-29 incident (Issue #104).
    pages: int = 0
    events: int = 0
    #: Events dropped because their identity (symbol/market) or their date could
    #: not be resolved.  ``events - unresolved`` is what entered the write path.
    unresolved: int = 0
    #: Rows written without a fiscal identity ``(fiscal_year, fiscal_quarter)``.
    #: Those rows are outside the identity contract: no unique-index protection,
    #: no reschedule move, no merge, and a date-based iCal UID.  Before Issue
    #: #113 a run could write 462 of them and still record ``success`` with
    #: nothing in ``details`` to show it (Issue #113 acceptance 4).
    unlabeled: int = 0
    #: Rows whose actual value the provider withdrew, cleared by this run
    #: (Issue #111).  Recorded so a value can disappear visibly instead of
    #: lingering for weeks after the provider stopped serving it.
    retracted: int = 0
    #: A sample of the cleared display keys, so the run's details name what went
    #: instead of only how much (capped; the count is the full number).
    retracted_keys: list[str] = field(default_factory=list)

    def resolved(self) -> int:
        """Events that carried a usable identity and date."""
        return max(self.events - self.unresolved, 0)

    def quality(self) -> SyncQuality:
        return SyncQuality(
            fetched=self.fetched,
            written=self.written,
            skipped=self.skipped,
            failed=self.failed_batches,
            unresolved=self.unresolved,
            unlabeled=self.unlabeled,
            retracted=self.retracted,
            retracted_sample=self.retracted_keys,
        )


def next_calendar_cursor(api_next_date: str, last_report_date: str, current_start: str) -> str | None:
    """Advance pagination even when Longbridge omits ``next_date``.

    The calendar endpoint can return a full page for only one or two days but
    leave ``next_date`` empty.  Stopping there silently drops every later
    earnings release and its consensus estimates.
    """
    candidates: list[str] = []
    try:
        candidate = api_next_date.strip()
        if candidate and date.fromisoformat(candidate) > date.fromisoformat(current_start):
            candidates.append(candidate)
    except ValueError:
        pass
    try:
        last_day = last_report_date.split(" ", 1)[0].replace(".", "-")
        candidates.append((date.fromisoformat(last_day) + timedelta(days=1)).isoformat())
    except (AttributeError, ValueError):
        pass
    return max(candidates) if candidates else None


#: Events requested per calendar call.  The provider caps one response at ~3,300
#: events and, when a single day holds more than the requested count, it returns
#: that many *and advances the cursor to the next date* — so the surplus is lost
#: for good.  ``--count 300`` therefore kept 300 of the 3,320 events announced on
#: 2026-05-07 (and of 1,735 on 2026-08-13, 2,509 on 2026-02-12): every busy
#: earnings day silently lost ~90% of its releases, which is also why some of
#: those symbols then appeared with no currency/time label at all (Issue #90).
#: 10,000 is above the provider's own ceiling — raising it further returns the
#: same totals (measured: 5,000 / 10,000 / 20,000 all returned 3,320) — so this
#: asks for the whole day and leaves the ceiling to the provider.
CALENDAR_PAGE_COUNT = 10000


def fetch_calendar(market: str, start: str, end: str) -> list[dict]:
    """Fetch all earnings calendar pages from Longbridge, paginating via next_date."""
    all_pages = []
    cursor_start = start
    # Empty calendar days must be advanced explicitly: this endpoint does not
    # seek to the next non-empty day when ``start`` itself has no releases.
    max_iterations = 800

    for i in range(max_iterations):
        cmd = [
            "longbridge", "finance-calendar", "report",
            "--market", market,
            "--start", cursor_start,
            "--end", end,
            "--count", str(CALENDAR_PAGE_COUNT),
            "--format", "json",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                raise RuntimeError(f"longbridge_cli_failed:{market}:{i}:{result.stderr[:200]}")
            data = json.loads(result.stdout)
        except Exception as exc:
            logger.error(f"Fetch error iteration {i}: {exc}")
            raise RuntimeError(f"longbridge_fetch_failed:{market}:{i}") from exc

        pages = data.get("list", [])
        if not pages:
            next_cursor = (date.fromisoformat(cursor_start) + timedelta(days=1)).isoformat()
            if next_cursor > end:
                break
            logger.debug("%s no releases on %s; advancing to %s", market, cursor_start, next_cursor)
            cursor_start = next_cursor
            continue
        all_pages.extend(pages)

        # A page that fills the request is the one case where the next cursor
        # still leaves events behind (see CALENDAR_PAGE_COUNT): the provider
        # advances the date instead of paging, so say so instead of losing them
        # silently (Issue #90).
        for page in pages:
            if len(page.get("infos") or []) >= CALENDAR_PAGE_COUNT:
                logger.warning(
                    "%s %s: %d events returned for one date — the provider's "
                    "ceiling was reached, any surplus is not in this response",
                    market, page.get("date"), len(page.get("infos") or []),
                )

        next_date = data.get("next_date", "")
        last_page_date = pages[-1].get("date", "")
        next_cursor = next_calendar_cursor(next_date, last_page_date, cursor_start)
        if not next_cursor or next_cursor > end:
            break
        if next_cursor <= cursor_start:
            raise RuntimeError(f"longbridge_pagination_stalled:{market}:{cursor_start}")
        cursor_start = next_cursor

        logger.info(f"  {market} iteration {i}: got {len(pages)} pages, last_date={last_page_date}, next={cursor_start}")
    else:
        raise RuntimeError(f"longbridge_pagination_limit:{market}:{max_iterations}")

    return all_pages


def parse_date_type(date_type: str) -> str | None:
    for k, v in {"盘前": "before", "盘后": "after", "盘中": "during",
                 "Before Open": "before", "After Close": "after"}.items():
        if k in (date_type or ""):
            return v
    return None


def extract_kv(data_kv: list[dict]) -> dict:
    result = {}
    for kv in data_kv:
        t = kv.get("type", "")
        raw = kv.get("value_raw")
        val = None
        if raw is not None and raw != "" and raw != "0.000000":
            try:
                val = float(raw)
            except (ValueError, TypeError):
                pass
        if t == "estimate_eps":
            result["eps_estimate"] = val
        elif t == "actual_eps":
            result["eps_actual"] = val
        elif t == "estimate_revenue":
            result["revenue_estimate"] = val
        elif t == "actual_revenue":
            result["revenue_actual"] = val
    return result


def extract_retractions(data_kv: list[dict]) -> dict:
    """Which actual figures this event explicitly withdrew (Issue #111).

    The provider withdraws a value by serving the field with an empty
    ``value_raw`` — ``{"type": "actual_revenue", "value_raw": ""}`` — which is not
    the same as not mentioning it.  Only that explicit form may clear a stored
    actual: a calendar window that simply does not cover an event must never be
    read as a withdrawal, or one shrunken window would wipe the table.  A zero is
    a value (``"0.000000"`` stays a real 0.00 EPS), so only a blank clears.
    """
    dropped = {"eps": False, "revenue": False}
    for kv in data_kv or ():
        field = str(kv.get("type", ""))
        if field not in ("actual_eps", "actual_revenue"):
            continue
        raw = kv.get("value_raw")
        if raw is None or str(raw).strip() != "":
            continue
        dropped["eps" if field == "actual_eps" else "revenue"] = True
    return dropped


#: ``Today 2026.10.30 (EST)`` — Longbridge prefixes same-day events with a word
#: but still writes the absolute date (Issue #107).  ``split(" ")[0]`` used to
#: return ``"Today"``, so 114 events per run were dropped as unparseable.
_ABSOLUTE_DATE = re.compile(r"(\d{4})[.\-/](\d{2})[.\-/](\d{2})")

#: Zones the provider names in its date label, as a UTC offset in hours.  Only
#: used to bucket an epoch timestamp into a calendar day — a fallback that only
#: runs when neither the label nor ``ext.local_date`` carries a date.
_ZONE_OFFSETS = (
    ("EST", -5), ("EDT", -4), ("美东", -4),
    ("CST", -6), ("CDT", -5),
    ("PST", -8), ("PDT", -7),
    ("HKT", 8), ("GMT+8", 8), ("+08", 8), ("香港", 8),
    ("JST", 9), ("GMT+9", 9),
    ("BST", 1), ("CET", 1), ("CEST", 2),
)


def parse_report_date(date_str: str) -> str | None:
    """Normalize provider dates, including a relative label that carries one.

    ``Today 2026.10.30 (EST)`` must yield ``2026-10-30``: the prefix is a display
    detail, the absolute date is right there in the string (Issue #107).
    """
    if not date_str:
        return None
    absolute = _ABSOLUTE_DATE.search(date_str)
    if absolute:
        try:
            year, month, day = (int(part) for part in absolute.groups())
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    try:
        parsed = str(date_str).split(" ")[0].replace(".", "-")
        return date.fromisoformat(parsed).isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def _date_from_epoch(value, zone: str = "") -> str | None:
    """Bucket an epoch timestamp into a calendar day in the event's own zone."""
    try:
        stamp = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    offset = next((hours for name, hours in _ZONE_OFFSETS if name in (zone or "")), 0)
    try:
        moment = datetime.fromtimestamp(stamp, tz=timezone.utc) + timedelta(hours=offset)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.date().isoformat()


def resolve_report_date(info: dict) -> str | None:
    """The event's absolute report date, from the most authoritative source available.

    Priority follows reliability, not convenience (Issue #107):

    1. ``ext.local_date`` — the provider's own ISO date for the event's market day;
    2. the absolute date inside the label (``Today 2026.10.30 (EST)``);
    3. the epoch ``datetime``, bucketed into the zone the label names;
    4. the legacy bare-ISO label.

    ``None`` means the event really carries no absolute date, which the caller
    counts as ``unresolved`` instead of dropping it silently.
    """
    ext = info.get("ext") or {}
    local_date = ext.get("local_date") if isinstance(ext, dict) else None
    if local_date:
        parsed = parse_report_date(str(local_date))
        if parsed:
            return parsed

    label = info.get("date", "")
    parsed = parse_report_date(label)
    if parsed:
        return parsed

    zone = ""
    if isinstance(ext, dict):
        zone = str(ext.get("date_zone") or "")
    if not zone:
        zone = str(label)
    return _date_from_epoch(info.get("datetime"), zone)


#: Confirmed annual rows per symbol — the closings a fiscal year is derived from.
#: ``report_type = 'A'`` covers an annual report whose quarter was never labelled;
#: ``fiscal_quarter = 4`` covers a Q4 release that carries the annual figures.
_ANNUAL_CLOSINGS_SQL = """
    SELECT symbol, market, fiscal_year, report_date
      FROM earnings
     WHERE is_predicted = FALSE
       AND fiscal_year IS NOT NULL
       AND (fiscal_quarter = 4 OR report_type = 'A')
     ORDER BY symbol, market, report_date
"""

#: Disclosure sequences keyed to the fiscal quarter they disclose.  A half-year
#: report is the second quarter and an annual report the fourth, whatever their
#: ``period`` field says (Issue #75).
_DISCLOSURE_QUARTERS = {"saf": 2, "af": 4}
_DISCLOSURE_PERIOD_TYPES = frozenset(_DISCLOSURE_QUARTERS)
_VALID_PERIOD_TYPES = {"qf", "3q"}

#: The release event of a period is the period's row: a disclosure event whose
#: period already has a release event in the calendar window is not written,
#: however far apart the two dates are (Issue #50 follow-up).  Distance used to
#: gate this (14 days), which left the annual report of a period whose release
#: sat further away as a second row for the same identity.


def _parse_int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _fiscal_year_for(market: str, fiscal_quarter: int, report_date: str, *,
                     symbol: str = "", closings=None) -> int | None:
    """Derive a quarter's fiscal year from the symbol's own closing history.

    A fiscal year is closed by its annual report, so an event belongs to the year
    after the last annual report that preceded it — true for any year end
    (Issue #113).  The calendar-month rule below is the fallback for a symbol
    whose annual report we have never seen: it is only correct for a December
    year-end, which is why 468 half-year reports of March/June year-end companies
    were labelled one year early and then rejected by the consistency check.
    """
    if closings is not None:
        from_history = fiscal.fiscal_year_after_closing(
            closings.get((symbol, market), ()), report_date
        )
        if from_history is not None:
            return from_history
    try:
        year = int(report_date[:4])
        month = int(report_date[5:7])
    except (TypeError, ValueError, IndexError):
        return None
    if market == "US":
        if fiscal_quarter <= 2:
            return year
        return year - 1 if month <= 6 else year
    if fiscal_quarter in (1, 2):
        return year
    return year - 1 if month <= 3 else year


def _raw_fiscal_period(ext: dict, report_date: str, market: str, *,
                       symbol: str = "", closings=None) -> tuple[int | None, int | None, str]:
    """Read a Longbridge fiscal period without conflating event sequences."""
    period_type = str(ext.get("period_type") or "").strip().lower()
    fiscal_quarter = _parse_int(ext.get("period"))
    if fiscal_quarter not in (1, 2, 3, 4):
        fiscal_quarter = None

    fiscal_year = _parse_int(ext.get("fiscal_year") or ext.get("year"))
    if fiscal_year is None and fiscal_quarter is not None:
        # Issue #113: the annual-report history outranks the month heuristic — a
        # June year-end company's Q1 is the calendar year's, a March year-end
        # company's is the year after its annual report.
        fiscal_year = _fiscal_year_for(market, fiscal_quarter, report_date,
                                       symbol=symbol, closings=closings)
    return fiscal_year, fiscal_quarter, period_type


class ClosingHistory(dict):
    """Annual-report history, read from the database the first time it is asked for.

    Lazy on purpose.  A run whose every event states its own ``fiscal_year`` never
    consults the history, so it must not pay for (or require) the read — and a
    unit test that fakes the provider should not have to fake this too.
    """

    def __init__(self):
        super().__init__()
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self.update(load_annual_closings())

    def get(self, key, default=None):
        self._load()
        return super().get(key, default)

    def __contains__(self, key) -> bool:
        self._load()
        return super().__contains__(key)


def load_annual_closings() -> dict[tuple[str, str], list[dict]]:
    """Annual-report history per symbol: the closings a fiscal year derives from.

    Read once per run.  The rule is "the event belongs to the fiscal year after
    the last annual report that preceded it", and that report is frequently
    *outside* the 180/365-day calendar window (a March year-end company's annual
    result lands 6 months before its half-year), so it cannot come from the
    window index — it has to come from the table (Issue #113).

    A failure here must not stop the sync: without the history the month
    heuristic still derives a year, just less reliably, and the rows it gets
    wrong are counted in ``unlabeled`` instead of being written silently.
    """
    closings: dict[tuple[str, str], list[dict]] = {}
    try:
        with db_cursor() as cur:
            cur.execute(_ANNUAL_CLOSINGS_SQL)
            for row in cur.fetchall():
                closings.setdefault((row["symbol"], row["market"]), []).append({
                    "fiscal_year": row["fiscal_year"],
                    "report_date": row["report_date"],
                })
    except Exception as exc:  # noqa: BLE001 - the sync must still run
        logger.warning(
            "could not read the annual-report history; deriving fiscal years from "
            "the calendar month instead (%s)", exc,
        )
        return {}
    return closings


def build_fiscal_period_index(pages: list[dict], closings=None) -> dict[tuple[str, str], list[dict]]:
    """Index canonical qf/3q events for mapping disclosure-only events.

    Longbridge's ``saf`` and ``af`` events are disclosure sequences, not a second
    fiscal-quarter numbering scheme.  The index is built before writing so a
    disclosure event can be recognised as the announcement of a period that
    already has its own release event.
    """
    index: dict[tuple[str, str], list[dict]] = {}
    for page in pages:
        for info in page.get("infos", []):
            symbol, market = from_lb_calendar_info(info)
            report_date = resolve_report_date(info)
            if not symbol or not report_date:
                continue
            ext = info.get("ext", {}).get("financial_report", {})
            fiscal_year, fiscal_quarter, period_type = _raw_fiscal_period(
                ext, report_date, market, symbol=symbol, closings=closings
            )
            if fiscal_year is None or fiscal_quarter is None:
                continue
            if period_type and period_type not in _VALID_PERIOD_TYPES:
                continue
            index.setdefault((symbol, market), []).append({
                "symbol": symbol,
                "market": market,
                "fiscal_year": fiscal_year,
                "fiscal_quarter": fiscal_quarter,
                "report_date": report_date,
            })
    return index


def fiscal_period_for_event(symbol: str, market: str, report_date: str, ext: dict,
                            canonical_periods: dict[tuple[str, str], list[dict]],
                            closings=None) -> tuple[int | None, int | None, bool]:
    """Return ``(FY, quarter, skip)`` using Longbridge event-sequence semantics.

    A ``saf``/``af`` event is the *disclosure* of a quarter: its own ``period``
    and ``fiscal_year`` fields belong to the disclosure sequence and are ignored
    (see the module docstring).  Its quarter comes from the sequence and its
    fiscal year from the event date, so the row lands on the period the release
    event already uses.

    ``skip`` is returned when that period already has its release event in the
    calendar window — the disclosure repeats a period that is already on the
    calendar and writing it would duplicate the row (Issue #50).  When the
    period has no release event at all (an ``af`` annual report with no ``qf``
    counterpart, e.g. HSBC's annual result), the disclosure *is* that period's
    event and keeps the identity, subject to the same ordering guard as any
    other label.
    """
    fiscal_year, fiscal_quarter, period_type = _raw_fiscal_period(
        ext, report_date, market, symbol=symbol, closings=closings
    )

    if period_type in _DISCLOSURE_QUARTERS:
        quarter = _DISCLOSURE_QUARTERS[period_type]
        fiscal_year = _fiscal_year_for(market, quarter, report_date,
                                       symbol=symbol, closings=closings)
        rows = canonical_periods.get((symbol, market), [])
        for row in rows:
            if row["fiscal_year"] != fiscal_year or row["fiscal_quarter"] != quarter:
                continue
            logger.debug(
                "ignoring Longbridge %s disclosure event %s.%s %s: FY%s Q%s "
                "release already recorded on %s",
                period_type, symbol, market, report_date, fiscal_year, quarter,
                row["report_date"],
            )
            return None, None, True
        if fiscal_year is None:
            logger.warning(
                "Longbridge %s event has no derivable fiscal year: %s.%s %s; "
                "keeping date without fiscal identity",
                period_type, symbol, market, report_date,
            )
            return None, None, False
        if not fiscal.fiscal_label_consistent(
            symbol, market, fiscal_year, quarter, report_date, rows
        ):
            logger.warning(
                "rejecting inconsistent Longbridge %s fiscal label %s.%s %s FY%s Q%s",
                period_type, symbol, market, report_date, fiscal_year, quarter,
            )
            return None, None, False
        return fiscal_year, quarter, False

    if period_type and period_type not in _VALID_PERIOD_TYPES:
        logger.warning(
            "ignoring unsupported Longbridge period_type=%s for %s.%s %s",
            period_type, symbol, market, report_date,
        )
        return None, None, False

    rows = canonical_periods.get((symbol, market), [])
    if fiscal_year is not None and fiscal_quarter is not None and not fiscal.fiscal_label_consistent(
        symbol, market, fiscal_year, fiscal_quarter, report_date, rows
    ):
        logger.warning(
            "rejecting inconsistent Longbridge fiscal label %s.%s %s FY%s Q%s",
            symbol, market, report_date, fiscal_year, fiscal_quarter,
        )
        return None, None, False
    return fiscal_year, fiscal_quarter, False


def dedupe_batch(rows: list[tuple]) -> list[tuple]:
    """Collapse provider duplicates before a bulk UPSERT.

    Longbridge can repeat one calendar event across adjacent result windows.
    PostgreSQL rejects duplicate conflict keys within one ``execute_values``
    statement; retain the copy carrying the most financial metadata.
    """
    unique: dict[tuple[str, str, str, str], tuple] = {}
    for row in rows:
        key = (row[0], row[1], row[3], row[4])
        existing = unique.get(key)
        score = sum(value not in (None, "") for value in row[2:])
        existing_score = sum(value not in (None, "") for value in existing[2:]) if existing else -1
        if score > existing_score:
            unique[key] = row
    return list(unique.values())


def _insert_earnings_rows(cur, insert_sql: str, rows: list[tuple]) -> tuple[list[tuple], int]:
    """Insert the batch, falling back to row-by-row on a fiscal-identity clash.

    The batch is one statement, so a single row that collides with
    ``idx_earnings_fiscal_identity`` — the same fiscal period arriving at a
    second ``(report_date, report_type)``, which the natural-key ON CONFLICT
    cannot catch — used to discard the other ~199 rows of the batch too.  Full-day
    calendar fetches (Issue #90) made that the normal case: 14 batches of 200 were
    lost silently on every run.  Each row now gets its own savepoint, so only the
    colliding rows are skipped, the rest of the batch lands, and every skip names
    the period, date and report type that already owns the identity (Issue #87).

    Returns ``(written_rows, skipped_count)``.
    """
    from psycopg2.extras import execute_values

    cur.execute("SAVEPOINT earnings_batch")
    try:
        execute_values(cur, insert_sql, rows, page_size=BATCH_SIZE)
    except psycopg2.errors.UniqueViolation as exc:
        cur.execute("ROLLBACK TO SAVEPOINT earnings_batch")
        logger.warning(
            "  batch insert hit %s on %d rows; retrying row by row so one "
            "colliding fiscal identity cannot discard the batch",
            type(exc).__name__, len(rows),
        )
    else:
        cur.execute("RELEASE SAVEPOINT earnings_batch")
        return list(rows), 0

    written: list[tuple] = []
    skipped = 0
    for row in rows:
        cur.execute("SAVEPOINT earnings_row")
        try:
            execute_values(cur, insert_sql, [row], page_size=1)
        except psycopg2.errors.UniqueViolation:
            cur.execute("ROLLBACK TO SAVEPOINT earnings_row")
            skipped += 1
            logger.warning(
                "  skipped %s.%s FY%s Q%s @%s (%s): another confirmed row already "
                "holds this fiscal period",
                row[0], row[1], row[5], row[6], row[3], row[4],
            )
        else:
            cur.execute("RELEASE SAVEPOINT earnings_row")
            written.append(row)
    return written, skipped


def flush_batch(cur, rows: list[tuple], retractions: dict | None = None) -> FlushStats:
    """Batch upsert using execute_values.

    Two Issue #50 guards run before the upsert, because the table's unique key is
    the report date — not the fiscal period:

    * a provider response that carries one fiscal period at two dates is
      collapsed to the newest date (adjacent calendar windows overlap);
    * a period that already has a confirmed row on a different date is re-dated
      onto the incoming one instead of being inserted as a second row.

    Issue #52: the re-dating step can be impossible (another fiscal period, or a
    prediction, already owns the provider's date).  Those periods are reported
    back in ``FlushStats`` instead of aborting the batch, and the upsert below
    no longer rewrites an existing row's ``fiscal_year``/``fiscal_quarter`` —
    the fiscal period is the row's persistent identity, so letting a provider
    candidate of a *different* period overwrite it would re-create exactly the
    duplicate-period disease #50 removed.

    Issue #81: a period's own stale prediction holding the provider's date is
    removed by the pass above so the confirmed row can move onto it (the
    Longbridge path carried the same ``is_predicted = FALSE`` promotion as the
    Futu path that aborted production); the upsert below now promotes a
    conflicting row only while it carries the incoming row's identity
    (``CONFIRM_IDENTITY_PROMOTION``).

    Issue #61: the batch also carries the event's declared currency so each
    written value can say which unit of money it is in.  The actual side is
    labelled only when this event supplied the actual value; a Futu-written
    actual on the same row keeps Futu's own attribution.
    """
    rows = dedupe_batch(rows)
    collapsed_out: list = []
    rows = fiscal.collapse_rows_by_period(
        rows,
        identity_of=lambda r: fiscal.fiscal_key_from_parts(r[0], r[1], r[5], r[6]),
        date_of=lambda r: r[3],
        rank_of=lambda r: 1 if fiscal.is_disclosure(r[4]) else 0,
        dropped=collapsed_out,
    )
    # Count what the collapse removed for the same reason the pass below drops
    # rows: a half-year/annual disclosure of a period whose release is in this
    # very response is not a row of its own.
    kept_release_periods = {
        (kept[0], kept[1], kept[5], kept[6])
        for kept in rows if not fiscal.is_disclosure(kept[4])
    }
    collapse_drops = sum(
        1 for row in collapsed_out
        if fiscal.is_disclosure(row[4]) and (row[0], row[1], row[5], row[6]) in kept_release_periods
    )
    if not rows:
        return FlushStats()
    outcome = fiscal.reschedule_confirmed_rows(cur, rows)
    # Issue #50 follow-up: one row per fiscal period — a disclosure event whose
    # period is owned by a release row (stored, or in this same batch) is dropped
    # by the pass above, so write exactly the rows it left.
    rows = outcome.rows
    if not rows:
        return FlushStats(dropped_disclosures=len(outcome.dropped) + collapse_drops)
    # Issue #87: one announcement is one row even when the two events carry
    # different fiscal labels (or none), which the identity-grouped pass above
    # cannot see.  Runs before the insert so the batch never mints a twin.
    same_date = fiscal.settle_same_date_rows(cur, rows)
    rows = same_date.rows
    for taken in same_date.promoted:
        logger.info(
            "promoted same-day disclosure %s.%s %s: %s → %s (row %s, Issue #87)",
            taken["symbol"], taken["market"], taken["report_date"], taken["from"],
            taken["to"], taken["id"],
        )
    if not rows:
        return FlushStats(
            dropped_disclosures=len(outcome.dropped) + collapse_drops + len(same_date.dropped),
        )
    for move in outcome.moves:
        logger.info(
            "rescheduled %s.%s FY%s Q%s: %s → %s (row %s, Issue #50)",
            move["symbol"], move["market"], move["fiscal_year"], move["fiscal_quarter"],
            move["from"], move["to"], move["id"],
        )
    from psycopg2.extras import execute_values
    columns = ", ".join(_BATCH_VALUE_COLUMNS)
    selected = ", ".join(
        f"v.{column}::{_BATCH_VALUE_CASTS[column]}" for column in _BATCH_VALUE_COLUMNS
    )
    assigned = ",\n            ".join(
        f"{column} = {expression}" for column, expression in _LONGBRIDGE_UPSERT.items()
    )
    revision = fiscal.revision_predicate(_LONGBRIDGE_UPSERT, _REVISION_COLUMNS)
    estimate_moved = fiscal.revision_predicate(_LONGBRIDGE_UPSERT, _ESTIMATE_STAMP_COLUMNS)
    actual_moved = fiscal.revision_predicate(_LONGBRIDGE_UPSERT, _ACTUAL_STAMP_COLUMNS)
    # Issue #103: the three stamps are conditional.  ``updated_at`` moves only
    # when a value a client can see moved, and each ``*_as_of`` only when its own
    # side did — a provider that re-serves the calendar must not announce a
    # revision (RFC 5545 ``SEQUENCE``) or a new collection time for a row whose
    # numbers are the same.
    insert_sql = f"""INSERT INTO earnings (symbol, market, company_name, report_date, report_type,
           fiscal_year, fiscal_quarter,
           eps_estimate, eps_actual, revenue_estimate, revenue_actual, before_after,
           estimate_currency, actual_currency, estimate_basis, actual_basis,
           estimate_as_of, actual_as_of)
        SELECT {selected},
               -- A first write *is* when the number was first seen; the conflict
               -- branch below only moves these when the number changes.
               CASE WHEN v.eps_estimate IS NOT NULL OR v.revenue_estimate IS NOT NULL
                    THEN NOW() END,
               CASE WHEN v.eps_actual IS NOT NULL OR v.revenue_actual IS NOT NULL
                    THEN NOW() END
        FROM (VALUES %s) AS v({columns})
        ON CONFLICT (symbol, market, report_date, report_type)
        DO UPDATE SET
            {assigned},
            estimate_as_of = CASE WHEN {estimate_moved} THEN NOW() ELSE earnings.estimate_as_of END,
            actual_as_of = CASE WHEN {actual_moved} THEN NOW() ELSE earnings.actual_as_of END,
            updated_at = CASE WHEN {revision} THEN NOW() ELSE earnings.updated_at END
        """
    written_rows, identity_skipped = _insert_earnings_rows(cur, insert_sql, rows)
    rows = written_rows
    keys = [(r[0], r[1], r[3]) for r in rows]
    # Issue #111: a withdrawn actual is applied after the upsert (which only ever
    # adds values, ``COALESCE``) and before the provenance UPDATE below, so the
    # row's status is recomputed from what the table now holds.
    retracted = 0
    retracted_keys: list[str] = []
    if retractions:
        cleared = execute_values(
            cur,
            _RETRACTION_SQL + """
            RETURNING e.symbol, e.market, e.report_date, e.report_type""",
            [(symbol, market, report_date, report_type, dropped["eps"], dropped["revenue"])
             for (symbol, market, report_date, report_type), dropped in retractions.items()],
            fetch=True,
        ) or []
        retracted = len(cleared)
        retracted_keys = [f"{row[0]}.{row[1]} {row[2]} {row[3]}".strip() for row in cleared]
    # Provenance and status still converge on every run (Issue #89/#91: a label
    # may not outlive its value, a row with actuals must read ``reported``), but
    # they no longer carry a timestamp: the upsert above owns the revision
    # stamps, and rewriting them here would put the run clock back on the row
    # (Issue #103).
    #
    # Issue #111 supersedes the bare "has actuals → reported" rule: a report date
    # still ahead means the event has not been disclosed, whatever the payload
    # carries, so the row stays ``scheduled`` until its day arrives.  The database's
    # ``CURRENT_DATE`` is the clock the read boundary uses too.
    execute_values(cur, """UPDATE earnings AS e SET
        date_source = 'longbridge', date_status = CASE WHEN (e.eps_actual IS NOT NULL OR e.revenue_actual IS NOT NULL) AND e.report_date <= CURRENT_DATE THEN 'reported' ELSE 'scheduled' END,
        estimate_source = CASE WHEN e.eps_estimate IS NOT NULL OR e.revenue_estimate IS NOT NULL THEN 'longbridge' ELSE e.estimate_source END,
        actual_source = CASE WHEN e.eps_actual IS NOT NULL OR e.revenue_actual IS NOT NULL THEN 'longbridge' ELSE e.actual_source END
        FROM (VALUES %s) AS v(symbol, market, report_date)
        WHERE (e.symbol,e.market,e.report_date)=(v.symbol,v.market,(v.report_date)::date)""", keys)
    execute_values(cur, """INSERT INTO earnings_estimate_snapshots (earning_id, source, eps_estimate, revenue_estimate, payload)
        SELECT e.id, 'longbridge', e.eps_estimate, e.revenue_estimate, '{"endpoint":"finance-calendar"}'::jsonb
        FROM earnings e JOIN (VALUES %s) AS v(symbol,market,report_date) ON (e.symbol,e.market,e.report_date)=(v.symbol,v.market,(v.report_date)::date)
        WHERE e.eps_estimate IS NOT NULL OR e.revenue_estimate IS NOT NULL""", keys)
    return FlushStats(rows=len(rows), moves=len(outcome.moves), skipped=len(outcome.skipped),
                      dropped_disclosures=(len(outcome.dropped) + collapse_drops
                                           + len(same_date.dropped)),
                      takeovers=sum(1 for move in outcome.moves if move.get("kind") == "takeover"),
                      identity_skipped=identity_skipped,
                      retracted=retracted,
                      retracted_keys=retracted_keys)


def _flush_pending(run_stats: SyncStats, batch: list[tuple], label: str,
                   retractions: dict | None) -> None:
    """Flush a batch with the withdrawals it carries, if it carries any.

    The extra argument is only passed when a withdrawal exists: a run that has
    nothing to retract calls ``_flush_batch`` exactly the way it always did, which
    is also the shape the stage's own tests fake.
    """
    if retractions:
        _flush_batch(run_stats, batch, label, retractions)
    else:
        _flush_batch(run_stats, batch, label)


def _count_unlabeled_keys(cur, keys: list[tuple]) -> int:
    """How many of these rows the table now holds without a fiscal identity.

    Issue #113: the count has to be *measured*, not inferred from the incoming
    tuples — a row can arrive unlabelled while the stored row already carries its
    identity (the upsert keeps it), and counting tuples over-reported 76 rows where
    the table held 36.  A diagnostic must never fail a batch, so a query error
    falls back to the incoming-tuple count and says so.
    """
    if not keys:
        return 0
    try:
        cur.execute(
            """SELECT count(*) AS n
                 FROM earnings e
                 JOIN unnest(%s::text[], %s::text[], %s::date[])
                   AS v(symbol, market, report_date)
                   ON e.symbol = v.symbol AND e.market = v.market
                  AND e.report_date = v.report_date
                WHERE e.is_predicted = FALSE AND e.fiscal_year IS NULL""",
            ([key[0] for key in keys], [key[1] for key in keys], [key[2] for key in keys]),
        )
        row = cur.fetchone()
        return int(row["n"] if isinstance(row, dict) else row[0])
    except Exception as exc:  # noqa: BLE001 - diagnostics never break a batch
        logger.warning("could not measure unlabelled rows for this batch: %s", exc)
        return 0


def _flush_batch(run_stats: SyncStats, batch: list[tuple], label: str,
                 retractions: dict | None = None) -> None:
    """Commit one batch, isolating its failure from the rest of the run.

    Issue #52: one unmovable row (a data conflict inside a 200-row batch) used to
    raise out of ``flush_batch`` and abort the whole run — killing the Longbridge
    stage, the remaining pages and every later stage of ``sync_all.sh``.  A batch
    that still fails (e.g. the database rejected something unforeseen) is counted
    and logged, then the run continues with the next batch so a single bad batch
    costs 200 records instead of the whole calendar.
    """
    unlabeled = 0
    try:
        with db_cursor() as cur:
            # Same rule as ``_flush_pending``: a batch with nothing withdrawn is
            # flushed through the three-argument call it has always used.
            flushed = flush_batch(cur, batch, retractions) if retractions else flush_batch(cur, batch)
            unlabeled = _count_unlabeled_keys(cur, [(row[0], row[1], row[3]) for row in batch])
    except SyncCancelledError:
        raise
    except Exception as exc:
        run_stats.failed_batches += 1
        logger.error(
            "  batch failed %s (%d records, fetched: %d, written: %d): %s",
            label, len(batch), run_stats.fetched, run_stats.written, exc,
        )
        return
    run_stats.written += flushed.rows
    run_stats.unlabeled += unlabeled
    run_stats.retracted += flushed.retracted
    if flushed.retracted:
        for key in flushed.retracted_keys:
            if key not in run_stats.retracted_keys and len(run_stats.retracted_keys) < 10:
                run_stats.retracted_keys.append(key)
        logger.warning(
            "  provider withdrew actual value(s) from %d row(s), e.g. %s (Issue #111)",
            flushed.retracted, ", ".join(flushed.retracted_keys[:5]),
        )
    run_stats.rescheduled += flushed.moves
    run_stats.skipped += flushed.skipped + flushed.identity_skipped
    run_stats.dropped_disclosures += flushed.dropped_disclosures
    run_stats.takeovers += flushed.takeovers
    logger.info(
        "  Flushed %d records (written: %d, fetched: %d, skipped: %d [%d held by another row], "
        "disclosures dropped: %d, takeovers: %d, unlabelled rows so far: %d)",
        flushed.rows, run_stats.written, run_stats.fetched, run_stats.skipped,
        flushed.identity_skipped, run_stats.dropped_disclosures, run_stats.takeovers,
        run_stats.unlabeled,
    )


def run_terminal_state(stats: SyncStats) -> tuple[str, str | None]:
    """Terminal ``sync_runs`` status/error_code for a finished Longbridge run.

    Issue #52: batch-level isolation means a run can finish *without* raising and
    still have written nothing (every batch failed).  Such a run must not be
    recorded as ``success``.  A run where only some batches failed stays
    ``success`` with ``details.status='partial'`` (see :meth:`SyncStats.quality`)
    so the remaining stages of ``sync_all.sh`` still run.

    Issue #104: the stage can also finish having seen calendar pages but resolved
    *no* event — what happens when the CLI changes its payload shape (0.31.0
    dropped ``counter_id``).  Every event was skipped, ``fetched`` stayed 0 and
    the run was still recorded ``success``, so ``/api/admin/health`` called a
    frozen calendar healthy for three days.  Such a run is a failure with its own
    code, not an empty-but-fine run.
    """
    if stats.failed_batches and stats.written == 0:
        return ("failed", "longbridge_batch_failed")
    if stats.pages > 0 and stats.resolved() == 0:
        return ("failed", "longbridge_no_rows")
    return ("success", None)


def sync_earnings(run_id: int, stats: SyncStats | None = None) -> SyncStats:
    """Full sync with wide date range, batched inserts.

    ``run_id`` is polled at each market/batch checkpoint so an admin cancel
    stops the job promptly instead of burning the full API budget (Issue #28).

    Returns the run's :class:`SyncStats`: what was fetched, what each committed
    batch actually wrote, and the reschedule conflicts the identity guards
    skipped (Issue #52) — so the audit row reflects the run instead of a
    hard-coded zero.  ``stats`` may be supplied by the caller to keep the
    counters of a run that ends in an exception.
    """
    today = date.today()
    start = (today - timedelta(days=180)).isoformat()
    end = (today + timedelta(days=365)).isoformat()

    run_stats = stats if stats is not None else SyncStats()

    # Issue #78: the audit row must age from real progress, not from its start
    # time — the reaper judges it by `timeout_seconds` (900s here).  Throttled to
    # one write per minute, not one per page.
    beats = HeartbeatThrottle()

    # Issue #113: derive a fiscal year from the symbol's own annual report rather
    # than from the calendar month.  ``ClosingHistory`` reads the table on first
    # use; a symbol with no annual report keeps the month heuristic and is counted
    # in ``unlabeled``.
    closings = ClosingHistory()

    for market in ["US", "HK"]:
        check_cancelled(run_id)
        logger.info(f"=== Fetching {market} earnings [{start} → {end}] ===")
        pages = fetch_calendar(market, start, end)
        run_stats.pages += len(pages)
        logger.info(f"  Total pages received: {len(pages)}")
        canonical_periods = build_fiscal_period_index(pages, closings)

        batch = []
        #: Actual figures this market's pages explicitly withdrew, keyed by the
        #: display key so the batch's own flush can apply them (Issue #111).
        retractions: dict[tuple[str, str, str, str], dict] = {}
        for page in pages:
            check_cancelled(run_id)
            beats.maybe(run_id, phase=f"calendar-{market.lower()}",
                        current=run_stats.fetched)
            for info in page.get("infos", []):
                run_stats.events += 1
                symbol, mkt = from_lb_calendar_info(info)
                if not symbol:
                    # Issue #104: never drop an event silently.  A provider that
                    # renames its identity field turns every event into this
                    # branch, and the run must be able to say so.
                    run_stats.unresolved += 1
                    continue

                report_date = resolve_report_date(info)
                if not report_date:
                    run_stats.unresolved += 1
                    continue

                company_name = info.get("counter_name", "")
                date_type = parse_date_type(info.get("date_type", ""))
                kv = extract_kv(info.get("data_kv", []))
                # Issue #61: the calendar states the currency of the figures in
                # this event; an unstated/unsupported value is recorded as the
                # explicit ``unknown`` marker rather than assumed.
                currency = normalize_currency(info.get("currency"))

                ext = info.get("ext", {}).get("financial_report", {})
                fiscal_year, fiscal_quarter, skip_event = fiscal_period_for_event(
                    symbol, mkt, report_date, ext, canonical_periods, closings
                )
                if skip_event:
                    continue
                # Issue #50 follow-up: record which event sequence the row came
                # from, so a period's release row is distinguishable from a
                # half-year/annual disclosure of the same period.
                report_type = fiscal.report_type_for_period_type(ext.get("period_type"))

                # Issue #111: a withdrawal names the row it belongs to — symbol,
                # market, date *and* report type.  Keying it by the day alone let a
                # blank on one event clear a same-day row of another report type.
                withdrawn = extract_retractions(info.get("data_kv"))
                if withdrawn["eps"] or withdrawn["revenue"]:
                    retractions[(symbol, mkt, report_date, report_type)] = withdrawn

                batch.append((
                    symbol, mkt, company_name, report_date, report_type,
                    fiscal_year, fiscal_quarter,
                    kv.get("eps_estimate"), kv.get("eps_actual"),
                    kv.get("revenue_estimate"), kv.get("revenue_actual"),
                    date_type,
                    # Issue #61: the calendar event states the currency its figures
                    # are in.  The estimate slot carries it whenever the event
                    # declares one — not only when this event also carries the
                    # estimate value: a period's estimate is written once and then
                    # re-served by events that only repeat the date, so gating on
                    # "this event has an estimate" left 34% of US rows with a
                    # stored estimate and no currency to compare it by (the
                    # upsert below decides whether the label applies to *this*
                    # row).  The actual slot keeps its gate: a Longbridge actual
                    # that Futu wrote must keep Futu's own reporting currency.
                    currency,
                    currency if (kv.get("eps_actual") is not None or kv.get("revenue_actual") is not None) else None,
                    UNKNOWN_ATTRIBUTION,
                    UNKNOWN_ATTRIBUTION,
                ))
                run_stats.fetched += 1

                # Flush when batch is full
                if len(batch) >= BATCH_SIZE:
                    _flush_pending(run_stats, batch, f"{market} page batch", retractions)
                    batch = []
                    retractions = {}
                    check_cancelled(run_id)

        # Flush remaining
        if batch:
            _flush_pending(run_stats, batch, f"{market} final batch", retractions)

    if run_stats.unresolved:
        # Not a failure by itself (some events legitimately carry no usable
        # date), but a payload that resolves nothing is exactly the Issue #104
        # incident — say it in one line instead of one line per event.
        logger.warning(
            "%d of %d calendar events could not be resolved (symbol/date); "
            "%d resolved across %d page(s) — details.unresolved carries the count",
            run_stats.unresolved, run_stats.events, run_stats.resolved(),
            run_stats.pages,
        )

    logger.info(
        "=== Sync complete: %d events in %d page(s), %d unresolved; "
        "%d records fetched, %d written, %d rescheduled, "
        "%d reschedule conflicts, %d failed batches ===",
        run_stats.events, run_stats.pages, run_stats.unresolved,
        run_stats.fetched, run_stats.written, run_stats.rescheduled,
        run_stats.skipped, run_stats.failed_batches,
    )
    return run_stats


if __name__ == "__main__":
    from app.db import init_db
    from app.sync_audit import (
        start_run, finish_run, advisory_lock,
        SyncCancelledError, LOCK_LONGBRIDGE_EARNINGS,
    )
    init_db()
    with advisory_lock(LOCK_LONGBRIDGE_EARNINGS) as acquired:
        if not acquired:
            logger.info("longbridge earnings sync locked by another process, skipping")
            sys.exit(0)
        run_id = start_run("longbridge", "longbridge",
                           idempotency_key="longbridge:earnings:full",
                           timeout_seconds=stage_timeout("longbridge"),
                           symbol_count=0)
        if run_id is None:
            logger.info("longbridge earnings sync already running, skipping")
            sys.exit(0)
        run_stats = SyncStats()
        try:
            sync_earnings(run_id, run_stats)
            status, error_code = run_terminal_state(run_stats)
            finish_run(run_id, status=status, error_code=error_code,
                       record_count=run_stats.written,
                       details=run_stats.quality().to_dict())
            if status == "failed":
                # The stage is recorded as failed even though no exception
                # escaped sync_earnings.  It also exits non-zero: `sync_all.sh`
                # runs every stage regardless of a predecessor's exit code (all
                # five `run_stage` calls are unconditional), so a non-zero code
                # costs nothing downstream and makes `cron_sync.sh` report what
                # actually happened instead of a green pipeline over a stage
                # that wrote nothing (Issue #52, Issue #104).
                logger.error(
                    "longbridge earnings sync FAILED (%s): %d page(s), %d event(s), "
                    "%d unresolved, %d written, %d failed batch(es)",
                    error_code, run_stats.pages, run_stats.events,
                    run_stats.unresolved, run_stats.written,
                    run_stats.failed_batches,
                )
                sys.exit(1)
            elif run_stats.failed_batches:
                # A partial run is not a green run: the details carry
                # status='partial' (SyncQuality.classify) for the admin.
                logger.warning(
                    "longbridge earnings sync finished with %d failed batch(es): "
                    "%d written, %d skipped",
                    run_stats.failed_batches, run_stats.written, run_stats.skipped,
                )
        except SyncCancelledError:
            # Admin cancelled this run; keep the terminal 'cancelled' state.
            finish_run(run_id, status="cancelled", error_code="cancelled_by_admin")
            logger.warning("longbridge earnings sync cancelled by admin; stopping")
            sys.exit(1)
        except Exception:
            # Report what the run did before it died.  Issue #52's incident wrote
            # ~10.8k rows and recorded ``fetched:0, written:0`` — the counters now
            # follow the batches that actually committed, so a mid-run failure
            # stays auditable instead of looking like a no-op.
            run_stats.failed_batches = max(run_stats.failed_batches, 1)
            finish_run(run_id, status="failed", error_code="longbridge_sync_failed",
                       record_count=run_stats.written,
                       details=run_stats.quality().to_dict())
            raise
