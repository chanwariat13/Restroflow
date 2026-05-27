"""
routes/client_dashboard.py
API endpoints for the client (restaurant owner) dashboard.
Login with slug + dashboard_password → see only their own data.
"""
import hmac
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy import text
from typing import Optional
import json, os

from app.models.database import MasterSession, Client
from app.utils.tenant import load_tenant
from app.utils import redis_client as rc
from app.utils.dates import fmt_date_short
from app.utils.security import hash_password, verify_password, needs_rehash

router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


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
    """Authenticate an owner against the stored dashboard password.

    Storage formats handled (mirrors staff PIN handling):
      * `pbkdf2_sha256$...`  — current encoding (PBKDF2-HMAC-SHA256)
      * anything else        — legacy plaintext, accepted via constant-time
                                byte compare and *transparently upgraded*
                                to PBKDF2 on this very request.

    The transparent upgrade happens on the master DB inside its own short
    session so we never write while another `MasterSession` is open. A
    write failure here is logged but does NOT fail the login — the user
    has already proved knowledge of the password and we'd rather degrade
    silently than lock them out of their own dashboard. The next login
    will retry the upgrade.
    """
    c = _get_client(slug)
    stored = c.dashboard_password or ""
    submitted = password or ""
    if not stored or not submitted or not verify_password(submitted, stored):
        raise HTTPException(status_code=401, detail="Wrong password")

    # Transparent legacy-plaintext upgrade. We only rewrite when the stored
    # value isn't already in the current encoding, to avoid a master-DB
    # write on every successful login.
    if needs_rehash(stored):
        try:
            db = MasterSession()
            try:
                row = db.query(Client).filter(Client.slug == slug).first()
                # Re-check the live row in case someone else just rotated it,
                # so we never clobber a fresh hash with one derived from a
                # stale read.
                if row and (row.dashboard_password or "") == stored:
                    row.dashboard_password = hash_password(submitted)
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning(
                "dashboard_password rehash failed for slug=%s: %s. "
                "Will retry on next login.",
                slug, e,
            )
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
    today = fmt_date_short(datetime.now(IST))

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

        # Low stock — only meaningful when the inventory module is enabled
        # for this client. Skipping the query when disabled also keeps the
        # overview snappy on tenants who don't use stock at all.
        if getattr(cfg, "inventory_enabled", True):
            low_stock = db.execute(text(
                "SELECT COUNT(*) as cnt FROM inventory WHERE current_stock <= min_threshold"
            )).scalar() or 0
        else:
            low_stock = 0

    return JSONResponse({
        "today": {"cash": cash, "online": online, "total": total, "orders": orders_count, "date": today},
        "live_tables": live_tables,
        "occupied_count": len(live_tables),
        "total_tables": cfg.table_count,
        "customer_count": int(cust_count),
        "low_stock_count": int(low_stock),
        "inventory_enabled": bool(getattr(cfg, "inventory_enabled", True)),
    })


# ── Menu management ────────────────────────────────────────────────────────
@router.get("/api/client/{slug}/menu")
async def get_menu(slug: str, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        rows = db.execute(text("SELECT * FROM menu ORDER BY category, name")).fetchall()
    out = []
    for r in rows:
        d = dict(r._mapping)
        # Mirror the admin endpoint's normalization so the client dashboard
        # gets a clean shape and the comma-separated dietary_tags stored in
        # the DB surface as an actual list.
        d["gst_rate"] = float(d["gst_rate"]) if d.get("gst_rate") is not None else None
        d["dietary_tags"] = [t.strip() for t in (d.get("dietary_tags") or "").split(",") if t.strip()]
        out.append(d)
    return JSONResponse(out)


class MenuItemBody(BaseModel):
    name: str; category: str = "Main Course"; price: float
    available: str = "Yes"; type: str = "veg"
    image: str = ""; description: str = ""; bestseller: str = "no"
    # P1 — owner can now set per-item GST and dietary tags from their own
    # dashboard. Previously only the super admin could. None on gst_rate
    # means "use the client default"; tags is a list of short slugs like
    # ["jain", "vegan", "glutenfree", "egg", "spicy"].
    gst_rate: Optional[float] = None
    dietary_tags: list[str] = []


def _tags_to_csv(tags) -> str:
    """Normalize a list/string of tags into a comma-separated lowercase csv.

    Mirrors the helper of the same name in admin.py so admin- and
    client-managed menu items share a wire format.
    """
    if isinstance(tags, str):
        parts = tags.split(",")
    else:
        parts = list(tags or [])
    cleaned: list[str] = []
    seen: set = set()
    for t in parts:
        v = str(t).strip().lower().replace(" ", "")
        if v and v not in seen:
            seen.add(v); cleaned.append(v)
    return ",".join(cleaned)


@router.post("/api/client/{slug}/menu")
async def add_menu_item(slug: str, body: MenuItemBody, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    tags = _tags_to_csv(body.dietary_tags)
    with cfg.db_session() as db:
        db.execute(text(
            "INSERT INTO menu (name,category,price,available,type,image,description,bestseller,gst_rate,dietary_tags) "
            "VALUES (:n,:cat,:p,:av,:t,:img,:desc,:best,:gst,:tags)"
        ), {"n": body.name, "cat": body.category, "p": body.price, "av": body.available,
            "t": body.type, "img": body.image, "desc": body.description, "best": body.bestseller,
            "gst": body.gst_rate, "tags": tags})
        db.commit()
    return JSONResponse({"success": True, "message": "Item added"})

@router.patch("/api/client/{slug}/menu/{item_id}")
async def update_menu_item(slug: str, item_id: int, body: MenuItemBody, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    tags = _tags_to_csv(body.dietary_tags)
    with cfg.db_session() as db:
        db.execute(text(
            "UPDATE menu SET name=:n,category=:cat,price=:p,available=:av,type=:t,"
            "image=:img,description=:desc,bestseller=:best,gst_rate=:gst,dietary_tags=:tags "
            "WHERE id=:id"
        ), {"n": body.name, "cat": body.category, "p": body.price, "av": body.available,
            "t": body.type, "img": body.image, "desc": body.description,
            "best": body.bestseller, "gst": body.gst_rate, "tags": tags, "id": item_id})
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
def _require_inventory_enabled(slug: str) -> None:
    """Reject inventory CRUD calls when the tenant has the module switched off.

    The flag lives on the Client row and is mirrored on TenantConfig. We
    fail closed with HTTP 403 so a client who has disabled the feature
    can't accidentally write rows that would re-surface if they re-enabled
    it without realising the API was still wired up. List / read
    endpoints are gated identically — there's no read-only mode.
    """
    cfg = load_tenant(slug)
    if not getattr(cfg, "inventory_enabled", True):
        raise HTTPException(status_code=403,
                            detail="Inventory module is disabled for this restaurant")


@router.get("/api/client/{slug}/inventory")
async def get_inventory(slug: str, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    _require_inventory_enabled(slug)
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
    _require_inventory_enabled(slug)
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
    _require_inventory_enabled(slug)
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
    today = fmt_date_short(datetime.now(IST))
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
        "payment_method": c.payment_method or "upi",
        # Razorpay key id is non-secret (it ships in the customer's browser
        # at checkout) so the owner is allowed to see what's currently
        # configured. The two secrets below are NEVER returned — instead
        # we surface boolean "is it set?" flags so the dashboard can show
        # a "Saved ✓" badge without leaking the value. Owners rotate the
        # secret by submitting a new one; an empty submission leaves it.
        "razorpay_key_id":           c.razorpay_key_id or "",
        "razorpay_key_secret_set":   bool(c.razorpay_key_secret),
        "razorpay_webhook_secret_set": bool(c.razorpay_webhook_secret),
        "inventory_enabled": bool(getattr(c, "inventory_enabled", True)),
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
    payment_method:     Optional[str] = None
    inventory_enabled:  Optional[bool] = None
    # Branding / theme — owner-editable from the dashboard.
    logo_url:           Optional[str] = None
    primary_color:      Optional[str] = None
    welcome_message:    Optional[str] = None
    banner_image:       Optional[str] = None
    # Razorpay credentials. key_id is non-secret; the two `*_secret` fields
    # are write-only — passing an empty string is treated as "leave the
    # existing secret untouched" so a save of the Payment Settings form
    # with the secret box left blank doesn't silently wipe credentials.
    razorpay_key_id:        Optional[str] = None
    razorpay_key_secret:    Optional[str] = None
    razorpay_webhook_secret: Optional[str] = None

@router.patch("/api/client/{slug}/settings")
async def update_settings(slug: str, body: SettingsUpdate, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    db = MasterSession()
    try:
        client = db.query(Client).filter(Client.slug == slug).first()
        changes = body.model_dump(exclude_none=True)
        # Validate payment_method if provided
        if "payment_method" in changes:
            if changes["payment_method"] not in ("razorpay", "upi_qr", "both", "upi"):
                return JSONResponse(
                    {"success": False, "error": "payment_method must be one of: razorpay, upi_qr, both, upi"},
                    status_code=400,
                )
        # Validate primary_color: must be a 6-digit hex with leading '#'.
        # Anything else is rejected outright instead of being silently
        # coerced — the customer pages already fall back to the default
        # for malformed values, but we'd rather give the owner explicit
        # feedback in the dashboard than write garbage to the master DB.
        if "primary_color" in changes:
            import re as _re
            if not _re.match(r"^#[0-9a-fA-F]{6}$", changes["primary_color"] or ""):
                return JSONResponse(
                    {"success": False, "error": "primary_color must be a 6-digit hex like #ff6b35"},
                    status_code=400,
                )
        # The two Razorpay `*_secret` fields are write-only. An empty
        # string submission is treated as "don't rotate" so an owner who
        # opens the Payment Settings form, switches modes and clicks Save
        # without re-entering the secret doesn't wipe the existing one.
        for sec_field in ("razorpay_key_secret", "razorpay_webhook_secret"):
            if sec_field in changes and not (changes[sec_field] or "").strip():
                changes.pop(sec_field)
        # Hash the new dashboard password so it lands on disk in the same
        # PBKDF2 encoding as everything else. An empty string is treated as
        # "don't rotate" so a UI that submits the form with the password
        # field blank doesn't silently wipe the owner's credential.
        if "dashboard_password" in changes:
            new_pw = (changes["dashboard_password"] or "").strip()
            if not new_pw:
                changes.pop("dashboard_password")
            else:
                changes["dashboard_password"] = hash_password(new_pw)
        for field, value in changes.items():
            setattr(client, field, value)
        db.commit()
        from app.utils.tenant import invalidate_cache
        invalidate_cache(slug)
        return JSONResponse({"success": True, "message": "Settings updated"})
    finally:
        db.close()


# ── QR Codes ───────────────────────────────────────────────────────────────
@router.get("/api/client/{slug}/qr-codes")
async def get_qr_codes(slug: str, request: Request, x_client_password: str = Header(...)):
    c = _auth_client(slug, x_client_password)
    import secrets as _sec
    from app.utils.tenant import invalidate_cache
    # Prefer an explicit PUBLIC_BASE_URL env var (set in deploys behind a
    # proxy where request.base_url would point at the internal scheme/host)
    # but fall back to the request's own base_url for portability — the
    # previous hardcoded "https://restroflow.coolify.yeshikasingh.cloud"
    # broke every other deployment.
    base_url = (os.getenv("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")
    try:
        secrets = json.loads(c.table_secrets or "{}")
    except Exception:
        secrets = {}
    # Honor the tenant's configured table_prefix so a client that picked
    # e.g. "A" sees "A1, A2…" QR codes — and the registration endpoint's
    # prefix-aware regex actually accepts them.
    prefix = (c.table_prefix or "T").upper()
    # Materialise random secrets for any tables missing them (see the
    # matching admin endpoint for the full rationale). Without this the
    # registration endpoint — which now fails closed instead of computing
    # a predictable default — would reject every QR code on a freshly-
    # onboarded client.
    added_any = False
    qr_codes = []
    for i in range(1, (c.table_count or 10) + 1):
        table = f"{prefix}{i}"
        secret = secrets.get(table)
        if not secret:
            secret = _sec.token_urlsafe(16)
            secrets[table] = secret
            added_any = True
        reg_url = f"{base_url}/r/{slug}?table={table}&secret={secret}"
        qr_api  = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&margin=10&data={reg_url}"
        qr_codes.append({"table": table, "secret": secret, "reg_url": reg_url, "qr_image": qr_api})
    if added_any:
        db = MasterSession()
        try:
            client = db.query(Client).filter(Client.slug == slug).first()
            if client:
                client.table_secrets = json.dumps(secrets)
                db.commit()
                invalidate_cache(slug)
        finally:
            db.close()
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



# ─────────────────────────────────────────────────────────────────────────────
# CLIENT — MENU ITEM VARIANTS + MODIFIERS  (P1 feature)
# Restaurant owner can add S/M/L sizes, Half/Full portions, and topping/sauce
# add-on groups directly from the client dashboard. Same data model the super
# admin endpoints write to, just gated by the dashboard password instead of
# the master ADMIN_SECRET.
# ─────────────────────────────────────────────────────────────────────────────
class CV_VariantBody(BaseModel):
    name: str
    price: float
    is_default: bool = False
    sort_order: int = 0
    available: bool = True


class CV_ModifierGroupBody(BaseModel):
    name: str
    min_select: int = 0
    max_select: int = 1
    sort_order: int = 0
    required: bool = False


class CV_ModifierBody(BaseModel):
    name: str
    price: float = 0
    is_default: bool = False
    sort_order: int = 0
    available: bool = True


# ── Variants ────────────────────────────────────────────────────────────────
@router.get("/api/client/{slug}/menu/{item_id}/variants")
async def client_list_variants(slug: str, item_id: int, x_client_password: str = Header(...)):
    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        if not db.execute(text("SELECT 1 FROM menu WHERE id=:id"), {"id": item_id}).fetchone():
            raise HTTPException(status_code=404, detail="Menu item not found")
        rows = db.execute(text(
            "SELECT id,menu_item_id,name,price,is_default,sort_order,available "
            "FROM menu_item_variants WHERE menu_item_id=:mid ORDER BY sort_order, id"
        ), {"mid": item_id}).fetchall()
    return JSONResponse([{
        "id": r.id, "menu_item_id": r.menu_item_id, "name": r.name,
        "price": float(r.price or 0), "is_default": bool(r.is_default),
        "sort_order": r.sort_order or 0, "available": bool(r.available)
    } for r in rows])


@router.post("/api/client/{slug}/menu/{item_id}/variants")
async def client_add_variant(slug: str, item_id: int, body: CV_VariantBody,
                              x_client_password: str = Header(...)):
    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        if not db.execute(text("SELECT 1 FROM menu WHERE id=:id"), {"id": item_id}).fetchone():
            raise HTTPException(status_code=404, detail="Menu item not found")
        if body.is_default:
            db.execute(text(
                "UPDATE menu_item_variants SET is_default=FALSE WHERE menu_item_id=:mid"
            ), {"mid": item_id})
        new_id = db.execute(text(
            "INSERT INTO menu_item_variants (menu_item_id,name,price,is_default,sort_order,available) "
            "VALUES (:mid,:n,:p,:d,:o,:a) RETURNING id"
        ), {"mid": item_id, "n": body.name, "p": body.price,
             "d": body.is_default, "o": body.sort_order, "a": body.available}).scalar()
        db.commit()
    return JSONResponse({"success": True, "id": new_id})


@router.patch("/api/client/{slug}/variants/{variant_id}")
async def client_update_variant(slug: str, variant_id: int, body: CV_VariantBody,
                                 x_client_password: str = Header(...)):
    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        row = db.execute(text("SELECT menu_item_id FROM menu_item_variants WHERE id=:id"),
                          {"id": variant_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Variant not found")
        if body.is_default:
            db.execute(text(
                "UPDATE menu_item_variants SET is_default=FALSE "
                "WHERE menu_item_id=:mid AND id<>:id"
            ), {"mid": row.menu_item_id, "id": variant_id})
        db.execute(text(
            "UPDATE menu_item_variants "
            "SET name=:n,price=:p,is_default=:d,sort_order=:o,available=:a "
            "WHERE id=:id"
        ), {"n": body.name, "p": body.price, "d": body.is_default,
             "o": body.sort_order, "a": body.available, "id": variant_id})
        db.commit()
    return JSONResponse({"success": True})


@router.delete("/api/client/{slug}/variants/{variant_id}")
async def client_delete_variant(slug: str, variant_id: int,
                                 x_client_password: str = Header(...)):
    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(text("DELETE FROM menu_item_variants WHERE id=:id"), {"id": variant_id})
        db.commit()
    return JSONResponse({"success": True})


# ── Modifier Groups + Modifiers ──────────────────────────────────────────────
@router.get("/api/client/{slug}/menu/{item_id}/modifier-groups")
async def client_list_groups(slug: str, item_id: int, x_client_password: str = Header(...)):
    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        if not db.execute(text("SELECT 1 FROM menu WHERE id=:id"), {"id": item_id}).fetchone():
            raise HTTPException(status_code=404, detail="Menu item not found")
        groups = db.execute(text(
            "SELECT id,name,min_select,max_select,sort_order,required "
            "FROM menu_item_modifier_groups WHERE menu_item_id=:mid "
            "ORDER BY sort_order, id"
        ), {"mid": item_id}).fetchall()
        out = []
        for g in groups:
            mods = db.execute(text(
                "SELECT id,name,price,is_default,sort_order,available "
                "FROM menu_item_modifiers WHERE group_id=:gid ORDER BY sort_order, id"
            ), {"gid": g.id}).fetchall()
            out.append({
                "id": g.id, "name": g.name,
                "min_select": g.min_select or 0,
                "max_select": g.max_select or 1,
                "sort_order": g.sort_order or 0,
                "required": bool(g.required),
                "modifiers": [{
                    "id": m.id, "name": m.name, "price": float(m.price or 0),
                    "is_default": bool(m.is_default),
                    "sort_order": m.sort_order or 0,
                    "available": bool(m.available),
                } for m in mods],
            })
    return JSONResponse(out)


@router.post("/api/client/{slug}/menu/{item_id}/modifier-groups")
async def client_add_group(slug: str, item_id: int, body: CV_ModifierGroupBody,
                            x_client_password: str = Header(...)):
    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    if body.max_select < max(1, body.min_select):
        raise HTTPException(status_code=400,
                            detail="max_select must be >= 1 and >= min_select")
    with cfg.db_session() as db:
        if not db.execute(text("SELECT 1 FROM menu WHERE id=:id"), {"id": item_id}).fetchone():
            raise HTTPException(status_code=404, detail="Menu item not found")
        new_id = db.execute(text(
            "INSERT INTO menu_item_modifier_groups "
            "(menu_item_id,name,min_select,max_select,sort_order,required) "
            "VALUES (:mid,:n,:mn,:mx,:o,:r) RETURNING id"
        ), {"mid": item_id, "n": body.name, "mn": body.min_select,
             "mx": body.max_select, "o": body.sort_order,
             "r": body.required or body.min_select > 0}).scalar()
        db.commit()
    return JSONResponse({"success": True, "id": new_id})


@router.patch("/api/client/{slug}/modifier-groups/{group_id}")
async def client_update_group(slug: str, group_id: int, body: CV_ModifierGroupBody,
                               x_client_password: str = Header(...)):
    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    if body.max_select < max(1, body.min_select):
        raise HTTPException(status_code=400,
                            detail="max_select must be >= 1 and >= min_select")
    with cfg.db_session() as db:
        db.execute(text(
            "UPDATE menu_item_modifier_groups "
            "SET name=:n,min_select=:mn,max_select=:mx,sort_order=:o,required=:r "
            "WHERE id=:id"
        ), {"n": body.name, "mn": body.min_select, "mx": body.max_select,
             "o": body.sort_order, "r": body.required or body.min_select > 0,
             "id": group_id})
        db.commit()
    return JSONResponse({"success": True})


@router.delete("/api/client/{slug}/modifier-groups/{group_id}")
async def client_delete_group(slug: str, group_id: int,
                               x_client_password: str = Header(...)):
    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        # Cascade delete child modifiers first
        db.execute(text("DELETE FROM menu_item_modifiers WHERE group_id=:gid"),
                    {"gid": group_id})
        db.execute(text("DELETE FROM menu_item_modifier_groups WHERE id=:id"),
                    {"id": group_id})
        db.commit()
    return JSONResponse({"success": True})


@router.post("/api/client/{slug}/modifier-groups/{group_id}/modifiers")
async def client_add_modifier(slug: str, group_id: int, body: CV_ModifierBody,
                               x_client_password: str = Header(...)):
    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        if not db.execute(text(
            "SELECT 1 FROM menu_item_modifier_groups WHERE id=:gid"
        ), {"gid": group_id}).fetchone():
            raise HTTPException(status_code=404, detail="Modifier group not found")
        new_id = db.execute(text(
            "INSERT INTO menu_item_modifiers "
            "(group_id,name,price,is_default,sort_order,available) "
            "VALUES (:gid,:n,:p,:d,:o,:a) RETURNING id"
        ), {"gid": group_id, "n": body.name, "p": body.price,
             "d": body.is_default, "o": body.sort_order, "a": body.available}).scalar()
        db.commit()
    return JSONResponse({"success": True, "id": new_id})


@router.patch("/api/client/{slug}/modifiers/{modifier_id}")
async def client_update_modifier(slug: str, modifier_id: int, body: CV_ModifierBody,
                                  x_client_password: str = Header(...)):
    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(text(
            "UPDATE menu_item_modifiers "
            "SET name=:n,price=:p,is_default=:d,sort_order=:o,available=:a "
            "WHERE id=:id"
        ), {"n": body.name, "p": body.price, "d": body.is_default,
             "o": body.sort_order, "a": body.available, "id": modifier_id})
        db.commit()
    return JSONResponse({"success": True})


@router.delete("/api/client/{slug}/modifiers/{modifier_id}")
async def client_delete_modifier(slug: str, modifier_id: int,
                                  x_client_password: str = Header(...)):
    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)
    with cfg.db_session() as db:
        db.execute(text("DELETE FROM menu_item_modifiers WHERE id=:id"),
                    {"id": modifier_id})
        db.commit()
    return JSONResponse({"success": True})




# ── Tally export ──────────────────────────────────────────────────
# Indian restaurant owners book all sales into Tally (Prime / ERP 9).
# This endpoint produces ready-to-import artefacts:
#   format=csv → flat CSV for Excel + manual journal entry
#   format=xml → Tally Day Book voucher XML; upload via Tally Gateway
#                → Import Data → Vouchers
# Every Sales Voucher includes the right ledger postings (CGST/SGST/IGST
# split, payment ledger, GSTIN if B2B).
@router.get("/api/client/{slug}/tally/export")
async def tally_export(
    slug: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    format: str = "xml",
    include_unpaid: bool = False,
    x_client_password: str = Header(...),
):
    from fastapi.responses import Response
    from app.services.tally import (
        build_orders_csv, build_tally_xml, fetch_orders_for_export,
    )
    from app.utils.audit import audit

    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)

    # Default range: previous month start → previous month end.
    today = datetime.now(IST).date()
    if not from_date or not to_date:
        first_of_this = today.replace(day=1)
        last_of_prev = first_of_this - timedelta(days=1)
        first_of_prev = last_of_prev.replace(day=1)
        from_date = from_date or first_of_prev.isoformat()
        to_date = to_date or last_of_prev.isoformat()

    fmt = (format or "xml").strip().lower()
    if fmt not in ("xml", "csv"):
        raise HTTPException(400, "format must be 'xml' or 'csv'")

    orders = fetch_orders_for_export(
        cfg, from_date=from_date, to_date=to_date,
        include_unpaid=bool(include_unpaid),
    )

    audit(
        "tally.export",
        actor="owner", actor_role="owner",
        slug=slug, target=fmt,
        payload={"from": from_date, "to": to_date,
                 "format": fmt, "rows": len(orders),
                 "include_unpaid": bool(include_unpaid)},
    )

    fname = f"tally-{slug}-{from_date}-to-{to_date}.{fmt}"
    if fmt == "csv":
        body = build_orders_csv(orders, cfg)
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{fname}"',
                "X-Row-Count": str(len(orders)),
            },
        )
    body = build_tally_xml(orders, cfg)
    return Response(
        content=body,
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Row-Count": str(len(orders)),
        },
    )


@router.get("/api/client/{slug}/tally/preview")
async def tally_preview(
    slug: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    include_unpaid: bool = False,
    x_client_password: str = Header(...),
):
    """JSON preview so the dashboard can show an "X orders, ₹Y total" summary
    before the owner clicks 'Download'."""
    from app.services.tally import fetch_orders_for_export

    _auth_client(slug, x_client_password)
    cfg = load_tenant(slug)

    today = datetime.now(IST).date()
    if not from_date or not to_date:
        from datetime import timedelta as _td
        first_of_this = today.replace(day=1)
        last_of_prev = first_of_this - _td(days=1)
        first_of_prev = last_of_prev.replace(day=1)
        from_date = from_date or first_of_prev.isoformat()
        to_date = to_date or last_of_prev.isoformat()

    orders = fetch_orders_for_export(
        cfg, from_date=from_date, to_date=to_date,
        include_unpaid=bool(include_unpaid),
    )
    total = sum(float(o.get("total") or 0) for o in orders)
    cgst = sum(float(o.get("cgst_amount") or 0) for o in orders)
    sgst = sum(float(o.get("sgst_amount") or 0) for o in orders)
    igst = sum(float(o.get("igst_amount") or 0) for o in orders)
    return JSONResponse({
        "from": from_date, "to": to_date,
        "row_count": len(orders),
        "total":   round(total, 2),
        "cgst":    round(cgst, 2),
        "sgst":    round(sgst, 2),
        "igst":    round(igst, 2),
        "tax":     round(cgst + sgst + igst, 2),
        "by_payment": _summarize_by_payment(orders),
        "sample":  [
            {
                "voucher": o.get("order_id"),
                "date": o.get("date_only"),
                "customer": o.get("customer_name"),
                "table": o.get("table_name"),
                "total": float(o.get("total") or 0),
            } for o in orders[:5]
        ],
    })


def _summarize_by_payment(orders) -> dict:
    out: dict = {}
    for o in orders:
        pm = (o.get("payment_method") or "Other").strip().title() or "Other"
        out.setdefault(pm, {"count": 0, "total": 0.0})
        out[pm]["count"] += 1
        out[pm]["total"] = round(out[pm]["total"] + float(o.get("total") or 0), 2)
    return out
