"""
services/whatsapp.py
All WhatsApp sending via Evolution API.
Takes TenantConfig so it knows which instance/key to use per client.
"""
import httpx
from app.utils.tenant import TenantConfig


async def _post(cfg: TenantConfig, endpoint: str, payload: dict) -> dict:
    headers = {"Content-Type": "application/json", "apikey": cfg.evolution_key}
    url = f"{cfg.evolution_url}/message/{endpoint}/{cfg.evolution_instance}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=payload, headers=headers)
        return r.json()


async def send_text(cfg: TenantConfig, phone: str, text: str) -> dict:
    return await _post(cfg, "sendText", {"number": phone, "text": text})


async def send_image(cfg: TenantConfig, phone: str, url: str, caption: str = "") -> dict:
    return await _post(cfg, "sendMedia", {
        "number": phone, "mediatype": "image", "media": url, "caption": caption
    })


async def send_document_base64(cfg: TenantConfig, phone: str, b64: str, filename: str, caption: str = "") -> dict:
    return await _post(cfg, "sendMedia", {
        "number": phone, "mediatype": "document",
        "media": b64, "fileName": filename, "caption": caption
    })


# ── Shortcuts ─────────────────────────────────────────────────────────────────
async def send_owner(cfg: TenantConfig, text: str):
    await send_text(cfg, cfg.staff_owner, text)

async def send_manager(cfg: TenantConfig, text: str):
    if cfg.staff_manager:
        await send_text(cfg, cfg.staff_manager, text)

async def send_kitchen(cfg: TenantConfig, text: str):
    if cfg.staff_kitchen:
        await send_text(cfg, cfg.staff_kitchen, text)

async def send_all_staff(cfg: TenantConfig, text: str):
    await send_text(cfg, cfg.staff_owner, text)
    if cfg.staff_manager:
        await send_text(cfg, cfg.staff_manager, text)


# ── Notification templates ────────────────────────────────────────────────────
async def notify_new_customer(cfg: TenantConfig, phone: str, name: str, table: str, time_ist: str):
    msg = (
        f"🔔 *NEW CUSTOMER REQUEST*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 Name: {name}\n📱 Phone: {phone}\n🪑 Table: {table}\n🕐 Time: {time_ist}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"✅ Reply: *APPROVE {phone}*\n❌ Reply: *REJECT {phone}*"
    )
    await send_all_staff(cfg, msg)


async def notify_new_order(cfg: TenantConfig, table: str, name: str, phone: str,
                            kitchen_items: str, total: float):
    msg = (
        f"🔥 *NEW ORDER* 🔥\n━━━━━━━━━━━━━━━━━━\n"
        f"🪑 *{table}* | 👤 {name} | 📱 {phone}\n━━━━━━━━━━━━━━━━━━\n\n"
        f"🍽️ *PREPARE:*\n{kitchen_items}\n━━━━━━━━━━━━━━━━━━\n"
        f"💵 Total: ₹{total:.0f}\n\n"
        f"Reply *{table}* to confirm | *DONE {table}* when ready"
    )
    await send_all_staff(cfg, msg)
    await send_kitchen(cfg, msg)


async def notify_payment(cfg: TenantConfig, phone: str, name: str, table: str,
                          order_id: str, total: float, method: str):
    msg = (
        f"✅ *PAYMENT RECEIVED*\n━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {name} | 🪑 {table} | 📱 {phone}\n"
        f"📋 {order_id} | 💰 ₹{total:.0f} via {method}\n━━━━━━━━━━━━━━━━━━"
    )
    await send_all_staff(cfg, msg)


async def notify_low_stock(cfg: TenantConfig, items: list[dict]):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    time_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p")
    msg = f"⚠️ *LOW STOCK ALERT*\n━━━━━━━━━━━━━━━━━━\n🕐 {time_ist}\n\n📦 *Items running low:*\n\n"
    for item in items:
        msg += f"🔴 *{item['item_name']}*\n   Current: {item['current_stock']}{item['unit']}\n   Min: {item['min_threshold']}{item['unit']}\n\n"
    msg += "━━━━━━━━━━━━━━━━━━\nType *RESTOCK* to update."
    await send_owner(cfg, msg)


async def notify_error(cfg: TenantConfig, module: str, node: str, error: str):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    time_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M:%S %p")
    msg = (
        f"🚨 *RESTROFLOW ERROR*\n━━━━━━━━━━━━━━━━━━\n"
        f"🏪 Client: {cfg.restaurant_name}\n"
        f"📋 Module: {module}\n⚙️ At: {node}\n🕐 {time_ist}\n\n"
        f"❌ {error[:300]}\n━━━━━━━━━━━━━━━━━━"
    )
    await send_owner(cfg, msg)
    await send_manager(cfg, msg)
