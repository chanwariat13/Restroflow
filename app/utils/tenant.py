"""
utils/tenant.py
Loads the correct client config for every request based on slug in the URL.
Caches in memory so DB is only hit once per client per server restart.
"""
import json
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from fastapi import HTTPException

from app.models.database import MasterSession, Client, get_tenant_session


@dataclass
class TenantConfig:
    """All config for one client. Passed into every handler."""
    slug:               str
    restaurant_name:    str
    menu_url:           str
    evolution_url:      str
    evolution_key:      str
    evolution_instance: str
    staff_owner:        str
    staff_manager:      str
    staff_kitchen:      str
    staff_extra:        list[str]
    all_staff:          list[str]
    kitchen_numbers:    list[str]
    payment_method:     str
    upi_id:             str
    upi_name:           str
    razorpay_key_id:    str
    razorpay_key_secret:str
    razorpay_webhook_secret: str
    table_count:        int
    table_prefix:       str
    table_secrets:      dict
    max_session_hours:  int
    gst_rate:           float
    session_ttl:        int
    premium_threshold:  int
    cleanup_minutes:    int
    festival_active:    bool
    festival_name:      str
    festival_emoji:     str
    discount_percent:   int
    festival_start:     str
    festival_end:       str
    gotenberg_url:      str
    tenant_db_url:      str
    # Branding
    logo_url:           str
    primary_color:      str
    welcome_message:    str
    banner_image:       str
    # Compliance
    gstin:              str = ""

    def is_festival_today(self) -> bool:
        if not self.festival_active:
            return False
        today = date.today().isoformat()
        return self.festival_start <= today <= self.festival_end

    def get_table_names(self) -> list[str]:
        return [f"{self.table_prefix}{i}" for i in range(1, self.table_count + 1)]

    def db_session(self):
        """Get a SQLAlchemy session for this client's tenant DB."""
        Session = get_tenant_session(self.tenant_db_url, self.slug)
        return Session()


# ── In-memory cache (slug → TenantConfig) ─────────────────────────────────────
_cache: dict[str, TenantConfig] = {}


def invalidate_cache(slug: str = None):
    """Call this after updating a client in DB."""
    if slug:
        _cache.pop(slug, None)
    else:
        _cache.clear()


def load_tenant(slug: str) -> TenantConfig:
    """Load client config. Uses cache, falls back to master DB."""
    if slug in _cache:
        return _cache[slug]

    db = MasterSession()
    try:
        client: Client = db.query(Client).filter(
            Client.slug == slug, Client.active == True
        ).first()
    finally:
        db.close()

    if not client:
        raise HTTPException(status_code=404, detail=f"Client '{slug}' not found or inactive")

    extra = [s.strip() for s in (client.staff_extra or "").split(",") if s.strip()]
    all_staff = list(filter(None, [client.staff_owner, client.staff_manager] + extra))

    try:
        secrets = json.loads(client.table_secrets or "{}")
    except Exception:
        secrets = {}

    cfg = TenantConfig(
        slug               = client.slug,
        restaurant_name    = client.restaurant_name,
        menu_url           = client.menu_url or "",
        evolution_url      = client.evolution_url,
        evolution_key      = client.evolution_key,
        evolution_instance = client.evolution_instance,
        staff_owner        = client.staff_owner,
        staff_manager      = client.staff_manager or "",
        staff_kitchen      = client.staff_kitchen or "",
        staff_extra        = extra,
        all_staff          = all_staff,
        kitchen_numbers    = [client.staff_kitchen] if client.staff_kitchen else [],
        payment_method     = client.payment_method or "upi",
        upi_id             = client.upi_id or "",
        upi_name           = client.upi_name or "",
        razorpay_key_id    = client.razorpay_key_id or "",
        razorpay_key_secret= client.razorpay_key_secret or "",
        razorpay_webhook_secret = (getattr(client, "razorpay_webhook_secret", "") or ""),
        table_count        = client.table_count or 10,
        table_prefix       = client.table_prefix or "T",
        table_secrets      = secrets,
        max_session_hours  = client.max_session_hours or 2,
        gst_rate           = float(client.gst_rate or 0.05),
        session_ttl        = client.session_ttl or 10800,
        premium_threshold  = client.premium_threshold or 2,
        cleanup_minutes    = client.cleanup_minutes or 30,
        festival_active    = bool(client.festival_active),
        festival_name      = client.festival_name or "",
        festival_emoji     = client.festival_emoji or "🎉",
        discount_percent   = client.discount_percent or 0,
        festival_start     = client.festival_start or "",
        festival_end       = client.festival_end or "",
        gotenberg_url      = client.gotenberg_url or "http://localhost:3000",
        tenant_db_url      = client.tenant_db_url,
        logo_url           = client.logo_url or "",
        primary_color      = client.primary_color or "#ff6b35",
        welcome_message    = client.welcome_message or "Welcome! Scan & Order",
        banner_image       = client.banner_image or "",
        gstin              = (getattr(client, "gstin", "") or ""),
    )

    _cache[slug] = cfg
    return cfg
