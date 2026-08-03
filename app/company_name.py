"""Company-name resolution for FinCal symbols.

Priority order (per user requirement):
1. Kurumi API      — `/api/stock/{symbol}/overview` returns `name`
2. Longbridge CLI  — `longbridge static` returns `name` (English/display)
3. Futu OpenD      — `get_stock_basicinfo` returns display name

Every successful lookup is cached in the `stock_names` table so subsequent
runs never re-hit the upstream providers for the same symbol.
"""
from __future__ import annotations

import json
import logging
import subprocess
import urllib.request
from urllib.error import URLError

import app.config as config

logger = logging.getLogger(__name__)

# Kurumi normalizes HK symbols to unpadded form: 0700.HK -> 700.HK
def kurumi_symbol(symbol: str, market: str) -> str:
    if market == "HK":
        return (symbol.split(".")[0].lstrip("0") or "0") + ".HK"
    return symbol


def fetch_from_kurumi(symbol: str, market: str) -> str:
    """Query Kurumi /api/stock/{symbol}/overview. Returns name or ''."""
    url = f"{config.KURUMI_API_URL}/api/stock/{kurumi_symbol(symbol, market)}/overview"
    try:
        with urllib.request.urlopen(url, timeout=config.KURUMI_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        name = str(data.get("name") or "").strip()
        return name
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        logger.info("kurumi lookup failed %s: %s", symbol, exc)
        return ""


def fetch_from_longbridge(symbol: str, market: str) -> str:
    """Query `longbridge static`. Returns name or ''."""
    lb_sym = (symbol.split(".")[0].lstrip("0") or "0") if market == "HK" else symbol
    try:
        p = subprocess.run(
            ["longbridge", "static", f"{lb_sym}.{market}" if market == "HK" else lb_sym,
             "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if p.returncode != 0:
            return ""
        rows = json.loads(p.stdout or "[]")
        if rows and isinstance(rows, list):
            return str(rows[0].get("name", "")).strip()
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        logger.info("longbridge lookup failed %s: %s", symbol, exc)
    return ""


def fetch_from_futu(symbol: str, market: str) -> str:
    """Query Futu OpenD get_stock_basicinfo. Returns name or ''."""
    try:
        from futu import RET_OK, Market, SecurityType, OpenQuoteContext
        from app.symbol import to_futu_code
    except ImportError:
        return ""
    code = to_futu_code(f"{symbol}.{market}" if market == "HK" else symbol)
    ctx = None
    try:
        ctx = OpenQuoteContext(host=config.FUTU_HOST, port=config.FUTU_PORT)
        ret, data = ctx.get_stock_basicinfo(
            Market.HK if market == "HK" else Market.US,
            SecurityType.STOCK, [code],
        )
        if ret == RET_OK and data is not None and not data.empty:
            return str(data.iloc[0].get("name", "")).strip()
    except Exception as exc:  # noqa: BLE001
        logger.info("futu lookup failed %s: %s", symbol, exc)
    finally:
        if ctx is not None:
            ctx.close()
    return ""


def resolve_company_name(symbol: str, market: str) -> tuple[str, str]:
    """Resolve a company name. Returns (name, source); (\"\", \"\") if unavailable."""
    for fetcher, source in (
        (fetch_from_kurumi, "kurumi"),
        (fetch_from_longbridge, "longbridge"),
        (fetch_from_futu, "futu"),
    ):
        try:
            name = fetcher(symbol, market)
        except Exception:  # noqa: BLE001
            continue
        if name:
            return name, source
    return "", ""
