"""
routes/whatsapp_bot.py
POST /webhook/{slug}/whatsapp  ← Evolution API calls this per client

slug in the URL identifies which client/restaurant this message belongs to.
All logic is identical to single-tenant version but uses TenantConfig + slug-prefixed Redis.
"""
import re
import httpx
import secrets as _secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request, BackgroundTasks, Path
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.utils.tenant import load_tenant, TenantConfig
from app.utils import redis_client as rc
from app.services import whatsapp as wa

router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")


@router.post("/webhook/{slug}/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    slug: str = Path(...)
):
    body = await request.json()
    cfg  = load_tenant(slug)
    background_tasks.add_task(_handle_message, cfg, body)
    return JSONResponse({"status": "ok"})


async def _handle_message(cfg: TenantConfig, body: dict):
    try:
        raw  = body.get("body") or body
        event = raw.get("event", "")
        if event and event != "messages.upsert":
            return

        data = raw.get("data") or {}
        if data.get("key", {}).get("fromMe"):
            return

        msg = data.get("message", {})
        text_content = (
            msg.get("conversation") or
            (msg.get("extendedTextMessage") or {}).get("text") or
            (msg.get("imageMessage") or {}).get("caption") or
            (msg.get("buttonsResponseMessage") or {}).get("selectedDisplayText") or
            (msg.get("listResponseMessage") or {}).get("title") or ""
        ).strip()

        if not text_content:
            return

        remote_jid = data.get("key", {}).get("remoteJid", "")
        if "@g.us" in remote_jid or remote_jid == "status@broadcast":
            return

        phone = remote_jid.split("@")[0]
        if not phone:
            return

        phone_last10 = phone[-10:]
        is_staff   = any(s[-10:] == phone_last10 for s in cfg.all_staff)
        is_kitchen = any(k[-10:] == phone_last10 for k in cfg.kitchen_numbers)
        session    = rc.get_session(cfg.slug, phone)

        if is_kitchen:
            await _handle_kitchen(cfg, phone, text_content)
        elif is_staff:
            await _handle_staff(cfg, phone, text_content)
        elif session:
            await _handle_customer(cfg, phone, text_content, session)
        else:
            if rc.is_blocked(cfg.slug, phone):
                await wa.send_text(cfg, phone, "⛔ Access denied. Please speak to our staff.")

    except Exception as e:
        await wa.notify_error(cfg, "whatsapp_bot", "_handle_message", str(e))


# ═══════════════════════════════════════════════════════
# STAFF
# ═══════════════════════════════════════════════════════
async def _handle_staff(cfg: TenantConfig, staff_phone: str, text: str):
    clean = re.sub(r"[_*~`]", "", text).strip()
    upper = clean.upper()

    m = re.match(r"^APPROVE\s+(\d+)$", upper)
    if m:
        await _approve(cfg, staff_phone, m.group(1)); return

    m = re.match(r"^REJECT\s+(\d+)$", upper)
    if m:
        await _reject(cfg, staff_phone, m.group(1)); return

    m = re.match(r"^(?:CASH\s+RECEIVED|CONFIRM\s+PAYMENT)\s+(\d+)$", upper)
    if m:
        await _cash_received(cfg, staff_phone, m.group(1)); return

    m = re.match(r"^BLOCK\s+(\d+)$", upper)
    if m:
        rc.block_phone(cfg.slug, m.group(1))
        await wa.send_text(cfg, staff_phone, f"🚫 {m.group(1)} blocked."); return

    m = re.match(r"^UNBLOCK\s+(\d+)$", upper)
    if m:
        rc.unblock_phone(cfg.slug, m.group(1))
        await wa.send_text(cfg, staff_phone, f"✅ {m.group(1)} unblocked."); return

    m = re.match(r"^FREE\s+T(\d+)$", upper)
    if m:
        table = f"T{m.group(1)}"
        phone = rc.get_table_phone(cfg.slug, table)
        if phone:
            rc.clear_customer(cfg.slug, phone, table)
            await wa.send_text(cfg, staff_phone, f"✅ Table {table} freed!")
        else:
            await wa.send_text(cfg, staff_phone, f"ℹ️ Table {table} already free.")
        return

    m = re.match(r"^STATUS\s+(T?\d+)$", upper)
    if m:
        await _status(cfg, staff_phone, m.group(1)); return

    if upper in ("TABLES", "TABLE STATUS"):
        await _tables(cfg, staff_phone); return

    m = re.match(r"^BILL\s+T(\d+)$", upper)
    if m:
        table = f"T{m.group(1)}"
        phone = rc.get_table_phone(cfg.slug, table) or ""
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(
                f"http://localhost:8000/webhook/{cfg.slug}/generate-bill",
                json={"table": table, "phone": phone}
            )
        await wa.send_text(cfg, staff_phone, f"📄 Bill sent for {table}")
        return

    if upper == "RESTOCK":
        await _restock_template(cfg, staff_phone); return

    lines = clean.split("\n")
    if any(l.strip().upper() == "RESTOCK UPDATE" for l in lines):
        await _restock_update(cfg, staff_phone, lines); return

    if upper == "STOCK":
        await _stock_list(cfg, staff_phone); return

    if upper == "REPORT":
        await _report(cfg, staff_phone); return

    m = re.match(r"^AMOUNT\s+T(\d+)$", upper)
    if m:
        await _amount(cfg, staff_phone, f"T{m.group(1)}"); return

    if upper in ("ADMIN", "HELP"):
        await _help(cfg, staff_phone); return

    await wa.send_text(cfg, staff_phone, "❓ Unknown command. Type *HELP* to see all commands.")


async def _approve(cfg: TenantConfig, staff_phone: str, cust_phone: str):
    session = rc.get_session(cfg.slug, cust_phone)
    if not session:
        await wa.send_text(cfg, staff_phone, f"⚠️ No pending request for {cust_phone}.")
        return
    if session.get("status") != "AWAITING_APPROVAL":
        await wa.send_text(cfg, staff_phone, f"⚠️ Already {session.get('status')}.")
        return

    token    = _secrets.token_hex(8)
    menu_url = f"{cfg.menu_url}?t={session['table']}&p={cust_phone}&n={session['name'].split()[0]}&k={token}"

    session.update({"status": "ORDERING", "approvedAt": datetime.utcnow().isoformat(),
                    "menuToken": token, "menuURL": menu_url})
    rc.save_session(cfg.slug, cust_phone, session, cfg.session_ttl)
    rc.set_table(cfg.slug, session["table"], cust_phone, pending=False, ttl=cfg.session_ttl)
    rc.delete_pending(cfg.slug, cust_phone)

    # Check returning customer
    visits = 0; spent = 0.0
    try:
        with cfg.db_session() as db:
            row = db.execute(text(
                "SELECT total_visits, total_spent FROM customers WHERE phone = :p"
            ), {"p": cust_phone}).fetchone()
            if row:
                visits, spent = row.total_visits, float(row.total_spent or 0)
    except Exception:
        pass

    note = f"\n\n🌟 *Returning customer!* Visit #{visits+1} | Spent ₹{spent:.0f}" if visits > 0 else ""

    await wa.send_text(cfg, cust_phone,
        f"✅ *Approved! Welcome to {cfg.restaurant_name}!*\n\n"
        f"👤 {session['name']}\n🪑 Table: {session['table']}{note}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n🍽️ *Order here:*\n\n👉 {menu_url}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n*1* - 📋 Menu | *7* - 🔔 Waiter | *0* - Menu")
    await wa.send_text(cfg, staff_phone, f"✅ Approved: {session.get('name')} at {session['table']}")


async def _reject(cfg: TenantConfig, staff_phone: str, cust_phone: str):
    session = rc.get_session(cfg.slug, cust_phone)
    name  = session.get("name", cust_phone) if session else cust_phone
    table = session.get("table", "") if session else ""
    rc.clear_customer(cfg.slug, cust_phone, table)
    await wa.send_text(cfg, cust_phone, "❌ Request not approved. Please speak to staff.")
    await wa.send_text(cfg, staff_phone, f"❌ Rejected: {name} ({cust_phone})")


async def _cash_received(cfg: TenantConfig, staff_phone: str, cust_phone: str):
    session = rc.get_session(cfg.slug, cust_phone)
    if not session:
        await wa.send_text(cfg, staff_phone, f"⚠️ No session for {cust_phone}"); return

    status = session.get("status", "")
    if status not in ("PENDING_CASH_PAYMENT", "PENDING_PAYMENT"):
        await wa.send_text(cfg, staff_phone, f"⚠️ Status is {status}. Not awaiting payment."); return

    orders = list(session.get("orders", []))
    sub    = sum(float(o["price"]) * int(o["quantity"]) for o in orders)
    tax    = round(sub * cfg.gst_rate)
    total  = _apply_discount(cfg, sub + tax)
    name   = session.get("name", "Customer")
    table  = session.get("table", "")
    now    = datetime.utcnow().isoformat()

    session.setdefault("paidOrders", []).append(
        {"items": orders, "paidAt": now, "paymentMethod": "Cash", "total": total}
    )
    session.update({"status": "PAID", "paymentMethod": "Cash", "paidAt": now, "orders": []})
    rc.save_session(cfg.slug, cust_phone, session, cfg.session_ttl)

    await _save_order(cfg, session, cust_phone, orders, "Cash", total, sub, tax)

    items_text = "\n".join(
        f"  {i+1}. {o['quantity']}x {o['name']} = ₹{float(o['price'])*o['quantity']:.0f}"
        for i, o in enumerate(orders)
    )
    await wa.send_text(cfg, cust_phone,
        f"✅ *Payment Confirmed!*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {name} | 🪑 {table}\n\n{items_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n💰 *Total: ₹{total:.0f}* (Cash)\n\nThank you! 🙏\n\n"
        f"*8* - 👋 Checkout | *7* - 🔔 Waiter")
    await wa.send_text(cfg, staff_phone,
        f"✅ Cash confirmed: {name} | {table} | ₹{total:.0f}")
    await _call_deduct_inventory(cfg, orders, cust_phone, table)


async def _status(cfg: TenantConfig, staff_phone: str, target: str):
    if re.match(r"^T\d+$", target.upper()):
        table = target.upper()
        phone = rc.get_table_phone(cfg.slug, table)
        if not phone:
            await wa.send_text(cfg, staff_phone, f"🟢 Table {table} is FREE"); return
        s = rc.get_session(cfg.slug, phone)
        if not s:
            await wa.send_text(cfg, staff_phone, f"🟡 {table}: stale key (session expired)"); return
        await wa.send_text(cfg, staff_phone,
            f"📊 *{table}*\n👤 {s.get('name')} | 📱 {phone}\n"
            f"📊 {s.get('status')} | 💰 ₹{s.get('total',0):.0f}")
    else:
        s = rc.get_session(cfg.slug, target)
        if not s:
            await wa.send_text(cfg, staff_phone, f"No session for {target}"); return
        await wa.send_text(cfg, staff_phone,
            f"📊 {s.get('name')} | 🪑 {s.get('table')} | {s.get('status')} | ₹{s.get('total',0):.0f}")


async def _tables(cfg: TenantConfig, staff_phone: str):
    occupied = rc.get_all_occupied_tables(cfg.slug, cfg.get_table_names())
    if not occupied:
        await wa.send_text(cfg, staff_phone, "🟢 All tables are FREE"); return
    msg = f"📊 *TABLE STATUS* ({len(occupied)} occupied)\n━━━━━━━━━━━━━━━━━━\n\n"
    for t in occupied:
        s = t["session"] or {}
        msg += f"🪑 *{t['table']}* — {s.get('name','?')}\n   {s.get('status','?')} | ₹{s.get('total',0):.0f}\n\n"
    await wa.send_text(cfg, staff_phone, msg)


async def _restock_template(cfg: TenantConfig, staff_phone: str):
    with cfg.db_session() as db:
        rows = db.execute(text("SELECT item_name, current_stock, unit FROM inventory ORDER BY item_name")).fetchall()
    msg = "📦 *RESTOCK TEMPLATE*\nEdit & send back:\n\nRESTOCK UPDATE\n"
    for r in rows:
        msg += f"{r.item_name}: {r.current_stock}{r.unit}\n"
    await wa.send_text(cfg, staff_phone, msg)


async def _restock_update(cfg: TenantConfig, staff_phone: str, lines: list[str]):
    updates = {}
    for line in lines:
        if line.strip().upper() == "RESTOCK UPDATE": continue
        parts = line.split(":")
        if len(parts) == 2:
            item = parts[0].strip()
            qty  = re.sub(r"[^\d.]", "", parts[1].strip())
            if item and qty:
                try: updates[item] = float(qty)
                except: pass
    if not updates:
        await wa.send_text(cfg, staff_phone, "⚠️ Format: ItemName: quantity"); return
    with cfg.db_session() as db:
        for item, qty in updates.items():
            db.execute(text("UPDATE inventory SET current_stock=:q, updated_at=NOW() WHERE item_name=:n"),
                       {"q": qty, "n": item})
        db.commit()
    await wa.send_text(cfg, staff_phone, "✅ *Stock Updated!*\n" + "\n".join(f"✅ {k}: {v}" for k,v in updates.items()))


async def _stock_list(cfg: TenantConfig, staff_phone: str):
    with cfg.db_session() as db:
        rows = db.execute(text("SELECT item_name, current_stock, min_threshold, unit FROM inventory ORDER BY item_name")).fetchall()
    msg = "📦 *CURRENT STOCK*\n━━━━━━━━━━━━━━━━━━\n\n"
    for r in rows:
        e = "🔴" if r.current_stock <= r.min_threshold else "🟢"
        msg += f"{e} *{r.item_name}*: {r.current_stock}{r.unit} (min: {r.min_threshold}{r.unit})\n"
    await wa.send_text(cfg, staff_phone, msg)


async def _report(cfg: TenantConfig, staff_phone: str):
    today = datetime.now(IST).strftime("%-d/%-m/%Y")
    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT payment_method, SUM(total) as amount, COUNT(*) as cnt "
            "FROM orders WHERE date_only=:d AND status='Paid' GROUP BY payment_method"
        ), {"d": today}).fetchall()
    if not rows:
        await wa.send_text(cfg, staff_phone, f"📊 No orders today ({today})."); return
    cash = online = total = orders = 0
    for r in rows:
        pm = (r.payment_method or "").lower()
        if pm == "cash": cash += float(r.amount or 0)
        else: online += float(r.amount or 0)
        total += float(r.amount or 0); orders += int(r.cnt or 0)
    await wa.send_text(cfg, staff_phone,
        f"📊 *TODAY* ({today})\n━━━━━━━━━━━━━━━━━━\n"
        f"📋 Orders: {orders}\n💵 Cash: ₹{cash:.0f}\n💳 Online: ₹{online:.0f}\n💰 *Total: ₹{total:.0f}*")


async def _amount(cfg: TenantConfig, staff_phone: str, table: str):
    phone = rc.get_table_phone(cfg.slug, table)
    if not phone:
        await wa.send_text(cfg, staff_phone, f"ℹ️ No session at {table}"); return
    s = rc.get_session(cfg.slug, phone)
    if not s:
        await wa.send_text(cfg, staff_phone, f"ℹ️ Session expired for {table}"); return
    orders = s.get("orders", []); paid = s.get("paidOrders", [])
    sub, tax, total = _calc_totals(cfg, orders)
    paid_total = sum(float(b.get("total",0)) for b in paid)
    await wa.send_text(cfg, staff_phone,
        f"💰 *{table} — {s.get('name')}*\n"
        f"Cart: ₹{total:.0f} | Paid: ₹{paid_total:.0f}\n📊 {s.get('status')}")


async def _help(cfg: TenantConfig, staff_phone: str):
    await wa.send_text(cfg, staff_phone,
        "🤖 *ADMIN COMMANDS*\n━━━━━━━━━━━━━━━━━━\n\n"
        "✅ *APPROVE 91xx*\n❌ *REJECT 91xx*\n💵 *CASH RECEIVED 91xx*\n"
        "🚫 *BLOCK 91xx*\n✅ *UNBLOCK 91xx*\n🗑️ *FREE T1*\n"
        "📊 *STATUS T1*\n📊 *TABLES*\n📄 *BILL T1*\n"
        "📦 *RESTOCK*\n📦 *STOCK*\n📊 *REPORT*\n💰 *AMOUNT T1*")


# ═══════════════════════════════════════════════════════
# KITCHEN
# ═══════════════════════════════════════════════════════
async def _handle_kitchen(cfg: TenantConfig, kitchen_phone: str, text: str):
    upper = re.sub(r"[_*~`]", "", text).strip().upper()

    m = re.match(r"^DONE\s+T(\d+)$", upper)
    if m:
        table = f"T{m.group(1)}"
        phone = rc.get_table_phone(cfg.slug, table)
        if not phone:
            await wa.send_text(cfg, kitchen_phone, f"⚠️ No session at {table}"); return
        session = rc.get_session(cfg.slug, phone)
        cust_name = (session or {}).get("name", "Customer")
        if session:
            orders = list(session.get("orders", []))
            session.setdefault("servedOrders", []).extend(orders)
            session.update({"kitchenOrders": [], "orders": [], "status": "ORDERING"})
            rc.save_session(cfg.slug, phone, session, cfg.session_ttl)
        await wa.send_text(cfg, phone,
            f"🍽️ *Your Food is Ready!* 🎉\n\nHey {cust_name}! ✅ Your meal is being served!\n\n"
            f"Bon Appétit! 😋\n\n*5* - 💵 Bill | *7* - 🔔 Waiter")
        await wa.send_text(cfg, kitchen_phone, f"✅ DONE confirmed for {table}.")
        await wa.send_all_staff(cfg, f"🍽️ *FOOD READY — {table}*\n👤 {cust_name}")
        return

    m = re.match(r"^T(\d+)$", upper)
    if m:
        table = f"T{m.group(1)}"
        phone = rc.get_table_phone(cfg.slug, table)
        if not phone:
            await wa.send_text(cfg, kitchen_phone, f"⚠️ No session at {table}"); return
        session = rc.get_session(cfg.slug, phone)
        if session:
            session.setdefault("kitchenOrders", []).extend(session.get("orders", []))
            rc.save_session(cfg.slug, phone, session, cfg.session_ttl)
        await wa.send_text(cfg, kitchen_phone, f"✅ Order confirmed for {table}. Preparing!")
        return

    await wa.send_text(cfg, kitchen_phone, "❓\n\n*T1* — confirm order\n*DONE T1* — food ready")


# ═══════════════════════════════════════════════════════
# CUSTOMER STATE MACHINE
# ═══════════════════════════════════════════════════════
async def _handle_customer(cfg: TenantConfig, phone: str, text: str, session: dict):
    clean  = re.sub(r"[_*~`]", "", text).strip()
    upper  = clean.upper()
    status = session.get("status", "UNKNOWN").upper()

    if status == "AWAITING_APPROVAL":
        await wa.send_text(cfg, phone, "⏳ Please wait for staff approval... 🙏"); return
    if status in ("ORDERING", "ORDER_PLACED"):
        await _cust_ordering(cfg, phone, clean, upper, session); return
    if status == "PENDING_PAYMENT":
        await _cust_pending_online(cfg, phone, upper, session); return
    if status == "PENDING_CASH_PAYMENT":
        await _cust_pending_cash(cfg, phone, upper, session); return
    if status == "PAID":
        await _cust_paid(cfg, phone, upper, session); return
    if status == "AWAITING_FEEDBACK":
        await _cust_feedback(cfg, phone, clean, upper, session); return
    if status == "AWAITING_FEEDBACK_TEXT":
        await _cust_feedback_text(cfg, phone, clean, upper, session); return
    await _main_menu(cfg, phone, session)


async def _cust_ordering(cfg, phone, clean, upper, session):
    orders   = session.get("orders", [])
    table    = session.get("table", "")
    name     = session.get("name", "")
    menu_url = session.get("menuURL", cfg.menu_url)

    if upper in ("1", "MENU", "VIEW MENU"):
        await wa.send_text(cfg, phone, f"📋 *Menu:*\n\n👉 {menu_url}"); return

    if upper in ("2", "VIEW ORDER", "ORDER"):
        if not orders:
            await wa.send_text(cfg, phone, "🛒 Cart empty. Tap *1* for menu."); return
        items = "\n".join(f"  {i+1}. {o['quantity']}x {o['name']} = ₹{float(o['price'])*o['quantity']:.0f}" for i,o in enumerate(orders))
        sub, tax, total = _calc_totals(cfg, orders)
        await wa.send_text(cfg, phone,
            f"🧾 *Your Order*\n━━━━━━━━━━━━━━━━━━\n\n{items}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n💰 Subtotal: ₹{sub:.0f}\n📋 GST: ₹{tax:.0f}\n💵 *Total: ₹{total:.0f}*"); return

    if upper in ("3", "KITCHEN", "SEND TO KITCHEN"):
        if not orders:
            await wa.send_text(cfg, phone, "🛒 Cart empty. Tap *1* first."); return
        session["status"] = "ORDER_PLACED"
        rc.save_session(cfg.slug, phone, session, cfg.session_ttl)
        kitchen_items = "\n".join(f"  • {o['quantity']}x {o['name']}" for o in orders)
        sub, tax, total = _calc_totals(cfg, orders)
        await wa.send_text(cfg, phone,
            f"✅ *Order sent to kitchen!*\n\n{kitchen_items}\n\n💵 ₹{total:.0f}\n🍳 Preparing...\n\n*5* - 💵 Bill | *7* - 🔔 Waiter")
        await wa.notify_new_order(cfg, table, name, phone, kitchen_items, total); return

    if upper in ("4", "CANCEL"):
        session.update({"orders": [], "total": 0, "status": "ORDERING"})
        rc.save_session(cfg.slug, phone, session, cfg.session_ttl)
        await wa.send_text(cfg, phone, "❌ Cart cleared.\n\n*1* - Menu"); return

    if upper in ("5", "BILL", "PAY"):
        if not orders:
            await wa.send_text(cfg, phone, "🛒 Cart empty."); return
        await _initiate_payment(cfg, phone, session); return

    if upper in ("7", "WAITER"):
        await wa.send_all_staff(cfg, f"🔔 *WAITER*\n🪑 {table} | 👤 {name}")
        await wa.send_text(cfg, phone, "🔔 Waiter notified!"); return

    if upper in ("8", "CHECKOUT"):
        await _checkout(cfg, phone, session); return

    await _main_menu(cfg, phone, session)


async def _cust_pending_online(cfg, phone, upper, session):
    if upper in ("2", "CASH"):
        orders = session.get("orders", [])
        sub, tax, total = _calc_totals(cfg, orders)
        total = _apply_discount(cfg, total)
        session.update({"status": "PENDING_CASH_PAYMENT", "paymentMethod": "Cash"})
        rc.save_session(cfg.slug, phone, session, cfg.session_ttl)
        items = "\n".join(f"  {i+1}. {o['quantity']}x {o['name']} = ₹{float(o['price'])*o['quantity']:.0f}" for i,o in enumerate(orders))
        await wa.send_text(cfg, phone, f"💵 *CASH PAYMENT*\n\n{items}\n\n💵 *Total: ₹{total:.0f}*\n\n👋 Pay staff.\n⏳ Waiting...\nType *4* to cancel.")
        await wa.send_all_staff(cfg, f"💵 *CASH SWITCH*\n👤 {session.get('name')} | 🪑 {session.get('table')} | ₹{total:.0f}\n✅ *CASH RECEIVED {phone}*")
        return
    if upper in ("4", "CANCEL"):
        session.update({"status": "ORDER_PLACED", "orders": []})
        rc.save_session(cfg.slug, phone, session, cfg.session_ttl)
        await wa.send_text(cfg, phone, "❌ Cancelled.\n\n*5* - Pay | *1* - Menu"); return
    await wa.send_text(cfg, phone, "⏳ Waiting for payment.\n\n*2* - 💵 Cash | *4* - ❌ Cancel")


async def _cust_pending_cash(cfg, phone, upper, session):
    if upper in ("4", "CANCEL"):
        session.update({"status": "ORDER_PLACED", "orders": []})
        rc.save_session(cfg.slug, phone, session, cfg.session_ttl)
        await wa.send_text(cfg, phone, "❌ Cancelled."); return
    if upper in ("7", "WAITER"):
        await wa.send_all_staff(cfg, f"🔔 *WAITER*\n🪑 {session.get('table')} | {session.get('name')}")
        await wa.send_text(cfg, phone, "🔔 Waiter notified!"); return
    orders = session.get("orders", [])
    sub, tax, total = _calc_totals(cfg, orders)
    total = _apply_discount(cfg, total)
    items = "\n".join(f"  {i+1}. {o['quantity']}x {o['name']} = ₹{float(o['price'])*o['quantity']:.0f}" for i,o in enumerate(orders))
    await wa.send_text(cfg, phone, f"🧾 *BILL*\n\n{items}\n\n💵 *TOTAL: ₹{total:.0f}*\n\n⏳ Waiting for cash...\nType *4* to cancel.")


async def _cust_paid(cfg, phone, upper, session):
    name = session.get("name", ""); table = session.get("table", "")
    if upper in ("8", "CHECKOUT"): await _checkout(cfg, phone, session); return
    if upper in ("5", "BILL"):
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(f"http://localhost:8000/webhook/{cfg.slug}/generate-bill",
                              json={"table": table, "phone": phone})
        await wa.send_text(cfg, phone, "📄 Bill sent!"); return
    if upper in ("7", "WAITER"):
        await wa.send_all_staff(cfg, f"🔔 *WAITER*\n🪑 {table} | 👤 {name}")
        await wa.send_text(cfg, phone, "🔔 Waiter notified!"); return
    if upper in ("1", "ORDER MORE"):
        session["status"] = "ORDERING"
        rc.save_session(cfg.slug, phone, session, cfg.session_ttl)
        await wa.send_text(cfg, phone, f"📋 *Order more:*\n\n👉 {session.get('menuURL', cfg.menu_url)}"); return
    paid_total = sum(float(b.get("total",0)) for b in (session.get("paidOrders") or []))
    await wa.send_text(cfg, phone,
        f"✅ *Thank you {name}!*\n💰 Paid: ₹{paid_total:.0f}\n\n"
        f"*1* - 📋 More | *5* - 📄 Bill | *7* - 🔔 Waiter | *8* - 👋 Checkout")


async def _initiate_payment(cfg: TenantConfig, phone: str, session: dict):
    orders = session.get("orders", [])
    sub, tax, total_before = _calc_totals(cfg, orders)
    total    = _apply_discount(cfg, total_before)
    discount = total_before - total
    order_id = f"ORD{int(datetime.now().timestamp())}"
    table    = session.get("table", "")
    name     = session.get("name", "")

    session.update({"orderId": order_id, "total": total, "subtotal": sub, "tax": tax,
                    "status": "PENDING_PAYMENT"})
    rc.save_session(cfg.slug, phone, session, cfg.session_ttl)

    bill = f"🧾 Subtotal: ₹{sub:.0f}\n🏛️ GST ({int(cfg.gst_rate*100)}%): ₹{tax:.0f}\n"
    if discount > 0:
        bill += f"🎉 {cfg.festival_name} (-{cfg.discount_percent}%): -₹{discount:.0f}\n"
    bill += f"━━━━━━━━━━━━━━━━━━\n💵 *Total: ₹{total:.0f}*\n"

    if cfg.payment_method == "razorpay":
        pay_url = ""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.razorpay.com/v1/payment_links",
                    auth=(cfg.razorpay_key_id, cfg.razorpay_key_secret),
                    json={"amount": int(total*100), "currency": "INR",
                          "notes": {"phone": phone, "order_id": order_id, "table": table}}
                )
                pay_url = resp.json().get("short_url", "")
        except Exception:
            pass
        await wa.send_text(cfg, phone,
            f"💳 *PAYMENT LINK*\n━━━━━━━━━━━━━━━━━━\n📋 {order_id} | 🪑 {table}\n\n{bill}\n"
            f"👆 Pay here:\n{pay_url}\n\n*2* - 💵 Cash | *4* - ❌ Cancel")
    else:
        import urllib.parse
        upi = f"upi://pay?pa={cfg.upi_id}&pn={urllib.parse.quote(cfg.upi_name)}&am={total:.2f}&tn={order_id}&cu=INR"
        qr  = f"https://api.qrserver.com/v1/create-qr-code/?size=512x512&margin=10&data={urllib.parse.quote(upi)}"
        await wa.send_image(cfg, phone, qr, "Scan to Pay")
        await wa.send_text(cfg, phone,
            f"💳 *UPI PAYMENT*\n━━━━━━━━━━━━━━━━━━\n📋 {order_id} | 🪑 {table}\n\n{bill}\n"
            f"👆 Screenshot QR → GPay/PhonePe → Scan from Gallery\n\n*2* - 💵 Cash | *4* - ❌ Cancel")

    await wa.send_all_staff(cfg, f"💵 *PAYMENT INITIATED*\n👤 {name} | 🪑 {table} | ₹{total:.0f}")


async def _checkout(cfg: TenantConfig, phone: str, session: dict):
    name  = session.get("name", "")
    table = session.get("table", "")
    grand = sum(float(b.get("total",0)) for b in (session.get("paidOrders") or []))
    session.update({"status": "AWAITING_FEEDBACK", "grandTotal": grand})
    rc.save_session(cfg.slug, phone, session, ttl=1800)
    rc.delete_table(cfg.slug, table)
    await wa.send_text(cfg, phone,
        f"👋 *Thank you {name}!*\n\nWe hope you enjoyed *{cfg.restaurant_name}*! 🙏\n\n"
        f"━━━━━━━━━━━━━━━━━━\n⭐ *Rate your experience:*\n\n"
        f"*1* 😞 Poor | *2* 😐 Average | *3* 🙂 Good | *4* 😊 Very Good | *5* 🤩 Excellent\n\n"
        f"Type *SKIP* to skip.")
    await wa.send_all_staff(cfg, f"👋 *CHECKOUT*\n🪑 {table} | 👤 {name} | ✅ Table FREE")


async def _cust_feedback(cfg, phone, clean, upper, session):
    if upper in ("SKIP", "NO", "DONE"):
        await _complete_checkout(cfg, phone, session); return
    if clean in ("1","2","3","4","5"):
        rating = int(clean)
        session.update({"feedbackRating": rating, "status": "AWAITING_FEEDBACK_TEXT"})
        rc.save_session(cfg.slug, phone, session, ttl=1800)
        await wa.send_text(cfg, phone,
            f"{'⭐'*rating}\n\nThank you for rating *{rating}/5*!\n\n💬 Any comments? (or *SKIP*)"); return
    await _save_feedback(cfg, phone, session, 0, clean)
    await _complete_checkout(cfg, phone, session)


async def _cust_feedback_text(cfg, phone, clean, upper, session):
    text_val = "" if upper in ("SKIP","NO","DONE") else clean
    await _save_feedback(cfg, phone, session, session.get("feedbackRating", 0), text_val)
    await _complete_checkout(cfg, phone, session)


async def _save_feedback(cfg, phone, session, rating, text_val):
    name  = session.get("name", ""); table = session.get("table", "")
    total = session.get("grandTotal", 0)
    try:
        with cfg.db_session() as db:
            db.execute(text(
                "INSERT INTO feedback (customer_name, phone, table_name, rating, feedback_text, session_total) "
                "VALUES (:n,:p,:t,:r,:tx,:tot)"
            ), {"n": name, "p": phone, "t": table, "r": rating, "tx": text_val or "", "tot": total})
            db.commit()
    except Exception:
        pass
    if text_val:
        await wa.send_owner(cfg,
            f"💬 *FEEDBACK*\n👤 {name} | 🪑 {table}\n{'⭐'*rating}\n📝 \"{text_val}\"\n💰 ₹{total:.0f}")


async def _complete_checkout(cfg, phone, session):
    rc.delete_session(cfg.slug, phone)
    await wa.send_text(cfg, phone,
        f"🙏 Thank you for visiting *{cfg.restaurant_name}*!\nSee you again! ✨")


async def _main_menu(cfg, phone, session):
    name = session.get("name", "")
    await wa.send_text(cfg, phone,
        f"🍽️ *{cfg.restaurant_name}*\n━━━━━━━━━━━━━━━━━━\n👋 Hi {name}!\n\n"
        f"*1* - 📋 View Menu\n*2* - 🧾 View Order\n*3* - 📤 Send to Kitchen\n"
        f"*4* - ❌ Cancel Cart\n*5* - 💵 Bill & Pay\n*7* - 🔔 Call Waiter\n*8* - 👋 Checkout")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _calc_totals(cfg: TenantConfig, orders: list) -> tuple[float, float, float]:
    sub = sum(float(o["price"]) * int(o["quantity"]) for o in orders)
    tax = round(sub * cfg.gst_rate)
    return sub, tax, sub + tax


def _apply_discount(cfg: TenantConfig, total: float) -> float:
    if not cfg.is_festival_today(): return total
    return total - round(total * cfg.discount_percent / 100)


async def _save_order(cfg, session, phone, orders, method, total, sub, tax):
    name     = session.get("name", "Unknown"); table = session.get("table", "")
    order_id = session.get("orderId") or f"ORD{int(datetime.now().timestamp())}"
    now      = datetime.now(IST)
    items_str = ", ".join(f"{o['quantity']}x {o['name']} (₹{float(o['price'])*o['quantity']:.0f})" for o in orders)
    try:
        with cfg.db_session() as db:
            db.execute(text("""
                INSERT INTO orders (order_id,date,date_only,customer_name,phone,table_name,
                items,subtotal,tax,total,payment_method,status,billed)
                VALUES (:oid,:date,:donly,:name,:phone,:table,:items,:sub,:tax,:total,:method,'Paid',FALSE)
            """), {"oid": order_id, "date": now.strftime("%d/%m/%Y, %I:%M:%S %p"),
                   "donly": now.strftime("%-d/%-m/%Y"), "name": name, "phone": phone,
                   "table": table, "items": items_str, "sub": sub, "tax": tax,
                   "total": total, "method": method})
            db.commit()
    except Exception as e:
        await wa.notify_error(cfg, "whatsapp_bot", "_save_order", str(e))


async def _call_deduct_inventory(cfg, orders, phone, table):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"http://localhost:8000/webhook/{cfg.slug}/deduct-inventory",
                              json={"items": [{"name": o["name"], "quantity": o["quantity"]} for o in orders],
                                    "phone": phone, "table": table})
    except Exception:
        pass
