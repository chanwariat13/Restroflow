"""
routes/whatsapp_bot.py
POST /webhook/{slug}/whatsapp  ← Evolution API calls this per client

slug in the URL identifies which client/restaurant this message belongs to.
All logic is identical to single-tenant version but uses TenantConfig + slug-prefixed Redis.
"""
import hmac
import logging
import os
import re
import httpx
import secrets as _secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request, BackgroundTasks, Path, Header
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.utils.tenant import load_tenant, TenantConfig
from app.utils import redis_client as rc
from app.utils.dates import fmt_date_short
from app.services import whatsapp as wa

router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# Internal HTTP self-calls (BILL, SPLIT, deduct-inventory) reach back into our
# own FastAPI process. The previous hardcoded "http://localhost:8000" broke
# whenever the service ran on a non-default port, behind a reverse proxy, or
# in any environment where the public-facing host differs from the bind addr.
# Operators can override via the INTERNAL_BASE_URL env var (e.g.
# "http://127.0.0.1:9000") while keeping the localhost default for the common
# single-process deployment.
_INTERNAL_BASE_URL = os.getenv("INTERNAL_BASE_URL", "http://localhost:8000").rstrip("/")

# Shared secret used to authenticate self-calls to the internal `generate-bill`,
# `split-bill`, `deduct-inventory`, and `unbilled-orders` endpoints. When set,
# api_routes._require_internal_auth rejects any call missing or mismatching
# this header. We always send it from the bot — if the env var is unset on
# the receiving side, the header is ignored (legacy mode).
_INTERNAL_API_KEY = (os.getenv("INTERNAL_API_KEY") or "").strip()


def _internal_headers() -> dict:
    """Headers attached to every internal self-call. Empty dict if no key set."""
    return {"X-Internal-Auth": _INTERNAL_API_KEY} if _INTERNAL_API_KEY else {}


# ── Inbound WhatsApp webhook authentication ────────────────────────────────
# Evolution API can be configured to send an `apikey` header on every webhook
# delivery. We require the inbound header to match `WHATSAPP_WEBHOOK_TOKEN`
# and reject anything else with 401 — closing the impersonation hole where
# a third party could POST a forged "messages.upsert" payload to
# /webhook/{slug}/whatsapp and get treated as a staff WhatsApp number.
#
# The header name is configurable (default `apikey`) so operators on a
# custom Evolution build can swap it.
#
# Boot-time guard: app/main.py refuses to start when the token is unset
# unless the operator has explicitly set WHATSAPP_WEBHOOK_AUTH_OPTOUT=1
# for a short migration window. In that opt-out window we still log every
# hit at WARNING and accept the request; otherwise the env var is
# guaranteed to be set by the time we get here.
_WHATSAPP_WEBHOOK_TOKEN = (os.getenv("WHATSAPP_WEBHOOK_TOKEN") or "").strip()
_WHATSAPP_WEBHOOK_HEADER = (
    os.getenv("WHATSAPP_WEBHOOK_HEADER") or "apikey"
).strip().lower() or "apikey"
_WHATSAPP_WEBHOOK_OPTOUT = (
    os.getenv("WHATSAPP_WEBHOOK_AUTH_OPTOUT") or ""
).strip() in {"1", "true", "yes"}


def _verify_whatsapp_webhook(request: Request) -> bool:
    """Return True iff the inbound webhook is authorized.

    * Token configured (the production path) → constant-time compare of
      the inbound header value to the env var. Missing or mismatched
      header → False.
    * Token not configured AND `WHATSAPP_WEBHOOK_AUTH_OPTOUT=1` → log
      every hit at WARNING and return True. The boot-time guard in
      `app/main.py` refuses to start in any other configuration, so this
      branch only triggers during an explicit short migration window.
    """
    if not _WHATSAPP_WEBHOOK_TOKEN:
        # Migration-window opt-out: the boot guard already validated that
        # `WHATSAPP_WEBHOOK_AUTH_OPTOUT=1` is set when we get here.
        logger.warning(
            "WhatsApp webhook accepted without auth (WHATSAPP_WEBHOOK_AUTH_OPTOUT=1). "
            "This is intended for short migrations only — set "
            "WHATSAPP_WEBHOOK_TOKEN and remove the opt-out as soon as possible."
        )
        return True
    provided = (request.headers.get(_WHATSAPP_WEBHOOK_HEADER) or "").strip()
    if not provided:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8"),
        _WHATSAPP_WEBHOOK_TOKEN.encode("utf-8"),
    )


def _new_order_id() -> str:
    """Cryptographically-random order id; see api_routes._new_order_id."""
    ts = int(datetime.now().timestamp())
    return f"ORD{ts}{_secrets.token_hex(5)}"


def _tprefix(cfg: TenantConfig) -> str:
    """Per-tenant uppercase table prefix (e.g. "T", "A", "TBL"). Falls back
    to "T" so legacy clients that haven't set table_prefix still work."""
    return (getattr(cfg, "table_prefix", None) or "T").upper()


def _tre(cfg: TenantConfig) -> str:
    """Regex-escaped prefix for use in `^<prefix>\\d+$` patterns."""
    return re.escape(_tprefix(cfg))


@router.post("/webhook/{slug}/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    slug: str = Path(...)
):
    # Authenticate before reading the body. An attacker who knows a slug but
    # not the webhook token cannot get past this gate to influence Redis
    # session state or trigger outbound WhatsApp sends.
    if not _verify_whatsapp_webhook(request):
        logger.warning("WhatsApp webhook rejected: bad/missing token slug=%s", slug)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
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
    P  = _tprefix(cfg)
    PE = _tre(cfg)

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

    m = re.match(rf"^FREE\s+{PE}(\d+)$", upper)
    if m:
        table = f"{P}{m.group(1)}"
        phone = rc.get_table_phone(cfg.slug, table)
        if phone:
            rc.clear_customer(cfg.slug, phone, table)
            await wa.send_text(cfg, staff_phone, f"✅ Table {table} freed!")
        else:
            await wa.send_text(cfg, staff_phone, f"ℹ️ Table {table} already free.")
        return

    m = re.match(rf"^STATUS\s+({PE}?\d+)$", upper)
    if m:
        await _status(cfg, staff_phone, m.group(1)); return

    if upper in ("TABLES", "TABLE STATUS"):
        await _tables(cfg, staff_phone); return

    m = re.match(rf"^BILL\s+{PE}(\d+)$", upper)
    if m:
        table = f"{P}{m.group(1)}"
        phone = rc.get_table_phone(cfg.slug, table) or ""
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(
                f"{_INTERNAL_BASE_URL}/webhook/{cfg.slug}/generate-bill",
                json={"table": table, "phone": phone},
                headers=_internal_headers(),
            )
        await wa.send_text(cfg, staff_phone, f"📄 Bill sent for {table}")
        return

    # SPLIT <prefix>1 N — split today's paid+unbilled orders for the table
    # into N equal shares; each share PDF goes to the owner. Optional
    # comma-separated phones follow the count: "SPLIT T1 3 91xx,91yy,91zz"
    # sends each share directly to the matching guest.
    m = re.match(rf"^SPLIT\s+{PE}(\d+)\s+(\d+)(?:\s+([\d,]+))?$", upper)
    if m:
        table = f"{P}{m.group(1)}"
        parts = max(2, int(m.group(2)))
        phones_raw = m.group(3) or ""
        phones = [p for p in phones_raw.split(",") if p]
        shares = [{"label": f"Share {i+1}",
                    "phone": phones[i] if i < len(phones) else "",
                    "items": []}
                   for i in range(parts)]
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{_INTERNAL_BASE_URL}/webhook/{cfg.slug}/split-bill",
                    json={"table": table, "mode": "equal",
                           "parts": parts, "shares": shares,
                           "notify_owner": True},
                    headers=_internal_headers(),
                )
            data = resp.json()
            if data.get("success"):
                amts = ", ".join(f"₹{a:.0f}" for a in data.get("share_totals", []))
                await wa.send_text(cfg, staff_phone,
                    f"✅ {table} split into {parts}: {amts}\n"
                    f"PDFs sent to{' guests + ' if phones else ' '}owner.")
            else:
                await wa.send_text(cfg, staff_phone,
                    f"⚠️ Split failed: {data.get('message') or data.get('error') or 'unknown'}")
        except Exception as e:
            await wa.send_text(cfg, staff_phone, f"⚠️ Split error: {e}")
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

    m = re.match(rf"^AMOUNT\s+{PE}(\d+)$", upper)
    if m:
        await _amount(cfg, staff_phone, f"{P}{m.group(1)}"); return

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
    PE = _tre(cfg)
    if re.match(rf"^{PE}\d+$", target.upper()):
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
    today = fmt_date_short(datetime.now(IST))
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
    P = _tprefix(cfg)
    await wa.send_text(cfg, staff_phone,
        "🤖 *ADMIN COMMANDS*\n━━━━━━━━━━━━━━━━━━\n\n"
        "✅ *APPROVE 91xx*\n❌ *REJECT 91xx*\n💵 *CASH RECEIVED 91xx*\n"
        f"🚫 *BLOCK 91xx*\n✅ *UNBLOCK 91xx*\n🗑️ *FREE {P}1*\n"
        f"📊 *STATUS {P}1*\n📊 *TABLES*\n📄 *BILL {P}1*\n"
        f"✂️ *SPLIT {P}1 N* (split bill in N equal shares;\n"
        f"    optional phones: SPLIT {P}1 3 91aa,91bb,91cc)\n"
        f"📦 *RESTOCK*\n📦 *STOCK*\n📊 *REPORT*\n💰 *AMOUNT {P}1*")


# ═══════════════════════════════════════════════════════
# KITCHEN
# ═══════════════════════════════════════════════════════
async def _handle_kitchen(cfg: TenantConfig, kitchen_phone: str, text: str):
    upper = re.sub(r"[_*~`]", "", text).strip().upper()
    P  = _tprefix(cfg)
    PE = _tre(cfg)

    m = re.match(rf"^DONE\s+{PE}(\d+)$", upper)
    if m:
        table = f"{P}{m.group(1)}"
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

    m = re.match(rf"^{PE}(\d+)$", upper)
    if m:
        table = f"{P}{m.group(1)}"
        phone = rc.get_table_phone(cfg.slug, table)
        if not phone:
            await wa.send_text(cfg, kitchen_phone, f"⚠️ No session at {table}"); return
        session = rc.get_session(cfg.slug, phone)
        if session:
            session.setdefault("kitchenOrders", []).extend(session.get("orders", []))
            rc.save_session(cfg.slug, phone, session, cfg.session_ttl)
        await wa.send_text(cfg, kitchen_phone, f"✅ Order confirmed for {table}. Preparing!")
        return

    await wa.send_text(cfg, kitchen_phone,
        f"❓\n\n*{P}1* — confirm order\n*DONE {P}1* — food ready")


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
        # Don't wipe the cart on payment cancel — the order has already been
        # sent to the kitchen and the customer would have to re-enter every
        # line. Just step back to ORDER_PLACED so they can retry payment or
        # tap *2* for cash.
        session["status"] = "ORDER_PLACED"
        rc.save_session(cfg.slug, phone, session, cfg.session_ttl)
        await wa.send_text(cfg, phone, "❌ Cancelled.\n\n*5* - Pay | *1* - Menu"); return
    await wa.send_text(cfg, phone, "⏳ Waiting for payment.\n\n*2* - 💵 Cash | *4* - ❌ Cancel")


async def _cust_pending_cash(cfg, phone, upper, session):
    if upper in ("4", "CANCEL"):
        # Same fix as _cust_pending_online: keep the cart so the customer can
        # switch back to online or just retry without re-typing their order.
        session["status"] = "ORDER_PLACED"
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
            await client.post(f"{_INTERNAL_BASE_URL}/webhook/{cfg.slug}/generate-bill",
                              json={"table": table, "phone": phone},
                              headers=_internal_headers())
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
    order_id = _new_order_id()
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
        # Try to obtain a Razorpay payment link, but never silently send the
        # customer a message containing an empty URL — fall back to UPI (if
        # configured) or a clear cash-only instruction. The session status
        # is reset so the user can retry or pay cash without being stuck in
        # PENDING_PAYMENT forever.
        pay_url = ""
        razorpay_err = ""
        if not (cfg.razorpay_key_id and cfg.razorpay_key_secret):
            razorpay_err = "Razorpay not configured for this restaurant."
        else:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(
                        "https://api.razorpay.com/v1/payment_links",
                        auth=(cfg.razorpay_key_id, cfg.razorpay_key_secret),
                        json={"amount": int(total*100), "currency": "INR",
                              "notes": {"phone": phone, "order_id": order_id, "table": table}}
                    )
                if resp.status_code in (200, 201):
                    pay_url = (resp.json() or {}).get("short_url") or ""
                else:
                    razorpay_err = f"HTTP {resp.status_code}"
            except Exception as e:
                razorpay_err = str(e)[:120]

        if pay_url:
            await wa.send_text(cfg, phone,
                f"💳 *PAYMENT LINK*\n━━━━━━━━━━━━━━━━━━\n📋 {order_id} | 🪑 {table}\n\n{bill}\n"
                f"👆 Pay here:\n{pay_url}\n\n*2* - 💵 Cash | *4* - ❌ Cancel")
        else:
            # Roll the session back so the customer isn't trapped in
            # PENDING_PAYMENT with no link to actually pay.
            session["status"] = "ORDER_PLACED"
            rc.save_session(cfg.slug, phone, session, cfg.session_ttl)
            upi_block = ""
            if cfg.upi_id:
                import urllib.parse
                upi = (f"upi://pay?pa={cfg.upi_id}"
                       f"&pn={urllib.parse.quote(cfg.upi_name or cfg.restaurant_name)}"
                       f"&am={total:.2f}&tn={order_id}&cu=INR")
                upi_block = f"\n💡 You can also pay via UPI:\n{upi}\n"
            await wa.send_text(cfg, phone,
                "⚠️ *Online payment is temporarily unavailable.*\n"
                f"━━━━━━━━━━━━━━━━━━\n📋 {order_id} | 🪑 {table}\n\n{bill}"
                f"{upi_block}\n👋 Please pay cash to staff (type *2*) or try again."
            )
            # Tell staff so they can collect cash promptly.
            await wa.send_all_staff(cfg,
                f"⚠️ *Razorpay link failed* for {name} | 🪑 {table} | ₹{total:.0f}\n"
                f"Reason: {razorpay_err or 'no short_url returned'}\n"
                "Customer prompted to pay cash.")
            return
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
    order_id = session.get("orderId") or _new_order_id()
    now      = datetime.now(IST)
    items_str = ", ".join(f"{o['quantity']}x {o['name']} (₹{float(o['price'])*o['quantity']:.0f})" for o in orders)
    try:
        with cfg.db_session() as db:
            db.execute(text("""
                INSERT INTO orders (order_id,date,date_only,customer_name,phone,table_name,
                items,subtotal,tax,total,payment_method,status,billed)
                VALUES (:oid,:date,:donly,:name,:phone,:table,:items,:sub,:tax,:total,:method,'Paid',FALSE)
            """), {"oid": order_id, "date": now.strftime("%d/%m/%Y, %I:%M:%S %p"),
                   "donly": fmt_date_short(now), "name": name, "phone": phone,
                   "table": table, "items": items_str, "sub": sub, "tax": tax,
                   "total": total, "method": method})
            db.commit()
    except Exception as e:
        await wa.notify_error(cfg, "whatsapp_bot", "_save_order", str(e))


async def _call_deduct_inventory(cfg, orders, phone, table):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{_INTERNAL_BASE_URL}/webhook/{cfg.slug}/deduct-inventory",
                              json={"items": [{"name": o.get("menu_name") or o["name"],
                                                "quantity": o["quantity"]} for o in orders],
                                    "phone": phone, "table": table},
                              headers=_internal_headers())
    except Exception:
        pass
