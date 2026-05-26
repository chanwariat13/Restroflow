"""
app/main.py - RestroFlow Multi-Tenant Entry Point
One deployment → unlimited clients
3 dashboards: Super Admin | Client Owner | Staff
"""
import logging
import os
import sys
import traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.models.database import setup_master_db
from app.scheduler.jobs import (
    run_cleanup, run_daily_report,
    run_monthly_report, run_festival_broadcast
)
from app.routes.whatsapp_bot     import router as bot_router
from app.routes.registration      import router as reg_router
from app.routes.api_routes        import router as api_router
from app.routes.admin             import router as admin_router
from app.routes.client_dashboard  import router as client_router
from app.routes.staff_dashboard   import router as staff_router
from app.routes.customer_pages    import router as pages_router
from app.routes.kds               import router as kds_router

logger = logging.getLogger(__name__)

# ── Refuse to start with the placeholder ADMIN_SECRET ────────────────────────
# routes/admin.py reads ADMIN_SECRET at import time. If it's still the default
# placeholder, the master admin API is effectively unauthenticated, so we abort
# boot here rather than ship a wide-open server.
_ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
_BANNED_ADMIN_SECRETS = {
    "", "change-this-secret", "changeme", "secret", "admin", "admin123",
    "password", "password123", "letmein", "test", "testing",
}
if _ADMIN_SECRET in _BANNED_ADMIN_SECRETS or len(_ADMIN_SECRET) < 16:
    logger.critical(
        "ADMIN_SECRET is missing or weak (must be at least 16 characters and "
        "not one of the well-known defaults). Refusing to start. Generate a "
        "strong value, e.g. `python -c \"import secrets;print(secrets.token_urlsafe(48))\"`."
    )
    raise SystemExit(
        "ADMIN_SECRET must be set to a strong (≥16-char), unique value before "
        "starting RestroFlow."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_master_db()
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(run_cleanup,            IntervalTrigger(minutes=30),                              id="cleanup")
    scheduler.add_job(run_daily_report,       CronTrigger(hour=7,  minute=0, timezone="Asia/Kolkata"), id="daily")
    scheduler.add_job(run_monthly_report,     CronTrigger(day=1, hour=7, minute=0, timezone="Asia/Kolkata"), id="monthly")
    scheduler.add_job(run_festival_broadcast, CronTrigger(hour=10, minute=0, timezone="Asia/Kolkata"), id="festival")
    scheduler.start()
    print("✅ RestroFlow started — Multi-Tenant Mode")
    print(f"✅ {len(scheduler.get_jobs())} scheduled jobs running")
    yield
    scheduler.shutdown()


app = FastAPI(title="RestroFlow", version="2.0.0", lifespan=lifespan)

# CORS: lock down to a configured allow-list. Set CORS_ORIGINS to a comma-
# separated list of allowed frontends (e.g. "https://app.example.com").
# Use "*" only for local development.
_cors_raw = os.getenv("CORS_ORIGINS", "*").strip()
_cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# All routes
app.include_router(bot_router)
app.include_router(reg_router)
app.include_router(api_router)
app.include_router(admin_router)
app.include_router(client_router)
app.include_router(staff_router)
app.include_router(pages_router)
app.include_router(kds_router)

# Static files
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")


@app.get("/")
async def root():
    # No bundled SPA index.html — send users to the unified login page.
    # Previously this returned FileResponse(STATIC_DIR/'index.html'), which
    # 500'd because that file does not exist.
    return RedirectResponse(url="/login", status_code=307)

@app.get("/health")
async def health():
    return {"status": "ok", "product": "RestroFlow", "version": "2.0.0"}

@app.get("/admin")
async def admin_page():
    return FileResponse(os.path.join(STATIC_DIR, "admin-dashboard.html"))

@app.get("/admin-dashboard")
async def admin_page_alt():
    return FileResponse(os.path.join(STATIC_DIR, "admin-dashboard.html"))


# Unified login. Both Super Admin and Restaurant users land here; the page
# validates credentials against the existing endpoints, stashes them in
# sessionStorage, and redirects to /admin-dashboard, /dashboard/{slug} or
# /staff/{slug} depending on the role.
@app.get("/login")
async def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    print(f"ERROR: {request.url.path}\n{tb}")
    return JSONResponse(status_code=500, content={"error": "Internal server error"})
