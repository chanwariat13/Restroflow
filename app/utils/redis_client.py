"""
utils/redis_client.py
All Redis keys are prefixed with the client slug.
  whiteSugar:session:917340226277
  hotelAbc:session:919876543210
So all clients share ONE Redis instance with zero key collision.
"""
import json
import os
import redis

_redis = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    db=int(os.getenv("REDIS_DB", 0)),
    password=os.getenv("REDIS_PASSWORD", None),
    decode_responses=True
)

# ── Key builders (always slug-prefixed) ──────────────────────────────────────
def _k(slug: str, *parts) -> str:
    return f"{slug}:" + ":".join(parts)

def session_key(slug, phone):  return _k(slug, "session", phone)
def table_key(slug, table):    return _k(slug, "table",   table)
def pending_key(slug, phone):  return _k(slug, "pending", phone)
def blocked_key(slug, phone):  return _k(slug, "blocked", phone)


# ── Session ───────────────────────────────────────────────────────────────────
def get_session(slug: str, phone: str) -> dict | None:
    raw = _redis.get(session_key(slug, phone))
    if not raw or raw in ("null", ""):
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def save_session(slug: str, phone: str, session: dict, ttl: int = 10800) -> None:
    _redis.setex(session_key(slug, phone), ttl, json.dumps(session))


def delete_session(slug: str, phone: str) -> None:
    _redis.delete(session_key(slug, phone))


# ── Table ─────────────────────────────────────────────────────────────────────
def get_table_phone(slug: str, table: str) -> str | None:
    val = _redis.get(table_key(slug, table))
    if not val or val in ("null", ""):
        return None
    return val.split("|")[0].strip()


def set_table(slug: str, table: str, phone: str, pending: bool = False, ttl: int = 10800) -> None:
    val = f"{phone}|PENDING" if pending else phone
    _redis.setex(table_key(slug, table), ttl, val)


def delete_table(slug: str, table: str) -> None:
    _redis.delete(table_key(slug, table))


def is_table_occupied(slug: str, table: str) -> bool:
    return _redis.exists(table_key(slug, table)) > 0


# ── Pending ───────────────────────────────────────────────────────────────────
def set_pending(slug: str, phone: str, data: dict, ttl: int = 10800) -> None:
    _redis.setex(pending_key(slug, phone), ttl, json.dumps(data))


def delete_pending(slug: str, phone: str) -> None:
    _redis.delete(pending_key(slug, phone))


# ── Blocked ───────────────────────────────────────────────────────────────────
def is_blocked(slug: str, phone: str) -> bool:
    return _redis.exists(blocked_key(slug, phone)) > 0


def block_phone(slug: str, phone: str, ttl: int = 86400) -> None:
    _redis.setex(blocked_key(slug, phone), ttl, "1")


def unblock_phone(slug: str, phone: str) -> None:
    _redis.delete(blocked_key(slug, phone))


# ── Cleanup helpers ───────────────────────────────────────────────────────────
def get_all_occupied_tables(slug: str, table_names: list[str]) -> list[dict]:
    results = []
    for table in table_names:
        phone = get_table_phone(slug, table)
        if not phone:
            continue
        session = get_session(slug, phone)
        results.append({"table": table, "phone": phone, "session": session})
    return results


def clear_customer(slug: str, phone: str, table: str) -> None:
    delete_session(slug, phone)
    delete_table(slug, table)
    delete_pending(slug, phone)


# ── Idempotency helpers ───────────────────────────────────────────────────────
# Used by the Razorpay webhook to short-circuit retries that would otherwise
# insert the same `orders` row twice and double-deduct inventory.
def claim_event(slug: str, namespace: str, event_id: str, ttl: int = 7 * 24 * 3600) -> bool:
    """
    Atomically mark `event_id` as processed for this tenant + namespace.
    Returns True if this is the first time we've seen the id (caller MUST
    proceed with side-effects), False if already claimed (caller MUST short-
    circuit). TTL defaults to 7 days, well past Razorpay's retry window.
    """
    if not event_id:
        # Without an id we can't dedupe — fall through and let the caller
        # handle as a fresh event. Better than blocking legitimate traffic.
        return True
    key = _k(slug, "evt", namespace, str(event_id))
    # SET key value NX EX ttl  →  returns OK only if key did not exist.
    return bool(_redis.set(key, "1", nx=True, ex=ttl))


# ── Simple per-key rate limit (token-counter style) ──────────────────────────
# Suitable for low-stakes, public, read-only endpoints (e.g. the
# returning-guest lookup) where we just want to stop a runaway scraper.
# Caller passes any opaque key (typically slug + ip + endpoint name); we
# return True if the request is within budget, False to reject with 429.
def rate_limit_check(key: str, limit: int, window_seconds: int = 60) -> bool:
    """
    Increments a counter at `rl:<key>` with a `window_seconds` TTL on first
    increment. Returns True iff the resulting count is ≤ `limit`. Race
    conditions are inconsequential at this resolution.
    """
    full_key = f"rl:{key}"
    pipe = _redis.pipeline()
    pipe.incr(full_key, 1)
    pipe.expire(full_key, window_seconds, nx=True)
    count, _ = pipe.execute()
    try:
        return int(count) <= int(limit)
    except (TypeError, ValueError):
        return True
