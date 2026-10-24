"""FastAPI application entry point."""
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from . import db
from .routers import api, admin, ical as ical_router
from .errors import error_middleware
from .etag_middleware import etag_middleware
from .log_redaction import install_log_redaction

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Issue #85: ``/ical/{token}`` is a bearer credential, and uvicorn's default
# access log writes the request path verbatim — production had 144 copies of one
# live subscription token in `docker logs`. Installed at import time (uvicorn
# imports the app before it serves anything) and on the emitting logger, so the
# token never reaches a handler; uvicorn's start command stays untouched.
install_log_redaction()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: init DB + seed earnings
    db.init_db()
    from .earnings import seed_earnings_if_empty
    seed_earnings_if_empty()
    # Recover stale sync runs from previous process (issue #3)
    from .sync_audit import recover_stale_runs
    recover_stale_runs()
    # Enforce each run's own timeout while this process is up (issue #33): the
    # reaper existed but had no caller, so a run left behind by a dead process
    # stayed 'running' until the next container start.
    from .timeout_reaper import start_timeout_reaper, stop_timeout_reaper
    start_timeout_reaper()
    try:
        yield
    finally:
        stop_timeout_reaper()


def install_middleware(app: FastAPI) -> None:
    """Register the HTTP stack in its one correct order.

    Starlette prepends on registration and then wraps in reverse, so the *last*
    registration is the outermost layer. Compression therefore has to come last:
    the bytes it codes are exactly the bytes the validators above produced, and
    the ETag/Last-Modified describe one uncompressed representation that both
    content-codings are derived from (Issue #99).

    Kept as a function so the tests can build a probe app with the production
    order instead of a copy of it.
    """
    # Error handling middleware (Issue #9)
    app.middleware("http")(error_middleware)

    # ETag middleware (Issue #10)
    app.middleware("http")(etag_middleware)

    # Response compression (Issue #99). Everything this service emits is text —
    # JSON, iCalendar, HTML/JS/CSS — and the two hot outlets are big: the month
    # view was 93 KiB on the wire and a ``scope=all`` feed 182 KiB, per poll,
    # per subscriber (measured 2026-10-24).
    #
    # ``minimum_size`` only applies to a body that arrives in one message: true
    # for the endpoints the ETag layer re-serializes (small ones stay identity,
    # with their ``Content-Length``), false for the streamed ones — both
    # middlewares above are ``BaseHTTPMiddleware``, which re-emits every response
    # as a body *stream*, so those are coded regardless of size and go out
    # chunked (tests/test_response_compression.py pins both cases).
    #
    # Do not add Traefik ``compress`` on top of this: the edge would either
    # recode an already-coded body or skip it, a redundant layer either way
    # (README, "API Endpoints").
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


app = FastAPI(title="FinCal", lifespan=lifespan)
install_middleware(app)

# iCal feed (no auth — token-based, must be before catch-all SPA)
app.include_router(ical_router.router)

# API routes (behind forwardAuth)
app.include_router(api.router)
app.include_router(admin.router)

# Static Vue SPA
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    assets_dir = os.path.join(static_dir, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(static_dir, "index.html"))
