"""
routes/staff_dashboard.py
Staff dashboard API — role-based access control.
Roles: owner | manager | kitchen | waiter
PIN set by super admin, changeable from owner dashboard.
Everything changeable without redeploy.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy import text
from typing import Optional
import os

from app.models.database import MasterSession, Client, StaffMember
from app.utils.tenant import load_tenant
from app.utils import redis_client as rc

router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")

# Role permissions
ROLE_PERMS = {
    "owner":   ["overview","tables","orders","menu","inventory","customers","reports","feedback","qr","settings","staff"],
    "manager": ["tables","orders","customers","feedback"],
    "kitchen": ["kitchen"],
    "waiter":  ["tables","orders"],
}


def _auth_staff(slug: str, phone: str, pin: str) -> StaffMember:
    db = MasterSession()
    try:
        member = db.query(StaffMember).filter(
            StaffMember.slug == slug,
            StaffMember.phone == phone,
            StaffMember.pin == pin,
            StaffMember.active == True
        ).first()
        if not member:
            raise HTTPException(status_code=401, detail="Wrong phone or PIN")
        return member
    finally:
        db.close()


def _check_perm(member: StaffMember, perm: str):
    if perm not in ROLE_PERMS.get(member.role, []):
        raise HTTPException(status_code=403, detail=f"Your role ({member.role}) cannot access this")


# ── Staff dashboard page ───────────────────────────────────────────────────
@router.get("/staff/{slug}")
async def staff_dashboard_page(slug: str):
    path = os.path.join(os.path.dirname(__file__), "..", "..", "static", "staff-dashboard.html")
    return FileResponse(path)


# ── Login ──────────────────────────────────────────────────────────────────
class StaffLoginReq(BaseModel):
    slug: str; phone: str; pin: str

@router.post("/api/staff/login")
async def staff_login(req: StaffLoginReq):
    try:
        m = _auth_staff(req.slug, req.phone, req.pin)
        # Get restaurant name
        db = MasterSession()
        try:
            c = db.query(Client).filter(Client.slug == req.slug).first()
            restro_name = c.restaurant_name if c else req.slug
        finally:
            db.close()
        return JSONResponse({
            "success": True,
            "name": m.name,
            "role": m.role,
            "slug": req.slug,
            "restaurant_name": restro_name,
            "permissions": ROLE_PERMS.get(m.role, [])
        })
    except HTTPException as e:
        return JSONResponse({"success": False, "error": e.detail}, status_code=e.status_code)


# ── Live tables (all roles except kitchen) ─────────────────────────────────
@router.get("/api/staff/{slug}/tables")
async def staff_tables(slug: str, phone: str, pin: str):
    m = _auth_staff(slug, phone, pin)
    _check_perm(m, "tables")
    cfg = load_tenant(slug)
    occupied = rc.get_all_occupied_tables(slug, cfg.get_table_names())
    live = []
    for t in occupied:
        s = t["session"] or {}
        live.append({
            "table": t["table"], "phone": t["phone"],
            "name": s.get("name","?"), "status": s.get("status","?"),
            "total": s.get("total",0), "orders": s.get("orders",[]),
            "createdAt": s.get("createdAt","")
        })
    return JSONResponse({
        "live_tables": live,
        "total_tables": cfg.table_count,
        "table_names": cfg.get_table_names()
    })


# ── Today's orders ─────────────────────────────────────────────────────────
@router.get("/api/staff/{slug}/orders")
async def staff_orders(slug: str, phone: str, pin: str):
    m = _auth_staff(slug, phone, pin)
    _check_perm(m, "orders")
    cfg = load_tenant(slug)
    today = datetime.now(IST).strftime("%-d/%-m/%Y")
    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT * FROM orders WHERE date_only=:d ORDER BY created_at DESC"
        ), {"d": today}).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])


# ── Kitchen orders (kitchen role only) ────────────────────────────────────
@router.get("/api/staff/{slug}/kitchen-orders")
async def kitchen_orders(slug: str, phone: str, pin: str):
    m = _auth_staff(slug, phone, pin)
    _check_perm(m, "kitchen")
    cfg = load_tenant(slug)
    # Get all occupied tables with ORDER_PLACED status
    occupied = rc.get_all_occupied_tables(slug, cfg.get_table_names())
    kitchen_list = []
    for t in occupied:
        s = t["session"] or {}
        if s.get("status") in ("ORDER_PLACED", "PENDING_PAYMENT", "PAID"):
            kitchen_orders = s.get("kitchenOrders") or s.get("orders", [])
            if kitchen_orders:
                kitchen_list.append({
                    "table": t["table"],
                    "name": s.get("name","?"),
                    "status": s.get("status"),
                    "orders": kitchen_orders,
                    "ordered_at": s.get("createdAt","")
                })
    return JSONResponse(kitchen_list)


# ── Mark table done (kitchen) ──────────────────────────────────────────────
class KitchenDoneReq(BaseModel):
    slug: str; phone: str; pin: str; table: str

@router.post("/api/staff/kitchen-done")
async def kitchen_done(req: KitchenDoneReq):
    m = _auth_staff(req.slug, req.phone, req.pin)
    _check_perm(m, "kitchen")
    from app.services import whatsapp as wa
    cfg = load_tenant(req.slug)
    cust_phone = rc.get_table_phone(req.slug, req.table)
    if not cust_phone:
        return JSONResponse({"success": False, "error": "No session at this table"})
    session = rc.get_session(req.slug, cust_phone)
    if session:
        orders = list(session.get("orders", []))
        session.setdefault("servedOrders", []).extend(orders)
        session.update({"kitchenOrders": [], "orders": [], "status": "ORDERING"})
        rc.save_session(req.slug, cust_phone, session, cfg.session_ttl)
        cust_name = session.get("name","Customer")
        await wa.send_text(cfg, cust_phone,
            f"🍽️ *Your Food is Ready!* 🎉\n\nHey {cust_name}! ✅ Enjoy your meal!\n\n*5* - 💵 Bill | *7* - 🔔 Waiter")
        await wa.send_all_staff(cfg, f"🍽️ *FOOD READY — {req.table}*\n👤 {cust_name}")
    return JSONResponse({"success": True})


# ── Approve customer (manager/owner) ──────────────────────────────────────
class ApproveReq(BaseModel):
    slug: str; phone: str; pin: str; cust_phone: str

@router.post("/api/staff/approve")
async def staff_approve(req: ApproveReq):
    m = _auth_staff(req.slug, req.phone, req.pin)
    _check_perm(m, "tables")
    import secrets as _sec
    from app.services import whatsapp as wa
    cfg = load_tenant(req.slug)
    session = rc.get_session(req.slug, req.cust_phone)
    if not session or session.get("status") != "AWAITING_APPROVAL":
        return JSONResponse({"success": False, "error": "No pending request"})
    token    = _sec.token_hex(8)
    menu_url = f"{cfg.menu_url or 'https://restroflow.coolify.yeshikasingh.cloud'}/menu/{req.slug}?t={session['table']}&p={req.cust_phone}&n={session['name'].split()[0]}&k={token}"
    session.update({"status":"ORDERING","approvedAt":datetime.utcnow().isoformat(),"menuToken":token,"menuURL":menu_url})
    rc.save_session(req.slug, req.cust_phone, session, cfg.session_ttl)
    rc.set_table(req.slug, session["table"], req.cust_phone, pending=False, ttl=cfg.session_ttl)
    rc.delete_pending(req.slug, req.cust_phone)
    await wa.send_text(cfg, req.cust_phone,
        f"✅ *Approved! Welcome to {cfg.restaurant_name}!*\n\n"
        f"👤 {session['name']}\n🪑 Table: {session['table']}\n\n"
        f"🍽️ *Order here:*\n👉 {menu_url}\n\n*1* - Menu | *7* - Waiter")
    return JSONResponse({"success": True, "message": f"Approved {session.get('name')}"})


# ── Reject customer ────────────────────────────────────────────────────────
@router.post("/api/staff/reject")
async def staff_reject(req: ApproveReq):
    m = _auth_staff(req.slug, req.phone, req.pin)
    _check_perm(m, "tables")
    from app.services import whatsapp as wa
    cfg = load_tenant(req.slug)
    session = rc.get_session(req.slug, req.cust_phone)
    table = (session or {}).get("table","")
    rc.clear_customer(req.slug, req.cust_phone, table)
    await wa.send_text(cfg, req.cust_phone, "❌ Request not approved. Please speak to staff.")
    return JSONResponse({"success": True})


# ── Confirm cash payment ───────────────────────────────────────────────────
class CashReq(BaseModel):
    slug: str; phone: str; pin: str; cust_phone: str

@router.post("/api/staff/cash-confirm")
async def cash_confirm(req: CashReq):
    m = _auth_staff(req.slug, req.phone, req.pin)
    _check_perm(m, "orders")
    from app.services import whatsapp as wa
    from sqlalchemy import text as sqlt
    cfg = load_tenant(req.slug)
    session = rc.get_session(req.slug, req.cust_phone)
    if not session:
        return JSONResponse({"success": False, "error": "No session"})
    orders = list(session.get("orders",[]))
    sub    = sum(float(o["price"])*int(o["quantity"]) for o in orders)
    tax    = round(sub * cfg.gst_rate)
    total  = sub + tax
    now    = datetime.utcnow().isoformat()
    session.setdefault("paidOrders",[]).append({"items":orders,"paidAt":now,"paymentMethod":"Cash","total":total})
    session.update({"status":"PAID","paymentMethod":"Cash","paidAt":now,"orders":[]})
    rc.save_session(req.slug, req.cust_phone, session, cfg.session_ttl)
    # Save to DB
    name = session.get("name","Customer"); table = session.get("table","")
    now_ist = datetime.now(IST)
    items_str = ", ".join(f"{o['quantity']}x {o['name']}" for o in orders)
    order_id = session.get("orderId") or f"ORD{int(datetime.now().timestamp())}"
    with cfg.db_session() as db:
        db.execute(sqlt("""
            INSERT INTO orders (order_id,date,date_only,customer_name,phone,table_name,
            items,subtotal,tax,total,payment_method,status,billed)
            VALUES (:oid,:date,:donly,:name,:phone,:table,:items,:sub,:tax,:total,'Cash','Paid',FALSE)
        """), {"oid":order_id,"date":now_ist.strftime("%d/%m/%Y, %I:%M:%S %p"),
               "donly":now_ist.strftime("%-d/%-m/%Y"),"name":name,"phone":req.cust_phone,
               "table":table,"items":items_str,"sub":sub,"tax":tax,"total":total})
        db.commit()
    await wa.send_text(cfg, req.cust_phone,
        f"✅ *Payment Confirmed!*\n👤 {name} | 🪑 {table}\n💰 ₹{total:.0f} (Cash)\n\nThank you! 🙏")
    await wa.send_all_staff(cfg, f"✅ Cash confirmed: {name} | {table} | ₹{total:.0f}")
    return JSONResponse({"success": True, "total": total})


# ── Free a table ───────────────────────────────────────────────────────────
class FreeTableReq(BaseModel):
    slug: str; phone: str; pin: str; table: str

@router.post("/api/staff/free-table")
async def free_table(req: FreeTableReq):
    m = _auth_staff(req.slug, req.phone, req.pin)
    _check_perm(m, "tables")
    cust_phone = rc.get_table_phone(req.slug, req.table)
    if cust_phone:
        rc.clear_customer(req.slug, cust_phone, req.table)
    return JSONResponse({"success": True, "message": f"Table {req.table} freed"})


# ── Overview (owner only) ──────────────────────────────────────────────────
@router.get("/api/staff/{slug}/overview")
async def staff_overview(slug: str, phone: str, pin: str):
    m = _auth_staff(slug, phone, pin)
    _check_perm(m, "overview")
    cfg = load_tenant(slug)
    today = datetime.now(IST).strftime("%-d/%-m/%Y")
    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT payment_method, SUM(total) as amount, COUNT(*) as cnt "
            "FROM orders WHERE date_only=:d AND status='Paid' GROUP BY payment_method"
        ), {"d": today}).fetchall()
        cash = online = total = orders_count = 0
        for r in rows:
            pm = (r.payment_method or "").lower()
            if pm == "cash": cash += float(r.amount or 0)
            else: online += float(r.amount or 0)
            total += float(r.amount or 0); orders_count += int(r.cnt or 0)
    return JSONResponse({"cash":cash,"online":online,"total":total,"orders":orders_count,"date":today})


# ── Pending approvals ──────────────────────────────────────────────────────
@router.get("/api/staff/{slug}/pending")
async def staff_pending(slug: str, phone: str, pin: str):
    m = _auth_staff(slug, phone, pin)
    _check_perm(m, "tables")
    cfg = load_tenant(slug)
    occupied = rc.get_all_occupied_tables(slug, cfg.get_table_names())
    pending = []
    for t in occupied:
        s = t["session"] or {}
        if s.get("status") == "AWAITING_APPROVAL":
            pending.append({"table": t["table"], "phone": t["phone"],
                           "name": s.get("name","?"), "status": "AWAITING_APPROVAL"})
    return JSONResponse(pending)
