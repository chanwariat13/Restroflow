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
    run_monthly_report, run_festival_broadcast,
    run_resume_paused,
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

# ── WhatsApp inbound webhook gating ──────────────────────────────────────────
# routes/whatsapp_bot.py historically logged-and-allowed when
# WHATSAPP_WEBHOOK_TOKEN was unset. Anyone who learnt a tenant slug could
# then POST a forged "messages.upsert" body to /webhook/{slug}/whatsapp from
# a phone matching the staff WhatsApp number and drive APPROVE / CASH
# RECEIVED / FREE / BLOCK from the outside.
#
# Earlier revisions of this file made the missing-token case *fatal* — the
# process refused to boot at all. That broke every deployment that hadn't
# yet wired up Evolution API (e.g. dashboards-only / KDS-only rollouts), so
# we now degrade gracefully instead:
#
#   * `WHATSAPP_WEBHOOK_TOKEN` set                → webhook is registered;
#       inbound auth enforced (constant-time header compare).
#   * `WHATSAPP_WEBHOOK_AUTH_OPTOUT=1` set        → webhook is registered;
#       every hit is logged at WARNING and accepted (short migration window).
#   * Neither set                                 → webhook is NOT registered;
#       a loud warning is logged at boot and `/health` reports the state.
#       Rest of the app (dashboards, KDS, customer pages, registration,
#       admin API, scheduled jobs, outbound WhatsApp sends) keeps working.
#
# Net effect: an operator who hasn't configured Evolution API can still
# deploy and use everything else, and there is no path through which a
# forged messages.upsert payload can reach `_handle_message` — because the
# route doesn't exist at all in that mode.
_WA_WEBHOOK_TOKEN = (os.getenv("WHATSAPP_WEBHOOK_TOKEN") or "").strip()
_WA_WEBHOOK_OPTOUT = (os.getenv("WHATSAPP_WEBHOOK_AUTH_OPTOUT") or "").strip() in {"1", "true", "yes"}
WHATSAPP_WEBHOOK_ENABLED = bool(_WA_WEBHOOK_TOKEN) or _WA_WEBHOOK_OPTOUT
if not WHATSAPP_WEBHOOK_ENABLED:
    logger.warning(
        "WHATSAPP_WEBHOOK_TOKEN is not set — the inbound WhatsApp webhook "
        "(/webhook/{slug}/whatsapp) is DISABLED. Inbound WhatsApp commands "
        "(APPROVE/CASH RECEIVED/BLOCK/etc.) will not work. Set "
        "WHATSAPP_WEBHOOK_TOKEN to a strong shared secret matching the "
        "Evolution API `apikey` header to enable it. The rest of RestroFlow "
        "(dashboards, KDS, customer pages, scheduled jobs, outbound WhatsApp) "
        "is unaffected. For an explicit short migration window, set "
        "WHATSAPP_WEBHOOK_AUTH_OPTOUT=1 instead — that registers the route "
        "but accepts every hit (logged at WARNING)."
    )
elif not _WA_WEBHOOK_TOKEN and _WA_WEBHOOK_OPTOUT:
    logger.warning(
        "WHATSAPP_WEBHOOK_AUTH_OPTOUT=1 — inbound WhatsApp webhook will "
        "accept ALL requests without authentication. This is a short "
        "migration mode only. Set WHATSAPP_WEBHOOK_TOKEN and remove the "
        "opt-out as soon as Evolution API is configured."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_master_db()
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(run_cleanup,            IntervalTrigger(minutes=30),                              id="cleanup")
    scheduler.add_job(run_daily_report,       CronTrigger(hour=7,  minute=0, timezone="Asia/Kolkata"), id="daily")
    scheduler.add_job(run_monthly_report,     CronTrigger(day=1, hour=7, minute=0, timezone="Asia/Kolkata"), id="monthly")
    scheduler.add_job(run_festival_broadcast, CronTrigger(hour=10, minute=0, timezone="Asia/Kolkata"), id="festival")
    # Auto-resume any client whose paused_until has elapsed. Cheap query on
    # an indexed column, safe at 5-min cadence.
    scheduler.add_job(run_resume_paused,      IntervalTrigger(minutes=5),                              id="resume_paused")
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
# bot_router exposes /webhook/{slug}/whatsapp. We only register it when
# WhatsApp inbound is enabled (see WHATSAPP_WEBHOOK_ENABLED above) — that
# way the missing-token case isn't just rejected with 401, it returns 404
# from FastAPI's normal not-found handler, leaking nothing about the
# tenant's existence.
if WHATSAPP_WEBHOOK_ENABLED:
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
    # Surface WhatsApp inbound state so operators can confirm whether the
    # /webhook/{slug}/whatsapp route is wired up without grepping logs.
    if _WA_WEBHOOK_TOKEN:
        wa_inbound = "enabled"
    elif _WA_WEBHOOK_OPTOUT:
        wa_inbound = "auth-optout"
    else:
        wa_inbound = "disabled"
    return {
        "status": "ok",
        "product": "RestroFlow",
        "version": "2.0.0",
        "whatsapp_inbound": wa_inbound,
    }

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
