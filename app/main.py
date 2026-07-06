"""FastAPI application entry point."""
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from . import db
from .routers import api, ical as ical_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: init DB + seed earnings
    db.init_db()
    from .earnings import seed_earnings_if_empty
    seed_earnings_if_empty()
    yield


app = FastAPI(title="FinCal", lifespan=lifespan)

# iCal feed (no auth — token-based, must be before catch-all SPA)
app.include_router(ical_router.router)

# API routes (behind forwardAuth)
app.include_router(api.router)

# Static Vue SPA
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    assets_dir = os.path.join(static_dir, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(static_dir, "index.html"))
