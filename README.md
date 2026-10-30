# fincal

A financial earnings calendar with iCal subscription support.

fincal aggregates earnings report dates from [Longbridge](https://longbridge.com) and [Futu OpenD](https://openapi.futunn.com), predicts future report dates from historical patterns, and lets users manage a personal watchlist with a shareable iCal feed — so earnings dates show up right in your calendar app.

## Features

- **Earnings Calendar** — month/week view with report dates, EPS & revenue estimates, actuals, and surprise percentages
- **Prediction Engine** — predicts future earnings dates by projecting the company's own historical report dates for that quarter onto the target year (nearest to the median month/day expectation, never a weekend the company does not use)
- **Watchlist** — personal stock list with per-user persistence
- **iCal Subscription** — one-click `.ics` feed URL for Apple Calendar, Google Calendar, Outlook, etc.
- **Pluggable Watchlist Source** — read symbols from an external database or any HTTP API ([docs](docs/watchlist-source.md))
- **Data Sources** — Longbridge CLI (primary) + Futu OpenD (actuals & date confirmation), with automatic fallback

## Authentication

fincal uses **Traefik forwardAuth** for user authentication — the reverse proxy delegates login to an external auth service, which injects user identity via HTTP headers (`X-User-Id`, `X-User-Email`, `X-User-Name`).

We recommend pairing fincal with **[kazusa-home-portal](https://github.com/GwoFinTech/kazusa-home-portal)**, which provides Google OAuth login, a service dashboard, and Traefik forwardAuth middleware out of the box. Set `AUTH_MIDDLEWARE=kazusa-auth@docker` in your `.env` to connect.

Any forwardAuth-compatible service works — set `AUTH_MIDDLEWARE` to your middleware name and `AUTH_LOGIN_URL` to your login page.

For single-user or local-only setups, leave `AUTH_LOGIN_URL` empty to run without authentication.

### Identity trust boundary

The four `X-User-*` headers are an *assertion*, and only Traefik can be trusted
to make it. Any container on the same Docker network can reach
`http://fincal:8000` directly and write those headers itself — on a shared
`traefik_traefik-net` bridge that means every co-tenant service, a stray
`docker run`, or a compromised container could send `X-User-Role: admin` and be
treated as an administrator (Issue #102). Three rules follow:

1. **Nothing outside the proxy's chain should be able to reach the container on
   a shared network without a verified assertion.** Set `AUTH_TRUST_SECRET` (a
   random ASCII string, 32+ chars) and the app refuses any request that does not
   carry it in `AUTH_TRUST_HEADER` (`X-Fincal-Proxy` by default) — `401
   untrusted_proxy` _before_ it reads any identity header. `docker-compose.yml`
   already ships the matching `fincal-identity` headers middleware on the main
   route, so the secret only has to be set in `.env`: it is read by both the
   label and the container.
2. **The two sides must be configured as a pair.** Env without the middleware
   (or a middleware whose value differs) is fail-closed: every authenticated
   request answers 401. `scripts/deploy.sh` therefore runs
   `scripts/check_auth_trust.py`, which reads the value Traefik will inject from
   the container's own labels, asks the container whether it accepts exactly
   that value, replays a forged request (must be 401 `untrusted_proxy`), and
   fails the deploy on any disagreement. Secrets are never printed. Leaving the
   secret empty keeps the old behaviour and is the right choice for a
   single-user deployment on a private network.
3. **Malformed identity is a client error.** `X-User-Id: abc`, `0` or `-1`
   answers `401 invalid_identity`; it used to fall out of `int()` as a 500
   `internal_error` with a stack trace. `/ical/{token}` (the token is the
   credential) and `/api/config` never consult the boundary, and
   `/api/admin/health` stays unauthenticated by design (aggregate probe data
   only, Issue #79) — a request that skips the proxy can still read dependency
   health, which is why network isolation (a dedicated bridge shared only with
   Traefik) remains the long-term fix.

Enabling or rotating the secret requires recreating the container
(`docker compose up -d` / `scripts/deploy.sh`), because both the label and the
env are fixed at that point. Rotate it together with the iCal tokens if a
co-tenant container is ever suspected of being compromised.

### Subscription tokens

`GET /ical/{token}` is public by design: the token *is* the credential, and with
`scope=all` it reads the whole **system universe** (`app.universe.popular_stocks()`
— the same 112-symbol list the calendar, `/api/export` and the read-only
「系统自选范围」 page use, Issue #100). Two consequences are baked in:

- **Tokens never reach a log line.** `app/log_redaction.py` installs a filter on
  `uvicorn.access` (at `import app.main`, so plain `uvicorn app.main:app` is
  covered), the feed's timeout warning logs an 8-hex fingerprint instead of the
  token, and provider/watchlist URLs are logged without their query string.
- **Tokens are rotatable.** `users.ical_token` is no longer write-once:

  ```bash
  python scripts/rotate_ical_tokens.py                     # dry run, all users
  python scripts/rotate_ical_tokens.py --user-id 3 --apply
  python scripts/rotate_ical_tokens.py --email a@b.c --apply
  docker restart fincal    # drops the feed cache the old URL is still served from
  ```

  The new URL is published by `GET /api/me` (`ical_url`); the script prints only
  fingerprints. Restarting is required after an out-of-process rotation because
  the feed cache lives in the server's memory — until then (or until the 3600 s
  TTL) the old URL keeps answering.

Rotating invalidates existing calendar subscriptions (clients must re-subscribe
from the URL in the app). After rotating, historical `docker logs` still hold
the old value, so treat that log as compromised and rotate it out; if Traefik's
access log is ever enabled, keep `/ical/` out of it or apply the same redaction.

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

   The budgets are *exported* and read again by the stage scripts
   (`app.config.stage_timeout()` → `start_run(timeout_seconds=…)`), so the
   stage's own audit row declares the budget it really ran under (Issue #78).
   The shell defaults and `app/config.py::STAGE_TIMEOUT_SECONDS` are the same
   table, and `tests/test_issue78_stage_timeouts.py` fails when one side is
   changed alone.

   A stage records what it really did (Issue #52, #104). `longbridge` writes
   `status=failed` with `error_code=longbridge_batch_failed` when every batch
   died, and with `error_code=longbridge_no_rows` when the provider returned
   calendar pages but **no event could be resolved** (identity or date
   unparseable — the shape of the 2026-10-29 CLI change). Both carry
   `details.unresolved` and exit non-zero, so the scheduled run fails loudly
   instead of logging a `success` that wrote 0 rows; the later stages still run.

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

## Calendar window (one horizon, every outlet)

`CALENDAR_FORWARD_DAYS` (default `420`) is the forward horizon of the calendar
read outlets, and the predictor's `MAX_FUTURE_DAYS` is the horizon it can place a
date in. They must be equal or the feed is shorter than the data behind it:

| Outlet | Window |
|--------|--------|
| iCal feed `/ical/{token}` | `today - 7d` … `today + CALENDAR_FORWARD_DAYS` |
| `/api/earnings` default (no `start`/`end` given) | `today - 7d` … `today + CALENDAR_FORWARD_DAYS` |
| App calendar / watchlist "next earnings" (it always sends `start`/`end`) | same horizon, `WATCHLIST_NEXT_WINDOW_DAYS` |
| Predictor (`scripts/predict_earnings.py`) | `today` … `today + MAX_FUTURE_DAYS` |

The feed used to stop at a hardcoded 120 days while the app and the predictor
used 420: the predictions were computed, stored and visible in the app, but 80%
of them were permanently missing from every subscription, with no error on either
side (Issue #59). `tests/test_calendar_window.py` and
`tests/test_frontend_structure.py` pin the outlets to that horizon, and
`app/routers/ical.py` no longer carries a window literal of its own.

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
| `AUTH_TRUST_SECRET` | *(empty)* | Proxy shared secret; when set, `X-User-*` headers are only believed if the request carries it (`401 untrusted_proxy`). Empty keeps the pre-#102 behaviour. Must be paired with the `fincal-identity` label |
| `AUTH_TRUST_HEADER` | `X-Fincal-Proxy` | Header the proxy injects the secret in (renaming it also renames the Compose label key) |
| `DB_HOST` | `localhost` | PostgreSQL host |
| `DB_PORT` | `5432` | PostgreSQL port |
| `DB_NAME` | `fincal` | Database name |
| `DB_USER` | `postgres` | Database user |
| `DB_PASSWORD` | *(empty)* | Database password |
| `DB_CONNECT_TIMEOUT_SECONDS` | `3` | `connect_timeout` of every request-path connection (Issue #101). Without it a dropped SYN held a thread for 131 s |
| `DB_STATEMENT_TIMEOUT_SECONDS` | `5` | Per-statement `statement_timeout` of the API role |
| `DB_LOCK_TIMEOUT_SECONDS` | `2` | `lock_timeout` of the API role |
| `DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS` | `15` | Reaps an abandoned transaction so it cannot hold a pool slot forever |
| `DB_SYNC_CONNECT_TIMEOUT_SECONDS` | `10` | `connect_timeout` for `scripts/…` (sync role) |
| `DB_SYNC_STATEMENT_TIMEOUT_SECONDS` | `300` | `statement_timeout` for the sync role — bulk upserts must not be killed |
| `DB_SYNC_LOCK_TIMEOUT_SECONDS` | `10` | `lock_timeout` for the sync role |
| `DB_SYNC_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS` | `600` | Idle-transaction timeout for the sync role |
| `DB_POOL_MIN_CONN` | `2` | Idle connections kept warm |
| `DB_POOL_MAX_CONN` | `20` | Pool slots; callers queue (bounded) instead of raising `PoolError` |
| `DB_POOL_ACQUIRE_TIMEOUT_SECONDS` | `5` | How long a request waits for a slot before `503 db_busy` + `Retry-After` |
| `DB_SYNC_POOL_ACQUIRE_TIMEOUT_SECONDS` | `60` | Same, for the sync role |
| `DB_HEALTH_PROBE_TIMEOUT_SECONDS` | `1.5` | Budget of the `/api/admin/health` + `/ready` PostgreSQL probe |
| `DB_CONNECT_FAILURE_COOLDOWN_SECONDS` | `5` | After a failed connect, other reads fail fast instead of paying `connect_timeout` again |
| `FINCAL_DB_ROLE` | *(auto)* | Overrides the budget role (`api`/`sync`); detected from the entry point otherwise |
| `UNIVERSE_REQUEST_WAIT_SECONDS` | `1` | How long a request waits for a universe refresh before serving the current snapshot |
| `WATCHLIST_SOURCE` | `tsummt` | `tsummt` or `http` ([docs](docs/watchlist-source.md)) |
| `KURUMI_API_URL` | `http://localhost:8000` | Base URL of the Kurumi/tsummt API used for company names. **Must be set inside the container** — the default is FinCal's own port, so the dependency probe reports `dependency_not_configured` instead of a false `healthy` (Issue #70) |
| `KURUMI_API_TIMEOUT` | `5` | Kurumi request timeout (seconds) |
| `ICAL_BASE_URL` | — | Public URL for iCal feeds |
| `CALENDAR_FORWARD_DAYS` | `420` | Forward horizon (days) of the iCal feed and the default `/api/earnings` window |
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
| `GET` | `/api/search` | Yes | Search stocks (the earnings universe; one row per symbol) |
| `GET` | `/api/export` | Yes | Export as CSV/JSON |
| `GET` | `/api/popular` | Yes | Default popular stocks |
| `GET` | `/ical/{token}` | No | iCal subscription feed |

**Subscription scope (Issue #100).** `scope=all` means *the system universe*,
not "every symbol the sync ever wrote": the feed reads the same
`app.universe.popular_stocks()` accessor as the calendar, `/api/export` and
`/api/universe`, filtered by the `markets` parameter. Before this change the
feed ran `SELECT DISTINCT symbol, market FROM earnings` and a production
subscriber received 2,544 symbols / 3,177 events — 86% of them for symbols no
screen in the app could show or explain. **This is a behavior change**: events
for symbols outside the universe (ADRs, small caps, unsynced HK codes) no longer
appear in an existing `scope=all` subscription. The system universe itself is
editable (`/api/admin/watchlist`), and an admin change invalidates every cached
feed, so a symbol added there is in the next poll rather than after the TTL.

The feed cache is bounded in **bytes of feed text**, not slots
(`FEED_CACHE_MAX_BYTES = 16 MiB`, plus a 512-entry ceiling): the key is
token + 5 options, so one subscriber can legitimately mint 12 keys, and a slot
count said nothing about memory.

**Compression (Issue #99).** Responses are gzipped by the application
(`GZipMiddleware`, `minimum_size=1024`, `compresslevel=6`) as the outermost
layer, so validators (`ETag`, `Last-Modified`) keep describing one uncompressed
representation — they are emitted as weak validators (`W/"…"`) because one
resource now has two content-codings. Do **not** also add Traefik
`compress@docker`: the edge would either recode an already-coded body or skip
it, a redundant layer either way. Two consequences worth knowing:

- The whitelisted JSON endpoints (`/api/search`, `/api/popular`,
  `/api/export`, …) are re-serialized by the ETag middleware as a single body,
  so they keep `Content-Length` and small ones (< 1 KiB) stay identity.
- Streamed paths (month view, iCal feed, health probe) reach the gzip layer as
  body chunks — both middlewares are `BaseHTTPMiddleware`, which re-emits every
  response as a stream — so `minimum_size` cannot fire there and the coded
  response is sent chunked (no `Content-Length`). 304 answers carry
  `Vary: Accept-Encoding` explicitly, since nothing compresses a bodyless
  response.

## Development

```bash
# Install dependencies
uv sync

# Tests (Issue #98): the default collection is fully offline — no PostgreSQL
# needed, so the same command is valid on a fresh clone, in CI and in a container
uv run python -m pytest -q

# Opt-in real-database checks (temp tables/throwaway schemas, rolled back):
# needs a reachable fincal DB. FINCAL_TEST_DB=1 enables the opt-in self-tests.
uv run python -m pytest -q --run-integration

# Run locally (requires PostgreSQL)
DB_HOST=localhost uv run uvicorn app.main:app --reload

# Run sync scripts
DB_HOST=localhost python scripts/sync_earnings.py
DB_HOST=localhost python scripts/predict_earnings.py
```

### Longbridge event sequences (Issue #75)

One company can appear several times on the same day in the Longbridge calendar.
`ext.financial_report.period_type` tells the sequences apart, and only one of
them encodes a fiscal quarter:

- `qf` / `3q` — earnings release. `period` is the fiscal quarter (1–4) and is the
  only value that becomes a fiscal identity.
- `saf` — half-year report disclosure. Its `period` is *not* the quarter (a
  half-year event is published as `period=4`), and its `fiscal_year` belongs to
  the disclosure sequence. It normally follows the `qf` release of the same
  quarter by 0–2 days.
- `af` — annual report disclosure, with the same caveat. For many non-US names
  this *is* the annual result event, with no separate `qf/4` event to pair with.

`scripts/sync_earnings.py` therefore takes a disclosure's quarter from its
sequence (`saf` → Q2, `af` → Q4) and its fiscal year from the event's own date.
A disclosure whose quarter already has a release event within
`DISCLOSURE_SAME_PERIOD_DAYS` (14) days is the same announcement and is skipped;
one with no release event at all keeps the identity, because it is that period's
only event. Reading `period` for `saf`/`af` is what previously labelled 394
production rows "Q4" on an August date (a half-year event rendered as "same
fiscal year Q4 before Q3").

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

### Revision stamps: `updated_at`, `estimate_as_of`, `actual_as_of` (Issue #103)

`earnings.updated_at` is not a write clock. It is the row's **revision counter**,
and three consumers read it as one:

* `app/ical.py` derives the VEVENT `SEQUENCE` (RFC 5545 §3.8.7.4) and
  `LAST-MODIFIED` from it — a client treats a larger `SEQUENCE` as "this event was
  revised, replace your copy";
* `app/routers/ical.py::_feed_last_modified` uses `max(updated_at)` as the feed's
  `Last-Modified` floor, so an advance without a content change makes
  `If-Modified-Since`-only clients download the feed again;
* `app/fiscal.authority_key` uses it as the tie-break between rows of one period.

`estimate_as_of` / `actual_as_of` (Issue #73) answer the narrower question "when
did *this* number last change".

Both sync stages therefore write these columns **only when a value changes**.
The conflict branch of the Longbridge upsert and the Futu date/actual upserts
compare the row's effective values (`COALESCE`/`CASE` applied, so a re-delivery
carrying no value is not a change) with `IS DISTINCT FROM` (NULL-safe) and stamp
`NOW()` only on a real difference; a first write still stamps the numbers it
inserts. Provider provenance (`date_source` / `date_status` / `estimate_source` /
`actual_source`) is deliberately **not** part of that comparison: both stages
rewrite it on every run and it would put the run clock back on the row.

Consequences worth knowing:

* rows whose content never changes keep an old `updated_at` — that is the point;
* the historical damage is not repaired: 26k rows were stamped by past runs, and
  a real "when did it change" time cannot be reconstructed, so `SEQUENCE` values
  only spread out as rows are genuinely revised from now on;
* a provenance-only change (e.g. Futu re-confirming a date Longbridge wrote) is
  visible in the calendar body but does not bump `SEQUENCE`.

### Sync-run timeout reaper (Issue #33)

`sync_runs.timeout_seconds` is written for every run by
`sync_audit.start_run()`, but nothing read it and `reap_timeout_runs()` had no
caller in the repository — so a run whose owning process died (a stage killed by
the cron wrapper's `timeout`, a container restarted mid-run, a provider call
that never returned) stayed `status='running'` until the container started
again. Until then `/api/admin/sync-runs` and the admin panel rendered a job that
was not running, and the per-run timeout column was a configuration illusion.

- The application lifespan now starts one daemon thread
  (`app/timeout_reaper.py`) that runs a reaping pass every
  `SYNC_RUN_REAPER_INTERVAL_SECONDS` (default `300`) and stops on shutdown.
- A run is interrupted once **its own** `timeout_seconds` has elapsed since its
  last heartbeat (aged from `started_at` when a run never wrote one), so a stage
  with a longer or shorter budget is no longer judged against a fixed hour.
- That column is only meaningful if the stage actually declares its budget: every
  stage script now writes `timeout_seconds=FINCAL_STAGE_TIMEOUT_<STAGE>` — the
  same variable `sync_all.sh` enforces with `timeout` (Issue #78) — and its inner
  loop heartbeats progress at most once a minute, so a *working* stage is not
  reaped at its budget while a *dead* one becomes terminal within its own budget
  plus one reaper interval. Before this, every run was written with the 3600s
  default: a 600s-budgeted prediction stage that cron killed kept a ghost
  `running` row for ~55 minutes (during which a manual re-run answered "already
  running, skipping" and refreshed nothing), and a stage whose real budget
  exceeded 3600s was interrupted *while alive*, silently discarding its terminal
  state.
- `finish_run()` logs a WARNING when the row it was asked to close is no longer
  `running` (cancelled by an admin, or already reaped): the run's real terminal
  state — `success` with its record count, or `failed` with its error code — is
  being dropped, which is exactly what `app/freshness.py` reads.
- Startup recovery (`recover_stale_runs()`, also reachable through
  `POST /api/admin/sync-runs/recover`) keeps its global grace period on purpose:
  that path must stay able to reclaim a run that declared an unusually long
  timeout. Neither path restarts the stage — the next scheduled run re-runs it.
- `SYNC_RUN_REAPER_ENABLED=false` turns the periodic reaper off.

### Sync freshness monitoring (Issue #53)

Dependency probes alone cannot tell "the dependencies are up" from "the pipeline
has been silently skipping a stage", and a fixed 24-hour run window is empty
~6 days out of 7 for a weekly pipeline. `app/freshness.py` therefore answers a
third question with **SELECT-only** queries and no external calls: *when did each
declared stage last succeed, and how old is the data it derives?*

- Staleness is decided from the **most recent success** per stage
  (`sync_runs.status='success'`), never from a fixed window; a stage with no
  successful run at all is reported as `never`.
- A stage is also judged by its **most recent run**, whatever its status
  (Issue #104). If that run failed, the stage is reported as `failed` with the
  run's own error code (`longbridge_no_rows`, `stock_names_unresolved`, …) and
  appears in `failed_stages` — waiting out the 192h threshold would have kept
  `/api/admin/health` green for a week over a stage that refreshed nothing. The
  aggregate `error_code` is then `sync_stage_run_failed`; a run that is still
  `running`, or was `cancelled`/`skipped`, does not fail the stage.
- Derived tables are checked too, but only ones that are **rendered to users
  and rewritten on every successful run** — that is the condition for
  `MAX(<time column>)` to mean "last successful run" (`earnings_consensus`,
  `earnings_forecast_eps`, `earnings_institution_ratings` on `MAX(fetched_at)`),
  so a fresh stage with stale output still shows up. The calendar table itself
  is judged by its `longbridge` stage row: since Issue #103 `earnings.updated_at`
  is a revision counter that deliberately does **not** move when a re-delivery
  changes nothing, so `MAX(updated_at)` no longer means "last successful run".
- On-demand caches are deliberately **not** judged this way: `stock_names` only
  writes when a missing name is resolved, so its `MAX(fetched_at)` freezes as
  soon as the pending set is empty (Issue #67). Its verdict comes from its own
  stage row, and a round that had targets but resolved none of them is recorded
  as `failed` with `error_code=stock_names_unresolved` (the stage then exits
  non-zero) instead of a silent `success`.
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
  when a stage or derived table is stale, or a stage's most recent run failed
  (`2` when the check itself cannot reach the database), so wiring it into the
  cron wrapper turns a silent gap into a next-run failure.

```bash
DB_HOST=localhost uv run python scripts/check_sync_freshness.py
# sync freshness (threshold 192h, checked 2026-09-17T18:34:31+00:00)
# name                             kind     status   age_hours  last_run   last_success_at
# longbridge                       stage    fresh        47.82  success    2026-09-15T18:45:27+00:00
# futu                             stage    fresh        47.69  success    2026-09-15T18:53:22+00:00
# stock_names                      stage    stale      1088.71  success    2026-08-03T09:51:57+00:00
# consensus                        stage    stale      1113.11  success    2026-08-02T09:27:43+00:00
# prediction                       stage    fresh        90.44  success    2026-09-14T00:08:00+00:00
# earnings_consensus               derived  stale      1113.11  -          2026-08-02T09:27:41+00:00
# earnings_forecast_eps            derived  fresh        47.69  -          2026-09-15T18:53:22+00:00
# …
# STALE: stage 'consensus' last succeeded at 2026-08-02T09:27:43+00:00 (1113.11h ago)
# exit=1

# A stage that ran and resolved nothing reads as:
# longbridge                       stage    failed         2.10  failed     2026-10-26T08:19:00+00:00
# FAILED: stage 'longbridge' — its most recent run failed (longbridge_no_rows) at
#   2026-10-29T03:02:13+00:00; last success 2026-10-26T08:19:00+00:00
```

Timestamps are compared and printed in UTC regardless of the database session
timezone, so the ages are stable.

### Diagnostics counters (Issue #83)

`/api/admin/diagnostics` publishes two counter sections that used to be
constants whatever the traffic — `cache` + `cache_by_name` (`hits` / `misses` /
`hit_rate` / `stale_returns` / `refresh_ok` / `refresh_fail`) and `providers`
(per-provider `calls` / `success` / `success_rate` / `avg_ms` / `errors`).
Issue #15 declared them, but no code wrote to them: the four cache paths ran
without touching a counter and the only instrumented provider entry point
(`provider_client.http_get_json`) is unreachable under the production
`WATCHLIST_SOURCE=hybrid`. They are now written at the decision points:

- `app/layer_cache.py` (the earnings calendar cache) — a fresh read is a
  **hit**, a read that has to produce the value is a **miss**, a caller handed a
  *stale* snapshot is a **stale return** (stale-while-revalidate, and the
  blocking path that falls back to the cached snapshot after a failed refresh),
  and the origin call's outcome is **refresh_ok** / **refresh_fail**. A caller
  that joins an in-flight refresh and returns its result is a hit: it cost the
  origin nothing.
- `app/routers/ical.py` (the feed cache) — hit/miss per request, the `304`
  conditional answer included (same cached body, same hit). An unknown token
  (`404`) is not counted: there is no feed to cache.
- `app/company_name.py` + `app/provider_client.py` — `kurumi`, `longbridge` and
  `futu` appear in `providers`, including the real upstream call
  `/api/admin/health` makes on every poll (`probe_kurumi` →
  `fetch_from_kurumi`), which was previously invisible.

Read them with three caveats in mind (`app/metrics.py` documents the same):

- **Per process, not cumulative.** Counters live in the process and reset on
  restart (deploy, crash, container recreate); they are not a lifetime report
  and are not aggregated across workers.
- **Aggregate, low cardinality.** The cache counters are published twice: the
  aggregate `cache` (every reporting cache summed) and `cache_by_name`, one
  bucket per cache (`earnings` for the calendar layer cache, `ical` for the feed
  cache). Provider counters are keyed by provider name only — the label set is
  bounded by the code, so no cache key, symbol, subscription token or user id can
  end up in a label (the iCal cache key carries a token, `_earnings_cache`'s key
  carries a user id, and neither is exposed).
- **A green number is not a promise.** `calls=0` after a restart means "nothing
  has happened yet", not "nothing is wrong"; use the freshness summary above for
  "has the pipeline run".

### Dependency probes (Issue #70, #79)

`/api/admin/health`'s `checks.kurumi` asks the endpoint the client actually
reads — `{KURUMI_API_URL}/api/stock/{symbol}/overview`, built by the same helper
as `company_name.fetch_from_kurumi`, so the probe cannot drift from the client
path again. A base URL that resolves back to FinCal itself (`localhost` /
`127.0.0.1` on `PORT`, or an unset value) is **not** probed and reported as
`{"status": "degraded", "error_code": "dependency_not_configured"}`: FinCal
answers its own port, so such a probe could only ever "succeed". An unreachable
upstream (refused, timed out or 404) is `degraded` with
`error_code=kurumi_unreachable`. `scripts/deploy.sh` prints the same verdict
from inside the container and raises `ACTION REQUIRED (Issue #70)` when
`KURUMI_API_URL` is unset there or points at FinCal itself.

**Service dependencies vs host tooling (Issue #79).** The probes are split by who
owns the subject. PostgreSQL, Kurumi, Futu OpenD and sync freshness are things
*this process* uses, so their verdicts decide the aggregate `status`. The
Longbridge CLI is not: the pipeline runs it on the **host**
(`scripts/sync_*.py` under cron) while the image ships only `app/` plus the web
dependencies, so `checks.longbridge` reports
`{"status": "not_applicable", "error_code": "tool_not_in_image"}` inside a
container. That entry stays visible in `checks`, but `not_applicable` does **not
vote** on the aggregate — probing a host tool as a dependency answered a constant
`degraded` for ~7 weeks, which made `degraded` the permanent state and drowned
out real outages. A tool that *is* present keeps voting: a non-zero exit is
`degraded` with `tool_nonzero_exit`, an unrunnable one `degraded` with
`tool_probe_failed`.

**Version self-attestation (Issue #79).** `checks.version` is the commit the
image was built from: `Dockerfile` declares `ARG GIT_COMMIT` /
`ENV FINCAL_GIT_COMMIT`, `docker-compose.yml` passes `${GIT_COMMIT:-dev}` as a
build arg, and `scripts/deploy.sh` exports the commit it is deploying. Without
the injection the field answered `"dev"` on every deployment, because only
`app/` is copied into the image and the `VERSION` file the deploy writes next to
the source tree never gets there. `scripts/deploy.sh` ends with
`scripts/check_service_health.py`, which asks the container for its own
`/api/admin/health` and exits non-zero when:

* `version` differs from the commit just deployed (host `/opt/fincal/VERSION`);
* a host-tool check is voting on the aggregate again (it must be
  `not_applicable`);
* the aggregate does not follow from the *applicable* probes only.

Run it by hand with `python3 scripts/check_service_health.py`; it is read-only
(one GET, no writes).

## Tech Stack

- **Backend:** Python 3.12, FastAPI, psycopg2, uvicorn
- **Frontend:** Vue 3 (CDN), single-file HTML SPA
- **Database:** PostgreSQL 13+
- **Deployment:** Docker Compose, Traefik v3
- **Data:** Longbridge CLI, Futu OpenD API

## License

[Apache License 2.0](LICENSE)
