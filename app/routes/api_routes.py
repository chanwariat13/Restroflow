"""
routes/api_routes.py
All remaining API routes — all multi-tenant via {slug} in URL.
  GET  /webhook/{slug}/menu
  POST /webhook/{slug}/receive-order
  POST /webhook/{slug}/get-cart
  POST /webhook/{slug}/generate-bill
  POST /webhook/{slug}/deduct-inventory
  POST /webhook/{slug}/razorpay-webhook
"""
import base64
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request, BackgroundTasks, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import text

from app.utils.tenant import load_tenant, TenantConfig
from app.utils import redis_client as rc
from app.services import whatsapp as wa

router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")


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
                "SELECT name, category, price, available, type, image, description, bestseller "
                "FROM menu ORDER BY category, name"
            )).fetchall()
        menu = []
        for i, row in enumerate(rows):
            avail = str(row.available or "yes").lower()
            menu.append({
                "id": str(i + 1), "name": row.name or "",
                "category": row.category or "Other",
                "price": float(row.price or 0),
                "available": avail not in ("no", "false", "0"),
                "type": (row.type or "veg").lower(),
                "image": row.image or "", "description": row.description or "",
                "bestseller": str(row.bestseller or "no").lower() == "yes",
            })
    except Exception:
        menu = []

    return JSONResponse(menu, headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=60"
    })


# ══════════════════════════════════════════════════════
# D — ORDER RECEIVER
# ══════════════════════════════════════════════════════
class OrderItem(BaseModel):
    id: str; name: str; price: float; qty: int; category: str = "Other"

class OrderRequest(BaseModel):
    phone: str; table: str; name: str = ""; token: str = ""
    items: list[OrderItem]; notes: str = ""; fullCart: bool = False


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

    if req.fullCart:
        session["orders"] = [{"id": i.id, "name": i.name, "price": i.price, "quantity": i.qty} for i in req.items]
    else:
        orders = {str(o["id"]): o for o in session.get("orders", [])}
        for i in req.items:
            if str(i.id) in orders:
                orders[str(i.id)]["quantity"] += i.qty
            else:
                orders[str(i.id)] = {"id": i.id, "name": i.name, "price": i.price, "quantity": i.qty}
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
async def generate_bill(req: BillRequest, slug: str = Path(...)):
    cfg   = load_tenant(slug)
    table = req.table.strip().upper()
    today = datetime.now(IST).strftime("%-d/%-m/%Y")

    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT order_id, date, customer_name, phone, table_name, "
            "items, subtotal, tax, total, payment_method "
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

    item_rows = ""
    for rnd, o in enumerate(orders, 1):
        items_text = str(o.get("items","")).replace("\n","<br>")
        item_rows += (
            f'<tr class="rh"><td colspan="2">Round {rnd}</td>'
            f'<td style="text-align:right;color:#888">{o.get("payment_method","")}</td></tr>'
            f'<tr><td colspan="2" style="padding-left:16px;color:#444">{items_text}</td>'
            f'<td style="text-align:right">&#8377;{float(o.get("total",0)):.2f}</td></tr>'
        )

    now_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Arial,sans-serif;padding:32px 28px;color:#222;background:#fff}}
.hdr{{text-align:center;margin-bottom:20px;border-bottom:2px solid #222;padding-bottom:14px}}
.hdr h1{{font-size:22px;font-weight:bold}}
.hdr p{{font-size:12px;color:#666;margin-top:4px}}
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
<div class="hdr"><h1>{cfg.restaurant_name}</h1><p>Thank you for dining with us!</p></div>
<div class="info">
  <div><b>Table:</b> {table} &nbsp; <b>Customer:</b> {customer}</div>
  <div style="font-size:11px;color:#aaa"><b>Date:</b> {now_ist}</div>
</div>
<table>
  <thead><tr><th colspan="2">Items</th><th style="text-align:right">Amount</th></tr></thead>
  <tbody>{item_rows}</tbody>
</table>
<div class="totals"><table>
  <tr><td>Subtotal</td><td>&#8377;{subtotal:.2f}</td></tr>
  <tr><td>Tax (GST {int(cfg.gst_rate*100)}%)</td><td>&#8377;{tax_sum:.2f}</td></tr>
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
async def deduct_inventory(req: DeductRequest, slug: str = Path(...)):
    cfg = load_tenant(slug)
    if not req.items:
        return JSONResponse({"success": False, "message": "No items"})

    with cfg.db_session() as db:
        for item in req.items:
            safe = item.name.replace("'", "''")
            db.execute(text(f"""
                UPDATE inventory inv
                SET current_stock = inv.current_stock - (mi.quantity_used * {item.quantity}),
                    updated_at = NOW()
                FROM menu_ingredients mi
                WHERE mi.menu_item = '{safe}'
                  AND mi.ingredient = inv.item_name
                  AND inv.current_stock >= (mi.quantity_used * {item.quantity})
            """))
        db.commit()

        low = db.execute(text(
            "SELECT item_name, current_stock, min_threshold, unit "
            "FROM inventory WHERE current_stock <= min_threshold ORDER BY (current_stock-min_threshold) ASC"
        )).fetchall()

    if low:
        await wa.notify_low_stock(cfg, [dict(r._mapping) for r in low])

    return JSONResponse({"success": True})


# ══════════════════════════════════════════════════════
# I — RAZORPAY WEBHOOK
# ══════════════════════════════════════════════════════
@router.post("/webhook/{slug}/razorpay-webhook")
async def razorpay_webhook(request: Request, background_tasks: BackgroundTasks, slug: str = Path(...)):
    body = await request.json()
    cfg  = load_tenant(slug)
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
               "donly": now_ist.strftime("%-d/%-m/%Y"), "name": name, "phone": phone,
               "table": table, "items": items_str, "sub": sub, "tax": tax,
               "total": total, "method": "Online"})
        db.commit()

    await wa.send_text(cfg, phone,
        f"✅ *Payment Confirmed!*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {name} | 🪑 {table}\n💰 ₹{total:.0f} via {method}\n\n"
        f"🍳 Order being prepared!\n\n*8* - 👋 Checkout | *7* - 🔔 Waiter | *5* - 💵 Bill")
    await wa.notify_payment(cfg, phone, name, table, order_id, total, method)
    await wa.send_to_kitchen(cfg,
        f"🔥 *NEW ORDER (PAID)*\n🪑 {table} | 👤 {name}\n\n"
        + "\n".join(f"  • {o['quantity']}x {o['name']}" for o in orders)
        + f"\n\n✅ ₹{total:.0f} paid\n*{table}* confirm | *DONE {table}* when ready")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"http://localhost:8000/webhook/{cfg.slug}/deduct-inventory",
                              json={"items": [{"name": o["name"], "quantity": o["quantity"]} for o in orders]})
    except Exception:
        pass
