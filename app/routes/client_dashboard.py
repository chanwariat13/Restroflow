"""
routes/client_dashboard.py
API endpoints for the client (restaurant owner) dashboard.
Login with slug + dashboard_password → see only their own data.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy import text
from typing import Optional
import json, os

from app.models.database import MasterSession, Client, get_tenant_session
from app.utils.tenant import load_tenant
from app.utils import redis_client as rc

router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")


def _get_client(slug: str) -> Client:
    db = MasterSession()
    try:
        c = db.query(Client).filter(Client.slug == slug, Client.active == True).first()
        if not c:
            raise HTTPException(status_code=404, detail="Client not found")
        return c
    finally:
        db.close()


def _auth_client(slug: str, password: str) -> Client:
    c = _get_client(slug)
    if not c.dashboard_password or c.dashboard_password != password:
        raise HTTPException(status_code=401, detail="Wrong password")
    return c


# ── Dashboard page ─────────────────────────────────────────────────────────
@router.get("/dashboard/{slug}")
async def client_dashboard_page(slug: str):
    path = os.path.join(os.path.dirname(__file__), "..", "..", "static", "client-dashboard.html")
    return FileResponse(path)


# ── Login ──────────────────────────────────────────────────────────────────
class LoginReq(BaseModel):
    slug: str
    password: str

@router.post("/api/client/login")
async def client_login(req: LoginReq):
    try:
        c = _auth_client(req.slug, req.password)
        return JSONResponse({"success": True, "restaurant_name": c.restaurant_name, "slug": c.slug})
    except HTTPException as e:
        return JSONResponse({"success": False, "error": e.detail}, status_code=e.status_code)


# ── Overview / Today stats ─────────────────────────────────────────────────
@router.get("/api/client/{slug}/overview")
async def client_overview(slug: str, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    today = datetime.now(IST).strftime("%-d/%-m/%Y")

    with cfg.db_session() as db:
        # Today orders
        rows = db.execute(text(
            "SELECT payment_method, SUM(total) as amount, COUNT(*) as cnt "
            "FROM orders WHERE date_only=:d AND status='Paid' GROUP BY payment_method"
        ), {"d": today}).fetchall()

        cash = online = total = orders_count = 0
        for r in rows:
            pm = (r.payment_method or "").lower()
            if pm == "cash": cash += float(r.amount or 0)
            else: online += float(r.amount or 0)
            total += float(r.amount or 0)
            orders_count += int(r.cnt or 0)

        # Live tables
        occupied = rc.get_all_occupied_tables(slug, cfg.get_table_names())
        live_tables = []
        for t in occupied:
            s = t["session"] or {}
            live_tables.append({
                "table": t["table"],
                "name": s.get("name", "?"),
                "status": s.get("status", "?"),
                "total": s.get("total", 0),
                "orders": len(s.get("orders", []))
            })

        # Customer count
        cust_count = db.execute(text("SELECT COUNT(*) as cnt FROM customers")).scalar() or 0

        # Low stock
        low_stock = db.execute(text(
            "SELECT COUNT(*) as cnt FROM inventory WHERE current_stock <= min_threshold"
        )).scalar() or 0

    return JSONResponse({
        "today": {"cash": cash, "online": online, "total": total, "orders": orders_count, "date": today},
        "live_tables": live_tables,
        "occupied_count": len(live_tables),
        "total_tables": cfg.table_count,
        "customer_count": int(cust_count),
        "low_stock_count": int(low_stock),
    })


# ── Menu management ────────────────────────────────────────────────────────
@router.get("/api/client/{slug}/menu")
async def get_menu(slug: str, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        rows = db.execute(text("SELECT * FROM menu ORDER BY category, name")).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])


class MenuItemBody(BaseModel):
    name: str; category: str = "Main Course"; price: float
    available: str = "Yes"; type: str = "veg"
    image: str = ""; description: str = ""; bestseller: str = "no"

@router.post("/api/client/{slug}/menu")
async def add_menu_item(slug: str, body: MenuItemBody, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(text(
            "INSERT INTO menu (name,category,price,available,type,image,description,bestseller) "
            "VALUES (:n,:cat,:p,:av,:t,:img,:desc,:best)"
        ), {"n": body.name, "cat": body.category, "p": body.price, "av": body.available,
            "t": body.type, "img": body.image, "desc": body.description, "best": body.bestseller})
        db.commit()
    return JSONResponse({"success": True, "message": "Item added"})

@router.patch("/api/client/{slug}/menu/{item_id}")
async def update_menu_item(slug: str, item_id: int, body: MenuItemBody, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(text(
            "UPDATE menu SET name=:n,category=:cat,price=:p,available=:av,type=:t,"
            "image=:img,description=:desc,bestseller=:best WHERE id=:id"
        ), {"n": body.name, "cat": body.category, "p": body.price, "av": body.available,
            "t": body.type, "img": body.image, "desc": body.description,
            "best": body.bestseller, "id": item_id})
        db.commit()
    return JSONResponse({"success": True})

@router.delete("/api/client/{slug}/menu/{item_id}")
async def delete_menu_item(slug: str, item_id: int, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(text("DELETE FROM menu WHERE id=:id"), {"id": item_id})
        db.commit()
    return JSONResponse({"success": True})


# ── Inventory ──────────────────────────────────────────────────────────────
@router.get("/api/client/{slug}/inventory")
async def get_inventory(slug: str, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        rows = db.execute(text("SELECT * FROM inventory ORDER BY item_name")).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])

class InventoryItem(BaseModel):
    item_name: str; unit: str = "g"
    current_stock: float; min_threshold: float; cost_price: float = 0

@router.post("/api/client/{slug}/inventory")
async def add_inventory(slug: str, body: InventoryItem, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(text(
            "INSERT INTO inventory (item_name,unit,current_stock,min_threshold,cost_price) "
            "VALUES (:n,:u,:cs,:mt,:cp)"
        ), {"n": body.item_name, "u": body.unit, "cs": body.current_stock,
            "mt": body.min_threshold, "cp": body.cost_price})
        db.commit()
    return JSONResponse({"success": True})

@router.patch("/api/client/{slug}/inventory/{item_id}")
async def update_inventory(slug: str, item_id: int, body: InventoryItem, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(text(
            "UPDATE inventory SET item_name=:n,unit=:u,current_stock=:cs,"
            "min_threshold=:mt,cost_price=:cp,updated_at=NOW() WHERE id=:id"
        ), {"n": body.item_name, "u": body.unit, "cs": body.current_stock,
            "mt": body.min_threshold, "cp": body.cost_price, "id": item_id})
        db.commit()
    return JSONResponse({"success": True})


# ── Customers ──────────────────────────────────────────────────────────────
@router.get("/api/client/{slug}/customers")
async def get_customers(slug: str, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT * FROM customers ORDER BY total_visits DESC, total_spent DESC LIMIT 100"
        )).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])


# ── Orders history ─────────────────────────────────────────────────────────
@router.get("/api/client/{slug}/orders")
async def get_orders(slug: str, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    today = datetime.now(IST).strftime("%-d/%-m/%Y")
    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT * FROM orders WHERE date_only=:d ORDER BY created_at DESC"
        ), {"d": today}).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])


# ── Reports ────────────────────────────────────────────────────────────────
@router.get("/api/client/{slug}/reports")
async def get_reports(slug: str, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT * FROM daily_collection ORDER BY created_at DESC LIMIT 30"
        )).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])


# ── Settings ───────────────────────────────────────────────────────────────
@router.get("/api/client/{slug}/settings")
async def get_settings(slug: str, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    secrets = {}
    try:
        secrets = json.loads(c.table_secrets or "{}")
    except Exception:
        pass
    return JSONResponse({
        "restaurant_name": c.restaurant_name,
        "menu_url": c.menu_url,
        "upi_id": c.upi_id,
        "upi_name": c.upi_name,
        "table_count": c.table_count,
        "gst_rate": float(c.gst_rate or 0.05),
        "google_review_url": c.google_review_url or "",
        "festival_active": c.festival_active,
        "festival_name": c.festival_name or "",
        "festival_emoji": c.festival_emoji or "🎉",
        "discount_percent": c.discount_percent or 0,
        "festival_start": c.festival_start or "",
        "festival_end": c.festival_end or "",
        "table_secrets": secrets,
        "staff_owner": c.staff_owner,
        "staff_manager": c.staff_manager or "",
        "staff_kitchen": c.staff_kitchen or "",
        "evolution_instance": c.evolution_instance,
        "logo_url": c.logo_url or "",
        "primary_color": c.primary_color or "#ff6b35",
        "welcome_message": c.welcome_message or "",
        "banner_image": c.banner_image or "",
    })

class SettingsUpdate(BaseModel):
    google_review_url:  Optional[str] = None
    festival_active:    Optional[bool] = None
    festival_name:      Optional[str] = None
    festival_emoji:     Optional[str] = None
    discount_percent:   Optional[int] = None
    festival_start:     Optional[str] = None
    festival_end:       Optional[str] = None
    dashboard_password: Optional[str] = None
    upi_id:             Optional[str] = None
    upi_name:           Optional[str] = None

@router.patch("/api/client/{slug}/settings")
async def update_settings(slug: str, body: SettingsUpdate, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    db = MasterSession()
    try:
        client = db.query(Client).filter(Client.slug == slug).first()
        for field, value in body.model_dump(exclude_none=True).items():
            setattr(client, field, value)
        db.commit()
        from app.utils.tenant import invalidate_cache
        invalidate_cache(slug)
        return JSONResponse({"success": True, "message": "Settings updated"})
    finally:
        db.close()


# ── QR Codes ───────────────────────────────────────────────────────────────
@router.get("/api/client/{slug}/qr-codes")
async def get_qr_codes(slug: str, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    base_url = f"https://restroflow.coolify.yeshikasingh.cloud"
    try:
        secrets = json.loads(c.table_secrets or "{}")
    except Exception:
        secrets = {}
    qr_codes = []
    for i in range(1, (c.table_count or 10) + 1):
        table = f"T{i}"
        secret = secrets.get(table, f"{slug[:3].upper()}{2025+i}")
        reg_url = f"{base_url}/r/{slug}?table={table}&secret={secret}"
        qr_api  = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&margin=10&data={reg_url}"
        qr_codes.append({"table": table, "secret": secret, "reg_url": reg_url, "qr_image": qr_api})
    return JSONResponse(qr_codes)


# ── Feedback ───────────────────────────────────────────────────────────────
@router.get("/api/client/{slug}/feedback")
async def get_feedback(slug: str, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT * FROM feedback ORDER BY date DESC LIMIT 50"
        )).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])
