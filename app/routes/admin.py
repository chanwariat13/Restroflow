"""
routes/admin.py
REST API to manage clients — add, update, list, activate/deactivate.
Protected by ADMIN_SECRET env variable.
This is how you onboard new restaurants — just one API call.
"""
import json
import os
from fastapi import APIRouter, Header, HTTPException, Request
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



# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — FULL CLIENT ACCESS
# Endpoints below give the super admin every capability a client owner has,
# scoped to a single restaurant via slug. All mirror the client/staff routes
# but are auth-gated by the master ADMIN_SECRET header.
# ─────────────────────────────────────────────────────────────────────────────

# ── Master Dashboard (combined stats across all clients) ─────────────────────
@router.get("/admin/dashboard")
async def admin_master_dashboard(x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.utils import redis_client as rc
    IST = ZoneInfo("Asia/Kolkata")
    today = datetime.now(IST).strftime("%-d/%-m/%Y")

    db = MasterSession()
    try:
        clients = db.query(Client).order_by(Client.created_at.desc()).all()
    finally:
        db.close()

    total_revenue = 0.0
    total_orders = 0
    total_occupied = 0
    total_tables = 0
    total_customers = 0
    total_low_stock = 0
    active_clients = 0
    per_client = []

    for c in clients:
        rev = orders = occ = custs = low = 0
        is_active = bool(c.active)
        if is_active:
            active_clients += 1
        try:
            cfg = load_tenant(c.slug)
            with cfg.db_session() as db2:
                rows = db2.execute(sqlt(
                    "SELECT COALESCE(SUM(total),0) AS amt, COUNT(*) AS cnt "
                    "FROM orders WHERE date_only=:d AND status='Paid'"
                ), {"d": today}).fetchone()
                if rows:
                    rev = float(rows.amt or 0)
                    orders = int(rows.cnt or 0)
                custs = db2.execute(sqlt("SELECT COUNT(*) FROM customers")).scalar() or 0
                low = db2.execute(sqlt(
                    "SELECT COUNT(*) FROM inventory WHERE current_stock<=min_threshold"
                )).scalar() or 0
            occupied = rc.get_all_occupied_tables(c.slug, cfg.get_table_names())
            occ = len(occupied)
        except Exception:
            pass

        total_revenue   += rev
        total_orders    += orders
        total_occupied  += occ
        total_tables    += int(c.table_count or 0)
        total_customers += int(custs or 0)
        total_low_stock += int(low or 0)

        per_client.append({
            "slug":            c.slug,
            "restaurant_name": c.restaurant_name,
            "logo_url":        c.logo_url or "",
            "primary_color":   c.primary_color or "#ff6b35",
            "active":          is_active,
            "table_count":     c.table_count or 0,
            "occupied":        occ,
            "today_revenue":   rev,
            "today_orders":    orders,
            "customers":       int(custs or 0),
            "low_stock":       int(low or 0),
            "payment_method":  c.payment_method or "upi",
        })

    return JSONResponse({
        "active_clients":  active_clients,
        "total_clients":   len(clients),
        "total_revenue":   round(total_revenue, 2),
        "total_orders":    total_orders,
        "total_occupied":  total_occupied,
        "total_tables":    total_tables,
        "total_customers": total_customers,
        "total_low_stock": total_low_stock,
        "date":            today,
        "per_client":      per_client,
    })


# ── Cross-client orders / customers ──────────────────────────────────────────
@router.get("/admin/all-orders")
async def admin_all_orders(x_admin_secret: str = Header(...), limit: int = 200):
    _auth(x_admin_secret)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%-d/%-m/%Y")

    db = MasterSession()
    try:
        clients = db.query(Client).filter(Client.active == True).all()
    finally:
        db.close()

    out = []
    for c in clients:
        try:
            cfg = load_tenant(c.slug)
            with cfg.db_session() as db2:
                rows = db2.execute(sqlt(
                    "SELECT * FROM orders WHERE date_only=:d ORDER BY created_at DESC LIMIT :l"
                ), {"d": today, "l": limit}).fetchall()
            for r in rows:
                d = dict(r._mapping)
                d["slug"] = c.slug
                d["restaurant_name"] = c.restaurant_name
                out.append(d)
        except Exception:
            continue
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return JSONResponse(out[:limit])


@router.get("/admin/all-customers")
async def admin_all_customers(x_admin_secret: str = Header(...), limit: int = 200):
    _auth(x_admin_secret)
    db = MasterSession()
    try:
        clients = db.query(Client).filter(Client.active == True).all()
    finally:
        db.close()

    out = []
    for c in clients:
        try:
            cfg = load_tenant(c.slug)
            with cfg.db_session() as db2:
                rows = db2.execute(sqlt(
                    "SELECT * FROM customers ORDER BY total_visits DESC, total_spent DESC LIMIT :l"
                ), {"l": limit}).fetchall()
            for r in rows:
                d = dict(r._mapping)
                d["slug"] = c.slug
                d["restaurant_name"] = c.restaurant_name
                out.append(d)
        except Exception:
            continue
    out.sort(key=lambda x: int(x.get("total_visits") or 0), reverse=True)
    return JSONResponse(out[:limit])


# ── Inventory CRUD (admin can manage stock for any client) ───────────────────
class AdminInventoryItem(BaseModel):
    item_name: str
    unit: str = "g"
    current_stock: float
    min_threshold: float
    cost_price: float = 0

@router.post("/admin/clients/{slug}/inventory")
async def admin_add_inventory(slug: str, body: AdminInventoryItem, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(sqlt(
            "INSERT INTO inventory (item_name,unit,current_stock,min_threshold,cost_price) "
            "VALUES (:n,:u,:cs,:mt,:cp)"
        ), {"n": body.item_name, "u": body.unit, "cs": body.current_stock,
            "mt": body.min_threshold, "cp": body.cost_price})
        db.commit()
    return JSONResponse({"success": True, "message": "Inventory item added"})

@router.patch("/admin/clients/{slug}/inventory/{item_id}")
async def admin_update_inventory(slug: str, item_id: int, body: AdminInventoryItem,
                                  x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(sqlt(
            "UPDATE inventory SET item_name=:n,unit=:u,current_stock=:cs,"
            "min_threshold=:mt,cost_price=:cp,updated_at=NOW() WHERE id=:id"
        ), {"n": body.item_name, "u": body.unit, "cs": body.current_stock,
            "mt": body.min_threshold, "cp": body.cost_price, "id": item_id})
        db.commit()
    return JSONResponse({"success": True})

@router.delete("/admin/clients/{slug}/inventory/{item_id}")
async def admin_delete_inventory(slug: str, item_id: int, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(sqlt("DELETE FROM inventory WHERE id=:id"), {"id": item_id})
        db.commit()
    return JSONResponse({"success": True})


# ── Feedback view ────────────────────────────────────────────────────────────
@router.get("/admin/clients/{slug}/feedback")
async def admin_feedback(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        rows = db.execute(sqlt(
            "SELECT * FROM feedback ORDER BY date DESC LIMIT 100"
        )).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])


# ── Full settings (everything the client owner can edit) ─────────────────────
@router.get("/admin/clients/{slug}/full-settings")
async def admin_get_full_settings(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    db = MasterSession()
    try:
        c = db.query(Client).filter(Client.slug == slug).first()
        if not c:
            raise HTTPException(status_code=404, detail="Client not found")
        try:
            secrets = json.loads(c.table_secrets or "{}")
        except Exception:
            secrets = {}
        return JSONResponse({
            "restaurant_name":   c.restaurant_name,
            "menu_url":          c.menu_url or "",
            "evolution_url":     c.evolution_url,
            "evolution_key":     c.evolution_key,
            "evolution_instance": c.evolution_instance,
            "staff_owner":       c.staff_owner,
            "staff_manager":     c.staff_manager or "",
            "staff_kitchen":     c.staff_kitchen or "",
            "staff_extra":       c.staff_extra or "",
            "payment_method":    c.payment_method or "upi",
            "upi_id":            c.upi_id or "",
            "upi_name":          c.upi_name or "",
            "razorpay_key_id":   c.razorpay_key_id or "",
            "table_count":       c.table_count or 10,
            "table_prefix":      c.table_prefix or "T",
            "table_secrets":     secrets,
            "max_session_hours": c.max_session_hours or 2,
            "gst_rate":          float(c.gst_rate or 0.05),
            "session_ttl":       c.session_ttl or 10800,
            "premium_threshold": c.premium_threshold or 2,
            "cleanup_minutes":   c.cleanup_minutes or 30,
            "festival_active":   bool(c.festival_active),
            "festival_name":     c.festival_name or "",
            "festival_emoji":    c.festival_emoji or "🎉",
            "discount_percent":  c.discount_percent or 0,
            "festival_start":    c.festival_start or "",
            "festival_end":      c.festival_end or "",
            "gotenberg_url":     c.gotenberg_url or "",
            "tenant_db_url":     c.tenant_db_url,
            "dashboard_password": c.dashboard_password or "",
            "google_review_url": c.google_review_url or "",
            "logo_url":          c.logo_url or "",
            "primary_color":     c.primary_color or "#ff6b35",
            "welcome_message":   c.welcome_message or "",
            "banner_image":      c.banner_image or "",
            "active":            bool(c.active),
        })
    finally:
        db.close()


class AdminFullSettings(BaseModel):
    restaurant_name:    Optional[str] = None
    menu_url:           Optional[str] = None
    evolution_url:      Optional[str] = None
    evolution_key:      Optional[str] = None
    evolution_instance: Optional[str] = None
    staff_owner:        Optional[str] = None
    staff_manager:      Optional[str] = None
    staff_kitchen:      Optional[str] = None
    staff_extra:        Optional[str] = None
    payment_method:     Optional[str] = None
    upi_id:             Optional[str] = None
    upi_name:           Optional[str] = None
    razorpay_key_id:    Optional[str] = None
    razorpay_key_secret: Optional[str] = None
    table_count:        Optional[int] = None
    max_session_hours:  Optional[int] = None
    gst_rate:           Optional[float] = None
    session_ttl:        Optional[int] = None
    premium_threshold:  Optional[int] = None
    cleanup_minutes:    Optional[int] = None
    festival_active:    Optional[bool] = None
    festival_name:      Optional[str] = None
    festival_emoji:     Optional[str] = None
    discount_percent:   Optional[int] = None
    festival_start:     Optional[str] = None
    festival_end:       Optional[str] = None
    gotenberg_url:      Optional[str] = None
    dashboard_password: Optional[str] = None
    google_review_url:  Optional[str] = None
    logo_url:           Optional[str] = None
    primary_color:      Optional[str] = None
    welcome_message:    Optional[str] = None
    banner_image:       Optional[str] = None

@router.patch("/admin/clients/{slug}/full-settings")
async def admin_update_full_settings(slug: str, body: AdminFullSettings,
                                      x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    db = MasterSession()
    try:
        c = db.query(Client).filter(Client.slug == slug).first()
        if not c:
            raise HTTPException(status_code=404, detail="Client not found")
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(c, field, value)
        db.commit()
        invalidate_cache(slug)
        return JSONResponse({"success": True, "message": "Settings updated"})
    finally:
        db.close()


# ── QR Codes for any client ─────────────────────────────────────────────────
@router.get("/admin/clients/{slug}/qr-codes")
async def admin_qr_codes(slug: str, request: Request, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    db = MasterSession()
    try:
        c = db.query(Client).filter(Client.slug == slug).first()
        if not c:
            raise HTTPException(status_code=404, detail="Client not found")
        try:
            secrets = json.loads(c.table_secrets or "{}")
        except Exception:
            secrets = {}
        base_url = str(request.base_url).rstrip("/")
        qr_codes = []
        for i in range(1, (c.table_count or 10) + 1):
            table  = f"{c.table_prefix or 'T'}{i}"
            secret = secrets.get(table, f"{slug[:3].upper()}{2025+i}")
            reg_url = f"{base_url}/r/{slug}?table={table}&secret={secret}"
            qr_api  = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&margin=10&data={reg_url}"
            qr_codes.append({"table": table, "secret": secret,
                              "reg_url": reg_url, "qr_image": qr_api})
        return JSONResponse(qr_codes)
    finally:
        db.close()


# ── Live operations — admin can run them on behalf of any client ────────────
@router.get("/admin/clients/{slug}/live")
async def admin_live_tables(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from app.utils import redis_client as rc
    cfg = load_tenant(slug)
    occupied = rc.get_all_occupied_tables(slug, cfg.get_table_names())
    live = []
    for t in occupied:
        s = t["session"] or {}
        live.append({
            "table":     t["table"],
            "phone":     t["phone"],
            "name":      s.get("name", "?"),
            "status":    s.get("status", "?"),
            "total":     s.get("total", 0),
            "orders":    s.get("orders", []),
            "createdAt": s.get("createdAt", ""),
        })
    return JSONResponse({
        "live_tables":   live,
        "total_tables":  cfg.table_count,
        "table_names":   cfg.get_table_names(),
    })


@router.get("/admin/clients/{slug}/pending")
async def admin_pending(slug: str, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from app.utils import redis_client as rc
    cfg = load_tenant(slug)
    occupied = rc.get_all_occupied_tables(slug, cfg.get_table_names())
    pending = []
    for t in occupied:
        s = t["session"] or {}
        if s.get("status") == "AWAITING_APPROVAL":
            pending.append({
                "table":  t["table"],
                "phone":  t["phone"],
                "name":   s.get("name", "?"),
                "status": "AWAITING_APPROVAL",
            })
    return JSONResponse(pending)


class AdminPhoneAction(BaseModel):
    cust_phone: str

@router.post("/admin/clients/{slug}/approve")
async def admin_approve(slug: str, body: AdminPhoneAction, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    import secrets as _sec
    from datetime import datetime
    from app.services import whatsapp as wa
    from app.utils import redis_client as rc
    cfg = load_tenant(slug)
    session = rc.get_session(slug, body.cust_phone)
    if not session or session.get("status") != "AWAITING_APPROVAL":
        return JSONResponse({"success": False, "error": "No pending request"})
    token    = _sec.token_hex(8)
    menu_url = (f"{cfg.menu_url or 'https://restroflow.coolify.yeshikasingh.cloud'}"
                f"/menu/{slug}?t={session['table']}&p={body.cust_phone}"
                f"&n={session['name'].split()[0]}&k={token}")
    session.update({
        "status":     "ORDERING",
        "approvedAt": datetime.utcnow().isoformat(),
        "menuToken":  token,
        "menuURL":    menu_url,
    })
    rc.save_session(slug, body.cust_phone, session, cfg.session_ttl)
    rc.set_table(slug, session["table"], body.cust_phone, pending=False, ttl=cfg.session_ttl)
    rc.delete_pending(slug, body.cust_phone)
    await wa.send_text(cfg, body.cust_phone,
        f"✅ *Approved! Welcome to {cfg.restaurant_name}!*\n\n"
        f"👤 {session['name']}\n🪑 Table: {session['table']}\n\n"
        f"🍽️ *Order here:*\n👉 {menu_url}\n\n*1* - Menu | *7* - Waiter")
    return JSONResponse({"success": True, "message": f"Approved {session.get('name')}"})


@router.post("/admin/clients/{slug}/reject")
async def admin_reject(slug: str, body: AdminPhoneAction, x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from app.services import whatsapp as wa
    from app.utils import redis_client as rc
    cfg = load_tenant(slug)
    session = rc.get_session(slug, body.cust_phone)
    table = (session or {}).get("table", "")
    rc.clear_customer(slug, body.cust_phone, table)
    await wa.send_text(cfg, body.cust_phone,
        "❌ Request not approved. Please speak to staff.")
    return JSONResponse({"success": True})


class AdminTableAction(BaseModel):
    table: str

@router.post("/admin/clients/{slug}/free-table")
async def admin_free_table(slug: str, body: AdminTableAction,
                            x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from app.utils import redis_client as rc
    cust_phone = rc.get_table_phone(slug, body.table)
    if cust_phone:
        rc.clear_customer(slug, cust_phone, body.table)
    return JSONResponse({"success": True, "message": f"Table {body.table} freed"})


@router.post("/admin/clients/{slug}/cash-confirm")
async def admin_cash_confirm(slug: str, body: AdminPhoneAction,
                              x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.services import whatsapp as wa
    from app.utils import redis_client as rc
    IST = ZoneInfo("Asia/Kolkata")
    cfg = load_tenant(slug)
    session = rc.get_session(slug, body.cust_phone)
    if not session:
        return JSONResponse({"success": False, "error": "No session"})
    orders = list(session.get("orders", []))
    sub    = sum(float(o["price"]) * int(o["quantity"]) for o in orders)
    tax    = round(sub * cfg.gst_rate)
    total  = sub + tax
    now    = datetime.utcnow().isoformat()
    session.setdefault("paidOrders", []).append({
        "items": orders, "paidAt": now, "paymentMethod": "Cash", "total": total
    })
    session.update({"status": "PAID", "paymentMethod": "Cash",
                    "paidAt": now, "orders": []})
    rc.save_session(slug, body.cust_phone, session, cfg.session_ttl)
    name      = session.get("name", "Customer")
    table     = session.get("table", "")
    now_ist   = datetime.now(IST)
    items_str = ", ".join(f"{o['quantity']}x {o['name']}" for o in orders)
    order_id  = session.get("orderId") or f"ORD{int(datetime.now().timestamp())}"
    with cfg.db_session() as db:
        db.execute(sqlt("""
            INSERT INTO orders (order_id,date,date_only,customer_name,phone,table_name,
            items,subtotal,tax,total,payment_method,status,billed)
            VALUES (:oid,:date,:donly,:name,:phone,:table,:items,:sub,:tax,:total,'Cash','Paid',FALSE)
        """), {"oid": order_id, "date": now_ist.strftime("%d/%m/%Y, %I:%M:%S %p"),
                "donly": now_ist.strftime("%-d/%-m/%Y"), "name": name,
                "phone": body.cust_phone, "table": table, "items": items_str,
                "sub": sub, "tax": tax, "total": total})
        db.commit()
    await wa.send_text(cfg, body.cust_phone,
        f"✅ *Payment Confirmed!*\n👤 {name} | 🪑 {table}\n"
        f"💰 ₹{total:.0f} (Cash)\n\nThank you! 🙏")
    await wa.send_all_staff(cfg,
        f"✅ Cash confirmed (admin): {name} | {table} | ₹{total:.0f}")
    return JSONResponse({"success": True, "total": total})


@router.post("/admin/clients/{slug}/kitchen-done")
async def admin_kitchen_done(slug: str, body: AdminTableAction,
                              x_admin_secret: str = Header(...)):
    _auth(x_admin_secret)
    from app.services import whatsapp as wa
    from app.utils import redis_client as rc
    cfg = load_tenant(slug)
    cust_phone = rc.get_table_phone(slug, body.table)
    if not cust_phone:
        return JSONResponse({"success": False, "error": "No session at this table"})
    session = rc.get_session(slug, cust_phone)
    if session:
        orders = list(session.get("orders", []))
        session.setdefault("servedOrders", []).extend(orders)
        session.update({"kitchenOrders": [], "orders": [], "status": "ORDERING"})
        rc.save_session(slug, cust_phone, session, cfg.session_ttl)
        cust_name = session.get("name", "Customer")
        await wa.send_text(cfg, cust_phone,
            f"🍽️ *Your Food is Ready!* 🎉\n\nHey {cust_name}! ✅ Enjoy your meal!\n\n"
            f"*5* - 💵 Bill | *7* - 🔔 Waiter")
        await wa.send_all_staff(cfg,
            f"🍽️ *FOOD READY — {body.table}*\n👤 {cust_name}")
    return JSONResponse({"success": True})


# ── Change admin password ───────────────────────────────────────────────────
class AdminPasswordChange(BaseModel):
    new_password: str

@router.post("/admin/change-password")
async def admin_change_password(body: AdminPasswordChange,
                                 x_admin_secret: str = Header(...)):
    """
    Note: ADMIN_SECRET is read from the env at module load. Persisting a new
    secret requires a deploy-time env-var change. This endpoint validates the
    request shape and returns a hint; the operator should update ADMIN_SECRET
    in the deployment environment.
    """
    _auth(x_admin_secret)
    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    return JSONResponse({
        "success": True,
        "message": "ADMIN_SECRET is set via environment variable. Update it in your deployment "
                   "config (e.g. Coolify env vars) and restart the service.",
        "next_steps": "Set ADMIN_SECRET to your new value in the deployment environment.",
    })
