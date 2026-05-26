"""
app/routes/kds.py — Web Kitchen Display System (KDS).

Replaces the kitchen's pile of paper KOTs and the cluttered staff
dashboard with a single browser tab on a wall-mounted tablet.

Each occupied table that has unserved orders is rendered as a card with:
    - Table number (large)
    - Customer name
    - Order placed time + live elapsed seconds
    - Item lines (qty × name)
    - Status: Queued → Preparing → Ready
    - Color: green (<5 min), yellow (<15 min), red (>15 min)

Status moves are persisted into the Redis session so they survive a
page refresh and reflect across multiple tablets in the same kitchen.
The only mutation we make to the customer-facing flow is when status
hits "Ready": we call the existing `kitchen-done`-equivalent path,
which clears the kitchen queue and pings the customer that food is up.

We intentionally do NOT introduce a new DB column. The KDS is a
read-modify-write of the existing Redis session, so an existing
deployment picks this up without any migration.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.models.database import MasterSession, Client, StaffMember
from app.utils.tenant import load_tenant
from app.utils import redis_client as rc

router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")

# ── Status machine (KDS-side only — never touches payment status) ──
KITCHEN_STATUSES = ("Queued", "Preparing", "Ready")
ELAPSED_WARN_SEC = 5 * 60
ELAPSED_CRIT_SEC = 15 * 60


def _auth_kitchen(slug: str, phone: str, pin: str) -> StaffMember:
    """
    Same shape as staff_dashboard._auth_staff but accepts owner /
    manager / kitchen roles. Owners often run the KDS during dinner
    service when the kitchen lead is busy.
    """
    db = MasterSession()
    try:
        m = db.query(StaffMember).filter(
            StaffMember.slug == slug,
            StaffMember.phone == phone,
            StaffMember.pin == pin,
            StaffMember.active == True,
        ).first()
        if not m:
            raise HTTPException(status_code=401, detail="Wrong phone or PIN")
        if m.role not in ("kitchen", "owner", "manager"):
            raise HTTPException(status_code=403,
                                detail=f"Role {m.role} cannot operate KDS")
        return m
    finally:
        db.close()


def _client(slug: str) -> Client:
    db = MasterSession()
    try:
        c = db.query(Client).filter(Client.slug == slug,
                                     Client.active == True).first()
        if not c:
            raise HTTPException(404, f"Client '{slug}' not found")
        return c
    finally:
        db.close()


def _elapsed_seconds(iso_or_str: Optional[str]) -> int:
    if not iso_or_str:
        return 0
    try:
        dt = datetime.fromisoformat(str(iso_or_str).replace("Z", "+00:00"))
    except Exception:
        return 0
    if dt.tzinfo is None:
        # Saved naive (UTC). Treat as UTC.
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(dt.tzinfo) - dt).total_seconds()))


def _bucket(elapsed: int) -> str:
    if elapsed >= ELAPSED_CRIT_SEC:
        return "critical"
    if elapsed >= ELAPSED_WARN_SEC:
        return "warning"
    return "fresh"


def _build_ticket(slug: str, t: dict) -> Optional[dict]:
    """
    Convert one occupied-table snapshot into a KDS ticket. Returns None
    when the table has nothing the kitchen needs to see (e.g. no
    pending orders, or all already served).
    """
    s = t.get("session") or {}
    if s.get("status") not in ("ORDER_PLACED", "PENDING_PAYMENT", "PAID", "ORDERING"):
        return None
    queue = list(s.get("kitchenOrders") or s.get("orders") or [])
    if not queue:
        return None
    placed_at = (
        s.get("orderPlacedAt") or s.get("kitchenAt")
        or s.get("paidAt") or s.get("createdAt")
    )
    elapsed = _elapsed_seconds(placed_at)
    return {
        "table":            t.get("table"),
        "phone":            t.get("phone"),
        "name":             s.get("name", "?"),
        "session_status":   s.get("status"),
        "kitchen_status":   s.get("kitchenStatus") or "Queued",
        "items":            queue,
        "items_count":      sum(int(i.get("quantity") or i.get("qty") or 1)
                                 for i in queue),
        "placed_at":        placed_at,
        "elapsed_seconds":  elapsed,
        "elapsed_bucket":   _bucket(elapsed),
        "kot_printed":      bool(s.get("kotPrintedAt")),
    }


# ── Page ───────────────────────────────────────────────────────────
@router.get("/kds/{slug}")
async def kds_page(slug: str):
    """
    Tablet-friendly fullscreen page. Auth is done client-side: page
    prompts for staff phone+PIN once, then polls /api/kds/{slug}.
    """
    _client(slug)  # 404 if slug doesn't exist
    path = os.path.join(os.path.dirname(__file__), "..", "..", "static",
                        "kds.html")
    if not os.path.exists(path):
        raise HTTPException(404, "KDS frontend not found")
    return FileResponse(path)


# ── Tickets feed ───────────────────────────────────────────────────
@router.get("/api/kds/{slug}")
async def kds_tickets(slug: str, phone: str, pin: str):
    _auth_kitchen(slug, phone, pin)
    cfg = load_tenant(slug)
    occupied = rc.get_all_occupied_tables(slug, cfg.get_table_names())
    tickets = []
    for t in occupied:
        try:
            tk = _build_ticket(slug, t)
        except Exception:
            tk = None
        if tk:
            tickets.append(tk)
    # Sort newest-on-the-floor first; critical/warning bubble to the top.
    tickets.sort(key=lambda x: (
        {"critical": 0, "warning": 1, "fresh": 2}[x["elapsed_bucket"]],
        -x["elapsed_seconds"],
    ))
    return JSONResponse({
        "restaurant_name": cfg.restaurant_name,
        "tickets":         tickets,
        "thresholds":      {"warn_seconds": ELAPSED_WARN_SEC,
                            "crit_seconds": ELAPSED_CRIT_SEC},
        "fetched_at":      datetime.now(IST).isoformat(timespec="seconds"),
    })


# ── Status transitions ─────────────────────────────────────────────
class KdsStatusReq(BaseModel):
    slug: str
    phone: str
    pin: str
    table: str
    status: str   # one of KITCHEN_STATUSES


@router.post("/api/kds/status")
async def kds_status(req: KdsStatusReq):
    """
    Move a ticket through Queued → Preparing → Ready. Setting Ready
    runs the same side-effects as the existing kitchen-done flow:
    tells the customer their food is up, clears the kitchen queue,
    and notifies floor staff.
    """
    if req.status not in KITCHEN_STATUSES:
        raise HTTPException(400,
            f"status must be one of {KITCHEN_STATUSES}")
    _auth_kitchen(req.slug, req.phone, req.pin)
    cfg = load_tenant(req.slug)

    cust_phone = rc.get_table_phone(req.slug, req.table)
    if not cust_phone:
        return JSONResponse({"success": False,
                             "error": "no active session at this table"})

    session = rc.get_session(req.slug, cust_phone)
    if not session:
        return JSONResponse({"success": False,
                             "error": "session expired"})

    if req.status == "Ready":
        # Replicate /api/staff/kitchen-done semantics (without re-auth
        # round-trip) so a single button on the KDS does the right thing.
        from app.services import whatsapp as wa
        orders = list(session.get("orders", []))
        session.setdefault("servedOrders", []).extend(orders)
        session.update({
            "kitchenOrders": [],
            "orders":        [],
            "status":        "ORDERING",
            "kitchenStatus": "Ready",
            "readyAt":       datetime.utcnow().isoformat(),
        })
        rc.save_session(req.slug, cust_phone, session, cfg.session_ttl)
        cust_name = session.get("name", "Customer")
        try:
            await wa.send_text(
                cfg, cust_phone,
                f"🍽️ *Your Food is Ready!* 🎉\n\n"
                f"Hey {cust_name}! ✅ Enjoy your meal!\n\n"
                f"*5* - 💵 Bill | *7* - 🔔 Waiter",
            )
            await wa.send_all_staff(
                cfg, f"🍽️ *FOOD READY — {req.table}*\n👤 {cust_name}",
            )
        except Exception:
            # WhatsApp transient failure must never block the status move.
            pass
        return JSONResponse({"success": True, "status": "Ready"})

    # Queued / Preparing — pure kitchen state, no customer notification.
    session["kitchenStatus"] = req.status
    if req.status == "Preparing":
        session.setdefault("preparingAt", datetime.utcnow().isoformat())
    rc.save_session(req.slug, cust_phone, session, cfg.session_ttl)
    return JSONResponse({"success": True, "status": req.status})


# ── KOT print from KDS ────────────────────────────────────────────
class KdsKotReq(BaseModel):
    slug: str
    phone: str
    pin: str
    table: str
    is_reprint: bool = False


@router.post("/api/kds/kot/print")
async def kds_kot_print(req: KdsKotReq):
    """
    Convenience: print a KOT for the active session at this table
    using the existing kot_printer service. Falls back to a clear
    error when the printer isn't configured/reachable.
    """
    _auth_kitchen(req.slug, req.phone, req.pin)
    cfg = load_tenant(req.slug)
    if not cfg.kot_enabled:
        raise HTTPException(409, "KOT printing is disabled for this client")
    if not cfg.kot_printer_ip:
        raise HTTPException(409, "kot_printer_ip is not configured")

    cust_phone = rc.get_table_phone(req.slug, req.table)
    if not cust_phone:
        raise HTTPException(404, "No active session at this table")
    session = rc.get_session(req.slug, cust_phone) or {}
    queue = list(session.get("kitchenOrders") or session.get("orders") or [])
    if not queue:
        raise HTTPException(409, "No items to print")

    from app.services.kot_printer import print_kot
    res = print_kot(
        restaurant_name=cfg.restaurant_name,
        order_id=session.get("currentOrderId") or "LIVE",
        table=req.table,
        items=queue,
        printer_ip=cfg.kot_printer_ip,
        printer_port=cfg.kot_printer_port,
        customer_name=session.get("name", ""),
        notes=session.get("notes", ""),
        is_reprint=bool(req.is_reprint),
        header_text=cfg.kot_header_text,
        cpl=cfg.kot_paper_width,
    )
    if res.get("ok"):
        session["kotPrintedAt"] = datetime.utcnow().isoformat()
        rc.save_session(req.slug, cust_phone, session, cfg.session_ttl)
    return JSONResponse(res)
