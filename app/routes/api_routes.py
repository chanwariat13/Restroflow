"""
routes/api_routes.py
All remaining API routes — all multi-tenant via {slug} in URL.
  GET  /webhook/{slug}/menu
  POST /webhook/{slug}/receive-order
  POST /webhook/{slug}/get-cart
  POST /webhook/{slug}/generate-bill
  POST /webhook/{slug}/deduct-inventory
  POST /webhook/{slug}/razorpay-webhook
  GET  /webhook/{slug}/lookup-customer  → returning-guest auto-fill
"""
import base64
import hmac
import json
import logging
import os
import re
import secrets as _secrets
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request, BackgroundTasks, Path, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import text

from app.utils.tenant import load_tenant, TenantConfig
from app.utils import redis_client as rc
from app.utils.security import verify_razorpay_signature
from app.utils.audit import audit
from app.utils.dates import fmt_date_short
from app.services import whatsapp as wa

router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


# ── Internal API key for routes that mutate state on behalf of the bot or
# admin dashboards. Without this, the previously-open `generate-bill`,
# `deduct-inventory`, `split-bill`, and `unbilled-orders` endpoints could be
# hit by anyone who guessed a slug — letting attackers mark orders billed,
# spam Gotenberg PDF generation, deduct arbitrary inventory, or send PDFs
# to phone numbers of their choice (cost amplification + spam vector).
#
# The bot now sends `X-Internal-Auth: <INTERNAL_API_KEY>` on its self-calls
# (see app/routes/whatsapp_bot.py); the dashboards mint a short-lived
# in-process token instead of going through the HTTP layer at all.
_INTERNAL_API_KEY = (os.getenv("INTERNAL_API_KEY") or "").strip()


def _require_internal_auth(provided: Optional[str]) -> None:
    """Constant-time check of the X-Internal-Auth header.

    Fails CLOSED when `INTERNAL_API_KEY` is unset. The previous
    fail-open-with-warning behaviour meant any operator who deployed
    without setting the env var exposed `generate-bill`, `split-bill`,
    `deduct-inventory`, and `unbilled-orders` to anyone who could guess a
    slug — including spam vectors and inventory deductions on demand.
    The bot in `app/routes/whatsapp_bot.py` reads the same env var and
    only sends the header when it's set, so once the operator configures
    it, both ends agree.
    """
    expected = _INTERNAL_API_KEY
    if not expected:
        # Fail closed and surface in logs so the operator notices on the
        # first failed call rather than silently allowing the world in.
        logger.error(
            "Internal endpoint hit but INTERNAL_API_KEY is not configured. "
            "Refusing the request. Set INTERNAL_API_KEY (and restart) to "
            "enable internal self-calls from the bot."
        )
        raise HTTPException(
            status_code=503,
            detail="Internal API disabled: INTERNAL_API_KEY not configured",
        )
    provided = (provided or "").strip()
    if not provided or not hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid internal auth")


def _new_order_id() -> str:
    """Cryptographically-random order id.

    The previous `f"ORD{int(now.timestamp())}"` collided whenever two
    requests landed in the same second — easy to trigger from concurrent
    Razorpay webhook retries or two pods racing on a single payment.
    `secrets.token_hex(5)` adds 40 bits of randomness on top of the
    timestamp, making collisions astronomically unlikely without changing
    the human-readable shape (still `ORD<digits><hex>`).
    """
    ts = int(datetime.now().timestamp())
    return f"ORD{ts}{_secrets.token_hex(5)}"


# ══════════════════════════════════════════════════════
# C — MENU API
# ══════════════════════════════════════════════════════
@router.get("/webhook/{slug}/menu")
async def get_menu(request: Request, slug: str = Path(...)):
    cfg    = load_tenant(slug)
    params = dict(request.query_params)
    is_view = str(params.get("view", "")).strip() == "1"
    table   = str(params.get("table", "")).strip().upper()

    if is_view and table:
        phone = rc.get_table_phone(cfg.slug, table)
        if not phone or not rc.get_session(cfg.slug, phone):
            return JSONResponse({"sessionEnded": True, "menu": []},
                                headers={"Access-Control-Allow-Origin": "*"})

    try:
        with cfg.db_session() as db:
            rows = db.execute(text(
                "SELECT id, name, category, price, available, type, image, description, bestseller, "
                "       gst_rate, dietary_tags "
                "FROM menu ORDER BY category, name"
            )).fetchall()

            # Pull variants + modifier groups + modifiers for ALL items in 3 queries
            item_ids = [int(r.id) for r in rows]
            variants_by_item: dict[int, list] = {}
            groups_by_item: dict[int, list] = {}
            mods_by_group: dict[int, list] = {}
            if item_ids:
                vrows = db.execute(text(
                    "SELECT id, menu_item_id, name, price, is_default, sort_order, available "
                    "FROM menu_item_variants WHERE menu_item_id = ANY(:ids) "
                    "ORDER BY menu_item_id, sort_order, id"
                ), {"ids": item_ids}).fetchall()
                for v in vrows:
                    variants_by_item.setdefault(int(v.menu_item_id), []).append({
                        "id": int(v.id), "name": v.name or "",
                        "price": float(v.price or 0),
                        "is_default": bool(v.is_default),
                        "available": bool(v.available),
                    })

                grows = db.execute(text(
                    "SELECT id, menu_item_id, name, min_select, max_select, sort_order, required "
                    "FROM menu_item_modifier_groups WHERE menu_item_id = ANY(:ids) "
                    "ORDER BY menu_item_id, sort_order, id"
                ), {"ids": item_ids}).fetchall()
                group_ids = [int(g.id) for g in grows]
                for g in grows:
                    groups_by_item.setdefault(int(g.menu_item_id), []).append({
                        "id": int(g.id), "name": g.name or "",
                        "min_select": int(g.min_select or 0),
                        "max_select": int(g.max_select or 1),
                        "required": bool(g.required) or int(g.min_select or 0) > 0,
                        "modifiers": [],  # filled below
                    })

                if group_ids:
                    mrows = db.execute(text(
                        "SELECT id, group_id, name, price, is_default, sort_order, available "
                        "FROM menu_item_modifiers WHERE group_id = ANY(:gids) "
                        "ORDER BY group_id, sort_order, id"
                    ), {"gids": group_ids}).fetchall()
                    for m in mrows:
                        mods_by_group.setdefault(int(m.group_id), []).append({
                            "id": int(m.id), "name": m.name or "",
                            "price": float(m.price or 0),
                            "is_default": bool(m.is_default),
                            "available": bool(m.available),
                        })
                # attach modifiers to their group
                for groups in groups_by_item.values():
                    for g in groups:
                        g["modifiers"] = mods_by_group.get(g["id"], [])

        menu = []
        for i, row in enumerate(rows):
            avail = str(row.available or "yes").lower()
            tags = [t.strip() for t in (str(row.dietary_tags or "")).split(",") if t.strip()]
            item_gst = float(row.gst_rate) if row.gst_rate is not None else None
            iid = int(row.id)
            variants = variants_by_item.get(iid, [])
            groups = groups_by_item.get(iid, [])
            menu.append({
                # Keep id stable across reloads → use DB id (was index before).
                "id": str(iid), "name": row.name or "",
                "category": row.category or "Other",
                "price": float(row.price or 0),
                "available": avail not in ("no", "false", "0"),
                "type": (row.type or "veg").lower(),
                "image": row.image or "", "description": row.description or "",
                "bestseller": str(row.bestseller or "no").lower() == "yes",
                "dietary_tags": tags,
                "gst_rate": item_gst,
                "variants": variants,         # [] when item has no size options
                "modifier_groups": groups,    # [] when item has no add-ons
                "has_options": bool(variants or groups),
            })
    except Exception:
        menu = []

    return JSONResponse(menu, headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=60"
    })


# ══════════════════════════════════════════════════════
# C2 — LOOKUP RETURNING CUSTOMER (auto-fill registration)
# ══════════════════════════════════════════════════════
@router.get("/webhook/{slug}/lookup-customer")
async def lookup_customer(request: Request, slug: str = Path(...)):
    """
    Given ?phone=91XXXX, return whether this guest has visited before.
    Used by the registration page to auto-fill the name field.
    Always returns {found: bool, name: str, visits: int} — never errors.

    SECURITY: This endpoint is unauthenticated by design (it's called from
    the registration page before the guest is registered). To prevent a
    runaway scraper from enumerating the entire customer phone book of a
    restaurant, we apply a simple per-IP rate limit.
    """
    cfg = load_tenant(slug)
    phone = str(request.query_params.get("phone", "")).strip()
    # Tighten the input: only accept 10–14 digit purely numeric phones so
    # an attacker can't smuggle SQL or large payloads through this open
    # endpoint. (Queries are already parameterised, but the validation
    # keeps logs clean and turns trash into a fast 200 with `found:false`.)
    if not phone or len(phone) < 10 or len(phone) > 14 or not phone.isdigit():
        return JSONResponse({"found": False, "name": "", "visits": 0})

    # Per-IP rate limit — 30 lookups/minute is plenty for a typical guest
    # who mistypes their number a few times. A scraper hitting 600/minute
    # gets 429s quickly.
    client_ip = request.client.host if request.client else "unknown"
    if not rc.rate_limit_check(f"lookup:{slug}:{client_ip}", limit=30, window_seconds=60):
        return JSONResponse(
            {"found": False, "name": "", "visits": 0,
             "error": "Too many requests, please slow down."},
            status_code=429,
        )

    try:
        with cfg.db_session() as db:
            row = db.execute(text(
                "SELECT name, total_visits FROM customers WHERE phone=:p LIMIT 1"
            ), {"p": phone}).fetchone()
        if row:
            return JSONResponse(
                {"found": True, "name": row.name or "", "visits": int(row.total_visits or 0)},
            )
    except Exception as e:
        logger.warning(f"lookup_customer failed slug={slug} phone={phone}: {e}")
    return JSONResponse({"found": False, "name": "", "visits": 0})


# ══════════════════════════════════════════════════════
# D — ORDER RECEIVER
# ══════════════════════════════════════════════════════
class OrderItem(BaseModel):
    id: str; name: str; price: float; qty: int; category: str = "Other"
    # NEW (P1): optional variant + modifier customisations
    variant_id: Optional[int] = None
    modifier_ids: list[int] = []
    # When present, overrides the rendered display name (e.g. "Pizza (Large) + Extra Cheese").
    # Server still recomputes the final price from DB to prevent tampering.
    display_name: Optional[str] = None

class OrderRequest(BaseModel):
    phone: str; table: str; name: str = ""; token: str = ""
    items: list[OrderItem]; notes: str = ""; fullCart: bool = False


def _resolve_item_price(db, item: OrderItem) -> tuple[float, str, str, list[int]]:
    """
    Returns (final_unit_price, display_name, base_menu_name, valid_modifier_ids) for one cart line.

    Server-side authoritative pricing: even if the client posts a wrong `price`,
    we look up the menu row + variant + modifiers from the tenant DB and rebuild
    the unit price. This is the only safe way to allow customisations.

    `base_menu_name` is the original menu row name — used for inventory lookup
    so that "Pizza (Large) + Extra Cheese" still deducts ingredients of "Pizza".

    Falls back to the client's posted price if the menu row cannot be matched
    (e.g. legacy orders sent without an integer DB id) — backward compatible.
    """
    try:
        item_id_int = int(item.id)
    except (TypeError, ValueError):
        return float(item.price), item.display_name or item.name, item.name, []

    row = db.execute(text("SELECT id, name, price FROM menu WHERE id=:id"),
                     {"id": item_id_int}).fetchone()
    if not row:
        return float(item.price), item.display_name or item.name, item.name, []

    base_name = row.name or item.name
    unit_price = float(row.price or 0)
    name_extra: list[str] = []

    # Variant overrides the base price
    if item.variant_id:
        v = db.execute(text(
            "SELECT name, price, available FROM menu_item_variants "
            "WHERE id=:vid AND menu_item_id=:mid"
        ), {"vid": int(item.variant_id), "mid": item_id_int}).fetchone()
        if v and bool(v.available if v.available is not None else True):
            unit_price = float(v.price or 0)
            if v.name:
                name_extra.append(f"({v.name})")

    # Modifiers add on top
    valid_mod_ids: list[int] = []
    if item.modifier_ids:
        # Only accept modifiers that belong to a group of THIS menu item
        rows = db.execute(text("""
            SELECT m.id AS id, m.name AS name, m.price AS price, m.available AS available
            FROM menu_item_modifiers m
            JOIN menu_item_modifier_groups g ON g.id = m.group_id
            WHERE g.menu_item_id = :mid
              AND m.id = ANY(:ids)
        """), {"mid": item_id_int,
               "ids": [int(x) for x in item.modifier_ids]}).fetchall()
        addon_names = []
        for r in rows:
            if r.available is False:
                continue
            unit_price += float(r.price or 0)
            valid_mod_ids.append(int(r.id))
            addon_names.append(r.name or "")
        if addon_names:
            name_extra.append("+ " + ", ".join(addon_names))

    display = base_name + ((" " + " ".join(name_extra)) if name_extra else "")
    return round(unit_price, 2), display, base_name, valid_mod_ids


@router.post("/webhook/{slug}/receive-order")
async def receive_order(req: OrderRequest, slug: str = Path(...)):
    cfg   = load_tenant(slug)
    phone = req.phone.strip()
    table = req.table.strip().upper()

    if not phone or not table or not req.items:
        return JSONResponse({"success": False, "error": "Missing fields"})

    session = rc.get_session(cfg.slug, phone)
    if not session:
        return JSONResponse({"success": False, "error": "No active session."})
    if session.get("status") not in ("ORDERING", "ORDER_PLACED"):
        return JSONResponse({"success": False, "error": f"Cannot add items. Status: {session.get('status')}"})
    if session.get("menuToken") and req.token and session["menuToken"] != req.token:
        return JSONResponse({"success": False, "error": "Invalid token."})

    # Server-side resolution: rebuild the cart with authoritative prices.
    # Each customised line is a SEPARATE cart entry so the same base item with
    # different toppings shows on a separate line in kitchen + bill.
    resolved: list[dict] = []
    with cfg.db_session() as db:
        for i in req.items:
            unit_price, display, base_name, valid_mods = _resolve_item_price(db, i)
            # Stable per-customisation cart key — same base + same variant + same
            # set of modifiers should stack (qty+=); different combos = new line.
            mod_key = ",".join(str(m) for m in sorted(valid_mods))
            cart_key = f"{i.id}|{i.variant_id or ''}|{mod_key}"
            resolved.append({
                "id": cart_key,
                "menu_id": i.id,
                "name": display,
                "menu_name": base_name,         # for inventory deduction (stays as base name)
                "price": unit_price,
                "quantity": int(i.qty),
                "category": i.category or "Other",
                "variant_id": i.variant_id,
                "modifier_ids": valid_mods,
            })

    if req.fullCart:
        session["orders"] = resolved
    else:
        orders = {str(o["id"]): o for o in session.get("orders", [])}
        for r in resolved:
            key = str(r["id"])
            if key in orders:
                orders[key]["quantity"] += r["quantity"]
            else:
                orders[key] = r
        session["orders"] = list(orders.values())

    sub = sum(float(o["price"]) * int(o["quantity"]) for o in session["orders"])
    tax = round(sub * cfg.gst_rate)
    total = sub + tax
    session["total"] = total
    session["status"] = "ORDER_PLACED"
    rc.save_session(cfg.slug, phone, session, cfg.session_ttl)

    order_list = "\n".join(
        f"  {j+1}. {o['quantity']}x {o['name']} = ₹{float(o['price'])*o['quantity']:.0f}"
        for j, o in enumerate(session["orders"])
    )
    await wa.send_text(cfg, phone,
        f"✅ *Order Updated!*\n━━━━━━━━━━━━━━━━━━\n\n🪑 {table}\n\n📋 *Cart:*\n{order_list}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n💰 Sub: ₹{sub:.0f} | GST: ₹{tax:.0f} | *Total: ₹{total:.0f}*\n\n"
        f"*3* - 📤 Kitchen | *5* - 💵 Pay | *7* - 🔔 Waiter")

    kitchen_items = "\n".join(f"  • {o['quantity']}x {o['name']}" for o in session["orders"])
    await wa.notify_new_order(cfg, table, session.get("name",""), phone, kitchen_items, total)

    return JSONResponse({"success": True, "total": total})


# ══════════════════════════════════════════════════════
# E — GET CART
# ══════════════════════════════════════════════════════
class CartRequest(BaseModel):
    phone: str; token: str = ""


@router.post("/webhook/{slug}/get-cart")
async def get_cart(req: CartRequest, slug: str = Path(...)):
    cfg     = load_tenant(slug)
    phone   = req.phone.strip()
    session = rc.get_session(cfg.slug, phone)

    if not session:
        return JSONResponse({"orders": [], "valid": False, "status": "NO_SESSION"},
                            headers={"Access-Control-Allow-Origin": "*"})
    if session.get("menuToken") and req.token and session["menuToken"] != req.token:
        return JSONResponse({"orders": [], "valid": False, "status": "INVALID_TOKEN"},
                            headers={"Access-Control-Allow-Origin": "*"})

    return JSONResponse(
        {"orders": session.get("orders", []), "valid": True, "status": session.get("status", "UNKNOWN")},
        headers={"Access-Control-Allow-Origin": "*"}
    )


# ══════════════════════════════════════════════════════
# K — BILL GENERATOR
# ══════════════════════════════════════════════════════
class BillRequest(BaseModel):
    table: str; phone: str = ""


@router.post("/webhook/{slug}/generate-bill")
async def generate_bill(req: BillRequest, slug: str = Path(...),
                        x_internal_auth: Optional[str] = Header(None)):
    _require_internal_auth(x_internal_auth)
    cfg   = load_tenant(slug)
    table = req.table.strip().upper()
    today = fmt_date_short(datetime.now(IST))

    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT order_id, date, customer_name, phone, table_name, "
            "items, subtotal, tax, total, payment_method, customer_gstin "
            "FROM orders WHERE table_name=:t AND status='Paid' AND billed=FALSE AND date_only=:d "
            "ORDER BY date ASC"
        ), {"t": table, "d": today}).fetchall()

    if not rows:
        return JSONResponse({"success": False, "message": "No paid unbilled orders found"})

    orders   = [dict(r._mapping) for r in rows]
    order_ids = [o["order_id"] for o in orders]
    customer = orders[0].get("customer_name", "Guest")
    phone    = req.phone or orders[0].get("phone", "")
    grand    = sum(float(o.get("total", 0)) for o in orders)
    tax_sum  = sum(float(o.get("tax",   0)) for o in orders)
    subtotal = grand - tax_sum

    # ── India compliance: split tax into CGST/SGST (intra-state) or IGST (inter-state)
    # We derive place-of-supply from the customer's GSTIN if present; otherwise
    # default to the restaurant's own state (intra-state). HSN/SAC for restaurant
    # service is 996331; can be overridden per-order if/when we wire it through.
    from app.services.gst import (
        compute_split, split_flat_tax, state_code_from_gstin, INDIAN_STATE_CODES,
    )

    seller_state = (cfg.state_code or "").strip()
    customer_state = ""
    customer_gstin = ""
    for o in orders:
        cg = (o.get("customer_gstin") or "").strip()
        if cg:
            customer_gstin = cg
            customer_state = state_code_from_gstin(cg)
            break
    inter_state = bool(seller_state and customer_state and seller_state != customer_state)
    cgst_sum, sgst_sum, igst_sum = split_flat_tax(
        subtotal, tax_sum, seller_state, customer_state, inter_state=inter_state
    )

    item_rows = ""
    for rnd, o in enumerate(orders, 1):
        items_text = str(o.get("items","")).replace("\n","<br>")
        item_rows += (
            f'<tr class="rh"><td colspan="2">Round {rnd}</td>'
            f'<td style="text-align:right;color:#888">{o.get("payment_method","")}</td></tr>'
            f'<tr><td colspan="2" style="padding-left:16px;color:#444">{items_text}</td>'
            f'<td style="text-align:right">&#8377;{float(o.get("total",0)):.2f}</td></tr>'
        )

    # Customer GSTIN (B2B invoice) — already extracted above for the GST split.

    seller_gstin = (cfg.gstin or "").strip()
    invoice_kind = "TAX INVOICE" if seller_gstin else "Bill"

    seller_gstin_html = (
        f'<div style="font-size:11px;color:#666;margin-top:2px">GSTIN: {seller_gstin}</div>'
        if seller_gstin else ""
    )
    customer_gstin_html = (
        f'<div style="font-size:11px;color:#666"><b>Customer GSTIN:</b> {customer_gstin}</div>'
        if customer_gstin else ""
    )

    # Tax-line block: CGST+SGST for intra-state, IGST for inter-state.
    rate_pct = int(round(float(cfg.gst_rate) * 100))
    if tax_sum <= 0:
        tax_block = ""
    elif inter_state:
        tax_block = (
            f'<tr><td>IGST ({rate_pct}%)</td>'
            f'<td>&#8377;{igst_sum:.2f}</td></tr>'
        )
    else:
        half_pct = rate_pct / 2
        # Show .0 only when needed.
        half_label = (f"{half_pct:.1f}".rstrip("0").rstrip("."))
        tax_block = (
            f'<tr><td>CGST ({half_label}%)</td>'
            f'<td>&#8377;{cgst_sum:.2f}</td></tr>'
            f'<tr><td>SGST ({half_label}%)</td>'
            f'<td>&#8377;{sgst_sum:.2f}</td></tr>'
        )

    pos_label = INDIAN_STATE_CODES.get(customer_state or seller_state, "") if (customer_state or seller_state) else ""
    pos_html = (
        f'<div style="font-size:11px;color:#666"><b>Place of Supply:</b> '
        f'{pos_label} ({customer_state or seller_state})</div>'
        if pos_label else ""
    )

    now_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Arial,sans-serif;padding:32px 28px;color:#222;background:#fff}}
.hdr{{text-align:center;margin-bottom:20px;border-bottom:2px solid #222;padding-bottom:14px}}
.hdr h1{{font-size:22px;font-weight:bold}}
.hdr p{{font-size:12px;color:#666;margin-top:4px}}
.kind{{font-size:11px;letter-spacing:2px;color:#888;margin-top:6px;text-transform:uppercase}}
.info{{display:flex;justify-content:space-between;font-size:13px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;margin-top:4px}}
th{{background:#222;color:#fff;padding:8px 10px;font-size:12px;text-align:left}}
th:last-child{{text-align:right}}
td{{padding:7px 10px;border-bottom:1px solid #f0f0f0;font-size:13px;vertical-align:top}}
.rh td{{background:#f7f7f7;font-weight:bold;font-size:12px;padding:5px 10px;border-bottom:none}}
.totals{{margin-top:18px;border-top:1px solid #ddd;padding-top:12px}}
.totals table{{width:50%;margin-left:auto}}
.totals td{{border:none;padding:4px 8px;font-size:13px}}
.totals td:last-child{{text-align:right}}
.grand td{{font-size:16px;font-weight:bold;border-top:2px solid #222;padding-top:8px}}
.ftr{{text-align:center;margin-top:28px;font-size:11px;color:#aaa;border-top:1px dashed #ddd;padding-top:14px}}
</style></head><body>
<div class="hdr"><h1>{cfg.restaurant_name}</h1>{seller_gstin_html}<p>Thank you for dining with us!</p>
<div class="kind">{invoice_kind}</div></div>
<div class="info">
  <div><b>Table:</b> {table} &nbsp; <b>Customer:</b> {customer}{customer_gstin_html}{pos_html}</div>
  <div style="font-size:11px;color:#aaa"><b>Date:</b> {now_ist}</div>
</div>
<table>
  <thead><tr><th colspan="2">Items</th><th style="text-align:right">Amount</th></tr></thead>
  <tbody>{item_rows}</tbody>
</table>
<div class="totals"><table>
  <tr><td>Subtotal</td><td>&#8377;{subtotal:.2f}</td></tr>
  {tax_block}
  <tr class="grand"><td>Grand Total</td><td>&#8377;{grand:.2f}</td></tr>
</table></div>
<div class="ftr">{cfg.restaurant_name} | Visit us again! &#128522;</div>
</body></html>"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{cfg.gotenberg_url}/forms/chromium/convert/html",
                files={"index.html": ("index.html", html.encode(), "text/html")}
            )
        pdf_b64 = base64.b64encode(resp.content).decode()
    except Exception as e:
        return JSONResponse({"success": False, "message": f"PDF error: {e}"})

    caption = f"*Bill - {cfg.restaurant_name}*\nTable: {table} | Total: ₹{grand:.2f}"
    if phone:
        await wa.send_document_base64(cfg, phone, pdf_b64, "bill.pdf", caption)
    await wa.send_document_base64(cfg, cfg.staff_owner, pdf_b64, "bill.pdf", caption)

    with cfg.db_session() as db:
        db.execute(text("UPDATE orders SET billed=TRUE WHERE order_id=ANY(:ids)"), {"ids": order_ids})
        db.commit()

    return JSONResponse({"success": True, "message": "Bill sent"})


# ══════════════════════════════════════════════════════
# L — INVENTORY DEDUCTION
# ══════════════════════════════════════════════════════
class DeductItem(BaseModel):
    name: str; quantity: int

class DeductRequest(BaseModel):
    items: list[DeductItem]; phone: str = ""; table: str = ""


@router.post("/webhook/{slug}/deduct-inventory")
async def deduct_inventory(req: DeductRequest, slug: str = Path(...),
                           x_internal_auth: Optional[str] = Header(None)):
    """
    Deducts inventory based on menu_ingredients × order quantity.
    SECURITY: Uses parameterized queries — item names are never interpolated into SQL.
    Requires X-Internal-Auth header (when INTERNAL_API_KEY is configured) to
    prevent unauthenticated stock manipulation by anyone who knows the slug.
    """
    _require_internal_auth(x_internal_auth)
    cfg = load_tenant(slug)
    if not req.items:
        return JSONResponse({"success": False, "message": "No items"})

    with cfg.db_session() as db:
        for item in req.items:
            db.execute(text("""
                UPDATE inventory inv
                SET current_stock = inv.current_stock - (mi.quantity_used * :qty),
                    updated_at = NOW()
                FROM menu_ingredients mi
                WHERE mi.menu_item = :item_name
                  AND mi.ingredient = inv.item_name
                  AND inv.current_stock >= (mi.quantity_used * :qty)
            """), {"qty": int(item.quantity), "item_name": item.name})
        db.commit()

        low = db.execute(text(
            "SELECT item_name, current_stock, min_threshold, unit "
            "FROM inventory WHERE current_stock <= min_threshold ORDER BY (current_stock-min_threshold) ASC"
        )).fetchall()

    if low:
        await wa.notify_low_stock(cfg, [dict(r._mapping) for r in low])

    return JSONResponse({"success": True})


# ══════════════════════════════════════════════════════
# I — RAZORPAY WEBHOOK (signature-verified)
# ══════════════════════════════════════════════════════
@router.post("/webhook/{slug}/razorpay-webhook")
async def razorpay_webhook(request: Request, background_tasks: BackgroundTasks, slug: str = Path(...)):
    """
    Razorpay POSTs the raw event JSON here, plus an X-Razorpay-Signature header
    that is HMAC_SHA256(webhook_secret, raw_body).hexdigest().

    SECURITY: We require a valid signature before processing. If the client has not
    yet configured a webhook secret, we still REJECT the request rather than process
    an unauthenticated payload. Configure razorpay_webhook_secret in admin → Settings.
    """
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "") or request.headers.get("x-razorpay-signature", "")

    cfg = load_tenant(slug)
    secret = (
        getattr(cfg, "razorpay_webhook_secret", "") or ""
    ).strip()

    if not secret:
        # No secret configured — refuse rather than process unauthenticated.
        audit("razorpay.webhook.rejected_no_secret", actor_role="system", slug=slug,
              target="razorpay", payload={"reason": "razorpay_webhook_secret not configured"},
              request=request)
        return JSONResponse(
            {"error": "Webhook secret not configured for this tenant. "
                      "Set razorpay_webhook_secret in admin settings."},
            status_code=503,
        )

    if not verify_razorpay_signature(raw, signature, secret):
        audit("razorpay.webhook.rejected_bad_signature", actor_role="system", slug=slug,
              target="razorpay", payload={"sig_present": bool(signature)}, request=request)
        return JSONResponse({"error": "Invalid signature"}, status_code=401)

    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    audit("razorpay.webhook.accepted", actor_role="system", slug=slug,
          target=str(body.get("event", "")), payload={"id": body.get("id", "")}, request=request)
    background_tasks.add_task(_process_razorpay, cfg, body)
    return JSONResponse({"status": "ok"})


async def _process_razorpay(cfg: TenantConfig, body: dict):
    if body.get("event") != "payment_link.paid":
        return

    pay_link = (body.get("payload", {}).get("payment_link") or {}).get("entity", {})
    payment  = (body.get("payload", {}).get("payment") or {}).get("entity", {})
    notes    = pay_link.get("notes", {})

    phone    = notes.get("phone", "")
    order_id = notes.get("order_id", "")
    table    = notes.get("table", "")
    if not phone:
        return

    # ── Idempotency guard ───────────────────────────────────────────────
    # Razorpay retries `payment_link.paid` events on any non-2xx or timeout
    # for up to 24h. Without this short-circuit, every retry inserted a
    # duplicate `orders` row, appended `paidOrders` again, and re-deducted
    # inventory. We claim a per-tenant, per-payment-id key in Redis with a
    # 7-day TTL — first claimer proceeds, retries no-op.
    payment_id = payment.get("id") or pay_link.get("id") or ""
    if payment_id and not rc.claim_event(cfg.slug, "razorpay", payment_id):
        logger.info(
            "Razorpay event already processed; skipping. slug=%s payment_id=%s",
            cfg.slug, payment_id,
        )
        return

    amount = payment.get("amount", 0) / 100
    method = payment.get("method", "Online")

    session = rc.get_session(cfg.slug, phone)
    if not session or session.get("status") not in ("PENDING_PAYMENT",):
        return

    orders = list(session.get("orders", []))
    sub    = sum(float(o["price"]) * int(o["quantity"]) for o in orders)
    tax    = round(sub * cfg.gst_rate)
    total  = sub + tax
    now    = datetime.utcnow().isoformat()
    name   = session.get("name", "Customer")

    session.setdefault("paidOrders", []).append(
        {"items": orders, "paidAt": now, "paymentMethod": method, "total": total}
    )
    session.update({"status": "PAID", "paymentMethod": "Online",
                    "paidAt": now, "razorpayId": payment.get("id",""), "orders": []})
    rc.save_session(cfg.slug, phone, session, cfg.session_ttl)

    items_str = ", ".join(f"{o['quantity']}x {o['name']}" for o in orders)
    now_ist   = datetime.now(IST)
    with cfg.db_session() as db:
        db.execute(text("""
            INSERT INTO orders (order_id,date,date_only,customer_name,phone,table_name,
            items,subtotal,tax,total,payment_method,status,billed)
            VALUES (:oid,:date,:donly,:name,:phone,:table,:items,:sub,:tax,:total,:method,'Paid',FALSE)
        """), {"oid": order_id, "date": now_ist.strftime("%d/%m/%Y, %I:%M:%S %p"),
               "donly": fmt_date_short(now_ist), "name": name, "phone": phone,
               "table": table, "items": items_str, "sub": sub, "tax": tax,
               "total": total, "method": "Online"})
        db.commit()

    await wa.send_text(cfg, phone,
        f"✅ *Payment Confirmed!*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {name} | 🪑 {table}\n💰 ₹{total:.0f} via {method}\n\n"
        f"🍳 Order being prepared!\n\n*8* - 👋 Checkout | *7* - 🔔 Waiter | *5* - 💵 Bill")
    await wa.notify_payment(cfg, phone, name, table, order_id, total, method)
    await wa.send_kitchen(cfg,
        f"🔥 *NEW ORDER (PAID)*\n🪑 {table} | 👤 {name}\n\n"
        + "\n".join(f"  • {o['quantity']}x {o['name']}" for o in orders)
        + f"\n\n✅ ₹{total:.0f} paid\n*{table}* confirm | *DONE {table}* when ready")

    # Deduct inventory directly (no internal HTTP self-call). The previous
    # implementation POSTed back to localhost:8000, which broke whenever the
    # service ran on a non-default port or behind a reverse proxy.
    try:
        with cfg.db_session() as db:
            for o in orders:
                qty = int(o["quantity"])
                base_name = o.get("menu_name") or o["name"]
                db.execute(text("""
                    UPDATE inventory inv
                    SET current_stock = inv.current_stock - (mi.quantity_used * :qty),
                        updated_at = NOW()
                    FROM menu_ingredients mi
                    WHERE mi.menu_item = :item_name
                      AND mi.ingredient = inv.item_name
                      AND inv.current_stock >= (mi.quantity_used * :qty)
                """), {"qty": qty, "item_name": base_name})
            db.commit()
            low = db.execute(text(
                "SELECT item_name, current_stock, min_threshold, unit "
                "FROM inventory WHERE current_stock <= min_threshold "
                "ORDER BY (current_stock-min_threshold) ASC"
            )).fetchall()
        if low:
            await wa.notify_low_stock(cfg, [dict(r._mapping) for r in low])
    except Exception as e:
        logger.warning("inventory deduction after razorpay failed: %s", e)



# ══════════════════════════════════════════════════════
# K2 — BILL SPLITTING (P1 feature)
# ══════════════════════════════════════════════════════
# Splits the unbilled-paid orders for a table across multiple guests in one of
# two modes:
#   - "equal":   total ÷ parts. Each share gets a PDF with the same item list
#                and a clearly highlighted "Your share: ₹X" line.
#   - "by_item": caller passes one entry per split with the items each guest
#                will pay for (line-by-line allocation). The sum of allocated
#                quantities per item must equal the original ordered qty.
#
# In both modes we generate one PDF per split via Gotenberg, optionally send
# each PDF to a per-share phone, and mark the underlying orders as billed=TRUE
# so they don't get billed twice. The owner always gets a copy of every PDF.
class SplitShare(BaseModel):
    """One share in a bill split.

    For mode="equal", only `phone` and `label` are honoured.
    For mode="by_item", `items` is a list of {key, qty} where `key` matches the
    cart key of an order line ("menuId|variantId|mod-csv") and `qty` is how
    much of that line this share is paying for.
    """
    label: str = ""
    phone: str = ""
    items: list[dict] = []  # [{"key": "5||", "qty": 1}] (only for by_item mode)


class SplitBillRequest(BaseModel):
    table: str
    mode: str = "equal"          # "equal" | "by_item"
    parts: int = 2               # used when mode == "equal"
    shares: list[SplitShare] = []
    notify_owner: bool = True    # always send a copy to the owner WhatsApp


def _parse_items_text(s: str) -> list[dict]:
    """
    Parse the "items" text column on the orders table. The producer side writes
    "qty x name (₹unit each = ₹line)" entries separated by ", ". We tolerate
    older variants ("qty x name") so legacy bills keep working. Returns a list
    of {qty, name, line_total} dicts; line_total is None when not present.
    """
    out = []
    if not s:
        return out
    parts = [p.strip() for p in str(s).split(",") if p.strip()]
    for p in parts:
        # Try newer format first: "qty x name (₹X each = ₹Y)"
        m = re.match(r"^(\d+)\s*x\s*(.+?)\s*\(₹\d+(?:\.\d+)?\s*each\s*=\s*₹(\d+(?:\.\d+)?)\)\s*$", p)
        if m:
            out.append({"qty": int(m.group(1)), "name": m.group(2).strip(),
                         "line_total": float(m.group(3))})
            continue
        # Legacy format: "qty x name"
        m = re.match(r"^(\d+)\s*x\s*(.+?)\s*$", p)
        if m:
            out.append({"qty": int(m.group(1)), "name": m.group(2).strip(),
                         "line_total": None})
            continue
        # Couldn't parse — keep the raw text on the line
        out.append({"qty": 1, "name": p, "line_total": None})
    return out


def _build_split_html(restaurant: str, seller_gstin: str, table: str,
                      share_label: str, total_share: int | float, total_share_breakdown: dict,
                      lines_html: str, share_idx: int, share_total: int) -> str:
    """Render a single split's HTML bill. Same look as generate-bill for consistency."""
    seller_gstin_html = (
        f'<div style="font-size:11px;color:#666;margin-top:2px">GSTIN: {seller_gstin}</div>'
        if seller_gstin else ""
    )
    now_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
    sub  = float(total_share_breakdown.get("sub", 0))
    tax  = float(total_share_breakdown.get("tax", 0))
    grand = float(total_share_breakdown.get("grand", total_share))
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Arial,sans-serif;padding:32px 28px;color:#222;background:#fff}}
.hdr{{text-align:center;margin-bottom:20px;border-bottom:2px solid #222;padding-bottom:14px}}
.hdr h1{{font-size:22px;font-weight:bold}}
.hdr p{{font-size:12px;color:#666;margin-top:4px}}
.kind{{font-size:11px;letter-spacing:2px;color:#888;margin-top:6px;text-transform:uppercase}}
.sharebadge{{display:inline-block;background:#222;color:#fff;font-size:11px;letter-spacing:2px;
  padding:4px 12px;border-radius:4px;margin-top:8px;text-transform:uppercase}}
.info{{display:flex;justify-content:space-between;font-size:13px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;margin-top:4px}}
th{{background:#222;color:#fff;padding:8px 10px;font-size:12px;text-align:left}}
th:last-child{{text-align:right}}
td{{padding:7px 10px;border-bottom:1px solid #f0f0f0;font-size:13px;vertical-align:top}}
.totals{{margin-top:18px;border-top:1px solid #ddd;padding-top:12px}}
.totals table{{width:55%;margin-left:auto}}
.totals td{{border:none;padding:4px 8px;font-size:13px}}
.totals td:last-child{{text-align:right}}
.share{{margin-top:10px;background:#fff7e6;border:2px solid #ff9933;border-radius:6px;
  padding:10px 14px;font-size:14px;font-weight:bold;color:#222}}
.share .amt{{float:right;color:#ff6600;font-size:18px}}
.ftr{{text-align:center;margin-top:28px;font-size:11px;color:#aaa;border-top:1px dashed #ddd;padding-top:14px}}
</style></head><body>
<div class="hdr">
  <h1>{restaurant}</h1>{seller_gstin_html}
  <p>Thank you for dining with us!</p>
  <div class="kind">Split Bill</div>
  <div class="sharebadge">Share {share_idx} of {share_total}{(' · '+share_label) if share_label else ''}</div>
</div>
<div class="info">
  <div><b>Table:</b> {table}</div>
  <div style="font-size:11px;color:#aaa"><b>Date:</b> {now_ist}</div>
</div>
<table>
  <thead><tr><th colspan="2">Items in your share</th><th style="text-align:right">Amount</th></tr></thead>
  <tbody>{lines_html}</tbody>
</table>
<div class="totals"><table>
  <tr><td>Subtotal (your share)</td><td>&#8377;{sub:.2f}</td></tr>
  <tr><td>Tax (proportional)</td><td>&#8377;{tax:.2f}</td></tr>
</table>
<div class="share">Your share <span class="amt">&#8377;{grand:.2f}</span></div>
</div>
<div class="ftr">{restaurant} | Visit us again! &#128522;</div>
</body></html>"""


@router.post("/webhook/{slug}/split-bill")
async def split_bill(req: SplitBillRequest, slug: str = Path(...),
                     x_internal_auth: Optional[str] = Header(None)):
    _require_internal_auth(x_internal_auth)
    cfg = load_tenant(slug)
    table = req.table.strip().upper()
    today = fmt_date_short(datetime.now(IST))

    if req.mode not in ("equal", "by_item"):
        return JSONResponse({"success": False, "error": "mode must be 'equal' or 'by_item'"},
                             status_code=400)

    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT order_id, items, subtotal, tax, total, customer_name, phone "
            "FROM orders WHERE table_name=:t AND status='Paid' AND billed=FALSE AND date_only=:d "
            "ORDER BY date ASC"
        ), {"t": table, "d": today}).fetchall()

    if not rows:
        return JSONResponse({"success": False, "message": "No paid unbilled orders found"})

    orders = [dict(r._mapping) for r in rows]
    order_ids = [o["order_id"] for o in orders]
    grand_subtotal = sum(float(o.get("subtotal", 0) or 0) for o in orders)
    grand_tax      = sum(float(o.get("tax", 0) or 0) for o in orders)
    grand_total    = sum(float(o.get("total", 0) or 0) for o in orders)
    table_customer = orders[0].get("customer_name", "Guest")
    seller_gstin   = (cfg.gstin or "").strip()

    shares_payload: list[dict] = []  # one entry per generated PDF

    if req.mode == "equal":
        parts = max(2, int(req.parts or 2))
        share_phones = [s.phone.strip() for s in req.shares] if req.shares else []
        share_labels = [s.label or f"Share {i+1}" for i, s in enumerate(req.shares)] \
            if req.shares else [f"Share {i+1}" for i in range(parts)]
        # Pad/truncate so we always have `parts` entries
        share_phones += [""] * (parts - len(share_phones))
        share_labels += [f"Share {i+1}" for i in range(len(share_labels), parts)]
        share_phones = share_phones[:parts]
        share_labels = share_labels[:parts]

        share_sub   = round(grand_subtotal / parts, 2)
        share_tax   = round(grand_tax     / parts, 2)
        share_grand = round(grand_total   / parts, 2)

        # Build a combined item list (same for every share, but with each one's
        # share of the line cost so the math adds up).
        all_items: list[dict] = []
        for o in orders:
            all_items.extend(_parse_items_text(o.get("items", "")))

        for idx in range(parts):
            lines = []
            for it in all_items:
                line_amt = (it.get("line_total") or 0) / parts \
                            if it.get("line_total") is not None \
                            else (grand_subtotal / max(1, len(all_items))) / parts
                lines.append(
                    f'<tr><td colspan="2" style="color:#333">{it["qty"]}x {it["name"]}</td>'
                    f'<td style="text-align:right">&#8377;{line_amt:.2f}</td></tr>'
                )
            shares_payload.append({
                "label":   share_labels[idx],
                "phone":   share_phones[idx],
                "lines":   "".join(lines),
                "sub":     share_sub,
                "tax":     share_tax,
                "grand":   share_grand,
            })

    else:  # by_item
        if not req.shares:
            return JSONResponse({"success": False,
                "error": "by_item mode requires at least one share with items[]"},
                                 status_code=400)

        # Build line index: parse each order's items text into addressable lines
        # keyed by (order_id, idx). For "by_item" we expect callers to allocate
        # by line index into this flat list. We then validate that the total
        # allocated qty per (oid, idx) does not exceed the line's qty.
        flat_lines = []  # [{order_id, idx, qty, name, line_total}]
        for o in orders:
            parsed = _parse_items_text(o.get("items", ""))
            for i, it in enumerate(parsed):
                flat_lines.append({
                    "order_id":   o["order_id"],
                    "idx":        i,
                    "qty":        int(it["qty"]),
                    "name":       it["name"],
                    "line_total": float(it.get("line_total") or 0),
                })
        line_lookup = {f'{l["order_id"]}#{l["idx"]}': l for l in flat_lines}

        # Track remaining qty per line so we can detect over-allocation
        remaining = {k: v["qty"] for k, v in line_lookup.items()}

        for s in req.shares:
            sub = 0.0
            allocated = []
            for it in s.items:
                key = it.get("key")
                qty = int(it.get("qty", 0))
                if not key or qty <= 0:
                    continue
                if key not in line_lookup:
                    return JSONResponse({"success": False,
                        "error": f"Unknown line key {key}"}, status_code=400)
                if remaining[key] < qty:
                    return JSONResponse({"success": False,
                        "error": f"Over-allocated line {key} "
                                 f"(requested {qty}, only {remaining[key]} left)"},
                                         status_code=400)
                remaining[key] -= qty
                line = line_lookup[key]
                # Pro-rate per-unit cost from the line total
                unit = line["line_total"] / max(1, line["qty"]) if line["line_total"] else 0
                line_amt = round(unit * qty, 2)
                sub += line_amt
                allocated.append({"qty": qty, "name": line["name"], "amt": line_amt})

            # Each share's tax is proportional to its subtotal share
            share_ratio = (sub / grand_subtotal) if grand_subtotal > 0 else 0
            share_tax_amt = round(grand_tax * share_ratio, 2)
            share_grand   = round(sub + share_tax_amt, 2)

            lines = "".join(
                f'<tr><td colspan="2" style="color:#333">{a["qty"]}x {a["name"]}</td>'
                f'<td style="text-align:right">&#8377;{a["amt"]:.2f}</td></tr>'
                for a in allocated
            )
            shares_payload.append({
                "label": s.label,
                "phone": s.phone.strip(),
                "lines": lines,
                "sub":   round(sub, 2),
                "tax":   share_tax_amt,
                "grand": share_grand,
            })

        # Warn (don't fail) if some lines are unallocated — operators may handle
        # them outside the split (e.g. comp). But return the leftovers so the
        # UI can show them.
        leftover = [{"key": k, "qty": q} for k, q in remaining.items() if q > 0]
        if leftover:
            shares_payload[-1]["leftover_warning"] = leftover

    # Generate PDFs and dispatch
    sent = []
    async with httpx.AsyncClient(timeout=30) as client:
        for idx, s in enumerate(shares_payload, 1):
            html = _build_split_html(
                cfg.restaurant_name, seller_gstin, table,
                s.get("label", ""),
                s["grand"],
                {"sub": s["sub"], "tax": s["tax"], "grand": s["grand"]},
                s["lines"], idx, len(shares_payload)
            )
            try:
                resp = await client.post(
                    f"{cfg.gotenberg_url}/forms/chromium/convert/html",
                    files={"index.html": ("index.html", html.encode(), "text/html")}
                )
                pdf_b64 = base64.b64encode(resp.content).decode()
            except Exception as e:
                return JSONResponse({"success": False, "message": f"PDF error on share {idx}: {e}"})

            caption = (f"*Split Bill ({idx}/{len(shares_payload)}) — {cfg.restaurant_name}*\n"
                        f"Table: {table} | Your share: ₹{s['grand']:.2f}")
            target_phone = s.get("phone", "")
            if target_phone:
                await wa.send_document_base64(cfg, target_phone, pdf_b64,
                                                f"split-bill-{idx}.pdf", caption)
                sent.append({"share": idx, "phone": target_phone, "amount": s["grand"]})
            if req.notify_owner:
                await wa.send_document_base64(cfg, cfg.staff_owner, pdf_b64,
                                                f"split-bill-{idx}.pdf", caption)

    # Mark these orders as billed so they don't get re-billed
    with cfg.db_session() as db:
        db.execute(text("UPDATE orders SET billed=TRUE WHERE order_id=ANY(:ids)"),
                    {"ids": order_ids})
        db.commit()

    audit("bill.split", actor_role="system", slug=slug, target=table,
          payload={"mode": req.mode, "shares": len(shares_payload),
                    "grand_total": grand_total, "table": table,
                    "sent": sent})

    return JSONResponse({
        "success": True,
        "mode":    req.mode,
        "shares":  len(shares_payload),
        "grand_total": grand_total,
        "share_totals": [s["grand"] for s in shares_payload],
        "labels":  [s.get("label", "") for s in shares_payload],
        "sent":    sent,
    })


# ══════════════════════════════════════════════════════
# K3 — UNBILLED ORDERS PREVIEW (helper for split-bill UI)
# ══════════════════════════════════════════════════════
@router.get("/webhook/{slug}/unbilled-orders")
async def unbilled_orders(slug: str = Path(...), table: str = "",
                          x_internal_auth: Optional[str] = Header(None)):
    """Return today's paid+unbilled orders for a table, parsed into addressable
    lines. The split-bill UI calls this to populate item allocations."""
    _require_internal_auth(x_internal_auth)
    cfg = load_tenant(slug)
    table = (table or "").strip().upper()
    if not table:
        return JSONResponse({"success": False, "error": "table is required"},
                             status_code=400)
    today = fmt_date_short(datetime.now(IST))
    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT order_id, items, subtotal, tax, total, payment_method "
            "FROM orders WHERE table_name=:t AND status='Paid' AND billed=FALSE AND date_only=:d "
            "ORDER BY date ASC"
        ), {"t": table, "d": today}).fetchall()
    out_orders = []
    grand_sub = grand_tax = grand_total = 0.0
    for o in rows:
        parsed = _parse_items_text(o.items)
        out_orders.append({
            "order_id":      o.order_id,
            "subtotal":      float(o.subtotal or 0),
            "tax":           float(o.tax or 0),
            "total":         float(o.total or 0),
            "payment_method": o.payment_method,
            "lines":         [{
                "key":  f'{o.order_id}#{i}',
                "qty":  it["qty"],
                "name": it["name"],
                "line_total": it.get("line_total"),
            } for i, it in enumerate(parsed)],
        })
        grand_sub   += float(o.subtotal or 0)
        grand_tax   += float(o.tax or 0)
        grand_total += float(o.total or 0)
    return JSONResponse({
        "success":       True,
        "table":         table,
        "orders":        out_orders,
        "grand_subtotal": grand_sub,
        "grand_tax":      grand_tax,
        "grand_total":    grand_total,
    }, headers={"Access-Control-Allow-Origin": "*"})
