# Watchlist Data Source Configuration

fincal fetches its stock universe (the set of symbols to track earnings for) from a
pluggable **watchlist source**.  Two backends ship out of the box and you can add your
own by subclassing `WatchlistSource`.

---

## Quick Start

| Source | Env vars | Description |
|--------|----------|-------------|
| **tsummt** (default) | — | Reads `tsummt.watchlist` table directly from PostgreSQL |
| **http** | `WATCHLIST_HTTP_URL` | Fetches JSON from any HTTP endpoint |

```bash
# Default — tsummt DB
WATCHLIST_SOURCE=tsummt

# Remote HTTP API
WATCHLIST_SOURCE=http
WATCHLIST_HTTP_URL=https://example.com/api/watchlist
WATCHLIST_HTTP_FIELD=code          # JSON key for symbol code (default: "code")
```

---

## Source: `tsummt` (default)

Connects to the **tsummt** PostgreSQL database and reads the `watchlist` table that
the [tsummt-web](https://github.com/GwoFinTech/tsummt-web) application manages.

**Requires:** network access to the tsummt Postgres instance.

**Config env vars:**

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_HOST` | `host.docker.internal` | Postgres host |
| `DB_PORT` | `5432` | Postgres port |
| `DB_USER` | `postgres` | Postgres user |
| `DB_PASSWORD` | *(empty)* | Postgres password |
| `TSUMMT_DB` | `tsummt` | Database name |

---

## Source: `http`

Fetches a JSON payload from a configurable URL and extracts symbol codes from it.

**Config env vars:**

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCHLIST_HTTP_URL` | *(required)* | Full URL of the watchlist API endpoint |
| `WATCHLIST_HTTP_FIELD` | `code` | JSON object key that contains the symbol code |

### Accepted Response Formats

The HTTP source auto-detects these shapes:

**1. Flat string array**

```json
["AAPL.US", "0700.HK", "TSLA.US"]
```

**2. Object array**

```json
[
  {"code": "AAPL.US", "name": "Apple"},
  {"code": "0700.HK", "name": "Tencent"}
]
```

The field name defaults to `code` — change it with `WATCHLIST_HTTP_FIELD`.

**3. Wrapped payloads**

```json
{"symbols": ["AAPL.US", "0700.HK"]}
{"data": [{"code": "AAPL.US"}]}
{"items": ["TSLA.US"]}
{"list": [{"code": "MSFT.US"}]}
```

Recognised wrapper keys: `symbols`, `data`, `items`, `list`.

---

## Adding a Custom Source

1. Create `app/watchlist/my_source.py`:

```python
from .base import WatchlistSource

class MyWatchlistSource(WatchlistSource):
    def fetch_symbols(self) -> list[str]:
        # Return codes in TICKER.MARKET format
        return ["AAPL.US", "0700.HK"]
```

2. Register it in `app/watchlist/loader.py`:

```python
def _create_source() -> WatchlistSource:
    src = config.WATCHLIST_SOURCE.lower().strip()
    if src == "my_source":
        from .my_source import MyWatchlistSource
        return MyWatchlistSource()
    # ... existing branches
```

3. Set `WATCHLIST_SOURCE=my_source` in your `.env`.

---

## Interface Contract

All sources implement `WatchlistSource` (defined in `app/watchlist/base.py`):

```python
class WatchlistSource(ABC):
    @abstractmethod
    def fetch_symbols(self) -> list[str]:
        """Return raw symbol codes.  TICKER.MARKET format preferred."""
        ...

    def get_symbols(self) -> list[str]:
        """Raw codes (cached)."""

    def get_symbols_by_market(self) -> dict[str, list[str]]:
        """{'US': ['AAPL', ...], 'HK': ['0700.HK', ...]}"""

    def get_futu_symbols(self) -> list[str]:
        """Canonical format: AAPL.US, 0700.HK"""

    def refresh(self) -> None:
        """Bust the in-memory cache."""
```

The base class provides `get_symbols_by_market()` and `get_futu_symbols()` as
derived methods — subclasses only need `fetch_symbols()`.

---

## How fincal Uses the Watchlist

| Component | Method used | Purpose |
|-----------|-------------|---------|
| `app/universe.py` | `get_symbols_by_market_with_status()` | Live default calendar/export/`/api/popular` universe, cached for `UNIVERSE_CACHE_TTL_SECONDS` (Issue #58) |
| `scripts/sync_futu.py` | `get_futu_symbols()` | Futu earnings date sync universe |
| `scripts/predict_earnings.py` | `get_symbols_by_market()` | Prediction universe |
| `scripts/sync_earnings.py` | *(market-based, no watchlist filter)* | Longbridge full-market sync |

The sync scripts don't filter by watchlist — they fetch the full market calendar
from Longbridge/Futu and upsert everything.  The watchlist controls **what the
UI shows by default** and **what Futu syncs** (Futu requires explicit symbol lists).

The read path (`app/universe.py`) used to be bound once at import time, which
meant an upstream add/remove — or a source that was unreachable during startup —
stayed frozen in the default calendar and the export until the next container
restart.  It now reads through a short TTL cache and is invalidated explicitly
after `POST/PUT/DELETE /api/admin/watchlist`, so a change is visible on the next
request; a degraded read keeps the last good (or hardcoded fallback) universe and
is reported in `/api/admin/diagnostics` under `universe.error_code`.

## Market identity of a code (Issue #66)

FinCal's calendar, sync and iCal feed only exist for **US** and **HK**, so the
market of a code is decided by its suffix in exactly one place —
`app/symbol.market_of()` — shared by the read path
(`group_symbols_by_market_with_skipped`) and the Futu sync
(`get_futu_symbols_with_skipped`):

| Code | Market | Note |
|------|--------|------|
| `AAPL`, `AAPL.US`, `US.AAPL` | US | bare tickers and the `TICKER.US` / `US.TICKER` spellings normalize to the bare ticker |
| `0700.HK`, `700.HK`, `HK.00700` | HK | normalized to the canonical 4-digit form |
| `BRK.A`, `BF.B`, `MKC.V`, `ETSS.U` | US | a single-letter suffix is a class-share/unit spelling, not a market |
| `000651.SZ`, `600519.SH`, `.SS`, `.BJ`, … | — | another exchange: **dropped**, reported, never presented as US |

An upstream watchlist that contains A-share codes (the tsummt watchlist does)
therefore no longer produces a `US` bucket full of codes that can never have an
earnings row.  The drop is never silent: it is logged and exposed in
`/api/admin/diagnostics` as `universe.skipped_count` / `universe.skipped_symbols`.
The write paths (`POST /api/watchlist`, `POST/PUT /api/admin/watchlist`) reject a
code whose suffix contradicts the requested market with the language-neutral
error code `symbol_market_mismatch` (400 / 422) instead of persisting a dead
entry.
