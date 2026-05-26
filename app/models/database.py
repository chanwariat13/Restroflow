"""
models/database.py
Two layers:
  1. MASTER DB  — one PostgreSQL, stores all clients config
  2. TENANT DB  — each client has their own PostgreSQL (orders, customers, etc.)
"""
from sqlalchemy import (
    create_engine, Column, Integer, String, Numeric,
    Boolean, Text, TIMESTAMP, func, inspect, text
)
from sqlalchemy.orm import declarative_base, sessionmaker
import os

# ── Master DB (one for all clients) ──────────────────────────────────────────
MASTER_DB_URL = os.getenv("MASTER_DATABASE_URL", "postgresql://postgres:password@localhost:5432/restroflow_master")

master_engine = create_engine(MASTER_DB_URL, pool_pre_ping=True)
MasterSession = sessionmaker(bind=master_engine, autocommit=False, autoflush=False)
MasterBase = declarative_base()


class Client(MasterBase):
    """One row per restaurant client."""
    __tablename__ = "clients"

    id                 = Column(Integer, primary_key=True)
    slug               = Column(String, unique=True, nullable=False)  # "whiteSugar", "hotelAbc"
    restaurant_name    = Column(String, nullable=False)
    menu_url           = Column(String, default="")

    # Evolution API
    evolution_url      = Column(String, nullable=False)
    evolution_key      = Column(String, nullable=False)
    evolution_instance = Column(String, nullable=False)

    # Staff phones
    staff_owner        = Column(String, nullable=False)
    staff_manager      = Column(String, default="")
    staff_kitchen      = Column(String, default="")
    staff_extra        = Column(String, default="")   # comma-separated

    # Payment
    payment_method     = Column(String, default="upi")   # "upi" or "razorpay"
    upi_id             = Column(String, default="")
    upi_name           = Column(String, default="")
    razorpay_key_id    = Column(String, default="")
    razorpay_key_secret= Column(String, default="")
    razorpay_webhook_secret = Column(String, default="")  # NEW: separate webhook secret for signature verification

    # Tables
    table_count        = Column(Integer, default=10)
    table_prefix       = Column(String, default="T")
    table_secrets      = Column(Text, default="{}")    # JSON string
    max_session_hours  = Column(Integer, default=2)

    # Business rules
    gst_rate           = Column(Numeric, default=0.05)
    gstin              = Column(String, default="")    # NEW: GSTIN of the restaurant for B2B invoice
    state_code         = Column(String, default="")    # NEW: 2-digit state code for CGST/SGST/IGST split
    legal_name         = Column(String, default="")    # NEW: Legal entity name for tax invoice
    pan                = Column(String, default="")    # NEW: PAN of business
    session_ttl        = Column(Integer, default=10800)
    premium_threshold  = Column(Integer, default=2)
    cleanup_minutes    = Column(Integer, default=30)

    # KOT (Kitchen Order Ticket) thermal printer config — ESC/POS over TCP:9100
    kot_enabled        = Column(Boolean, default=False)  # NEW: master toggle
    kot_printer_ip     = Column(String,  default="")     # NEW: LAN IP of thermal printer
    kot_printer_port   = Column(Integer, default=9100)   # NEW: raw print port
    kot_paper_width    = Column(Integer, default=42)     # NEW: chars/line (42=80mm, 32=58mm)
    kot_header_text    = Column(String,  default="")     # NEW: custom line under restaurant name

    # Festival
    festival_active    = Column(Boolean, default=False)
    festival_name      = Column(String, default="")
    festival_emoji     = Column(String, default="🎉")
    discount_percent   = Column(Integer, default=10)
    festival_start     = Column(String, default="")
    festival_end       = Column(String, default="")

    # External services
    gotenberg_url      = Column(String, default="http://localhost:3000")

    # Each client's own DB
    tenant_db_url      = Column(String, nullable=False)

    # Client dashboard
    dashboard_password = Column(String, default="")
    google_review_url  = Column(String, default="")

    # Branding
    logo_url           = Column(String, default="")
    primary_color      = Column(String, default="#ff6b35")
    welcome_message    = Column(String, default="Welcome! Scan & Order")
    banner_image       = Column(String, default="")

    active             = Column(Boolean, default=True)
    created_at         = Column(TIMESTAMP, default=func.now())


class StaffMember(MasterBase):
    """Staff members per client — changeable anytime from dashboard."""
    __tablename__ = "staff_members"

    id         = Column(Integer, primary_key=True)
    slug       = Column(String, nullable=False)          # client slug
    phone      = Column(String, nullable=False)          # WhatsApp number
    name       = Column(String, nullable=False)
    role       = Column(String, nullable=False)          # owner/manager/kitchen/waiter
    pin        = Column(String, nullable=False)          # 4-6 digit PIN
    active     = Column(Boolean, default=True)
    deleted_at = Column(TIMESTAMP, nullable=True)        # NEW: soft-delete timestamp
    created_at = Column(TIMESTAMP, default=func.now())


class AuditLog(MasterBase):
    """
    Audit trail. Every privileged action (admin, owner, manager) is logged here.
    Stays append-only; never delete rows. One row = one action.
    """
    __tablename__ = "audit_log"

    id          = Column(Integer, primary_key=True)
    slug        = Column(String, default="", index=True)   # client slug, "" for master-level
    actor       = Column(String, default="")               # admin name / phone / "system"
    actor_role  = Column(String, default="")               # superadmin/owner/manager/staff/customer/system
    action      = Column(String, default="")               # e.g. client.create, menu.delete, payment.confirm
    target      = Column(String, default="")               # id or slug of the affected resource
    payload     = Column(Text, default="")                 # JSON string (capped to 5000 chars)
    ip          = Column(String, default="")
    user_agent  = Column(String, default="")
    created_at  = Column(TIMESTAMP, default=func.now(), index=True)


def get_master_db():
    db = MasterSession()
    try:
        yield db
    finally:
        db.close()


# ── Tenant DB (per-client) ────────────────────────────────────────────────────
TenantBase = declarative_base()

_tenant_engines: dict = {}   # slug → engine
_tenant_sessions: dict = {}  # slug → SessionLocal


def get_tenant_engine(tenant_db_url: str, slug: str):
    if slug not in _tenant_engines:
        _tenant_engines[slug] = create_engine(tenant_db_url, pool_pre_ping=True)
        # Lazy migration: idempotently add any missing columns to this tenant's DB.
        # Safe to call repeatedly — only ADDs columns, never alters existing ones.
        try:
            migrate_tenant_db(_tenant_engines[slug], slug)
        except Exception as e:
            print(f"⚠️  tenant migration warning ({slug}): {e}")
    return _tenant_engines[slug]


def get_tenant_session(tenant_db_url: str, slug: str):
    if slug not in _tenant_sessions:
        eng = get_tenant_engine(tenant_db_url, slug)
        _tenant_sessions[slug] = sessionmaker(bind=eng, autocommit=False, autoflush=False)
    return _tenant_sessions[slug]


# ── Tenant table models ───────────────────────────────────────────────────────
class Order(TenantBase):
    __tablename__ = "orders"
    id             = Column(Integer, primary_key=True)
    order_id       = Column(String)
    date           = Column(String)
    date_only      = Column(String)
    customer_name  = Column(String)
    phone          = Column(String)
    table_name     = Column(String)
    items          = Column(Text)
    subtotal       = Column(Numeric)
    tax            = Column(Numeric)
    total          = Column(Numeric)
    payment_method = Column(String)
    status         = Column(String)
    created_at     = Column(TIMESTAMP, default=func.now())
    billed         = Column(Boolean, default=False)
    customer_gstin = Column(String, default="")          # NEW: B2B GSTIN for tax invoice
    # NEW: India-compliance GST split (CGST + SGST for intra-state, IGST for inter-state)
    cgst_amount      = Column(Numeric, default=0)
    sgst_amount      = Column(Numeric, default=0)
    igst_amount      = Column(Numeric, default=0)
    place_of_supply  = Column(String,  default="")  # 2-digit state code
    is_inter_state   = Column(Boolean, default=False)
    hsn_code         = Column(String,  default="996331")  # SAC for restaurant service
    # NEW: KOT print tracking — persistent so a kitchen can reprint on demand
    kot_printed_at   = Column(TIMESTAMP, nullable=True)
    kot_print_count  = Column(Integer,   default=0)


class Customer(TenantBase):
    __tablename__ = "customers"
    id           = Column(Integer, primary_key=True)
    name         = Column(String)
    phone        = Column(String)
    first_visit  = Column(String)
    last_visit   = Column(String)
    total_visits = Column(Integer, default=1)
    total_spent  = Column(Numeric, default=0)
    created_at   = Column(TIMESTAMP, default=func.now())


class DailyCollection(TenantBase):
    __tablename__ = "daily_collection"
    id            = Column(Integer, primary_key=True)
    date          = Column(String)
    total_orders  = Column(Integer)
    cash_amount   = Column(Numeric)
    online_amount = Column(Numeric)
    total_amount  = Column(Numeric)
    created_at    = Column(TIMESTAMP, default=func.now())


class Cancellation(TenantBase):
    __tablename__ = "cancellations"
    id            = Column(Integer, primary_key=True)
    date          = Column(String)
    customer_name = Column(String)
    phone         = Column(String)
    table_name    = Column(String)
    items         = Column(Text)
    total         = Column(Numeric)
    reason        = Column(Text)
    created_at    = Column(TIMESTAMP, default=func.now())


class Feedback(TenantBase):
    __tablename__ = "feedback"
    id            = Column(Integer, primary_key=True)
    date          = Column(TIMESTAMP, default=func.now())
    customer_name = Column(String)
    phone         = Column(String)
    table_name    = Column(String)
    rating        = Column(Integer)
    feedback_text = Column(Text)
    session_total = Column(Numeric)


class Inventory(TenantBase):
    __tablename__ = "inventory"
    id            = Column(Integer, primary_key=True)
    item_name     = Column(String)
    unit          = Column(String)
    current_stock = Column(Numeric)
    min_threshold = Column(Numeric)
    cost_price    = Column(Numeric)
    updated_at    = Column(TIMESTAMP, default=func.now())


class MenuIngredient(TenantBase):
    __tablename__ = "menu_ingredients"
    id            = Column(Integer, primary_key=True)
    menu_item     = Column(String)
    ingredient    = Column(String)
    quantity_used = Column(Numeric)
    unit          = Column(String)


class MenuItem(TenantBase):
    __tablename__ = "menu"
    id           = Column(Integer, primary_key=True)
    name         = Column(String, nullable=False)
    category     = Column(String, default="Main Course")
    price        = Column(Numeric, nullable=False)
    available    = Column(String, default="Yes")
    type         = Column(String, default="veg")
    image        = Column(String, default="")
    description  = Column(String, default="")
    bestseller   = Column(String, default="no")
    gst_rate     = Column(Numeric, nullable=True)        # NEW: per-item GST override (null → use client default)
    dietary_tags = Column(String, default="")            # NEW: csv: jain,vegan,glutenfree,egg,spicy


# ── Order modifiers + variants (P1 feature) ──────────────────────────────────
# An item can have ONE size variant (S/M/L, Half/Full, etc.) that REPLACES the
# base price, and ZERO OR MORE modifier groups (toppings, sauces, prep style).
# Each group has min/max selection rules. Selected modifiers ADD to the price.
class MenuItemVariant(TenantBase):
    """e.g. menu_item_id=1 (Pizza) → [Small ₹0, Medium ₹100, Large ₹200] (price = absolute, not delta)."""
    __tablename__ = "menu_item_variants"
    id            = Column(Integer, primary_key=True)
    menu_item_id  = Column(Integer, nullable=False, index=True)
    name          = Column(String, nullable=False)       # "Small", "Medium", "Half", "Full"
    price         = Column(Numeric, nullable=False)      # absolute price for this variant
    is_default    = Column(Boolean, default=False)       # the variant pre-selected in UI
    sort_order    = Column(Integer, default=0)
    available     = Column(Boolean, default=True)


class MenuItemModifierGroup(TenantBase):
    """e.g. menu_item_id=1 → group "Extra Toppings" (min=0, max=4)."""
    __tablename__ = "menu_item_modifier_groups"
    id            = Column(Integer, primary_key=True)
    menu_item_id  = Column(Integer, nullable=False, index=True)
    name          = Column(String, nullable=False)       # "Extra Toppings", "Spice Level"
    min_select    = Column(Integer, default=0)           # 0 = optional
    max_select    = Column(Integer, default=1)           # 1 = single-choice (radio), N = multi (checkbox)
    sort_order    = Column(Integer, default=0)
    required      = Column(Boolean, default=False)       # convenience flag (=> min_select>=1)


class MenuItemModifier(TenantBase):
    """e.g. group "Extra Toppings" → [Cheese ₹40, Olives ₹30, Mushroom ₹35]."""
    __tablename__ = "menu_item_modifiers"
    id            = Column(Integer, primary_key=True)
    group_id      = Column(Integer, nullable=False, index=True)
    name          = Column(String, nullable=False)       # "Extra Cheese", "Mild", "Hot"
    price         = Column(Numeric, default=0)           # add-on price (can be 0 for free options)
    is_default    = Column(Boolean, default=False)
    sort_order    = Column(Integer, default=0)
    available     = Column(Boolean, default=True)


# ── Postgres type mapping for auto-migration ──────────────────────────────────
def _pg_type_for(col: Column) -> str:
    """Map a SQLAlchemy Column to a Postgres column type string."""
    t = col.type.__class__.__name__
    return {
        "Integer":   "INTEGER",
        "String":    "VARCHAR",
        "Text":      "TEXT",
        "Numeric":   "NUMERIC",
        "Boolean":   "BOOLEAN",
        "TIMESTAMP": "TIMESTAMP",
    }.get(t, "TEXT")


def _format_default(col: Column):
    """Render a SQL literal for the column's Python-side default, or None."""
    d = col.default
    if d is None or d.is_callable or d.is_sequence:
        return None
    val = d.arg
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        escaped = val.replace("'", "''")
        return f"'{escaped}'"
    return None


def migrate_master_db():
    """
    Idempotent auto-migration: for every table in MasterBase, add any columns
    the model declares but the live DB is missing. Safe to run on every startup.
    Only ADDS columns — never drops or alters existing ones.
    """
    _migrate_metadata(master_engine, MasterBase)


def migrate_tenant_db(engine, slug: str):
    """Same as migrate_master_db but for a tenant's own DB."""
    _migrate_metadata(engine, TenantBase)


def _migrate_metadata(engine, base):
    """
    Shared idempotent migrator. For every table the metadata declares, ALTER TABLE
    ADD COLUMN IF NOT EXISTS for any column missing in the live DB. Never drops.
    """
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue  # create_all() will handle brand new tables
            existing_cols = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                col_type = _pg_type_for(col)
                default_sql = _format_default(col)
                stmt = f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS "{col.name}" {col_type}'
                if default_sql is not None:
                    stmt += f" DEFAULT {default_sql}"
                conn.execute(text(stmt))
                print(f"🛠  Added missing column: {table.name}.{col.name} ({col_type})")


def setup_master_db():
    """Create master DB tables — clients + staff_members — and run auto-migration."""
    MasterBase.metadata.create_all(bind=master_engine)
    migrate_master_db()
    print("✅ Master DB tables created")
    print("✅ Master DB ready. Now add clients via POST /admin/clients")


def setup_tenant_db(tenant_db_url: str, slug: str):
    """Create all tables in a client's own database."""
    eng = get_tenant_engine(tenant_db_url, slug)
    TenantBase.metadata.create_all(bind=eng)
    print(f"✅ Tenant DB tables created for: {slug}")
