from datetime import date
import sys
from pathlib import Path

from psycopg2 import errors

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from reconcile_fiscal_labels import (  # noqa: E402
    Proposal,
    Release,
    apply_proposals,
    build_proposals,
)


def test_qf_release_corrects_stale_fiscal_year_without_deleting_row():
    rows = [{
        "id": 9, "symbol": "DLTH", "market": "US",
        "fiscal_year": 2026, "fiscal_quarter": 4,
        "report_date": date(2026, 3, 19), "date_source": "longbridge",
        "is_predicted": False,
    }, {
        "id": 10, "symbol": "DLTH", "market": "US",
        "fiscal_year": 2026, "fiscal_quarter": 2,
        "report_date": date(2026, 9, 4), "date_source": "longbridge",
        "is_predicted": False,
    }]
    releases = [Release("DLTH", "US", 2027, 2, "2026-09-03")]
    proposals = build_proposals(rows, releases)
    assert len(proposals) == 1
    assert (proposals[0].after_fiscal_year, proposals[0].after_fiscal_quarter) == (2027, 2)
    assert proposals[0].source_id == 10


def test_legacy_q4_between_q1_and_q3_becomes_q2():
    rows = [{
        "id": 18, "symbol": "0323.HK", "market": "HK",
        "fiscal_year": 2026, "fiscal_quarter": 1,
        "report_date": date(2026, 4, 24), "date_source": "longbridge",
        "is_predicted": False,
    }, {
        "id": 19, "symbol": "0323.HK", "market": "HK",
        "fiscal_year": 2026, "fiscal_quarter": 3,
        "report_date": date(2026, 10, 30), "date_source": "longbridge",
        "is_predicted": False,
    }, {
        "id": 20, "symbol": "0323.HK", "market": "HK",
        "fiscal_year": 2026, "fiscal_quarter": 4,
        "report_date": date(2026, 8, 28), "date_source": "longbridge",
        "is_predicted": False,
    }]
    releases = [
        Release("0323.HK", "HK", 2026, 1, "2026-04-24"),
        Release("0323.HK", "HK", 2026, 3, "2026-10-30"),
    ]
    proposals = build_proposals(rows, releases)
    assert len(proposals) == 1
    assert (proposals[0].after_fiscal_year, proposals[0].after_fiscal_quarter) == (2026, 2)


def test_non_longbridge_and_predictions_are_untouched():
    rows = [{
        "id": 30, "symbol": "X", "market": "US",
        "fiscal_year": 2026, "fiscal_quarter": 4,
        "report_date": date(2026, 3, 1), "date_source": "futu",
        "is_predicted": False,
    }, {
        "id": 31, "symbol": "Y", "market": "US",
        "fiscal_year": 2026, "fiscal_quarter": 4,
        "report_date": date(2026, 3, 1), "date_source": "longbridge",
        "is_predicted": True,
    }]
    releases = [Release("X", "US", 2026, 3, "2026-03-02")]
    assert build_proposals(rows, releases) == []


class _ApplyCursor:
    def __init__(self, rows, holders=None, update_errors=None):
        self.rows = {row["id"]: dict(row) for row in rows}
        self.holders = holders or {}
        self.update_errors = update_errors or set()
        self.executed = []
        self._one = None
        self._many = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self.executed.append((normalized, params))
        if normalized.startswith("SELECT to_jsonb"):
            row = self.rows.get(params[0])
            self._one = None if row is None else {"row_data": dict(row)}
        elif normalized.startswith("SELECT id FROM earnings"):
            key = tuple(params[:4])
            self._many = [{"id": item} for item in self.holders.get(key, ())]
        elif normalized.startswith("UPDATE earnings"):
            if params[2] in self.update_errors:
                raise errors.UniqueViolation("duplicate key")
            self.rows[params[2]]["fiscal_year"] = params[0]
            self.rows[params[2]]["fiscal_quarter"] = params[1]
            self.rowcount = 1

    def fetchone(self):
        value, self._one = self._one, None
        return value

    def fetchall(self):
        value, self._many = self._many, []
        return value

    def sql_calls(self):
        return [sql for sql, _ in self.executed]


def _proposal(source_id, symbol, after_year=2027, after_quarter=2):
    return Proposal(
        source_id=source_id,
        symbol=symbol,
        market="US",
        report_date="2026-09-01",
        before_fiscal_year=2026,
        before_fiscal_quarter=4,
        after_fiscal_year=after_year,
        after_fiscal_quarter=after_quarter,
        reason="test",
    )


def test_apply_skips_occupied_target_and_commits_other_rows():
    cursor = _ApplyCursor(
        rows=[
            {"id": 1, "symbol": "CLEAN", "market": "US", "fiscal_year": 2026,
             "fiscal_quarter": 4, "report_date": "2026-09-01", "is_predicted": False},
            {"id": 2, "symbol": "CONFLICT", "market": "US", "fiscal_year": 2026,
             "fiscal_quarter": 4, "report_date": "2026-09-01", "is_predicted": False},
        ],
        holders={("CONFLICT", "US", 2027, 2): (3,)},
    )
    outcome = apply_proposals(cursor, [_proposal(1, "CLEAN"), _proposal(2, "CONFLICT")])
    assert outcome.applied == 1
    assert [row["reason"] for row in outcome.skipped] == ["target_occupied"]
    assert outcome.skipped[0]["holder_ids"] == [3]
    assert any(call.startswith("SAVEPOINT fiscal_label_row") for call in cursor.sql_calls())


def test_apply_uses_one_winner_for_batch_target_collision():
    cursor = _ApplyCursor(rows=[
        {"id": 1, "symbol": "SAME", "market": "US", "fiscal_year": 2026,
         "fiscal_quarter": 4, "report_date": "2026-09-01", "is_predicted": False},
        {"id": 2, "symbol": "SAME", "market": "US", "fiscal_year": 2026,
         "fiscal_quarter": 3, "report_date": "2026-09-02", "is_predicted": False,
         "eps_actual": 1.2},
    ])
    outcome = apply_proposals(cursor, [_proposal(1, "SAME"), _proposal(2, "SAME")])
    assert outcome.applied == 1
    assert [row["reason"] for row in outcome.skipped] == ["batch_target_conflict"]
    assert outcome.skipped[0]["winner_id"] == 2


def test_apply_savepoint_contains_late_unique_violation():
    cursor = _ApplyCursor(
        rows=[
            {"id": 1, "symbol": "OK", "market": "US", "fiscal_year": 2026,
             "fiscal_quarter": 4, "report_date": "2026-09-01", "is_predicted": False},
            {"id": 2, "symbol": "RACE", "market": "US", "fiscal_year": 2026,
             "fiscal_quarter": 4, "report_date": "2026-09-02", "is_predicted": False},
        ],
        update_errors={2},
    )
    outcome = apply_proposals(cursor, [_proposal(1, "OK"), _proposal(2, "RACE")])
    assert outcome.applied == 1
    assert [row["reason"] for row in outcome.skipped] == ["unique_violation"]
    assert "ROLLBACK TO SAVEPOINT fiscal_label_row" in cursor.sql_calls()
    assert "RELEASE SAVEPOINT fiscal_label_row" in cursor.sql_calls()
