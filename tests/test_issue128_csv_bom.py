"""Issue #128: the CSV export starts with a UTF-8 BOM so Excel shows 中文.

Excel for Windows decodes a BOM-less UTF-8 CSV through the local code page, so
「腾讯控股」 arrives as mojibake unless the user goes through 导入 → 选择 UTF-8 on
every single file. The calendar's audience is exactly that: people who open the
export in Excel.

The BOM has a recorded cost — a parser that does not strip it sees it glued to the
first field name (`\\ufeffsymbol`) — so the tests pin both sides: the bytes are
there, and `utf-8-sig` / `csv` / `pandas` style readers still see clean headers.
"""
import csv
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402
from app import db, universe  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.watchlist.base import WatchlistSource  # noqa: E402

BOM = b"\xef\xbb\xbf"

SAMPLE = [{
    "id": 1,
    "symbol": "0700.HK", "market": "HK", "company_name": "腾讯控股",
    "report_date": "2026-08-13", "report_type": "Q",
    "fiscal_year": 2026, "fiscal_quarter": 2,
    "before_after": "after", "eps_estimate": 3.1, "eps_actual": 3.2,
    "revenue_estimate": 100.0, "revenue_actual": 101.0,
    "is_predicted": False, "date_source": "futu", "date_status": "reported",
}]


class _FakeCursor:
    def execute(self, *a, **kw):
        pass

    def fetchall(self):
        return []

    def fetchone(self):
        return {"id": 1, "portal_user_id": 1, "email": "t@t.com", "name": "T", "ical_token": "tok-abc"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _FakeConn:
    def cursor(self, **kw):
        return _FakeCursor()

    def __enter__(self):
        return _FakeCursor()

    def __exit__(self, *a):
        pass


class _FakeSource(WatchlistSource):
    def fetch_symbols(self):
        return ["AAPL.US", "0700.HK"]


@pytest.fixture(autouse=True)
def _offline_boundaries(monkeypatch):
    monkeypatch.setattr(db, "db_cursor", lambda: _FakeConn())
    monkeypatch.setattr(
        universe, "_universe",
        universe.SymbolUniverse(source_factory=lambda: _FakeSource()),
    )
    monkeypatch.setattr("app.earnings.fetch_earnings_from_db", lambda **kw: SAMPLE)


@pytest.fixture
def client():
    app.dependency_overrides = {}
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "email": "t@t.com", "name": "T", "role": "admin",
    }
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides = {}


def test_the_csv_body_starts_with_the_bom(client):
    resp = client.get("/api/export?start=2026-08-01&end=2026-08-31&format=csv")
    assert resp.status_code == 200
    assert resp.content[:3] == BOM, (
        f"expected EF BB BF, got {resp.content[:3]!r} — Excel would read this as the local code page"
    )


def test_the_bom_does_not_disturb_a_reader_that_strips_it(client):
    resp = client.get("/api/export?start=2026-08-01&end=2026-08-31&format=csv")
    text = resp.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][0] == "id", f"first header must stay clean, got {rows[0][0]!r}"
    assert "symbol" in rows[0]
    assert rows[1][rows[0].index("company_name")] == "腾讯控股"


def test_the_chinese_company_name_survives_the_round_trip(client):
    resp = client.get("/api/export?start=2026-08-01&end=2026-08-31&format=csv")
    text = resp.content.decode("utf-8-sig")
    assert "腾讯控股" in text


def test_a_reader_that_ignores_the_bom_sees_it_on_the_first_field(client):
    """The documented cost of the BOM: plain utf-8 keeps it on the first header.

    Pinned so a future reader of this file knows why the leading field can carry
    `\\ufeff` and that `utf-8-sig` / `encoding="utf-8-sig"` is the way out.
    """
    resp = client.get("/api/export?start=2026-08-01&end=2026-08-31&format=csv")
    plain = resp.content.decode("utf-8")
    assert plain.startswith("\ufeff")


def test_json_is_untouched_by_the_bom(client):
    resp = client.get("/api/export?start=2026-08-01&end=2026-08-31&format=json")
    assert resp.status_code == 200
    assert not resp.content.startswith(BOM), "the BOM belongs to the CSV branch only"
    assert resp.json()[0]["company_name"] == "腾讯控股"


def test_an_empty_export_still_carries_the_bom_and_a_header(client):
    with patch("app.earnings.fetch_earnings_from_db", lambda **kw: []):
        resp = client.get("/api/export?start=2026-08-01&end=2026-08-31&format=csv")
    assert resp.status_code == 200
    assert resp.content.startswith(BOM), "the BOM is written before the header row"
    header = resp.content.decode("utf-8-sig").splitlines()[0]
    assert header.startswith("symbol,"), header
