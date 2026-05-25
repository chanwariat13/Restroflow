"""
routes/admin.py
REST API to manage clients — add, update, list, activate/deactivate.
Protected by ADMIN_SECRET env variable.
This is how you onboard new restaurants — just one API call.
"""
import json
import os
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from app.models.database import MasterSession, Client, setup_tenant_db
from app.utils.tenant import invalidate_cache

router = APIRouter()

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "change-this-secret")


def _auth(secret: str):
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


class ClientCreate(BaseModel):
    slug:                str
    restaurant_name:     str
    menu_url:            str = ""
    evolution_url:       str
    evolution_key:       str
    evolution_instance:  str
    staff_owner:         str
    staff_manager:       str = ""
    staff_kitchen:       str = ""
    staff_extra:         str = ""   # comma-separated phones
    payment_method:      str = "upi"
    upi_id:              str = ""
    upi_name:            str = ""
    razorpay_key_id:     str = ""
    razorpay_key_secret: str = ""
    table_count:         int = 10
    table_prefix:        str = "T"
    table_secrets:       dict = {}
    max_session_hours:   int = 2
    gst_rate:            float = 0.05
    session_ttl:         int = 10800
    premium_threshold:   int = 2
    cleanup_minutes:     int = 30
    festival_active:     bool = False
    festival_name:       str = ""
    festival_emoji:      str = "🎉"
    discount_percent:    int = 10
    festival_start:      str = ""
    festival_end:        str = ""
    gotenberg_url:       str = "http://localhost:3000"
    tenant_db_url:       str
    dashboard_password:  str = ""
    google_review_url:   str = ""
    logo_url:            str = ""
    primary_color:       str = "#ff6b35"
    welcome_message:     str = "Welcome! Scan & Order"
    banner_image:        str = ""


class ClientUpdate(BaseModel):
    restaurant_name:     Optional[str] = None
    menu_url:            Optional[str] = None
    staff_owner:         Optional[str] = None
    staff_manager:       Optional[str] = None
    staff_kitchen:       Optional[str] = None
    upi_id:              Optional[str] = None
    upi_name:            Optional[str] = None
    table_count:         Optional[int] = None
    gst_rate:            Optional[float] = None
    festival_active:     Optional[bool] = None
    festival_name:       Optional[str] = None
    festival_start:      Optional[str] = None
    festival_end:        Optional[str] = None
    discount_percent:    Optional[int] = None
    active:              Optional[bool] = None
    dashboard_password:  Optional[str] = None
    google_review_url:   Optional[str] = None
    logo_url:            Optional[str] = None
    primary_color:       Optional[str] = None
    welcome_message:     Optional[str] = None
    banner_image:        Optional[str] = None


# ── List all clients ──────────────────────────────────────────────────────────
@router.get("/admin/clients")
async def list_clients(x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    db = MasterSession()
    try:
        clients = db.query(Client).order_by(Client.created_at.desc()).all()
        return JSONResponse([{
            "slug":             c.slug,
            "restaurant_name":  c.restaurant_name,
            "active":           c.active,
            "table_count":      c.table_count,
            "payment_method":   c.payment_method,
            "evolution_instance": c.evolution_instance,
        } for c in clients])
    finally:
        db.close()


# ── Get one client ────────────────────────────────────────────────────────────
@router.get("/admin/clients/{slug}")
async def get_client(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    db = MasterSession()
    try:
        c = db.query(Client).filter(Client.slug == slug).first()
        if not c:
            raise HTTPException(status_code=404, detail="Client not found")
        return JSONResponse({
            "slug": c.slug, "restaurant_name": c.restaurant_name,
            "active": c.active, "table_count": c.table_count,
            "staff_owner": c.staff_owner, "staff_manager": c.staff_manager,
            "staff_kitchen": c.staff_kitchen, "payment_method": c.payment_method,
            "upi_id": c.upi_id, "gst_rate": float(c.gst_rate or 0),
            "festival_active": c.festival_active, "festival_name": c.festival_name,
        })
    finally:
        db.close()


# ── Add new client ────────────────────────────────────────────────────────────
@router.post("/admin/clients")
async def add_client(data: ClientCreate, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    db = MasterSession()
    try:
        existing = db.query(Client).filter(Client.slug == data.slug).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Client '{data.slug}' already exists")

        client = Client(
            slug=data.slug, restaurant_name=data.restaurant_name,
            menu_url=data.menu_url, evolution_url=data.evolution_url,
            evolution_key=data.evolution_key, evolution_instance=data.evolution_instance,
            staff_owner=data.staff_owner, staff_manager=data.staff_manager,
            staff_kitchen=data.staff_kitchen, staff_extra=data.staff_extra,
            payment_method=data.payment_method, upi_id=data.upi_id,
            upi_name=data.upi_name, razorpay_key_id=data.razorpay_key_id,
            razorpay_key_secret=data.razorpay_key_secret,
            table_count=data.table_count, table_prefix=data.table_prefix,
            table_secrets=json.dumps(data.table_secrets),
            max_session_hours=data.max_session_hours, gst_rate=data.gst_rate,
            session_ttl=data.session_ttl, premium_threshold=data.premium_threshold,
            cleanup_minutes=data.cleanup_minutes, festival_active=data.festival_active,
            festival_name=data.festival_name, festival_emoji=data.festival_emoji,
            discount_percent=data.discount_percent, festival_start=data.festival_start,
            festival_end=data.festival_end, gotenberg_url=data.gotenberg_url,
            tenant_db_url=data.tenant_db_url,
            dashboard_password=data.dashboard_password,
            google_review_url=data.google_review_url,
            logo_url=data.logo_url,
            primary_color=data.primary_color,
            welcome_message=data.welcome_message,
            banner_image=data.banner_image,
            active=True
        )
        db.add(client); db.commit()

        # Create tenant DB tables automatically
        setup_tenant_db(data.tenant_db_url, data.slug)

        return JSONResponse({"success": True, "message": f"Client '{data.slug}' created!", "slug": data.slug})
    finally:
        db.close()


# ── Update client ─────────────────────────────────────────────────────────────
@router.patch("/admin/clients/{slug}")
async def update_client(slug: str, data: ClientUpdate, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    db = MasterSession()
    try:
        c = db.query(Client).filter(Client.slug == slug).first()
        if not c:
            raise HTTPException(status_code=404, detail="Client not found")

        for field, value in data.model_dump(exclude_none=True).items():
            setattr(c, field, value)
        db.commit()
        invalidate_cache(slug)  # Clear cache so next request gets fresh config

        return JSONResponse({"success": True, "message": f"Client '{slug}' updated"})
    finally:
        db.close()


# ── Toggle active ─────────────────────────────────────────────────────────────
@router.post("/admin/clients/{slug}/deactivate")
async def deactivate_client(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    db = MasterSession()
    try:
        c = db.query(Client).filter(Client.slug == slug).first()
        if not c: raise HTTPException(status_code=404)
        c.active = False; db.commit()
        invalidate_cache(slug)
        return JSONResponse({"success": True, "message": f"'{slug}' deactivated"})
    finally:
        db.close()


@router.post("/admin/clients/{slug}/activate")
async def activate_client(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    db = MasterSession()
    try:
        c = db.query(Client).filter(Client.slug == slug).first()
        if not c: raise HTTPException(status_code=404)
        c.active = True; db.commit()
        invalidate_cache(slug)
        return JSONResponse({"success": True, "message": f"'{slug}' activated"})
    finally:
        db.close()


# ── Staff Management ──────────────────────────────────────────────────────
class StaffCreate(BaseModel):
    phone: str; name: str
    role: str  # owner | manager | kitchen | waiter
    pin: str

@router.get("/admin/clients/{slug}/staff")
async def list_staff(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from app.models.database import StaffMember
    db = MasterSession()
    try:
        members = db.query(StaffMember).filter(StaffMember.slug == slug).all()
        return JSONResponse([{
            "id": m.id, "phone": m.phone, "name": m.name,
            "role": m.role, "pin": m.pin, "active": m.active
        } for m in members])
    finally:
        db.close()

@router.post("/admin/clients/{slug}/staff")
async def add_staff(slug: str, data: StaffCreate, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from app.models.database import StaffMember
    db = MasterSession()
    try:
        member = StaffMember(slug=slug, phone=data.phone, name=data.name, role=data.role, pin=data.pin, active=True)
        db.add(member); db.commit()
        return JSONResponse({"success": True, "message": f"Staff '{data.name}' added"})
    finally:
        db.close()

@router.patch("/admin/clients/{slug}/staff/{staff_id}")
async def update_staff(slug: str, staff_id: int, data: StaffCreate, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from app.models.database import StaffMember
    db = MasterSession()
    try:
        m = db.query(StaffMember).filter(StaffMember.id == staff_id, StaffMember.slug == slug).first()
        if not m: raise HTTPException(status_code=404)
        m.phone = data.phone; m.name = data.name; m.role = data.role; m.pin = data.pin
        db.commit()
        return JSONResponse({"success": True, "message": "Staff updated"})
    finally:
        db.close()

@router.delete("/admin/clients/{slug}/staff/{staff_id}")
async def delete_staff(slug: str, staff_id: int, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from app.models.database import StaffMember
    db = MasterSession()
    try:
        m = db.query(StaffMember).filter(StaffMember.id == staff_id, StaffMember.slug == slug).first()
        if not m: raise HTTPException(status_code=404)
        db.delete(m); db.commit()
        return JSONResponse({"success": True})
    finally:
        db.close()


# ── Menu CSV Import ──────────────────────────────────────────────────────────
import csv, io

@router.post("/admin/clients/{slug}/import-menu")
async def import_menu(slug: str, request: Request, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from app.utils.tenant import load_tenant
    from sqlalchemy import text

    body = await request.body()
    content = body.decode("utf-8-sig")  # handle BOM

    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)

    if not rows:
        raise HTTPException(status_code=400, detail="Empty CSV")

    cfg = load_tenant(slug)
    inserted = 0

    with cfg.db_session() as db:
        # Clear existing menu
        db.execute(text("DELETE FROM menu"))
        for row in rows:
            try:
                price = float(row.get("Price", 0) or 0)
                if price <= 0:
                    continue
                db.execute(text("""
                    INSERT INTO menu (name, category, price, available, type, description, image, bestseller)
                    VALUES (:name, :cat, :price, :avail, :type, :desc, :img, :best)
                """), {
                    "name":  row.get("Name", "").strip(),
                    "cat":   row.get("Category", "Other").strip(),
                    "price": price,
                    "avail": row.get("Available", "Yes").strip(),
                    "type":  row.get("Type", "veg").strip().lower(),
                    "desc":  row.get("Description", "").strip(),
                    "img":   row.get("Image", "").strip(),
                    "best":  row.get("Bestseller", "no").strip().lower(),
                })
                inserted += 1
            except Exception:
                continue
        db.commit()

    return JSONResponse({"success": True, "message": f"Imported {inserted} menu items", "count": inserted})


# ── Admin: Full Menu Management for any client ────────────────────────────────
from app.utils.tenant import load_tenant
from sqlalchemy import text as sqlt

@router.get("/admin/clients/{slug}/menu")
async def admin_get_menu(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        rows = db.execute(sqlt("SELECT * FROM menu ORDER BY category, name")).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])


class AdminMenuItem(BaseModel):
    name: str; category: str = "Main Course"; price: float
    available: str = "Yes"; type: str = "veg"
    image: str = ""; description: str = ""; bestseller: str = "no"

@router.post("/admin/clients/{slug}/menu")
async def admin_add_menu_item(slug: str, body: AdminMenuItem, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(sqlt(
            "INSERT INTO menu (name,category,price,available,type,image,description,bestseller) "
            "VALUES (:n,:cat,:p,:av,:t,:img,:desc,:best)"
        ), {"n":body.name,"cat":body.category,"p":body.price,"av":body.available,
            "t":body.type,"img":body.image,"desc":body.description,"best":body.bestseller})
        db.commit()
    return JSONResponse({"success": True, "message": "Item added"})

@router.patch("/admin/clients/{slug}/menu/{item_id}")
async def admin_update_menu_item(slug: str, item_id: int, body: AdminMenuItem, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(sqlt(
            "UPDATE menu SET name=:n,category=:cat,price=:p,available=:av,type=:t,"
            "image=:img,description=:desc,bestseller=:best WHERE id=:id"
        ), {"n":body.name,"cat":body.category,"p":body.price,"av":body.available,
            "t":body.type,"img":body.image,"desc":body.description,"best":body.bestseller,"id":item_id})
        db.commit()
    return JSONResponse({"success": True})

@router.delete("/admin/clients/{slug}/menu/{item_id}")
async def admin_delete_menu_item(slug: str, item_id: int, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(sqlt("DELETE FROM menu WHERE id=:id"), {"id": item_id})
        db.commit()
    return JSONResponse({"success": True})


# ── Admin: View any client dashboard data ─────────────────────────────────────
@router.get("/admin/clients/{slug}/overview")
async def admin_client_overview(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.utils import redis_client as rc
    IST = ZoneInfo("Asia/Kolkata")
    cfg = load_tenant(slug)
    today = datetime.now(IST).strftime("%-d/%-m/%Y")
    with cfg.db_session() as db:
        rows = db.execute(sqlt(
            "SELECT payment_method, SUM(total) as amount, COUNT(*) as cnt "
            "FROM orders WHERE date_only=:d AND status='Paid' GROUP BY payment_method"
        ), {"d": today}).fetchall()
        cash = online = total = orders_count = 0
        for r in rows:
            pm = (r.payment_method or "").lower()
            if pm == "cash": cash += float(r.amount or 0)
            else: online += float(r.amount or 0)
            total += float(r.amount or 0); orders_count += int(r.cnt or 0)
        cust_count = db.execute(sqlt("SELECT COUNT(*) FROM customers")).scalar() or 0
        low_stock  = db.execute(sqlt("SELECT COUNT(*) FROM inventory WHERE current_stock<=min_threshold")).scalar() or 0
    occupied = rc.get_all_occupied_tables(slug, cfg.get_table_names())
    live_tables = []
    for t in occupied:
        s = t["session"] or {}
        live_tables.append({"table":t["table"],"name":s.get("name","?"),"status":s.get("status","?"),"total":s.get("total",0)})
    return JSONResponse({
        "today": {"cash":cash,"online":online,"total":total,"orders":orders_count,"date":today},
        "live_tables": live_tables, "occupied_count": len(live_tables),
        "total_tables": cfg.table_count, "customer_count": int(cust_count), "low_stock_count": int(low_stock)
    })

@router.get("/admin/clients/{slug}/orders")
async def admin_client_orders(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    cfg = load_tenant(slug)
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%-d/%-m/%Y")
    with cfg.db_session() as db:
        rows = db.execute(sqlt("SELECT * FROM orders WHERE date_only=:d ORDER BY created_at DESC"), {"d":today}).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])

@router.get("/admin/clients/{slug}/customers")
async def admin_client_customers(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        rows = db.execute(sqlt("SELECT * FROM customers ORDER BY total_visits DESC LIMIT 100")).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])

@router.get("/admin/clients/{slug}/inventory")
async def admin_client_inventory(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        rows = db.execute(sqlt("SELECT * FROM inventory ORDER BY item_name")).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])

@router.get("/admin/clients/{slug}/reports")
async def admin_client_reports(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        rows = db.execute(sqlt("SELECT * FROM daily_collection ORDER BY created_at DESC LIMIT 30")).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])
