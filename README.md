# fincal

A financial earnings calendar with iCal subscription support.

fincal aggregates earnings report dates from [Longbridge](https://longbridge.com) and [Futu OpenD](https://openapi.futunn.com), predicts future report dates from historical patterns, and lets users manage a personal watchlist with a shareable iCal feed — so earnings dates show up right in your calendar app.

## Features

- **Earnings Calendar** — month/week view with report dates, EPS & revenue estimates, actuals, and surprise percentages
- **Prediction Engine** — predicts future earnings dates from historical quarterly patterns (median month/day + year offset)
- **Watchlist** — personal stock list with per-user persistence
- **iCal Subscription** — one-click `.ics` feed URL for Apple Calendar, Google Calendar, Outlook, etc.
- **Pluggable Watchlist Source** — read symbols from an external database or any HTTP API ([docs](docs/watchlist-source.md))
- **Data Sources** — Longbridge CLI (primary) + Futu OpenD (actuals & date confirmation), with automatic fallback

## Authentication

fincal uses **Traefik forwardAuth** for user authentication — the reverse proxy delegates login to an external auth service, which injects user identity via HTTP headers (`X-User-Id`, `X-User-Email`, `X-User-Name`).

We recommend pairing fincal with **[kazusa-home-portal](https://github.com/GwoFinTech/kazusa-home-portal)**, which provides Google OAuth login, a service dashboard, and Traefik forwardAuth middleware out of the box. Set `AUTH_MIDDLEWARE=kazusa-auth@docker` in your `.env` to connect.

Any forwardAuth-compatible service works — set `AUTH_MIDDLEWARE` to your middleware name and `AUTH_LOGIN_URL` to your login page.

For single-user or local-only setups, leave `AUTH_LOGIN_URL` empty to run without authentication.

## Quick Start

### Prerequisites

- Docker & Docker Compose
- PostgreSQL (or use the included `docker-compose.yml` with an external PG instance)
- [Longbridge CLI](https://github.com/longportapp/openapi-sdk/tree/main/longbridge-cli) (optional, for earnings data sync)
- [Futu OpenD](https://openapi.futunn.com) (optional, for actual EPS/revenue)

### 1. Clone & Configure

```bash
git clone https://github.com/GwoFinTech/fincal.git
cd fincal
cp .env.example .env
# Edit .env — set DOMAIN, DB credentials, ICAL_BASE_URL
```

### 2. Initialize Database

```bash
createdb fincal
# Tables are auto-created on first startup
```

### 3. Deploy

```bash
docker compose up -d
```

The app is now at `https://your-domain`.

### 4. Seed Data (optional)

```bash
# Sync earnings from Longbridge
python scripts/sync_earnings.py

# Sync from Futu OpenD (requires Futu OpenD running)
python scripts/sync_futu.py

# Predict future dates from historical patterns
python scripts/predict_earnings.py

# Or run the pipeline (per-stage timeouts, failure isolation, stage summary)
bash scripts/sync_all.sh

# Verify every stage actually ran recently (read-only; exit 1 when a stage is
# stale, 2 when the check itself cannot reach the database)
python scripts/check_sync_freshness.py
```

### Sync scheduling contract

`scripts/cron_sync.sh` is the **only** scheduling entrypoint. A scheduler must
call exactly that file and must not keep its own stage list:

```bash
# weekly, e.g. `0 8 * * 1` — the whole scheduled job, nothing else
bash /opt/fincal/scripts/cron_sync.sh
```

The entrypoint runs the pipeline and then the freshness gate:

1. `scripts/sync_all.sh` — the pipeline. The stage order inside it is the
   contract, and it is the **only** place the stage list is written down:

   | Stage (`sync_runs.stage`) | Script | Default budget |
   |---|---|---|
   | `longbridge` | `scripts/sync_earnings.py` | 900s |
   | `futu` | `scripts/sync_futu.py` | 1500s |
   | `stock_names` | `scripts/sync_stock_names.py` | 900s |
   | `consensus` | `scripts/sync_consensus.py` | 2400s |
   | `prediction` | `scripts/predict_earnings.py` | 600s |

   Every stage runs under `timeout` (override with
   `FINCAL_STAGE_TIMEOUT_<STAGE>`, e.g. `FINCAL_STAGE_TIMEOUT_CONSENSUS=3600`),
   a failing or timed-out stage never stops the remaining stages, a failed
   stage keeps its per-stage log under `/tmp/fincal-sync.*/`, and the run ends
   with a `stage summary` table.

2. `scripts/check_sync_freshness.py` — the hard gate, run **after** the stages
   so a first catch-up run is judged on the data it just wrote. Its non-zero
   exit (a stage that stopped running, or a derived table that is stale) plus
   the stage failure above are merged into the job's exit code, so a silent gap
   becomes a failed run that names the stale stages.

Adding a stage means adding it to `sync_all.sh` **and** registering it in
`app/freshness.py::STAGE_SCRIPTS` — `tests/test_sync_freshness.py` and
`tests/test_sync_scripts.py` fail if the two drift apart, and the latter also
fails if the entrypoint stops covering a stage or stops calling the gate. The
tests cannot see the scheduler host though, so `scripts/deploy.sh` prints the
deployed entrypoint, its stage list and the hash of both sync scripts, and warns
when a scheduler-side wrapper still carries its own stage list — that is how
`consensus` and `stock_names` once went 47 days without a run while every health
endpoint stayed green (Issue #57).

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Longbridge  │     │  Futu OpenD  │     │  HTTP API    │
│    CLI       │     │              │     │  (watchlist) │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                    │                     │
       ▼                    ▼                     ▼
┌─────────────────────────────────────────────────────┐
│                   fincal (FastAPI)                   │
│  ┌─────────┐  ┌───────────┐  ┌──────────────────┐  │
│  │ Earnings │  │ Prediction │  │  Watchlist Source │  │
│  │   Sync   │  │   Engine   │  │  (tsummt / http) │  │
│  └─────────┘  └───────────┘  └──────────────────┘  │
│                                                     │
│  ┌─────────────┐  ┌──────────┐  ┌───────────────┐  │
│  │  REST API   │  │  iCal    │  │  Vue3 SPA     │  │
│  │  /api/*     │  │  /ical/* │  │  (static)     │  │
│  └─────────────┘  └──────────┘  └───────────────┘  │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
                   PostgreSQL
```

## Configuration

All configuration is via environment variables. See [`.env.example`](.env.example) for the full list.

| Variable | Default | Description |
|----------|---------|-------------|
| `DOMAIN` | — | Public domain (Traefik routing) |
| `AUTH_MIDDLEWARE` | — | Traefik forwardAuth middleware name |
| `AUTH_LOGIN_URL` | *(empty)* | Auth login page URL (empty = no login button) |
| `DB_HOST` | `localhost` | PostgreSQL host |
| `DB_PORT` | `5432` | PostgreSQL port |
| `DB_NAME` | `fincal` | Database name |
| `DB_USER` | `postgres` | Database user |
| `DB_PASSWORD` | *(empty)* | Database password |
| `WATCHLIST_SOURCE` | `tsummt` | `tsummt` or `http` ([docs](docs/watchlist-source.md)) |
| `ICAL_BASE_URL` | — | Public URL for iCal feeds |
| `FUTU_HOST` | `127.0.0.1` | Futu OpenD host |
| `FUTU_PORT` | `11112` | Futu OpenD port |
| `FUTU_DATES_TIMEOUT_SECONDS` | `15` | Per-symbol earnings-date call watchdog |
| `FUTU_ACTUALS_TIMEOUT_SECONDS` | `20` | Per-symbol EPS/revenue call watchdog |
| `SYNC_STAGE_STALE_AFTER_HOURS` | `192` | Sync stage / derived-data staleness threshold (8 days) |
| `SYNC_RUNS_WINDOW_HOURS` | `336` | `/api/admin/diagnostics` sync-run window (14 days) |

## Watchlist Source

fincal supports pluggable watchlist backends. The default (`tsummt`) reads from a PostgreSQL table; the `http` backend fetches from any JSON API.

See [docs/watchlist-source.md](docs/watchlist-source.md) for configuration and how to add custom sources.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/config` | No | Public app config |
| `GET` | `/api/me` | Yes | Current user info + iCal URL |
| `GET` | `/api/earnings` | Yes | Earnings calendar data |
| `GET` | `/api/watchlist` | Yes | User's watchlist |
| `POST` | `/api/watchlist` | Yes | Add to watchlist |
| `DELETE` | `/api/watchlist` | Yes | Remove from watchlist |
| `GET` | `/api/search` | Yes | Search stocks |
| `GET` | `/api/export` | Yes | Export as CSV/JSON |
| `GET` | `/api/popular` | Yes | Default popular stocks |
| `GET` | `/ical/{token}` | No | iCal subscription feed |

## Development

```bash
# Install dependencies
uv sync

# Run locally (requires PostgreSQL)
DB_HOST=localhost uv run uvicorn app.main:app --reload

# Run sync scripts
DB_HOST=localhost python scripts/sync_earnings.py
DB_HOST=localhost python scripts/predict_earnings.py
```

### Weekly sync behavior

The Longbridge sync is required. Futu is an optional enrichment source: before
constructing its client, the script performs a 3-second TCP preflight against
`FUTU_HOST:FUTU_PORT`. When OpenD is unavailable, the Futu stage is logged as
skipped and the Longbridge/prediction stages continue instead of waiting for
the OpenD client's indefinite reconnect loop.

Each per-symbol OpenD call is additionally bounded by a wall-clock watchdog
(`FUTU_DATES_TIMEOUT_SECONDS` / `FUTU_ACTUALS_TIMEOUT_SECONDS`, Issue #48). The
watchdog raises a catchable `TimeoutError`, so a single wedged symbol is counted
as a failed symbol and the rest of the batch continues — it no longer kills the
whole process and strands a `running` audit row that would block every later
sync. The audited run is also wrapped in a `finally` that forces a terminal
state on any exit path.

### Sync freshness monitoring (Issue #53)

Dependency probes alone cannot tell "the dependencies are up" from "the pipeline
has been silently skipping a stage", and a fixed 24-hour run window is empty
~6 days out of 7 for a weekly pipeline. `app/freshness.py` therefore answers a
third question with **SELECT-only** queries and no external calls: *when did each
declared stage last succeed, and how old is the data it derives?*

- Staleness is decided from the **most recent success** per stage
  (`sync_runs.status='success'`), never from a fixed window; a stage with no
  successful run at all is reported as `never`.
- Derived tables rendered to users are checked too (`earnings_consensus`,
  `earnings_forecast_eps`, `earnings_institution_ratings`, `stock_names` on
  `MAX(fetched_at)`, `earnings` on `MAX(updated_at)`), so a fresh stage with
  stale output still shows up.
- Threshold: `SYNC_STAGE_STALE_AFTER_HOURS` (default `192` = weekly + 1 day of
  grace). A stage or table older than that is `stale`.
- `/api/admin/health` gains a `checks.sync_freshness` entry (aggregate status,
  stage names and a language-neutral `error_code` such as `sync_stage_stale` —
  no per-stage timestamps, since the endpoint has no application-level auth) and
  `status` becomes non-`healthy` when anything is stale. `/api/admin/ready`
  stays dependency-only: staleness degrades reporting, it never fails readiness
  and never blocks a sync.
- `/api/admin/diagnostics` keeps `sync_runs_24h` and additionally reports
  `sync_runs_window` over `SYNC_RUNS_WINDOW_HOURS` (default `336` = 14 days),
  plus the full `freshness` summary with per-stage `last_success_at` / `age_hours`.
- `scripts/check_sync_freshness.py` prints the per-stage table and exits `1`
  when a stage or derived table is stale (`2` when the check itself cannot reach
  the database), so wiring it into the cron wrapper turns a silent gap into a
  next-run failure.

```bash
DB_HOST=localhost uv run python scripts/check_sync_freshness.py
# sync freshness (threshold 192h, checked 2026-09-17T18:34:31+00:00)
# name                             kind     status   age_hours  last_success_at
# longbridge                       stage    fresh        47.82  2026-09-15T18:45:27+00:00
# futu                             stage    fresh        47.69  2026-09-15T18:53:22+00:00
# stock_names                      stage    stale      1088.71  2026-08-03T09:51:57+00:00
# consensus                        stage    stale      1113.11  2026-08-02T09:27:43+00:00
# prediction                       stage    fresh        90.44  2026-09-14T00:08:00+00:00
# earnings_consensus               derived  stale      1113.11  2026-08-02T09:27:41+00:00
# earnings                         derived  fresh        47.69  2026-09-15T18:53:22+00:00
# …
# STALE: stage 'consensus' last succeeded at 2026-08-02T09:27:43+00:00 (1113.11h ago)
# exit=1
```

Timestamps are compared and printed in UTC regardless of the database session
timezone, so the ages are stable.

## Tech Stack

- **Backend:** Python 3.12, FastAPI, psycopg2, uvicorn
- **Frontend:** Vue 3 (CDN), single-file HTML SPA
- **Database:** PostgreSQL 13+
- **Deployment:** Docker Compose, Traefik v3
- **Data:** Longbridge CLI, Futu OpenD API

## License

[Apache License 2.0](LICENSE)
