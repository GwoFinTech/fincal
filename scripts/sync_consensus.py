#!/usr/bin/env python3
"""Persist Longbridge per-quarter consensus for the managed universe."""
import json, logging, os, subprocess, sys, time
from decimal import Decimal, InvalidOperation
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import db_cursor, init_db
from app.watchlist import get_source
from app.symbol import normalize
from app.sync_audit import start_run, finish_run
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger(__name__)

def number(value):
    try: return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError): return None

def lb_symbol(symbol, market):
    return f"{symbol}.US" if market == "US" else f"{symbol.split('.')[0].lstrip('0')}.HK"

def sync():
    rows=[]; failures=[]
    for market, symbols in get_source().get_symbols_by_market().items():
        for symbol in symbols:
            try:
                p = None
                for attempt in range(4):
                    p=subprocess.run(["longbridge","consensus",lb_symbol(symbol,market),"--format","json"],capture_output=True,text=True,timeout=30)
                    if p.returncode == 0:
                        break
                    if "429002" not in p.stderr or attempt == 3:
                        raise RuntimeError(p.stderr[:120])
                    delay = 20 * (attempt + 1)
                    log.warning("consensus rate limited for %s; retrying in %ss", symbol, delay)
                    time.sleep(delay)
                assert p is not None
                data=json.loads(p.stdout)
                time.sleep(3)  # Longbridge consensus endpoint enforces a tight per-minute quota
                for period in data.get("list",[]):
                    fy,fq=period.get("fiscal_year"),period.get("fiscal_period")
                    if not fy or not fq: continue
                    values={d.get("key"):number(d.get("estimate")) for d in period.get("details",[])}
                    rows.append((normalize(symbol,market),market,int(fy),int(fq),data.get("currency"),values.get("eps"),values.get("normalized_eps"),values.get("revenue"),values.get("ebit"),values.get("net_income"),values.get("normalized_net_income"),json.dumps(period)))
            except Exception as exc:
                failures.append(symbol); log.warning("consensus failed %s: %s",symbol,exc)
    from psycopg2.extras import execute_values
    with db_cursor() as cur:
        execute_values(cur,"""INSERT INTO earnings_consensus (symbol,market,fiscal_year,fiscal_quarter,currency,eps_gaap,eps_adjusted,revenue,ebit,net_income,normalized_net_income,payload)
VALUES %s ON CONFLICT (symbol,market,fiscal_year,fiscal_quarter,source) DO UPDATE SET currency=EXCLUDED.currency,eps_gaap=EXCLUDED.eps_gaap,eps_adjusted=EXCLUDED.eps_adjusted,revenue=EXCLUDED.revenue,ebit=EXCLUDED.ebit,net_income=EXCLUDED.net_income,normalized_net_income=EXCLUDED.normalized_net_income,payload=EXCLUDED.payload,fetched_at=NOW()""",rows,page_size=200)
    return len(rows),failures
if __name__=='__main__':
    init_db(); source=get_source(); n=sum(len(v) for v in source.get_symbols_by_market().values()); run=start_run('consensus','longbridge',symbol_count=n)
    try:
        total, failed=sync(); finish_run(run,status='failed' if failed else 'success',record_count=total,details={'failed_symbols':failed},error_code='consensus_symbol_fetch_failed' if failed else None)
        if failed: sys.exit(1)
    except Exception:
        finish_run(run,status='failed',error_code='consensus_sync_failed'); raise
    log.info('consensus synced: %s records',total)
