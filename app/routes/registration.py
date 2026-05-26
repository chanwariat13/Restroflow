"""
routes/registration.py - POST /webhook/{slug}/register
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.utils.tenant import load_tenant
from app.utils import redis_client as rc
from app.services import whatsapp as wa

router = APIRouter()


class RegisterRequest(BaseModel):
    phone:  str
    name:   str
    table:  str
    secret: str


@router.post("/webhook/{slug}/register")
async def register(req: RegisterRequest, slug: str = Path(...)):
    cfg   = load_tenant(slug)
    phone = req.phone.strip()
    name  = req.name.strip()
    table = req.table.strip().upper()

    if not phone or len(phone) < 10 or not phone.isdigit():
        return JSONResponse({"success": False, "error": "Invalid phone number"})
    if not name or len(name) < 2:
        return JSONResponse({"success": False, "error": "Invalid name"})
    # Allow whatever table prefix this tenant configured (e.g. "T", "A", "TBL").
    # The previous hardcoded "^T\d{1,2}$" silently rejected every QR code for
    # any client that picked a different prefix.
    _prefix = re.escape((cfg.table_prefix or "T").upper())
    if not re.match(rf"^{_prefix}\d{{1,3}}$", table):
        return JSONResponse({"success": False, "error": "Invalid table"})

    expected = cfg.table_secrets.get(table)
    # SECURITY: We deliberately do NOT compute a "default" secret here.
    # The previous fallback `f"{slug[:3].upper()}{2025+i}"` made every QR
    # code on a freshly-onboarded tenant predictable: anyone who knew the
    # slug (which is exposed in /r/{slug} and /menu/{slug} URLs) could
    # bypass the QR check and self-register at any table. The QR-codes
    # admin/owner endpoints have been updated to materialise real random
    # secrets into `table_secrets` JSON when they're missing, so a real
    # operator-issued QR will always have a populated secret here.
    #
    # If the table secret is somehow not configured, fail closed so a
    # forged QR cannot succeed.
    if not expected:
        return JSONResponse({
            "success": False,
            "error": "Table not configured. Please ask staff to regenerate the QR codes.",
        })
    if expected != req.secret.strip():
        return JSONResponse({"success": False, "error": "Invalid QR code. Scan the correct table QR."})

    if rc.is_blocked(cfg.slug, phone):
        return JSONResponse({"success": False, "error": "Access denied. Please speak to staff."})

    existing = rc.get_session(cfg.slug, phone)
    if existing and existing.get("status") not in ("CHECKED_OUT", None):
        return JSONResponse({"success": False, "error": f"You already have an active session at table {existing.get('table')}."})

    table_phone = rc.get_table_phone(cfg.slug, table)
    if table_phone and table_phone != phone:
        return JSONResponse({"success": False, "error": f"Table {table} is occupied."})

    ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    session = {
        "phone": phone, "name": name, "table": table,
        "status": "AWAITING_APPROVAL", "orders": [], "total": 0,
        "createdAt": datetime.utcnow().isoformat(),
    }

    rc.save_session(cfg.slug, phone, session, cfg.session_ttl)
    rc.set_pending(cfg.slug, phone, {"phone": phone, "name": name, "table": table, "time": ist})
    rc.set_table(cfg.slug, table, phone, pending=True, ttl=cfg.session_ttl)

    await wa.send_text(cfg, phone,
        f"🍽️ *Welcome to {cfg.restaurant_name}!*\n\n"
        f"👤 Hi {name}!\n🪑 Table: {table}\n\n"
        f"⏳ Request sent to staff. Please wait...\n🔔 You'll be notified once approved! 🙏")
    await wa.notify_new_customer(cfg, phone, name, table, ist)

    return JSONResponse({"success": True, "message": "Request sent! Please wait for approval."})
