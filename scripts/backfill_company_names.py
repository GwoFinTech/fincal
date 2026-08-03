#!/usr/bin/env python3
"""Backfill missing company names from Longbridge static data.

Also normalizes HK symbol padding (1.HK -> 0001.HK, 700.HK -> 0700.HK) and
merges duplicate rows after normalization so the calendar keys stay canonical.
"""
import json
import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor  # noqa: E402
from app.symbol import normalize  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_names")


def lb_static_name(lb_symbol: str) -> str:
    """Return the name from `longbridge static` JSON or '' on failure."""
    try:
        p = subprocess.run(
            ["longbridge", "static", lb_symbol, "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if p.returncode != 0:
            return ""
        rows = json.loads(p.stdout or "[]")
        if rows and isinstance(rows, list):
            return str(rows[0].get("name", "")).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("static lookup failed for %s: %s", lb_symbol, e)
    return ""


def normalize_hk_rows() -> None:
    """Rewrite unpadded HK symbols to canonical 4-digit and merge duplicates."""
    with db_cursor() as cur:
        cur.execute(
            """SELECT id, symbol, market FROM earnings
               WHERE market = 'HK' AND symbol ~ '^[0-9]{1,3}\\.HK$'"""
        )
        rows = cur.fetchall()
    by_canonical = {}
    for row in rows:
        canonical = normalize(row["symbol"], "HK")
        by_canonical.setdefault(canonical, []).append(row)

    for canonical, dupes in by_canonical.items():
        ids = [d["id"] for d in dupes]
        with db_cursor() as cur:
            # 1) Merge each non-canonical row into an existing canonical row
            #    when the natural key already exists, else rewrite it in place.
            cur.execute(
                """SELECT id FROM earnings WHERE symbol = %s AND market = 'HK' AND id <> ALL(%s)""",
                (canonical, ids),
            )
            existing_rows = cur.fetchall()
            for d in dupes:
                # 1a) Merge values from the dupe row into a same-key canonical row.
                cur.execute(
                    """UPDATE earnings m SET
                         company_name = CASE WHEN m.company_name = '' THEN e.company_name ELSE m.company_name END,
                         before_after = COALESCE(m.before_after, e.before_after),
                         fiscal_year = COALESCE(m.fiscal_year, e.fiscal_year),
                         fiscal_quarter = COALESCE(m.fiscal_quarter, e.fiscal_quarter),
                         eps_estimate = COALESCE(m.eps_estimate, e.eps_estimate),
                         eps_actual = COALESCE(m.eps_actual, e.eps_actual),
                         revenue_estimate = COALESCE(m.revenue_estimate, e.revenue_estimate),
                         revenue_actual = COALESCE(m.revenue_actual, e.revenue_actual),
                         is_predicted = m.is_predicted AND e.is_predicted
                       FROM earnings e
                       WHERE e.id = %s AND m.symbol = %s AND m.market = 'HK'
                         AND m.report_date = e.report_date AND m.report_type = e.report_type
                         AND m.id <> e.id""",
                    (d["id"], canonical),
                )
                # 1b) Drop the dupe row once merged.
                cur.execute(
                    """DELETE FROM earnings e
                       USING earnings m
                       WHERE e.id = %s AND m.symbol = %s AND m.market = 'HK'
                         AND m.report_date = e.report_date AND m.report_type = e.report_type
                         AND m.id <> e.id""",
                    (d["id"], canonical),
                )
            # 2) Delete rows that were merged onto an existing canonical row.
            cur.execute(
                """DELETE FROM earnings e
                   WHERE e.symbol = %s AND e.market = 'HK' AND e.id = ANY(%s)
                     AND EXISTS (SELECT 1 FROM earnings m
                                 WHERE m.symbol = %s AND m.market = 'HK'
                                   AND m.report_date = e.report_date AND m.report_type = e.report_type
                                   AND m.id <> e.id)""",
                (canonical, ids, canonical),
            )
            # 3) Rewrite remaining (no natural-key collision) rows to canonical.
            cur.execute(
                """UPDATE earnings SET symbol = %s
                   WHERE market = 'HK' AND id = ANY(%s) AND symbol <> %s""",
                (canonical, ids, canonical),
            )
        logger.info("normalized %s -> %s (%d rows)", ",".join(d["symbol"] for d in dupes), canonical, len(ids))


def backfill_names() -> int:
    """Fetch Longbridge static names for rows without one; returns count filled."""
    with db_cursor() as cur:
        cur.execute(
            """SELECT DISTINCT symbol, market FROM earnings
               WHERE (company_name IS NULL OR company_name = '')
               ORDER BY market, symbol"""
        )
        targets = cur.fetchall()

    filled = 0
    for t in targets:
        symbol, market = t["symbol"], t["market"]
        lb_sym = symbol if market == "US" else (symbol.split(".")[0].lstrip("0") or "0")
        name = lb_static_name(f"{lb_sym}.{market}" if market == "HK" else lb_sym)
        if not name:
            continue
        with db_cursor() as cur:
            cur.execute(
                """UPDATE earnings SET company_name = %s
                   WHERE symbol = %s AND market = %s AND (company_name IS NULL OR company_name = '')""",
                (name, symbol, market),
            )
        filled += 1
        logger.info("filled %s (%s) -> %s", symbol, market, name)
    logger.info("backfill complete: %d symbols filled", filled)
    return filled


if __name__ == "__main__":
    normalize_hk_rows()
    backfill_names()
