"""Issue #50 follow-up: one fiscal period is one row, and a release owns it.

Longbridge publishes *two* calendar events for one fiscal period of many
issuers, both declaring ``period='4'``:

* the ``qf``/``3q`` **release** — the quarter's own figures
  (DEA 2026-02-21, revenue 87,725,750);
* the ``af``/``saf`` **disclosure** — the full-year / half-year totals
  (DEA 2026-02-23, revenue 334,384,600).

Parity was invisible in the database (every row said ``report_type='Q'``), so
the authority rule "newest report date wins" promoted the later annual row and
the calendar showed full-year revenue on a 2025 Q4 release date; the duplicate
merge would then have deleted the release's figures into a backup table.

Covered here:

* ``period_type`` → stored ``report_type`` (``Q``/``H``/``A``) and the helpers
  that read it;
* the authority rule: a release outranks a disclosure of the same period;
* the write-path guards: a disclosure whose period has a release is not written,
  a release takes the period over from a stored disclosure row, and a Futu row
  inherits its period's report type instead of inserting a twin;
* the reconciliation: snapshot collisions are offset by microseconds, never
  dropped or refused.
"""
import importlib.util
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import fiscal  # noqa: E402


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sync_earnings = _load("sync_earnings_report_type", "scripts/sync_earnings.py")
reconcile = _load("reconcile_report_type", "scripts/reconcile_fiscal_rows.py")
predict_earnings = _load("predict_earnings_report_type", "scripts/predict_earnings.py")


def _ts(day: int) -> datetime:
    return datetime(2026, 9, day, tzinfo=timezone.utc)


class _FakeCursor:
    """Records statements; replays canned ``fetchall`` results in order."""

    def __init__(self, results=None):
        self.executed = []
        self._results = list(results or [])
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((" ".join(str(sql).split()), params))

    def fetchall(self):
        return self._results.pop(0) if self._results else []

    def fetchone(self):
        return self._results.pop(0) if self._results else None

    def updates(self):
        return [call for call in self.executed if call[0].upper().startswith("UPDATE")]

    def sql_calls(self):
        return [sql for sql, _ in self.executed]


class _RecordingExecuteValues:
    def __init__(self):
        self.calls = []

    def __call__(self, cur, sql, argslist, page_size=None, fetch=False):
        self.calls.append({"sql": " ".join(str(sql).split()), "rows": list(argslist)})


def _stored(row_id, report_type, report_date, **extra):
    row = {
        "id": row_id, "symbol": "DEA", "market": "US", "fiscal_year": 2025,
        "fiscal_quarter": 4, "report_date": report_date, "report_type": report_type,
        "is_predicted": False, "eps_actual": None, "revenue_actual": None,
        "updated_at": _ts(1),
    }
    row.update(extra)
    return row


def _lb_row(report_date, report_type, fiscal_year=2025, fiscal_quarter=4,
            eps_estimate=None, eps_actual=None, revenue_estimate=None, revenue_actual=None):
    """Longbridge batch tuple in ``sync_earnings.flush_batch`` layout."""
    return ("DEA", "US", "Deere & Co", report_date, report_type, fiscal_year, fiscal_quarter,
            eps_estimate, eps_actual, revenue_estimate, revenue_actual,
            "after", None, None, "estimate", "actual")


# ── report type: period_type → stored value ─────────────────────────────────

class ReportTypeTests(TestCase):
    def test_period_types_map_to_report_types(self):
        self.assertEqual(fiscal.report_type_for_period_type("qf"), fiscal.REPORT_TYPE_QUARTERLY)
        self.assertEqual(fiscal.report_type_for_period_type("3q"), "Q")
        self.assertEqual(fiscal.report_type_for_period_type("saf"), "H")
        self.assertEqual(fiscal.report_type_for_period_type("af"), "A")
        self.assertEqual(fiscal.report_type_for_period_type("AF"), "A")
        self.assertEqual(fiscal.report_type_for_period_type(None), fiscal.DEFAULT_REPORT_TYPE)
        self.assertEqual(fiscal.report_type_for_period_type("weird"), fiscal.DEFAULT_REPORT_TYPE)

    def test_only_half_year_and_annual_are_disclosures(self):
        self.assertFalse(fiscal.is_disclosure("Q"))
        self.assertFalse(fiscal.is_disclosure(None))
        self.assertTrue(fiscal.is_disclosure("H"))
        self.assertTrue(fiscal.is_disclosure("a"))


# ── authority: the release owns its period ──────────────────────────────────

class AuthorityTests(TestCase):
    def test_a_release_outranks_a_later_dated_disclosure_of_the_same_period(self):
        release = _stored(1, "Q", date(2026, 2, 21), revenue_estimate=87725750)
        disclosure = _stored(2, "A", date(2026, 2, 23), revenue_estimate=334384600)
        self.assertEqual(fiscal.authority_key(release) < fiscal.authority_key(disclosure), True)

    def test_a_release_without_actuals_still_outranks_a_disclosure_with_actuals(self):
        release = _stored(1, "Q", date(2026, 2, 21))
        disclosure = _stored(2, "A", date(2026, 2, 23), eps_actual=5.0)
        self.assertLess(fiscal.authority_key(release), fiscal.authority_key(disclosure))

    def test_among_releases_the_newest_report_date_wins(self):
        older = _stored(1, "Q", date(2026, 2, 21))
        newer = _stored(2, "Q", date(2026, 2, 26), eps_actual=1.0)
        self.assertLess(fiscal.authority_key(newer), fiscal.authority_key(older))

    def test_the_read_path_shows_the_release_row_for_the_period(self):
        rows = [
            _stored(15346, "A", date(2026, 2, 23), revenue_estimate=334384600),
            _stored(98493, "Q", date(2026, 2, 21), revenue_estimate=87725750),
        ]
        collapsed = fiscal.collapse_fiscal_duplicates(rows)
        self.assertEqual([row["id"] for row in collapsed], [98493])


class CollapseRankTests(TestCase):
    def test_write_path_collapse_keeps_the_release_over_the_newer_disclosure(self):
        rows = [
            _lb_row("2026-02-21", "Q", revenue_estimate=87725750),
            _lb_row("2026-02-23", "A", revenue_estimate=334384600),
        ]
        collapsed = fiscal.collapse_rows_by_period(
            rows,
            identity_of=lambda r: fiscal.fiscal_key_from_parts(r[0], r[1], r[5], r[6]),
            date_of=lambda r: r[3],
            rank_of=lambda r: 1 if fiscal.is_disclosure(r[4]) else 0,
        )
        self.assertEqual([row[3] for row in collapsed], ["2026-02-21"])

    def test_without_a_rank_the_newest_date_still_wins(self):
        rows = [
            _lb_row("2026-02-21", "Q"),
            _lb_row("2026-02-23", "A"),
        ]
        collapsed = fiscal.collapse_rows_by_period(
            rows,
            identity_of=lambda r: fiscal.fiscal_key_from_parts(r[0], r[1], r[5], r[6]),
            date_of=lambda r: r[3],
        )
        self.assertEqual([row[3] for row in collapsed], ["2026-02-23"])


# ── write path: conflicts are dropped / taken over ──────────────────────────

class RescheduleDropTests(TestCase):
    """``reschedule_confirmed_rows`` decides who owns a period, before the upsert."""

    def _reschedule(self, stored, rows, **kwargs):
        cursor = _FakeCursor(results=[stored])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder):
            outcome = fiscal.reschedule_confirmed_rows(cursor, rows, **kwargs)
        return outcome, cursor

    def test_an_in_batch_disclosure_is_dropped_for_its_release(self):
        outcome, _ = self._reschedule([], [
            _lb_row("2026-02-21", "Q", revenue_estimate=87725750),
            _lb_row("2026-02-23", "A", revenue_estimate=334384600),
        ])
        self.assertEqual([row[3] for row in outcome.rows], ["2026-02-21"])
        self.assertEqual(len(outcome.dropped), 1)
        self.assertEqual(outcome.dropped[0]["report_type"], "A")

    def test_a_disclosure_is_dropped_when_the_release_row_is_already_stored(self):
        outcome, cursor = self._reschedule([_stored(1, "Q", date(2026, 2, 21))], [
            _lb_row("2026-02-23", "A", revenue_estimate=334384600),
        ])
        self.assertEqual(outcome.rows, [])
        self.assertEqual(outcome.dropped[0]["reason"], "release_row_owns_period")
        self.assertEqual(outcome.dropped[0]["holder"], {"id": 1, "report_type": "Q"})
        # Crucially: the stored release row is not dragged onto the disclosure date.
        self.assertEqual(cursor.updates(), [])

    def test_a_disclosure_without_a_release_event_stays_the_period_row(self):
        outcome, _ = self._reschedule([], [
            _lb_row("2026-10-20", "A", fiscal_year=2026, fiscal_quarter=3, revenue_estimate=1.0),
        ])
        self.assertEqual([row[3] for row in outcome.rows], ["2026-10-20"])
        self.assertEqual(outcome.dropped, [])

    def test_a_release_for_a_release_held_period_keeps_both_kinds_of_move_simple(self):
        outcome, cursor = self._reschedule([_stored(7, "Q", date(2026, 2, 24))], [
            _lb_row("2026-02-21", "Q", revenue_estimate=87725750),
        ])
        self.assertEqual(outcome.dropped, [])
        updates = cursor.updates()
        self.assertEqual(len(updates), 1)
        self.assertNotIn("report_type", updates[0][0])
        self.assertEqual(outcome.moves[0].get("kind"), None)


class RescheduleTakeoverTests(TestCase):
    def _reschedule(self, stored, rows, **kwargs):
        cursor = _FakeCursor(results=[stored])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder):
            outcome = fiscal.reschedule_confirmed_rows(cursor, rows, **kwargs)
        return outcome, cursor

    def test_a_release_takes_over_a_stored_disclosure_row_and_moves_it(self):
        outcome, cursor = self._reschedule(
            [_stored(7, "A", date(2026, 2, 23), revenue_estimate=334384600)],
            [_lb_row("2026-02-21", "Q", revenue_estimate=87725750)],
        )
        updates = cursor.updates()
        self.assertEqual(len(updates), 1)
        self.assertIn("report_type = %s", updates[0][0])
        params = updates[0][1]
        self.assertEqual(params[0], date(2026, 2, 21))       # new report date
        self.assertEqual(params[1], "Q")                      # promoted report type
        self.assertEqual(params[4], 87725750)                 # the release's own revenue
        self.assertEqual(params[-2:], (7, date(2026, 2, 23)))  # row id + previous date
        self.assertEqual([move["kind"] for move in outcome.moves], ["takeover"])
        self.assertEqual(outcome.dropped, [])

    def test_a_release_on_the_stored_disclosure_date_only_promotes_the_row(self):
        outcome, cursor = self._reschedule(
            [_stored(7, "A", date(2026, 2, 21))],
            [_lb_row("2026-02-21", "Q", revenue_estimate=87725750)],
        )
        updates = cursor.updates()
        self.assertEqual(len(updates), 1)
        self.assertNotIn("report_date = %s", updates[0][0])
        self.assertEqual([move["kind"] for move in outcome.moves], ["takeover"])

    def test_a_takeover_never_nulls_the_disclosure_figures_it_replaces_nothing_of(self):
        """``COALESCE`` keeps a stored number the release event itself lacks."""
        _, cursor = self._reschedule(
            [_stored(7, "A", date(2026, 2, 23), revenue_actual=334384600)],
            [_lb_row("2026-02-21", "Q", revenue_estimate=87725750)],
        )
        self.assertIn("revenue_actual = COALESCE(%s, revenue_actual)", cursor.updates()[0][0])

    def test_without_takeover_a_stored_report_type_is_left_alone(self):
        """Futu rows carry no figures and no period sequence (``takeover=False``)."""
        outcome, cursor = self._reschedule(
            [_stored(7, "A", date(2026, 2, 23))],
            [_lb_row("2026-02-21", "Q")],
            value_fields=(), takeover=False,
        )
        updates = cursor.updates()
        self.assertEqual(len(updates), 1)
        self.assertNotIn("report_type", updates[0][0])
        self.assertEqual(outcome.moves[0].get("kind"), None)


class AlignReportTypeTests(TestCase):
    """A date/actual provider must update its period's row, not twin it."""

    def _reschedule(self, stored, rows):
        cursor = _FakeCursor(results=[stored])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder):
            outcome = fiscal.reschedule_confirmed_rows(
                cursor, rows, value_fields=(), takeover=False, align_report_type=True)
        return outcome, cursor

    def test_a_futu_row_inherits_the_period_report_type(self):
        rows = [_lb_row("2026-02-23", "Q")]
        outcome, _ = self._reschedule([_stored(7, "A", date(2026, 2, 23))], rows)
        self.assertEqual([row[4] for row in outcome.rows], ["A"])
        self.assertEqual(rows[0][4], "Q", "the caller's tuple must not be mutated")

    def test_a_row_without_a_stored_period_is_unchanged(self):
        rows = [_lb_row("2026-02-23", "Q")]
        outcome, _ = self._reschedule([[]], rows)
        self.assertEqual(outcome.rows, rows)

    def test_a_matching_report_type_is_unchanged(self):
        rows = [_lb_row("2026-02-23", "q")]
        outcome, _ = self._reschedule([_stored(7, "Q", date(2026, 2, 23))], rows)
        self.assertEqual(outcome.rows, rows)


class SyncEarningsGuardTests(TestCase):
    def test_the_batch_written_for_a_release_plus_disclosure_holds_only_the_release(self):
        cursor = _FakeCursor(results=[[]])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder), \
             mock.patch.object(sync_earnings, "db_cursor", return_value=_ctx(cursor)):
            stats = sync_earnings.flush_batch(cursor, [
                _lb_row("2026-02-21", "Q", revenue_estimate=87725750),
                _lb_row("2026-02-23", "A", revenue_estimate=334384600),
            ])
        insert = [call for call in recorder.calls if "INSERT INTO earnings " in call["sql"]][0]
        self.assertEqual([row[3] for row in insert["rows"]], ["2026-02-21"])
        self.assertEqual(stats.dropped_disclosures, 1)

    def test_a_lone_disclosure_is_still_written(self):
        cursor = _FakeCursor(results=[[]])
        recorder = _RecordingExecuteValues()
        with mock.patch("psycopg2.extras.execute_values", recorder), \
             mock.patch.object(sync_earnings, "db_cursor", return_value=_ctx(cursor)):
            stats = sync_earnings.flush_batch(cursor, [
                _lb_row("2026-02-23", "A", revenue_estimate=334384600),
            ])
        insert = [call for call in recorder.calls if "INSERT INTO earnings " in call["sql"]][0]
        self.assertEqual([row[3] for row in insert["rows"]], ["2026-02-23"])
        self.assertEqual(stats.dropped_disclosures, 0)


def _ctx(cursor):
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = cursor
    ctx.__exit__.return_value = False
    return ctx


class MarkConfirmedTests(TestCase):
    """``predict_earnings.mark_confirmed`` must not create a second confirmed row."""

    def test_stale_predictions_are_removed_before_any_prediction_is_promoted(self):
        cursor = _FakeCursor()
        with mock.patch.object(predict_earnings, "db_cursor", return_value=_ctx(cursor)):
            predict_earnings.mark_confirmed()
        sql = cursor.sql_calls()
        delete = next(i for i, text in enumerate(sql) if text.startswith("DELETE FROM earnings WHERE is_predicted"))
        promote = next(i for i, text in enumerate(sql) if "date_status = 'reported'" in text)
        self.assertLess(delete, promote)

    def test_the_promote_refuses_a_period_another_confirmed_row_holds(self):
        cursor = _FakeCursor()
        with mock.patch.object(predict_earnings, "db_cursor", return_value=_ctx(cursor)):
            predict_earnings.mark_confirmed()
        promote = next(text for text in cursor.sql_calls() if "date_status = 'reported'" in text)
        self.assertIn("NOT EXISTS", promote)
        self.assertIn("c.is_predicted = FALSE", promote)
        self.assertIn("c.fiscal_quarter = earnings.fiscal_quarter", promote)


# ── reconciliation: snapshot collisions are offset, not lost ────────────────

class SnapshotCollisionTests(TestCase):
    def _plan(self, survivor_id=1, dropped_ids=(2,)):
        return reconcile.GroupPlan(
            key=("DEA", "US", 2025, 4),
            survivor={"id": survivor_id, "report_date": date(2026, 2, 21)},
            dropped=[{"id": row_id, "report_date": date(2026, 2, 23)} for row_id in dropped_ids],
        )

    def test_first_free_microsecond_is_used(self):
        taken = {}
        marker = ("longbridge", datetime(2026, 2, 21, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(reconcile._free_microsecond(marker, taken), 1)
        taken = {(marker[0], marker[1])}
        self.assertEqual(reconcile._free_microsecond(marker, taken), 1)
        taken.add(("longbridge", marker[1] + timedelta(microseconds=1)))
        self.assertEqual(reconcile._free_microsecond(marker, taken), 2)

    def test_a_colliding_snapshot_is_offset_and_kept(self):
        survivor_ts = datetime(2026, 2, 21, 1, 0, tzinfo=timezone.utc)
        cursor = _FakeCursor(results=[[
            {"id": 11, "earning_id": 1, "source": "longbridge", "captured_at": survivor_ts},
            {"id": 12, "earning_id": 2, "source": "longbridge", "captured_at": survivor_ts},
        ]])
        shifts = reconcile._repoint_snapshots(cursor, self._plan())
        self.assertEqual(len(shifts), 1)
        self.assertEqual(shifts[0]["snapshot_id"], 12)
        self.assertEqual(shifts[0]["shift_microseconds"], 1)
        self.assertEqual(shifts[0]["to"], (survivor_ts.replace(microsecond=1)).isoformat())
        updates = cursor.updates()
        self.assertEqual(len(updates), 1)
        self.assertIn("captured_at", updates[0][0])
        # No snapshot is deleted: both rows survive, one on a nudged timestamp.
        self.assertEqual([sql for sql, _ in cursor.executed if sql.upper().startswith("DELETE")], [])

    def test_non_colliding_snapshots_are_repointed_in_one_statement(self):
        cursor = _FakeCursor(results=[[
            {"id": 11, "earning_id": 1, "source": "longbridge", "captured_at": datetime(2026, 2, 21, 1, 0, tzinfo=timezone.utc)},
            {"id": 12, "earning_id": 2, "source": "futu", "captured_at": datetime(2026, 2, 22, 1, 0, tzinfo=timezone.utc)},
        ]])
        shifts = reconcile._repoint_snapshots(cursor, self._plan())
        self.assertEqual(shifts, [])
        updates = cursor.updates()
        self.assertEqual(len(updates), 1)
        self.assertNotIn("captured_at", updates[0][0])
        self.assertEqual(updates[0][1], (1, [12]))

    def test_the_plan_reports_both_report_types_so_a_reviewer_can_see_parity(self):
        plan = reconcile.GroupPlan(
            key=("DEA", "US", 2025, 4),
            survivor={"id": 98493, "report_date": date(2026, 2, 21), "report_type": "Q"},
            dropped=[{"id": 15346, "report_date": date(2026, 2, 23), "report_type": "A"}],
        )
        payload = plan.as_dict()
        self.assertEqual(payload["survivor_report_type"], "Q")
        self.assertEqual(payload["dropped_report_types"], ["A"])
