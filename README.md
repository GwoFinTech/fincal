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

# Or run all at once
bash scripts/sync_all.sh
```

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
| `FUTU_PORT` | `11111` | Futu OpenD port |

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

## Tech Stack

- **Backend:** Python 3.12, FastAPI, psycopg2, uvicorn
- **Frontend:** Vue 3 (CDN), single-file HTML SPA
- **Database:** PostgreSQL 13+
- **Deployment:** Docker Compose, Traefik v3
- **Data:** Longbridge CLI, Futu OpenD API

## License

[Apache License 2.0](LICENSE)
