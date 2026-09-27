"""Regression tests for predict_earnings (Issues #2, #54, #60).

Issue #60: the predicted date
-----------------------------
``predict_for_symbol`` used to build a date out of independently taken medians —
``int(median([month]))``, ``int(median([day]))`` — which averages a *cyclic*
quantity (samples in March and August gave ``5.5`` → May, a month the company
never reported in) and has no notion of weekday (so 43 production predictions
sat on a Saturday/Sunday).  The rule now *chooses one of the company's own
historical report dates for that quarter*, projected onto the target fiscal
year, ranked against the median expectation, with a weekday guard; the write
path re-dates the period's superseded prediction instead of leaving the old
(later) date to be displayed.

The symbol table
----------------
``normalize()`` is the single source of truth for HK codes, and it is an
*identity* mapping for legitimate five-digit codes (``8xxxx`` RMB counters such
as ``82333.HK`` = the RMB counter of ``2333.HK``).  Issue #54 covers the cleanup
step that ignored that: ``merge_duplicate_symbols()`` selected every five-digit
code, copied the rows onto their "canonical" symbol and then deleted the source
rows — for a symbol that canonicalises to itself the copy is a no-op and the
delete is pure data loss (production: 25 symbols / 51 rows / 31 estimate
snapshots, every prediction run).

What is pinned below
--------------------
* the identity predicate (``app.symbol.is_dirty_hk_5digit``);
* the cleanup's *state* effect, replayed on ``_ModelCursor`` — an in-memory
  model of the exact statements the step issues — so a legitimate five-digit
  symbol provably keeps its row count and its ids, dirty five-digit codes are
  merged without duplicating a period, and estimate snapshots follow their row
  instead of cascading away;
* the guards that make a self-merge impossible (identity check + the SQL proof
  on the DELETE), the audit counters, idempotency, dry-run and the snapshot
  collision path that keeps a row rather than losing history.

``_ModelCursor`` mirrors production semantics rather than reading them from a
database: the SQL text is asserted separately (provenance columns, the DELETE
proof, re-point-before-delete ordering).
"""
import importlib.util
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.symbol import is_dirty_hk_5digit, normalize  # noqa: E402


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


predict_earnings = _load("predict_earnings_issue54", "scripts/predict_earnings.py")


def test_five_digit_hk_normalizes_to_four_digit():
    """00700.HK → 0700.HK, 00005.HK → 0005.HK"""
    assert normalize("00700", "HK") == "0700.HK"
    assert normalize("00005", "HK") == "0005.HK"
    assert normalize("00001", "HK") == "0001.HK"
    assert normalize("09988", "HK") == "9988.HK"


def test_four_digit_hk_unchanged():
    """Already canonical codes stay canonical."""
    assert normalize("0700", "HK") == "0700.HK"
    assert normalize("0005", "HK") == "0005.HK"
    assert normalize("9988", "HK") == "9988.HK"


def test_two_and_three_digit_hk_pad_to_four():
    """Short codes are zero-padded."""
    assert normalize("700", "HK") == "0700.HK"
    assert normalize("5", "HK") == "0005.HK"
    assert normalize("1", "HK") == "0001.HK"


def test_old_lstrip_8_would_have_broken():
    """Demonstrate the old bug: lstrip('8') corrupts codes starting with 8.

    Old logic: (sym.split('.')[0].lstrip('8') or '0').zfill(4) + '.HK'
    For 00823.HK → lstrip('8') on '00823' → '00823' (no change, '8' not leading)
    But for 80000.HK → lstrip('8') on '80000' → '000' → zfill(4) → '0000' → '0000.HK'

    The new normalize() handles all cases correctly.
    """
    # 80000 is a valid HK code (e.g. HSI futures proxy)
    assert normalize("80000", "HK") == "80000.HK"
    # Old code would have produced '0000.HK' — wrong!
    old_result = ("80000".lstrip("8") or "0").zfill(4) + ".HK"
    assert old_result == "0000.HK"  # proves the bug existed


# ── Issue #54: a five-digit code that is already canonical is not a duplicate ─

def test_legitimate_five_digit_codes_are_not_dirty():
    """8xxxx RMB counters (and every other non-zero five-digit code) are identity."""
    for code in ("82333.HK", "80000.HK", "89988.HK", "12345.HK", "80000"):
        assert is_dirty_hk_5digit(code) is False, code


def test_zero_padded_five_digit_codes_are_dirty():
    for code in ("00700.HK", "00005.HK", "09988.HK", "00700"):
        assert is_dirty_hk_5digit(code) is True, code


def test_canonical_and_irregular_codes_are_not_dirty():
    """Only the provider's zero-padded five-digit spelling needs a rename."""
    for code in ("0700.HK", "0005.HK", "700.HK", "AAPL", ""):
        assert is_dirty_hk_5digit(code) is False, code


def _ts(day: int) -> datetime:
    return datetime(2026, 9, day, tzinfo=timezone.utc)


def _row(row_id: int, symbol: str, market: str, report_date: date,
         report_type: str = "Q", **values):
    row = {
        "id": row_id, "symbol": symbol, "market": market, "report_date": report_date,
        "report_type": report_type, "eps_estimate": None, "eps_actual": None,
        "revenue_estimate": None, "revenue_actual": None, "is_predicted": False,
        "date_source": "longbridge", "date_status": "scheduled", "actual_source": None,
        "estimate_source": None, "estimate_as_of": None, "estimate_currency": None,
        "estimate_basis": None, "actual_as_of": None, "company_name": "",
    }
    row.update(values)
    return row


class _ModelCursor:
    """In-memory stand-in for PostgreSQL, replaying the statements the step issues.

    Every statement shape comes from the module constants, so an unexpected query
    fails the test loudly instead of silently returning nothing.
    """

    def __init__(self, earnings, snapshots):
        self.earnings = [dict(row) for row in earnings]
        self.snapshots = [dict(snap) for snap in snapshots]
        self.statements = []
        self.rowcount = 0
        self._rows = []
        self._next_id = max([row["id"] for row in self.earnings] or [0]) + 1

    # ── cursor API ──────────────────────────────────────────────────────────
    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.statements.append(flat)
        self._rows = []
        self.rowcount = 0
        handler = self._dispatch(flat)
        handler(params or ())

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def _new_id(self):
        self._next_id += 1
        return self._next_id - 1

    def _dispatch(self, flat):
        if flat.startswith("SELECT DISTINCT symbol FROM earnings WHERE symbol LIKE"):
            return self._select_us_duplicates
        if flat.startswith("SELECT DISTINCT symbol FROM earnings WHERE symbol ~"):
            return self._select_hk_five_digit
        if flat.startswith("SELECT id, report_date, report_type FROM earnings WHERE symbol"):
            return self._select_rows_of_symbol
        if flat == " ".join(predict_earnings._MERGE_UPSERT.split()):
            return self._merge_upsert
        if flat == " ".join(predict_earnings._SNAPSHOT_REPOINT.split()):
            return self._snapshot_repoint
        if flat == " ".join(predict_earnings._SNAPSHOT_COUNT.split()):
            return self._snapshot_count
        if flat == " ".join(predict_earnings._DELETE_ROWS.split()):
            return self._delete_rows
        raise AssertionError(f"merge_duplicate_symbols issued an unexpected statement: {flat}")

    # ── statements ──────────────────────────────────────────────────────────
    def _select_us_duplicates(self, params):
        self._rows = [{"symbol": symbol} for symbol in self._distinct_symbols(
            lambda row: row["market"] == "US" and row["symbol"].endswith(".US"))]

    def _select_hk_five_digit(self, params):
        self._rows = [{"symbol": symbol} for symbol in self._distinct_symbols(
            lambda row: re.fullmatch(r"\d{5}\.HK", row["symbol"]) is not None)]

    def _distinct_symbols(self, predicate):
        """``SELECT DISTINCT symbol`` — one entry per symbol, in row order."""
        seen, symbols = set(), []
        for row in self.earnings:
            if predicate(row) and row["symbol"] not in seen:
                seen.add(row["symbol"])
                symbols.append(row["symbol"])
        return symbols

    def _select_rows_of_symbol(self, params):
        symbol, market = params
        self._rows = [{"id": row["id"], "report_date": row["report_date"],
                       "report_type": row["report_type"]}
                      for row in self.earnings
                      if row["symbol"] == symbol and row["market"] == market]

    def _merge_upsert(self, params):
        canonical, dirty, market = params
        kept_rows = []
        for row in [r for r in self.earnings if r["symbol"] == dirty and r["market"] == market]:
            existing = next((r for r in self.earnings
                             if r["symbol"] == canonical and r["market"] == market
                             and r["report_date"] == row["report_date"]
                             and r["report_type"] == row["report_type"]), None)
            if existing is None:
                # A new canonical row is born with the source row's values.
                existing = dict(row, symbol=canonical, id=self._new_id())
                self.earnings.append(existing)
            else:
                # Only fill what the canonical row does not know yet; provenance
                # travels with the row, a confirmed row is never made predicted.
                for field in ("eps_estimate", "eps_actual", "revenue_estimate", "revenue_actual",
                              "before_after", "estimate_source", "estimate_as_of",
                              "estimate_currency", "estimate_basis", "actual_source"):
                    if existing.get(field) is None:
                        existing[field] = row.get(field)
                for field in ("date_source", "date_status"):
                    if existing.get(field) in (None, "unknown", "scheduled", "algorithm", "predicted") \
                            and row.get(field) not in (None, "unknown", "scheduled", "algorithm", "predicted"):
                        existing[field] = row.get(field)
                existing["is_predicted"] = bool(existing.get("is_predicted")) and bool(row.get("is_predicted"))
            kept_rows.append({"id": existing["id"], "report_date": row["report_date"],
                              "report_type": row["report_type"]})
        self._rows = kept_rows

    def _snapshot_repoint(self, params):
        kept_id, old_id, _ = params
        taken = {(snap["source"], snap["captured_at"]) for snap in self.snapshots
                 if snap["earning_id"] == kept_id}
        moved = 0
        for snap in self.snapshots:
            marker = (snap["source"], snap["captured_at"])
            if snap["earning_id"] == old_id and marker not in taken:
                snap["earning_id"] = kept_id
                taken.add(marker)
                moved += 1
        self.rowcount = moved

    def _snapshot_count(self, params):
        self._rows = [{"n": sum(1 for snap in self.snapshots if snap["earning_id"] == params[0])}]

    def _delete_rows(self, params):
        ids, symbol, market, canonical = params
        if symbol == canonical:                      # the SQL proof holds the line
            return
        keep = []
        for row in self.earnings:
            if row["id"] in ids and row["symbol"] == symbol and row["market"] == market:
                self.rowcount += 1
                continue
            keep.append(row)
        self.earnings = keep

    # ── helpers ─────────────────────────────────────────────────────────────
    def rows(self, symbol=None, market=None):
        return [row for row in self.earnings
                if (symbol is None or row["symbol"] == symbol)
                and (market is None or row["market"] == market)]

    def snapshots_of(self, earning_id):
        return [snap for snap in self.snapshots if snap["earning_id"] == earning_id]

    def writes(self):
        return [sql for sql in self.statements
                if sql.startswith(("INSERT", "UPDATE", "DELETE"))]


def _run_merge_on(cursor, dry_run=False):
    """Run the cleanup against an existing model cursor (so state accumulates)."""
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    with mock.patch.object(predict_earnings, "db_cursor", return_value=ctx):
        stats = predict_earnings.merge_duplicate_symbols(dry_run=dry_run)
    return stats


def _run_merge(earnings, snapshots, dry_run=False):
    """Run the cleanup once on a fresh in-memory database; returns (stats, cursor)."""
    cursor = _ModelCursor(earnings, snapshots)
    return _run_merge_on(cursor, dry_run=dry_run), cursor


def _hk_counter_fixture():
    """The production shape: a legit 82333.HK next to genuinely dirty 00700.HK.

    ``82333.HK`` is the RMB counter of ``2333.HK`` and must survive untouched;
    ``00700.HK`` is a provider's zero-padded ``0700.HK`` and must be merged.
    """
    earnings = [
        _row(1, "82333.HK", "HK", date(2026, 8, 26), revenue_estimate=120.0),
        _row(2, "82333.HK", "HK", date(2026, 3, 24), eps_actual=0.5),
        _row(3, "0700.HK", "HK", date(2026, 5, 13), company_name="Tencent"),
        _row(4, "00700.HK", "HK", date(2026, 8, 12), revenue_estimate=100.0),
        _row(5, "00700.HK", "HK", date(2026, 5, 13), eps_actual=2.5,
             date_source="longbridge", actual_source="longbridge"),
    ]
    snapshots = [
        {"id": 100, "earning_id": 1, "source": "longbridge", "captured_at": _ts(1)},
        {"id": 101, "earning_id": 2, "source": "longbridge", "captured_at": _ts(2)},
        {"id": 102, "earning_id": 5, "source": "longbridge", "captured_at": _ts(3)},
    ]
    return earnings, snapshots


class LegitimateFiveDigitHkTests(TestCase):
    """Issue #54, acceptance criterion 1/2: the deletion path must be gone."""

    def test_legit_rows_keep_their_count_and_their_ids(self):
        earnings, snapshots = _hk_counter_fixture()
        before_ids = sorted(row["id"] for row in earnings if row["symbol"] == "82333.HK")

        stats, cursor = _run_merge(earnings, snapshots)

        after = cursor.rows("82333.HK")
        self.assertEqual(sorted(row["id"] for row in after), before_ids)
        self.assertEqual(len(after), 2)
        self.assertEqual(stats.skipped, 1)
        # Nothing was written for the legitimate symbol and no snapshot moved.
        self.assertEqual(cursor.snapshots_of(1)[0]["earning_id"], 1)
        self.assertEqual(cursor.snapshots_of(2)[0]["earning_id"], 2)

    def test_legit_symbol_alone_produces_no_write_at_all(self):
        """The old code deleted exactly this fixture on every prediction run."""
        earnings = [_row(1, "82333.HK", "HK", date(2026, 8, 26)),
                    _row(2, "82333.HK", "HK", date(2026, 3, 24))]
        stats, cursor = _run_merge(earnings, [])

        self.assertEqual(cursor.writes(), [])
        self.assertEqual((stats.moved, stats.deleted, stats.skipped), (0, 0, 1))
        self.assertEqual(len(cursor.earnings), 2)

    def test_a_self_merge_never_reaches_the_delete(self):
        """Even called directly, a symbol that is its own canonical form is a no-op."""
        cursor = _ModelCursor([_row(1, "82333.HK", "HK", date(2026, 8, 26))], [])
        stats = predict_earnings.MergeStats()
        predict_earnings.merge_symbol_onto_canonical(
            cursor, "82333.HK", "HK", "82333.HK", stats)
        self.assertEqual(cursor.statements, [])
        self.assertEqual((stats.moved, stats.deleted, stats.skipped), (0, 0, 1))

    def test_delete_carries_the_self_merge_proof(self):
        sql = " ".join(predict_earnings._DELETE_ROWS.split())
        self.assertIn("symbol <> %s", sql)
        self.assertIn("id = ANY(%s)", sql)      # only rows whose data was moved


class DirtySymbolMergeTests(TestCase):
    """Issue #54, acceptance criteria 1-3: merge without duplication or loss."""

    def test_dirty_rows_are_merged_onto_the_canonical_symbol(self):
        earnings, snapshots = _hk_counter_fixture()
        stats, cursor = _run_merge(earnings, snapshots)

        self.assertEqual(cursor.rows("00700.HK"), [])
        canonical = cursor.rows("0700.HK")
        keys = [(row["report_date"], row["report_type"]) for row in canonical]
        self.assertEqual(len(keys), len(set(keys)))            # no duplicated period
        self.assertEqual(sorted(keys), [(date(2026, 5, 13), "Q"), (date(2026, 8, 12), "Q")])

    def test_merged_row_keeps_the_existing_ids_and_fills_the_gaps(self):
        earnings, snapshots = _hk_counter_fixture()
        _, cursor = _run_merge(earnings, snapshots)

        by_date = {row["report_date"]: row for row in cursor.rows("0700.HK")}
        kept = by_date[date(2026, 5, 13)]
        self.assertEqual(kept["id"], 3)                        # the row the user already had
        self.assertEqual(kept["company_name"], "Tencent")      # existing values win
        self.assertEqual(float(kept["eps_actual"]), 2.5)       # the dirty row filled the gap
        self.assertEqual(by_date[date(2026, 8, 12)]["revenue_estimate"], 100.0)

    def test_estimate_snapshots_follow_their_row_instead_of_cascading(self):
        earnings, snapshots = _hk_counter_fixture()
        before = len(snapshots)
        stats, cursor = _run_merge(earnings, snapshots)

        self.assertEqual(len(cursor.snapshots), before)         # nothing cascaded away
        self.assertEqual(stats.snapshots_moved, 1)
        self.assertEqual([snap["id"] for snap in cursor.snapshots_of(3)], [102])
        orphan_ids = [snap["id"] for snap in cursor.snapshots
                      if not cursor.rows() or snap["earning_id"] not in
                      {row["id"] for row in cursor.earnings}]
        self.assertEqual(orphan_ids, [])

    def test_second_run_is_idempotent_and_deletes_nothing(self):
        cursor = _ModelCursor(*_hk_counter_fixture())
        first = _run_merge_on(cursor)
        rows_after_first = len(cursor.earnings)

        seen = len(cursor.statements)
        stats = _run_merge_on(cursor)
        second_run = cursor.statements[seen:]

        self.assertEqual(first.deleted, 2)
        self.assertEqual(stats.deleted, 0)
        self.assertEqual(stats.moved, 0)
        self.assertEqual(stats.skipped, 1)                      # 82333.HK, still legit
        self.assertEqual([sql for sql in second_run
                          if sql.startswith(("INSERT", "UPDATE", "DELETE"))], [])
        self.assertEqual(len(cursor.earnings), rows_after_first)

    def test_dry_run_reports_without_writing(self):
        earnings, snapshots = _hk_counter_fixture()
        ids_before = [row["id"] for row in earnings]
        stats, cursor = _run_merge(earnings, snapshots, dry_run=True)

        self.assertEqual(cursor.writes(), [])
        self.assertEqual([row["id"] for row in cursor.earnings], ids_before)
        self.assertEqual(sorted({row["symbol"] for row in cursor.earnings}),
                         ["00700.HK", "0700.HK", "82333.HK"])
        self.assertEqual((stats.moved, stats.deleted), (2, 0))

    def test_us_dotted_symbols_still_merge(self):
        earnings = [_row(1, "AAPL.US", "US", date(2026, 10, 29)),
                    _row(2, "AAPL", "US", date(2027, 1, 28))]
        snapshots = [{"id": 7, "earning_id": 1, "source": "longbridge", "captured_at": _ts(4)}]
        stats, cursor = _run_merge(earnings, snapshots)

        self.assertEqual([row["symbol"] for row in cursor.rows("AAPL.US")], [])
        self.assertEqual(len(cursor.rows("AAPL")), 2)
        # The snapshot follows its data onto an existing AAPL row — never orphaned.
        survivor = [row["id"] for row in cursor.rows("AAPL") if row["id"] != 1]
        self.assertEqual(len(cursor.snapshots), 1)
        self.assertIn(cursor.snapshots[0]["earning_id"], survivor)
        self.assertEqual((stats.moved, stats.deleted), (1, 1))

    def test_a_snapshot_collision_keeps_the_row_instead_of_losing_history(self):
        """Two rows holding the same snapshot identity may not be collapsed by delete."""
        captured = _ts(5)
        earnings = [_row(1, "0700.HK", "HK", date(2026, 5, 13)),
                    _row(2, "00700.HK", "HK", date(2026, 5, 13), eps_actual=2.5)]
        snapshots = [
            {"id": 10, "earning_id": 1, "source": "longbridge", "captured_at": captured},
            {"id": 11, "earning_id": 2, "source": "longbridge", "captured_at": captured},
        ]
        stats, cursor = _run_merge(earnings, snapshots)

        self.assertEqual(len(cursor.snapshots), 2)
        self.assertEqual([snap["id"] for snap in cursor.snapshots_of(2)], [11])
        self.assertEqual(stats.blocked, 1)
        self.assertEqual(stats.deleted, 0)
        self.assertEqual(len(cursor.rows("00700.HK")), 1)       # kept, not silently deleted

    def test_audit_counters_summarise_the_run(self):
        earnings, snapshots = _hk_counter_fixture()
        stats, _ = _run_merge(earnings, snapshots)
        self.assertEqual((stats.moved, stats.deleted, stats.skipped), (2, 2, 1))
        self.assertIn("deleted=2", stats.summary())
        self.assertIn("snapshots_moved=1", stats.summary())
        self.assertTrue(stats.changed)

    def test_a_run_that_only_skips_still_logs_its_counters(self):
        """Issue #54 criterion 4: the production log must show the whole picture."""
        earnings = [_row(1, "82333.HK", "HK", date(2026, 8, 26))]
        with mock.patch.object(predict_earnings.logger, "info") as info:
            stats, _ = _run_merge(earnings, [])
        messages = [call.args[0] for call in info.call_args_list]
        self.assertFalse(stats.changed)
        self.assertTrue(
            any("moved=0" in message and "skipped=1" in message and "deleted=0" in message
                for message in messages),
            messages,
        )


class MergeSqlContractTests(TestCase):
    """The SQL that keeps the invariant must actually carry it."""

    def test_snapshots_are_repointed_before_the_row_is_deleted(self):
        earnings, snapshots = _hk_counter_fixture()
        _, cursor = _run_merge(earnings, snapshots)
        repoint = next(i for i, sql in enumerate(cursor.statements)
                       if sql.startswith("UPDATE earnings_estimate_snapshots"))
        delete = next(i for i, sql in enumerate(cursor.statements)
                      if sql.startswith("DELETE FROM earnings"))
        self.assertLess(repoint, delete)

    def test_merge_upsert_carries_provenance_and_freezes_identity(self):
        sql = " ".join(predict_earnings._MERGE_UPSERT.split())
        for column in ("date_source", "date_status", "estimate_source", "estimate_as_of",
                       "estimate_currency", "estimate_basis", "actual_source", "actual_as_of"):
            self.assertIn(column, sql)
        # Issues #50/#52 rules hold for a rename too.
        self.assertIn("fiscal_year = CASE WHEN earnings.fiscal_year IS NULL", sql)
        self.assertIn("fiscal_quarter = CASE WHEN earnings.fiscal_quarter IS NULL", sql)
        self.assertNotIn("is_predicted = EXCLUDED.is_predicted", sql)

    def test_only_selects_run_while_scanning_candidates(self):
        """The scan itself is read-only; writes happen per dirty symbol."""
        cursor = _ModelCursor([_row(1, "82333.HK", "HK", date(2026, 8, 26))], [])
        predict_earnings.merge_symbol_onto_canonical(
            cursor, "00700.HK", "HK", "0700.HK", predict_earnings.MergeStats())
        # No rows for the dirty symbol → the scan was the only statement.
        self.assertEqual([sql for sql in cursor.statements if not sql.startswith("SELECT")], [])


# ── Issue #60: the predicted date comes from the company's own history ───────

def _sample(day: date, fiscal_year: int, before_after: str = "after") -> dict:
    """One confirmed same-quarter report date, as ``predict_for_symbol`` builds it."""
    return {
        "date": day, "year": day.year, "month": day.month, "day": day.day,
        "year_offset": day.year - fiscal_year, "before_after": before_after,
    }


class DateChoiceTests(TestCase):
    """Acceptance criteria 1-3 of Issue #60, on the pure date rule."""

    def test_march_and_august_samples_never_predict_may(self):
        """Production 0300.HK FY2027Q4: the median month used to invent May."""
        samples = [_sample(date(2026, 3, 30), 2026), _sample(date(2026, 8, 29), 2026)]

        predicted = predict_earnings.pick_predicted_date(samples, 2027)

        self.assertEqual(predicted, date(2027, 3, 30))
        self.assertIn(predicted.month, {3, 8})
        # The legacy rule's output is only an anchor now, never the result.
        self.assertNotEqual(predicted, date(2027, 5, 29))

    def test_predicted_month_is_always_an_observed_month(self):
        # Every sample month must survive the projection, including a cross-year Q4.
        for months in ([(3, 30), (8, 29)], [(2, 26), (2, 27), (3, 26)], [(12, 20), (1, 9)]):
            samples = [_sample(date(2025, m, d), 2025) for m, d in months]
            predicted = predict_earnings.pick_predicted_date(samples, 2026)
            self.assertIn(predicted.month, {m for m, _ in months}, predicted)

    def test_weekend_candidate_is_replaced_by_a_date_the_company_used(self):
        """Acceptance criterion 2: the projected weekend winner is not kept.

        Both samples are Fridays and Saturdays in 2026, so one year later the
        projection lands on Sunday (2027-08-15) and Friday (2027-08-20); the Sunday
        is nearer to the median anchor but is not a date the company used.
        """
        samples = [_sample(date(2026, 8, 15), 2026), _sample(date(2026, 8, 20), 2026)]

        predicted = predict_earnings.pick_predicted_date(samples, 2027)

        self.assertEqual(predicted, date(2027, 8, 20))
        self.assertNotIn(predicted.weekday(), predict_earnings.WEEKEND_DAYS)

    def test_weekend_only_history_falls_back_to_the_nearest_weekday(self):
        """HK does report on some Saturdays (3% of confirmed rows) — but a projection
        that lands on a weekend is moved to the nearest weekday the company used."""
        samples = [_sample(date(2025, 8, 30), 2025), _sample(date(2026, 8, 29), 2026)]

        predicted = predict_earnings.pick_predicted_date(samples, 2027)

        # Projected candidates: Sunday 2027-08-29 (nearest to the anchor) and Monday
        # 2027-08-30 — the weekday one wins, still inside the observed month.
        self.assertEqual(predicted, date(2027, 8, 30))
        self.assertNotIn(predicted.weekday(), predict_earnings.WEEKEND_DAYS)
        self.assertEqual(predicted.month, 8)

    def test_the_weekday_fallback_never_leaves_the_sample_month(self):
        # 2027-05-01 is a Saturday: Friday would be April, so Monday is used.
        self.assertEqual(predict_earnings._nearest_weekday(date(2027, 5, 1)), date(2027, 5, 3))
        # 2027-02-28 is a Sunday: Monday would be March, so Friday is used.
        self.assertEqual(predict_earnings._nearest_weekday(date(2027, 2, 28)), date(2027, 2, 26))
        self.assertEqual(predict_earnings._nearest_weekday(date(2027, 5, 6)), date(2027, 5, 6))

    def test_weekend_fallback_stays_in_the_sample_month_even_with_a_year_away_candidate(self):
        """0300.HK FY2027Q4: samples of two different fiscal years.

        The projections are March 2028 (a weekday, because that sample carries
        ``year_offset = 1``) and Sunday 2027-08-29.  Taking the 2028 date would push
        the prediction past the 420-day horizon, so the rule stays in the month it
        chose and moves to the adjacent weekday instead.
        """
        samples = [_sample(date(2026, 3, 30), 2025), _sample(date(2026, 8, 29), 2026)]

        predicted = predict_earnings.pick_predicted_date(samples, 2027)

        self.assertEqual(predicted, date(2027, 8, 30))
        self.assertNotIn(predicted.weekday(), predict_earnings.WEEKEND_DAYS)

    def test_february_29_is_clamped(self):
        """Acceptance criterion 3③: a leap-day sample projects onto a legal date."""
        samples = [_sample(date(2024, 2, 29), 2024), _sample(date(2023, 2, 28), 2023)]

        predicted = predict_earnings.pick_predicted_date(samples, 2025)

        self.assertEqual(predicted, date(2025, 2, 28))

    def test_same_input_produces_the_same_date_every_run(self):
        """Acceptance criterion 3④: no ±1 day drift between runs."""
        samples = [_sample(date(2025, 1, 30), 2025), _sample(date(2026, 1, 29), 2026)]

        first = predict_earnings.pick_predicted_date(samples, 2027)
        second = predict_earnings.pick_predicted_date(samples, 2027)

        self.assertEqual(first, second)
        self.assertEqual(first, date(2027, 1, 29))
        # The output is exactly one of the projected samples — never an average.
        projected = {date(2027, 1, 29), date(2027, 1, 30)}
        self.assertIn(first, projected)

    def test_only_the_four_most_recent_samples_are_candidates(self):
        samples = [_sample(date(2020, 9, 9), 2020)] + [_sample(date(y, 4, 24), y)
                                                       for y in (2022, 2023, 2024, 2025)]

        predicted = predict_earnings.pick_predicted_date(samples, 2026)

        self.assertEqual(predicted, date(2026, 4, 24))
        self.assertNotEqual(predicted.month, 9)      # the fifth-oldest sample is not a candidate

    def test_no_samples_means_no_prediction(self):
        self.assertIsNone(predict_earnings.pick_predicted_date([], 2027))


class _PredictDb:
    """In-memory model of the statements ``predict_for_symbol`` issues.

    Only the queries the script actually sends are answered — anything else raises,
    so a new statement cannot pass unnoticed.
    """

    def __init__(self, rows, extra_holders=()):
        self.rows = [dict(row) for row in rows]
        self.extra_holders = set(extra_holders)     # (symbol, market, report_date, report_type)
        self.executed = []
        self.moves = []
        self.inserts = []
        self.savepoints = []
        self.rowcount = 0
        self._result = []

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        self.executed.append((flat, params))
        self._result = []
        self.rowcount = 0
        handler = self._dispatch(flat)
        handler(params or ())

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    def _dispatch(self, flat):
        if flat.startswith("SELECT id, report_date, report_type"):
            return self._history
        if flat.startswith("SELECT company_name FROM earnings"):
            return self._company_name
        if flat.startswith("SELECT id FROM earnings WHERE symbol = %s"):
            return self._target_holder
        if flat.startswith("UPDATE earnings SET report_date"):
            return self._move
        if flat.startswith("INSERT INTO earnings"):
            return self._insert
        if flat.startswith(("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")):
            return lambda params: self.savepoints.append(flat)
        raise AssertionError(f"predict_for_symbol issued an unexpected statement: {flat}")

    def _history(self, params):
        symbol, market = params
        self._result = [dict(row) for row in self.rows
                        if row["symbol"] == symbol and row["market"] == market]

    def _company_name(self, params):
        self._result = [{"company_name": "Apple"}] if self.rows else []

    def _target_holder(self, params):
        symbol, market, report_date, report_type = params
        key = (symbol, market, report_date, report_type)
        hits = [row for row in self.rows
                if (row["symbol"], row["market"], row["report_date"], row["report_type"]) == key]
        self._result = [{"id": hits[0]["id"]}] if hits else ([{"id": -1}] if key in self.extra_holders else [])

    def _move(self, params):
        new_date, row_id, old_date = params
        for row in self.rows:
            if (row["id"] == row_id and row["report_date"] == old_date and row["is_predicted"]
                    and str(row["date_source"] or "").lower() == "algorithm"
                    and row["eps_actual"] is None and row["revenue_actual"] is None):
                row["report_date"] = new_date
                self.moves.append({"id": row_id, "from": old_date, "to": new_date})
                self.rowcount = 1
                return

    def _insert(self, params):
        self.inserts.append(params)

    def rows_of(self, symbol, market):
        return [row for row in self.rows
                if row["symbol"] == symbol and row["market"] == market]

    def updates_on(self, marker):
        return [call for call in self.executed if marker in call[0]]


def _predict_row(row_id, symbol, market, report_date, fiscal_year, fiscal_quarter,
                 *, is_predicted=False, date_source="longbridge", eps_actual=None,
                 revenue_actual=None, report_type="Q"):
    return {
        "id": row_id, "symbol": symbol, "market": market, "report_date": report_date,
        "report_type": report_type, "fiscal_year": fiscal_year, "fiscal_quarter": fiscal_quarter,
        "before_after": "after", "is_predicted": is_predicted, "date_source": date_source,
        "eps_actual": eps_actual, "revenue_actual": revenue_actual, "company_name": "Apple",
    }


def _run_predict(rows, extra_holders=()):
    """Run ``predict_for_symbol`` against the in-memory model; returns (count, db, stats)."""
    db = _PredictDb(rows, extra_holders=extra_holders)
    stats = predict_earnings.PredictionStats()
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = db
    ctx.__exit__.return_value = False
    with mock.patch.object(predict_earnings, "db_cursor", return_value=ctx):
        count = predict_earnings.predict_for_symbol("AAPL", "US", stats)
    return count, db, stats


def _aapl_fixture():
    """AAPL: two January samples and a superseded Saturday prediction for FY2027Q1.

    Left alone the fixed rule predicts 2027-01-29 while the stale row sits on
    2027-01-30 (a Saturday, the next day) — and the read path ranks by date, so the
    stale row would keep winning.
    """
    return [
        _predict_row(1, "AAPL", "US", date(2025, 1, 30), 2025, 1, eps_actual=2.4),
        _predict_row(2, "AAPL", "US", date(2026, 1, 29), 2026, 1, eps_actual=2.5),
        _predict_row(3, "AAPL", "US", date(2027, 1, 30), 2027, 1,
                     is_predicted=True, date_source="algorithm"),
    ]


class SupersededPredictionMoveTests(TestCase):
    """Acceptance criteria 1-2 need the corrected date to be the one displayed."""

    def test_period_row_is_moved_onto_the_corrected_date(self):
        count, db, stats = _run_predict(_aapl_fixture())

        self.assertEqual(count, 1)
        self.assertEqual(db.moves, [{"id": 3, "from": date(2027, 1, 30), "to": date(2027, 1, 29)}])
        self.assertEqual(stats.rescheduled, 1)
        self.assertEqual(stats.predicted, 1)
        # One row per period: the stale date is gone, the new one was written.
        self.assertEqual([row["report_date"] for row in db.rows_of("AAPL", "US")],
                         [date(2025, 1, 30), date(2026, 1, 29), date(2027, 1, 29)])
        self.assertEqual([call[3] for call in db.inserts], ["2027-01-29"])

    def test_move_skips_when_another_row_owns_the_target_date(self):
        count, db, stats = _run_predict(_aapl_fixture(),
                                        extra_holders=[("AAPL", "US", date(2027, 1, 29), "Q")])

        self.assertEqual(count, 1)
        self.assertEqual(db.moves, [])
        self.assertEqual(stats.rescheduled, 0)
        self.assertEqual(stats.reschedule_skipped, 1)
        # The stale row is left alone rather than made to collide with the occupant.
        self.assertEqual(db.rows_of("AAPL", "US")[2]["report_date"], date(2027, 1, 30))

    def test_a_provider_owned_row_is_never_moved(self):
        rows = _aapl_fixture()
        rows[2]["date_source"] = "longbridge"          # e.g. a rescheduled confirmed row

        count, db, stats = _run_predict(rows)

        self.assertEqual(count, 1)
        self.assertEqual(db.moves, [])
        self.assertEqual((stats.rescheduled, stats.reschedule_skipped), (0, 0))
        self.assertEqual([sql for sql, _ in db.executed if sql.startswith("SAVEPOINT")], [])

    def test_a_row_carrying_actuals_is_never_moved(self):
        rows = _aapl_fixture()
        rows[2]["eps_actual"] = 3.1                    # prediction already superseded by data

        _, db, stats = _run_predict(rows)

        self.assertEqual(db.moves, [])
        self.assertEqual(stats.rescheduled, 0)

    def test_the_move_sql_carries_its_own_proof(self):
        sql = " ".join(predict_earnings._MOVE_PREDICTED_ROW_SQL.split())
        self.assertIn("is_predicted = TRUE", sql)
        self.assertIn("date_source = 'algorithm'", sql)
        self.assertIn("eps_actual IS NULL AND revenue_actual IS NULL", sql)
        # The occupant check is by display key only (Issue #52): no period filter.
        holder = " ".join(predict_earnings._TARGET_HOLDER_SQL.split())
        self.assertNotIn("fiscal_year", holder)
        self.assertNotIn("is_predicted", holder)

    def test_a_new_period_does_not_attempt_a_move(self):
        rows = _aapl_fixture()
        rows = [row for row in rows if not row["is_predicted"]]

        _, db, stats = _run_predict(rows)

        self.assertEqual(db.moves, [])
        self.assertEqual(stats.rescheduled, 0)
        self.assertEqual(stats.predicted, 1)
