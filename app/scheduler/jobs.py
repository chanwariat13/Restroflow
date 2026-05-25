"""
scheduler/jobs.py
All scheduled jobs run for ALL active clients automatically.
Adding a new client to the DB → they get reports, cleanup, broadcasts automatically.
No code changes needed.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import text

from app.models.database import MasterSession, Client
from app.utils.tenant import load_tenant, TenantConfig
from app.utils import redis_client as rc
from app.services import whatsapp as wa

IST = ZoneInfo("Asia/Kolkata")


def _get_all_active_clients() -> list[TenantConfig]:
    db = MasterSession()
    try:
        clients = db.query(Client).filter(Client.active == True).all()
        result = []
        for c in clients:
            try:
                result.append(load_tenant(c.slug))
            except Exception:
                pass
        return result
    finally:
        db.close()


# ── F: Auto Cleanup (every 30 min) ───────────────────────────────────────────
async def run_cleanup():
    for cfg in _get_all_active_clients():
        try:
            await _cleanup_one(cfg)
        except Exception as e:
            await wa.notify_error(cfg, "cleanup", "run_cleanup", str(e))


async def _cleanup_one(cfg: TenantConfig):
    now = datetime.now(timezone.utc)
    for entry in rc.get_all_occupied_tables(cfg.slug, cfg.get_table_names()):
        table   = entry["table"]
        phone   = entry["phone"]
        session = entry["session"]

        if not session:
            rc.clear_customer(cfg.slug, phone, table)
            await wa.send_all_staff(cfg, f"🧹 *AUTO CLEANUP*\n🪑 {table} freed (expired)")
            continue

        created_raw = session.get("createdAt")
        if not created_raw:
            continue
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_hours = (now - created).total_seconds() / 3600
        except Exception:
            continue

        if age_hours > cfg.max_session_hours:
            rc.clear_customer(cfg.slug, phone, table)
            await wa.send_all_staff(cfg,
                f"🧹 *AUTO CLEANUP*\n━━━━━━━━━━━━━━━━━━\n\n"
                f"🪑 Table {table} freed\n👤 {session.get('name','?')}\n"
                f"📱 {phone}\n⏰ Age: {age_hours:.1f}h\n📊 Was: {session.get('status','?')}")


# ── G: Daily Report (7 AM IST) ────────────────────────────────────────────────
async def run_daily_report():
    for cfg in _get_all_active_clients():
        try:
            await _daily_report_one(cfg)
        except Exception as e:
            await wa.notify_error(cfg, "daily_report", "run_daily_report", str(e))


async def _daily_report_one(cfg: TenantConfig):
    yesterday = (datetime.now(IST) - timedelta(days=1))
    date_str  = yesterday.strftime("%-d/%-m/%Y")

    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT payment_method, SUM(total) as amount, COUNT(*) as cnt "
            "FROM orders WHERE date_only=:d AND status='Paid' GROUP BY payment_method"
        ), {"d": date_str}).fetchall()

        cash = online = total = orders = 0
        for r in rows:
            pm = (r.payment_method or "").lower()
            if pm == "cash": cash += float(r.amount or 0)
            else: online += float(r.amount or 0)
            total  += float(r.amount or 0)
            orders += int(r.cnt or 0)

        # Upsert daily_collection
        existing = db.execute(text("SELECT id FROM daily_collection WHERE date=:d"), {"d": date_str}).fetchone()
        if existing:
            db.execute(text(
                "UPDATE daily_collection SET total_orders=:o,cash_amount=:c,online_amount=:on,total_amount=:t WHERE date=:d"
            ), {"o": orders, "c": cash, "on": online, "t": total, "d": date_str})
        else:
            db.execute(text(
                "INSERT INTO daily_collection (date,total_orders,cash_amount,online_amount,total_amount) VALUES (:d,:o,:c,:on,:t)"
            ), {"d": date_str, "o": orders, "c": cash, "on": online, "t": total})

        # Update customers
        paid_rows = db.execute(text(
            "SELECT DISTINCT ON (phone) phone, customer_name, total FROM orders "
            "WHERE date_only=:d AND status='Paid' ORDER BY phone, created_at DESC"
        ), {"d": date_str}).fetchall()

        for row in paid_rows:
            phone = row.phone; name = row.customer_name or "Customer"; spent = float(row.total or 0)
            fmt = yesterday.strftime("%-d/%-m/%Y")
            existing_c = db.execute(text("SELECT id FROM customers WHERE phone=:p"), {"p": phone}).fetchone()
            if existing_c:
                db.execute(text("UPDATE customers SET total_visits=total_visits+1, total_spent=total_spent+:s, last_visit=:d WHERE phone=:p"),
                           {"s": spent, "d": fmt, "p": phone})
            else:
                db.execute(text("INSERT INTO customers (name,phone,first_visit,last_visit,total_visits,total_spent) VALUES (:n,:p,:d,:d,1,:s)"),
                           {"n": name, "p": phone, "d": fmt, "s": spent})

        # Check low stock
        low = db.execute(text(
            "SELECT item_name, current_stock, min_threshold, unit FROM inventory WHERE current_stock<=min_threshold"
        )).fetchall()
        db.commit()

    avg = round(total / orders) if orders > 0 else 0
    await wa.send_owner(cfg,
        f"📊 *DAILY REPORT*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 {date_str}\n📋 Orders: {orders}\n"
        f"💵 Cash: ₹{cash:.0f}\n💳 Online: ₹{online:.0f}\n"
        f"💰 *Total: ₹{total:.0f}*\n📈 Avg: ₹{avg:.0f}\n━━━━━━━━━━━━━━━━━━\n🙏 {cfg.restaurant_name}")

    if low:
        await wa.notify_low_stock(cfg, [dict(r._mapping) for r in low])


# ── H: Monthly Report (1st of month, 7 AM) ───────────────────────────────────
async def run_monthly_report():
    for cfg in _get_all_active_clients():
        try:
            await _monthly_report_one(cfg)
        except Exception as e:
            await wa.notify_error(cfg, "monthly_report", "run_monthly_report", str(e))


async def _monthly_report_one(cfg: TenantConfig):
    now        = datetime.now(IST)
    last_month = datetime(now.year, now.month - 1 if now.month > 1 else 12, 1, tzinfo=IST)
    month_name = last_month.strftime("%B %Y")

    with cfg.db_session() as db:
        rows = db.execute(text("SELECT date, total_orders, cash_amount, online_amount, total_amount FROM daily_collection")).fetchall()

    total_orders = total_cash = total_online = total_amount = days = 0
    best = {"date": "", "amount": 0.0}

    for row in rows:
        parts = str(row.date or "").split("/")
        if len(parts) < 3: continue
        try:
            d = datetime(int(parts[2]), int(parts[1]), int(parts[0]), tzinfo=IST)
        except Exception:
            continue
        if d.month == last_month.month and d.year == last_month.year:
            t = float(row.total_amount or 0)
            total_orders += int(row.total_orders or 0)
            total_cash   += float(row.cash_amount or 0)
            total_online += float(row.online_amount or 0)
            total_amount += t; days += 1
            if t > best["amount"]: best = {"date": row.date, "amount": t}

    avg = round(total_amount / days) if days > 0 else 0
    await wa.send_owner(cfg,
        f"📊 *MONTHLY REPORT*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 *{month_name}*\n\n📋 Orders: {total_orders}\n"
        f"💵 Cash: ₹{total_cash:.0f}\n💳 Online: ₹{total_online:.0f}\n"
        f"💰 *Total: ₹{total_amount:.0f}*\n\n━━━━━━━━━━━━━━━━━━\n"
        f"📅 Days: {days}\n📈 Avg: ₹{avg:.0f}\n🏆 Best: {best['date']} (₹{best['amount']:.0f})\n"
        f"━━━━━━━━━━━━━━━━━━\n🙏 {cfg.restaurant_name}")

    with cfg.db_session() as db:
        customers = db.execute(text("SELECT name, phone, total_visits, total_spent FROM customers ORDER BY total_visits DESC")).fetchall()

    seen = set()
    for c in customers:
        phone = str(c.phone or "").strip()
        if not phone or phone in seen: continue
        seen.add(phone)
        visits = int(c.total_visits or 0); spent = float(c.total_spent or 0)
        fname  = (c.name or "Valued Customer").split()[0]

        if visits >= cfg.premium_threshold:
            msg = (f"🌟 *Namaste {fname}!*\n\nAap *{cfg.restaurant_name}* ke premium customer hain! 🎉\n\n"
                   f"🔄 Visits: {visits}\n💰 Spent: ₹{spent:.0f}\n\n━━━━━━━━━━━━━━━━━━\n\n"
                   f"🎁 Is month *10% special discount* milega!\nBas staff ko number batayein.\n\n"
                   f"━━━━━━━━━━━━━━━━━━\n🍽️ — *{cfg.restaurant_name}*")
        else:
            msg = (f"🍽️ *Namaste {fname}!*\n\n*{cfg.restaurant_name}* mein aapka swagat! 🙏\n\n"
                   f"━━━━━━━━━━━━━━━━━━\n\n🎁 Phir se aayiye!\nEk *complimentary welcome drink* milega! 🥤\n\n"
                   f"━━━━━━━━━━━━━━━━━━\n🍽️ — *{cfg.restaurant_name}*")
        try:
            await wa.send_text(cfg, phone, msg)
        except Exception:
            pass


# ── J: Festival Broadcast (10 AM daily) ──────────────────────────────────────
async def run_festival_broadcast():
    for cfg in _get_all_active_clients():
        if not cfg.is_festival_today():
            continue
        try:
            await _festival_one(cfg)
        except Exception as e:
            await wa.notify_error(cfg, "festival_broadcast", "run_festival_one", str(e))


async def _festival_one(cfg: TenantConfig):
    with cfg.db_session() as db:
        rows = db.execute(text(
            "SELECT DISTINCT ON (phone) phone, customer_name FROM orders "
            "WHERE status='Paid' AND phone IS NOT NULL AND phone!='' ORDER BY phone, created_at DESC"
        )).fetchall()

    sent = failed = 0
    for row in rows:
        phone = str(row.phone or "").strip()
        fname = (str(row.customer_name or "Valued Customer")).split()[0]
        if not phone: continue
        msg = (
            f"{cfg.festival_emoji} *Happy {cfg.festival_name}!* {cfg.festival_emoji}\n"
            f"━━━━━━━━━━━━━━━━━━\n\nDear *{fname}*, 🙏\n\n"
            f"This {cfg.festival_name}, enjoy:\n\n"
            f"🎁 *{cfg.discount_percent}% OFF* on your entire bill!\n\n"
            f"📅 Valid: {cfg.festival_start} to {cfg.festival_end}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n📍 *{cfg.restaurant_name}*\nSee you soon! 😊🍽️"
        )
        try:
            await wa.send_text(cfg, phone, msg); sent += 1
        except Exception:
            failed += 1

    await wa.send_owner(cfg,
        f"📢 *FESTIVAL BROADCAST DONE*\n🎉 {cfg.festival_name}\n✅ Sent: {sent} | ❌ Failed: {failed}")
