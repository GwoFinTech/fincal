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
      - US codes with a stray ``.US`` suffix (AAPL.US) or a Futu-style
        ``US.`` prefix (US.AAPL), stripping both to the canonical bare ticker
    """
    market = market.strip().upper()
    s = symbol.strip().upper()

    if market == "HK":
        s = s.replace(".HK", "")
        if s.isdigit():
            # Ensure 4-digit canonical: 700 → 0700
            s = (s.lstrip("0") or "0").zfill(4)
        return f"{s}.{market}"

    # US stocks are stored as bare tickers (no market suffix, matching the
    # earnings table). Strip any user-supplied ".US" suffix or Futu-style
    # "US." prefix so a pasted code (AAPL.US / US.AAPL) matches the canonical
    # form used by watchlist → earnings lookups (Issue #43).
    if s.startswith("US."):
        s = s[3:]
    if s.endswith(".US"):
        s = s[:-3]
    return s


# ── Market identity (Issue #66) ────────────────────────────────────

# The only markets FinCal can serve: the earnings table, the Futu/Longbridge
# sync and the iCal feed all key on US and HK.  A code from any other exchange
# has no calendar behind it, so it must never be presented as one of these.
SERVED_MARKETS = ("US", "HK")


def bare_ticker(code: str) -> str:
    """The ticker part of a code, with any market prefix/suffix removed.

    ``AAPL.US`` → ``AAPL``, ``US.AAPL`` → ``AAPL``, ``700.HK`` → ``700``.  A
    single-letter suffix stays part of the ticker (``BRK.A`` → ``BRK.A``): it is
    a class-share/unit spelling, not a market.
    """
    s = str(code).strip().upper()
    for prefix in ("US.", "HK."):
        if s.startswith(prefix) and len(s) > len(prefix):
            s = s[len(prefix):]
            break
    if "." in s:
        head, tail = s.rsplit(".", 1)
        if head and tail in SERVED_MARKETS:
            s = head
    return s


def market_of(code: str) -> str | None:
    """``'US'`` / ``'HK'`` for a code FinCal can serve, ``None`` otherwise.

    Market identity is a property of the code's suffix, and it must have exactly
    one definition: the sync path used to classify ``000651.SZ`` as "not US/HK"
    while the read path bucketed the same code as a US ticker, so the default
    universe offered 25 A-share codes that can never have an earnings row
    (Issue #66).

    * ``.HK`` → HK; ``.US`` or no suffix → US (the historical bare-ticker
      contract);
    * a single-letter suffix is a class share / unit / similar spelling of a US
      listing (``BRK.A``, ``BF.B``, ``MKC.V``, ``ETSS.U``) → US;
    * any other suffix belongs to an exchange FinCal does not serve
      (``.SZ``/``.SH``/``.SS``/``.BJ``, ``.TW``, …) → ``None``.
    """
    s = str(code).strip().upper()
    if not s:
        return None
    for prefix in ("US.", "HK."):
        if s.startswith(prefix) and len(s) > len(prefix):
            return prefix[:-1]
    if "." not in s:
        return "US"
    head, tail = s.rsplit(".", 1)
    if not head:
        return None
    if tail in SERVED_MARKETS:
        return tail
    if len(tail) == 1 and tail.isalpha():
        return "US"
    return None


def market_mismatch(symbol: str, market: str) -> bool:
    """True when ``symbol`` cannot belong to ``market`` (Issue #66).

    Guards the write paths, which used to accept ``market=US`` with an A-share
    code and persist ``600028.SH`` as a US symbol: the row then never matches an
    earnings row while every sync keeps skipping it, so the watchlist entry shows
    an empty row forever with no self-healing path.

    ``market`` is expected to be one of :data:`SERVED_MARKETS` — the callers
    report an unsupported market as a separate error code first.  An empty
    symbol is not a mismatch here either (``symbol_required`` covers it).
    """
    m = str(market).strip().upper()
    s = str(symbol).strip().upper()
    if not s:
        return False
    if m == "HK":
        # HK tickers are numeric and may be pasted bare (700 / 0700 / 00700) or
        # with the suffix; anything else is another market's code.
        return not (s.endswith(".HK") or ("." not in s and s.isdigit()))
    if m == "US":
        return market_of(s) != "US"
    return False


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


# ── Dirty (non-canonical) HK codes ─────────────────────────────────

def is_dirty_hk_5digit(code: str) -> bool:
    """True for a five-digit HK code that ``normalize()`` would rename.

    ``00700`` is a provider's zero-padded spelling of ``0700.HK`` and therefore
    dirty; a legitimate five-digit HKEX code such as ``82333`` (the RMB counter
    of ``2333.HK``, the ``8xxxx`` series) is mapped to itself and must never be
    treated as a duplicate (Issue #54).
    """
    c = code.strip().upper()
    if c.endswith(".HK"):
        c = c[: -len(".HK")]
    return c.isdigit() and len(c) == 5 and normalize(c, "HK") != f"{c}.HK"


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
