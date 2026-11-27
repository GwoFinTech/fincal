"""Issue #156: an annual / interim row must not borrow the quarter's consensus.

``earnings_consensus`` is keyed by ``(symbol, market, fiscal_year, fiscal_quarter)``
and holds the provider's **quarterly** consensus.  An annual results event ('A')
carries the whole year's figures on the same fiscal key — a February 2027 annual
result is stored under FY2026 Q4 — so the read path's join handed the quarter's
EBIT and net income to the year's revenue (0700.HK 969.12B against 64.52B, read
as a 6.7% net margin; AMZN 829B against 26.58B, 3.2%).  In production, 41 of the
future 'A' rows and 1 'H' row were borrowing a quarter in this way.

Pinned here:

* the SQL gate (``e.report_type='Q'``) is executed against the local PostgreSQL
  inside a transaction that is always rolled back: the annual row's
  ``consensus_*`` columns are NULL while the same ``(fy, fq)`` on a quarterly row
  still carries its values — and the same fixture without the gate *does* attach
  them, so removing the gate makes this test fail;
* ``attach_provider_pairs`` refuses an annual / interim row for the same reason;
* the panel says why the block is absent (a disclosure note), and the EBIT /
  net-income estimate cells read 「—」;
* the CSV export (whose only data source is the same read boundary) leaves the
  ``consensus_*`` cells empty on the annual row and unchanged on the quarterly one.
"""
import asyncio
import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import skipUnless

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for path in (ROOT, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app import earnings as earnings_module  # noqa: E402
from app.routers import api as api_module  # noqa: E402
import frontend_contract as fc  # noqa: E402  (needs the path above)

APP_JS = ROOT / "app" / "static" / "assets" / "app-setup.js"

ANNUAL_SYMBOL = "ZZANN156"
QUARTER_SYMBOL = "ZZQT156"
FISCAL_YEAR = 2026
FISCAL_QUARTER = 4
ANNUAL_DATE = date(2027, 2, 4)
QUARTER_DATE = date(2026, 11, 5)
#: The quarter's consensus, exactly as production holds it for 0700.HK 2026Q4.
CONSENSUS = {"currency": "HKD", "eps_gaap": 7.1326, "eps_adjusted": 7.20,
             "revenue": 249_812_000_000.0, "ebit": 73_010_000_000.0,
             "net_income": 64_520_000_000.0, "normalized_net_income": 64_900_000_000.0}


# ── The read boundary, against the local database (rolled back) ────────────


def _db_connect():
    import psycopg2

    from app import config

    return psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
        user=config.DB_USER, password=config.DB_PASSWORD, connect_timeout=3,
    )


def _db_reachable():
    try:
        _db_connect().close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _cursor_ctx(cursor):
    @contextmanager
    def _ctx():
        yield cursor

    return _ctx


def _seed(cur):
    """One annual row and one quarterly row of the same fiscal period."""
    cur.execute("DELETE FROM earnings WHERE symbol = ANY(%s)",
                ([ANNUAL_SYMBOL, QUARTER_SYMBOL],))
    cur.execute("DELETE FROM earnings_consensus WHERE symbol = ANY(%s)",
                ([ANNUAL_SYMBOL, QUARTER_SYMBOL],))
    for symbol, report_date, report_type, revenue_estimate, eps_estimate in (
        (ANNUAL_SYMBOL, ANNUAL_DATE, "A", 829_030_000_000.0, 28.88),
        (QUARTER_SYMBOL, QUARTER_DATE, "Q", 250_000_000_000.0, 7.0),
    ):
        cur.execute(
            """INSERT INTO earnings (symbol, market, company_name, report_date,
                   report_type, fiscal_year, fiscal_quarter, date_source,
                   date_status, estimate_source, revenue_estimate, eps_estimate,
                   estimate_currency)
               VALUES (%s, 'US', 'ZZ Issue156', %s, %s, %s, %s, 'longbridge',
                       'scheduled', 'longbridge', %s, %s, 'HKD')""",
            (symbol, report_date, report_type, FISCAL_YEAR, FISCAL_QUARTER,
             revenue_estimate, eps_estimate),
        )
    for symbol in (ANNUAL_SYMBOL, QUARTER_SYMBOL):
        cur.execute(
            """INSERT INTO earnings_consensus (symbol, market, fiscal_year,
                   fiscal_quarter, currency, eps_gaap, eps_adjusted, revenue,
                   ebit, net_income, normalized_net_income, source)
               VALUES (%s, 'US', %s, %s, %s, %s, %s, %s, %s, %s, %s, 'longbridge')""",
            (symbol, FISCAL_YEAR, FISCAL_QUARTER, CONSENSUS["currency"],
             CONSENSUS["eps_gaap"], CONSENSUS["eps_adjusted"], CONSENSUS["revenue"],
             CONSENSUS["ebit"], CONSENSUS["net_income"],
             CONSENSUS["normalized_net_income"]),
        )


UNGATED_SQL = """
    SELECT e.symbol, c.eps_gaap, c.ebit, c.net_income
    FROM earnings e
    LEFT JOIN earnings_consensus c ON c.symbol=e.symbol AND c.market=e.market
      AND c.fiscal_year=e.fiscal_year AND c.fiscal_quarter=e.fiscal_quarter
      AND c.source='longbridge'
    WHERE e.symbol = ANY(%s)
    ORDER BY e.symbol
"""


@pytest.mark.integration
@pytest.mark.skipif(not _db_reachable(), reason="local fincal PostgreSQL not available")
def test_the_annual_row_does_not_borrow_the_quarterly_consensus(monkeypatch):
    """Acceptance 1 (+ reverse validation): the gate is what withholds the values."""
    import psycopg2.extras

    from app import db as db_module

    conn = _db_connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _seed(cur)
            monkeypatch.setattr(db_module, "db_cursor", _cursor_ctx(cur))

            rows = earnings_module.fetch_earnings_from_db(
                symbols=[ANNUAL_SYMBOL, QUARTER_SYMBOL], markets=["US"],
                start=date(2026, 11, 1), end=date(2027, 3, 1))
            by_symbol = {row["symbol"]: row for row in rows}
            annual, quarter = by_symbol[ANNUAL_SYMBOL], by_symbol[QUARTER_SYMBOL]

            # The annual row keeps its own (whole-year) figures …
            assert float(annual["revenue_estimate"]) == pytest.approx(829_030_000_000.0)
            # … and carries none of the quarter's consensus.
            for field in ("consensus_eps_gaap", "consensus_eps_adjusted",
                          "consensus_revenue", "consensus_ebit",
                          "consensus_net_income", "consensus_normalized_net_income",
                          "consensus_currency", "consensus_fetched_at"):
                assert annual[field] is None, (field, annual[field])

            # The quarterly row of the same (fy, fq) is untouched.
            assert float(quarter["consensus_eps_gaap"]) == pytest.approx(7.1326)
            assert float(quarter["consensus_ebit"]) == pytest.approx(73_010_000_000.0)
            assert float(quarter["consensus_net_income"]) == pytest.approx(
                64_520_000_000.0)
            assert quarter["consensus_currency"] == "HKD"

            # Reverse validation: with the gate removed, this very fixture puts
            # the quarter's numbers back on the annual row — so a fix that drops
            # `e.report_type='Q'` fails the assertions above.
            cur.execute(UNGATED_SQL, ([ANNUAL_SYMBOL, QUARTER_SYMBOL],))
            ungated = {row["symbol"]: row for row in cur.fetchall()}
            assert float(ungated[ANNUAL_SYMBOL]["net_income"]) == pytest.approx(
                64_520_000_000.0)
            assert float(ungated[ANNUAL_SYMBOL]["eps_gaap"]) == pytest.approx(7.1326)
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.integration
@pytest.mark.skipif(not _db_reachable(), reason="local fincal PostgreSQL not available")
def test_the_csv_export_withholds_the_annual_rows_consensus(monkeypatch):
    """Acceptance 3: `/api/export` reads the same boundary and prints empty cells."""
    import csv
    import io
    import psycopg2.extras

    from app import db as db_module

    conn = _db_connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _seed(cur)
            monkeypatch.setattr(db_module, "db_cursor", _cursor_ctx(cur))
            monkeypatch.setattr(api_module, "ensure_user", lambda *a, **k: {})
            monkeypatch.setattr("app.universe.popular_stocks",
                                lambda: ([ANNUAL_SYMBOL, QUARTER_SYMBOL], []))

            response = api_module.api_export(
                start=date(2026, 11, 1), end=date(2027, 3, 1), format="csv",
                user={"id": 1, "email": "zz@example.test", "name": "ZZ"})
            # StreamingResponse's iterator is async; drain it the way the server does.
            chunks: list[bytes] = []

            async def _drain(iterator):
                async for chunk in iterator:
                    chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())

            asyncio.run(_drain(getattr(response, "body_iterator")))
            body = b"".join(chunks).decode("utf-8-sig")
            parsed = {row["symbol"]: row for row in csv.DictReader(io.StringIO(body))}

            annual, quarter = parsed[ANNUAL_SYMBOL], parsed[QUARTER_SYMBOL]
            for field in ("consensus_eps_gaap", "consensus_revenue",
                          "consensus_ebit", "consensus_net_income"):
                assert annual[field] == "", (field, annual[field])
            assert quarter["consensus_eps_gaap"]
            assert quarter["consensus_ebit"]
    finally:
        conn.rollback()
        conn.close()


# ── The provider pair keeps the same rule (preventive) ─────────────────────


def _row(symbol: str, report_type: str) -> dict:
    """A row whose own EPS pair is currency-blocked, so a pair *could* attach."""
    return {
        "symbol": symbol, "market": "US",
        "fiscal_year": FISCAL_YEAR, "fiscal_quarter": FISCAL_QUARTER,
        "report_type": report_type,
        "eps_estimate": 7.3713, "eps_actual": 7.059,
        "estimate_currency": "HKD", "actual_currency": "CNY",
        "comparison_unavailable_reason": "currency_mismatch",
        "revenue_estimate": 250_000_000_000.0, "revenue_actual": None,
    }


PAIR = {"EPS_GAAP": {"currency": "CNY", "consensus": 6.2873, "actual": 6.947,
                     "num_analysts": 7, "source": "futu"}}


def test_a_disclosure_row_never_adopts_a_quarterly_provider_pair(monkeypatch):
    annual, quarter = _row(ANNUAL_SYMBOL, "A"), _row(QUARTER_SYMBOL, "Q")
    calls = []

    def fake_pairs(keys):
        calls.extend(keys)
        # The pair table is keyed by fiscal quarter: both keys exist.
        return {key: PAIR for key in keys}

    monkeypatch.setattr(earnings_module, "load_provider_pairs", fake_pairs)
    earnings_module.attach_provider_pairs([annual, quarter])

    assert annual.get("eps_pair_estimate") is None
    assert annual.get("eps_pair_actual") is None
    # The quarterly row is still rescued by its own pair (nothing changed there).
    assert quarter["eps_pair_estimate"] == pytest.approx(6.2873)
    assert quarter["eps_pair_currency"] == "CNY"
    # The disclosure row was filtered out before the lookup, not after it.
    assert (ANNUAL_SYMBOL, "US", FISCAL_YEAR, FISCAL_QUARTER) not in calls


# ── The panel: a note in place of the block, and 「—」 in the cells ─────────


def _probe(payload: list[dict]) -> list[dict]:
    source = fc.function_source(fc.read_app_js(), "useFormatters")
    probe = f"""
{source}
const fmt = useFormatters();
const rows = {json.dumps(payload)};
console.log(JSON.stringify(rows.map(row => ({{
  note: fmt.disclosureConsensusNote(row),
  hasBlock: fmt.hasLongbridgeConsensus(row),
  ebitEstimate: fmt.metricEstimate(row, 'ebit') ?? null,
  netIncomeEstimate: fmt.metricEstimate(row, 'net_income') ?? null,
}}))));
"""
    completed = subprocess.run(["node", "-e", probe], capture_output=True,
                               text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


ANNUAL_ROW = {"symbol": ANNUAL_SYMBOL, "report_type": "A",
              "fiscal_year": FISCAL_YEAR, "fiscal_quarter": FISCAL_QUARTER,
              "revenue_estimate": 829_030_000_000.0, "eps_estimate": 28.88}
QUARTER_ROW = {"symbol": QUARTER_SYMBOL, "report_type": "Q",
               "fiscal_year": FISCAL_YEAR, "fiscal_quarter": FISCAL_QUARTER,
               "consensus_eps_gaap": CONSENSUS["eps_gaap"],
               "consensus_ebit": CONSENSUS["ebit"],
               "consensus_net_income": CONSENSUS["net_income"]}


@skipUnless(shutil.which("node"), "node is required for the JS behaviour check")
class TestPanelNote:
    def test_an_annual_row_explains_why_the_block_is_absent(self):
        annual, interim = _probe([ANNUAL_ROW, {**ANNUAL_ROW, "report_type": "H"}])
        assert "本次为年报披露" in annual["note"]
        assert "期间口径与本次年报不同" in annual["note"]
        assert "故不在此列出" in annual["note"]
        assert annual["hasBlock"] is False
        assert interim["note"].startswith("本次为中报披露")
        assert "中报" in interim["note"]
        assert interim["hasBlock"] is False

    def test_a_quarterly_row_keeps_its_block_and_gets_no_note(self):
        quarter = _probe([QUARTER_ROW])[0]
        assert quarter["note"] == ""
        assert quarter["hasBlock"] is True

    def test_the_ebit_and_net_income_cells_read_empty_on_a_disclosure(self):
        annual = _probe([ANNUAL_ROW])[0]
        assert annual["ebitEstimate"] is None
        assert annual["netIncomeEstimate"] is None


def test_the_panel_renders_the_note_where_the_block_stood():
    html = fc.read_index()
    # The note is rendered from the served helper, not hard-coded per row.
    assert 'v-if="disclosureConsensusNote(selectedEarning)"' in html
    assert "{{ disclosureConsensusNote(selectedEarning) }}" in html
    # And it sits next to the Longbridge block it replaces (same section title),
    # so a reader keeps the label the neighbouring quarter showed.
    start = html.index('hasLongbridgeConsensus(selectedEarning)"')
    note_at = html.index('disclosureConsensusNote(selectedEarning)"')
    assert 0 < note_at - start < 12000

