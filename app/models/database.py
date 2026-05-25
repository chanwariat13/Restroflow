"""
models/database.py
Two layers:
  1. MASTER DB  — one PostgreSQL, stores all clients config
  2. TENANT DB  — each client has their own PostgreSQL (orders, customers, etc.)
"""
from sqlalchemy import (
    create_engine, Column, Integer, String, Numeric,
    Boolean, Text, TIMESTAMP, func
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

    # Tables
    table_count        = Column(Integer, default=10)
    table_prefix       = Column(String, default="T")
    table_secrets      = Column(Text, default="{}")    # JSON string
    max_session_hours  = Column(Integer, default=2)

    # Business rules
    gst_rate           = Column(Numeric, default=0.05)
    session_ttl        = Column(Integer, default=10800)
    premium_threshold  = Column(Integer, default=2)
    cleanup_minutes    = Column(Integer, default=30)

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
    created_at = Column(TIMESTAMP, default=func.now())


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
    id          = Column(Integer, primary_key=True)
    name        = Column(String, nullable=False)
    category    = Column(String, default="Main Course")
    price       = Column(Numeric, nullable=False)
    available   = Column(String, default="Yes")
    type        = Column(String, default="veg")
    image       = Column(String, default="")
    description = Column(String, default="")
    bestseller  = Column(String, default="no")


def setup_master_db():
    """Create master DB tables — clients + staff_members."""
    MasterBase.metadata.create_all(bind=master_engine)
    print("✅ Master DB tables created")
    print("✅ Master DB ready. Now add clients via POST /admin/clients")


def setup_tenant_db(tenant_db_url: str, slug: str):
    """Create all tables in a client's own database."""
    eng = get_tenant_engine(tenant_db_url, slug)
    TenantBase.metadata.create_all(bind=eng)
    print(f"✅ Tenant DB tables created for: {slug}")
