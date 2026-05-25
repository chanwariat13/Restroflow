"""
app/main.py - RestroFlow Multi-Tenant Entry Point
One deployment → unlimited clients
3 dashboards: Super Admin | Client Owner | Staff
"""
import traceback
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
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

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# All routes
app.include_router(bot_router)
app.include_router(reg_router)
app.include_router(api_router)
app.include_router(admin_router)
app.include_router(client_router)
app.include_router(staff_router)
app.include_router(pages_router)

# Static files
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/health")
async def health():
    return {"status": "ok", "product": "RestroFlow", "version": "2.0.0"}

@app.get("/admin")
async def admin_page():
    return FileResponse(os.path.join(STATIC_DIR, "admin-dashboard.html"))

@app.get("/admin-dashboard")
async def admin_page_alt():
    return FileResponse(os.path.join(STATIC_DIR, "admin-dashboard.html"))


@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    print(f"ERROR: {request.url.path}\n{tb}")
    return JSONResponse(status_code=500, content={"error": "Internal server error"})
