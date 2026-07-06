"""Unified stock symbol format conversion.

Internal canonical format: TICKER.MARKET  (e.g. AAPL.US, 0700.HK)
Futu API format:          MARKET.TICKER with 5-digit HK (e.g. US.AAPL, HK.00700)
Longbridge API format:    TICKER.MARKET  (same as internal — uppercase)
"""

# ── Internal → Futu ────────────────────────────────────────────────

def to_futu_code(symbol: str) -> str:
    """Convert internal symbol (AAPL.US, 0700.HK) → Futu format (US.AAPL, HK.00700).

    - Strips whitespace, uppercases
    - Swaps TICKER.MARKET → MARKET.TICKER
    - Pads HK numeric tickers to 5 digits
    """
    s = symbol.strip().upper()
    if "." in s:
        parts = s.rsplit(".", 1)
        if len(parts) == 2:
            ticker, market = parts
            if market == "HK" and ticker.isdigit():
                ticker = ticker.zfill(5)
            return f"{market}.{ticker}"
    return s


# ── Futu → Internal ────────────────────────────────────────────────

def from_futu_code(futu_code: str) -> str:
    """Convert Futu format (US.AAPL, HK.00700) → internal symbol (AAPL.US, 0700.HK).

    - Strips leading zeros from HK tickers: 00700 → 0700 (4-digit canonical)
    - Swaps MARKET.TICKER → TICKER.MARKET
    """
    s = futu_code.strip().upper()
    if "." in s:
        parts = s.split(".", 1)
        if len(parts) == 2:
            market, ticker = parts
            if market == "HK" and ticker.isdigit():
                # 00700 → 700 → 0700 (strip all zeros then pad to 4)
                ticker = (ticker.lstrip("0") or "0").zfill(4)
            return f"{ticker}.{market}"
    return s


# ── Internal normalization ─────────────────────────────────────────

def normalize(symbol: str, market: str) -> str:
    """Normalize any user-supplied symbol into canonical internal format.

    Handles:
      - HK codes with or without leading zeros (700, 0700, 00700)
      - Case insensitivity
      - With or without .HK suffix for HK codes
    """
    market = market.strip().upper()
    s = symbol.strip().upper().replace(".HK", "") if market == "HK" else symbol.strip().upper()

    if market == "HK" and s.isdigit():
        # Ensure 4-digit canonical: 700 → 0700
        s = (s.lstrip("0") or "0").zfill(4)

    return f"{s}.{market}" if market == "HK" else s


# ── Longbridge helpers ─────────────────────────────────────────────

def to_lb_symbol(symbol: str) -> str:
    """Internal symbol is already Longbridge format. Just sanitize."""
    return symbol.strip().upper()


def from_lb_counter_id(counter_id: str) -> tuple[str, str]:
    """Parse Longbridge counter_id (ST/HK/700, ST/US/AAPL) → (symbol, market).

    Returns internal canonical format.
    """
    parts = counter_id.strip().upper().split("/")
    if len(parts) != 3:
        return ("", "")
    market = parts[1]
    ticker = parts[2]
    if market == "HK" and ticker.isdigit():
        ticker = (ticker.lstrip("0") or "0").zfill(4)
    symbol = f"{ticker}.{market}" if market == "HK" else ticker
    return (symbol, market)


# ── Sortable key for HK tickers ────────────────────────────────────

def sort_key(symbol: str) -> str:
    """Provide a sortable key that handles numeric HK codes correctly.

    0700.HK should sort numerically, not lexicographically.
    """
    s = symbol.upper()
    if s.endswith(".HK"):
        num = s.replace(".HK", "")
        if num.isdigit():
            return f"HK:{int(num):06d}"
    return s
