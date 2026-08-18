#!/usr/bin/env python3
"""Persist Longbridge quarterly consensus and forecast-EPS revision ranges."""
import json
import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import db_cursor, init_db
from app.symbol import normalize
from app.phase3 import rating_row
from app.sync_audit import finish_run, start_run
from app.watchlist import get_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def number(value):
    """Convert provider numeric strings without turning missing values into zero."""
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


def integer(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def lb_symbol(symbol, market):
    return f"{symbol}.US" if market == "US" else f"{symbol.split('.')[0].lstrip('0')}.HK"


def provider_json(command, symbol, market, *, pace_seconds):
    """Call a rate-limited Longbridge endpoint with bounded 429002 retries."""
    target = lb_symbol(symbol, market)
    proc = None
    for attempt in range(4):
        proc = subprocess.run(
            ["longbridge", command, target, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            if pace_seconds:
                time.sleep(pace_seconds)
            return json.loads(proc.stdout)
        if "429002" not in proc.stderr or attempt == 3:
            raise RuntimeError(proc.stderr[:300] or f"{command} exited {proc.returncode}")
        delay = 20 * (attempt + 1)
        log.warning("%s rate limited for %s; retrying in %ss", command, symbol, delay)
        time.sleep(delay)
    raise RuntimeError(f"{command} failed for {target}")  # defensive; loop always returns/raises


def consensus_rows(symbol, market, data):
    rows = []
    for period in data.get("list", []):
        fiscal_year, fiscal_quarter = period.get("fiscal_year"), period.get("fiscal_period")
        if not fiscal_year or not fiscal_quarter:
            continue
        values = {detail.get("key"): number(detail.get("estimate")) for detail in period.get("details", [])}
        rows.append((
            normalize(symbol, market), market, int(fiscal_year), int(fiscal_quarter), data.get("currency"),
            values.get("eps"), values.get("normalized_eps"), values.get("revenue"), values.get("ebit"),
            values.get("net_income"), values.get("normalized_net_income"), json.dumps(period),
        ))
    return rows


def timestamp_date(value):
    """Convert Longbridge epoch-second fields to UTC dates; zero means no bound."""
    timestamp = integer(value)
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, UTC).date()


def forecast_eps_rows(symbol, market, data):
    """Map every forecast-EPS revision interval to one durable range row."""
    rows = []
    for item in data.get("items", []):
        start_date = timestamp_date(item.get("forecast_start_date"))
        if start_date is None:
            continue
        # The open-ended final interval has end=0. Store its start as the durable
        # range end; the payload preserves the original open-ended provider value.
        end_date = timestamp_date(item.get("forecast_end_date")) or start_date
        rows.append((
            normalize(symbol, market), market, start_date, end_date,
            number(item.get("forecast_eps_lowest")), number(item.get("forecast_eps_highest")),
            number(item.get("forecast_eps_mean")), number(item.get("forecast_eps_median")),
            integer(item.get("institution_total")), integer(item.get("institution_up")),
            integer(item.get("institution_down")), json.dumps(item),
        ))
    return rows


def persist(consensus, forecasts, ratings, symbols):
    from psycopg2.extras import execute_values

    with db_cursor() as cur:
        if consensus:
            execute_values(cur, """INSERT INTO earnings_consensus
                (symbol,market,fiscal_year,fiscal_quarter,currency,eps_gaap,eps_adjusted,revenue,ebit,net_income,normalized_net_income,payload)
                VALUES %s
                ON CONFLICT (symbol,market,fiscal_year,fiscal_quarter,source) DO UPDATE SET
                    currency=EXCLUDED.currency, eps_gaap=EXCLUDED.eps_gaap, eps_adjusted=EXCLUDED.eps_adjusted,
                    revenue=EXCLUDED.revenue, ebit=EXCLUDED.ebit, net_income=EXCLUDED.net_income,
                    normalized_net_income=EXCLUDED.normalized_net_income, payload=EXCLUDED.payload, fetched_at=NOW()""",
                consensus,
                page_size=200,
            )
        if forecasts:
            execute_values(cur, """INSERT INTO earnings_forecast_eps
                (symbol,market,forecast_start_date,forecast_end_date,eps_low,eps_high,eps_mean,eps_median,institution_total,institution_up,institution_down,payload)
                VALUES %s
                ON CONFLICT (symbol,market,forecast_start_date,forecast_end_date,source) DO UPDATE SET
                    eps_low=EXCLUDED.eps_low, eps_high=EXCLUDED.eps_high, eps_mean=EXCLUDED.eps_mean,
                    eps_median=EXCLUDED.eps_median, institution_total=EXCLUDED.institution_total,
                    institution_up=EXCLUDED.institution_up, institution_down=EXCLUDED.institution_down,
                    payload=EXCLUDED.payload, fetched_at=NOW()""",
                forecasts,
                page_size=200,
            )
        if ratings:
            rating_values = [row[:-1] + (json.dumps(row[-1]),) for row in ratings]
            execute_values(cur, """INSERT INTO earnings_institution_ratings
                (symbol,market,currency_symbol,target_price,strong_buy,buy,hold,underperform,sell,recommendation,provider_updated_at,payload)
                VALUES %s ON CONFLICT (symbol,market,source) DO UPDATE SET
                    currency_symbol=EXCLUDED.currency_symbol, target_price=EXCLUDED.target_price,
                    strong_buy=EXCLUDED.strong_buy, buy=EXCLUDED.buy, hold=EXCLUDED.hold,
                    underperform=EXCLUDED.underperform, sell=EXCLUDED.sell, recommendation=EXCLUDED.recommendation,
                    provider_updated_at=EXCLUDED.provider_updated_at, payload=EXCLUDED.payload, fetched_at=NOW()""",
                rating_values, page_size=200)
        if symbols:
            # The installed Longbridge CLI exposes no documented financial-guidance endpoint.
            # Persist the negative capability so clients never mistake absent data for zero guidance.
            execute_values(cur, """INSERT INTO earnings_guidance_status (symbol,market,status,reason,source,payload)
                VALUES %s ON CONFLICT (symbol,market,source) DO UPDATE SET
                    status=EXCLUDED.status, reason=EXCLUDED.reason, payload=EXCLUDED.payload, checked_at=NOW()""",
                [(symbol, market, "unavailable", "longbridge_guidance_endpoint_unavailable", "longbridge", json.dumps({"checked_command": "longbridge --help"})) for symbol, market in symbols],
                page_size=200)


def sync():
    consensus, forecasts, ratings, failures, symbols = [], [], [], [], []
    for market, market_symbols in get_source().get_symbols_by_market().items():
        for symbol in market_symbols:
            symbols.append((normalize(symbol, market), market))
            try:
                consensus.extend(consensus_rows(symbol, market, provider_json("consensus", symbol, market, pace_seconds=3)))
                forecasts.extend(forecast_eps_rows(symbol, market, provider_json("forecast-eps", symbol, market, pace_seconds=0.5)))
                ratings.append(rating_row(normalize(symbol, market), market, provider_json("institution-rating", symbol, market, pace_seconds=0.5)))
            except Exception as exc:
                failures.append(symbol)
                log.warning("consensus sync failed for %s: %s", symbol, exc)
    persist(consensus, forecasts, ratings, symbols)
    return len(consensus), len(forecasts), len(ratings), failures


if __name__ == "__main__":
    init_db()
    source = get_source()
    symbol_count = sum(len(symbols) for symbols in source.get_symbols_by_market().values())
    run = start_run("consensus", "longbridge", symbol_count=symbol_count,
                     idempotency_key="longbridge:consensus:full")
    if run is None:
        log.info("consensus sync already running, skipping")
        sys.exit(0)
    try:
        consensus_count, forecast_count, rating_count, failed = sync()
        finish_run(
            run,
            status="failed" if failed else "success",
            record_count=consensus_count + forecast_count + rating_count,
            details={"consensus_records": consensus_count, "forecast_eps_records": forecast_count, "institution_rating_records": rating_count, "guidance_status": "unavailable", "failed_symbols": failed},
            error_code="consensus_symbol_fetch_failed" if failed else None,
        )
        if failed:
            sys.exit(1)
    except Exception:
        finish_run(run, status="failed", error_code="consensus_sync_failed")
        raise
    log.info("consensus synced: %s quarterly rows, %s forecast-EPS rows", consensus_count, forecast_count)
