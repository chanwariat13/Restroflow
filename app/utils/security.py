"""
utils/security.py
Crypto helpers — keep tiny and focused. Currently:
  - verify_razorpay_signature: HMAC-SHA256 webhook signature check
  - hash_pin / verify_pin / needs_rehash: PBKDF2 staff-PIN hashing with
    transparent legacy-plaintext upgrade
"""
import hashlib
import hmac
import secrets


def verify_razorpay_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """
    Verify the X-Razorpay-Signature header against the raw request body.

    Razorpay computes:  HMAC_SHA256(webhook_secret, raw_body).hexdigest()
    and sends it in the X-Razorpay-Signature header.

    Returns True iff the signature matches. Constant-time comparison.
    """
    if not signature or not secret or not raw_body:
        return False
    try:
        expected = hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False


# ── Staff-PIN hashing ────────────────────────────────────────────────────────
# Staff PINs were stored as plaintext 4-6 digit strings. A read-only DB leak
# (a forgotten backup, a misconfigured replica, an SQL-injection elsewhere)
# would have handed every staff role at every restaurant to the attacker.
#
# We hash with PBKDF2-HMAC-SHA256, 600,000 iterations (OWASP 2023 baseline
# for stdlib-only deployments — argon2 / bcrypt would require a new
# dependency). PINs are short and bruteforceable in seconds at GPU rates,
# but the per-row salt + iteration cost stops *bulk* offline cracking.
#
# Storage formats handled by `verify_pin`:
#   pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>   (current)
#   <bare 4-6 digit string>                            (legacy)
# A successful legacy verify must trigger a rehash (caller's responsibility);
# `needs_rehash` returns True for any non-current encoding.

PBKDF2_ALGO = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000


def hash_pin(pin: str) -> str:
    """Hash a fresh PIN with PBKDF2-HMAC-SHA256 + a 16-byte random salt."""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac(
        "sha256",
        (pin or "").encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return f"{PBKDF2_ALGO}${PBKDF2_ITERATIONS}${salt}${h}"


def _verify_pbkdf2_pin(pin: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != PBKDF2_ALGO:
        return False
    try:
        iterations = int(parts[1])
    except ValueError:
        return False
    salt = parts[2]
    expected_hex = parts[3]
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        (pin or "").encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(expected_hex, actual)


def verify_pin(pin: str, stored: str) -> bool:
    """Verify a submitted PIN against a stored value.

    Accepts both the current `pbkdf2_sha256$...` encoding and legacy
    plaintext (bare digits or any other previously-stored value). Constant-
    time within each branch. Callers that detect a successful legacy verify
    should rehash via `hash_pin` and persist — see `needs_rehash`.
    """
    if not stored:
        return False
    if stored.startswith(PBKDF2_ALGO + "$"):
        return _verify_pbkdf2_pin(pin or "", stored)
    # Legacy plaintext — constant-time byte compare so we don't leak the
    # PIN's prefix length via timing.
    return hmac.compare_digest(
        (pin or "").encode("utf-8"),
        stored.encode("utf-8"),
    )


def needs_rehash(stored: str) -> bool:
    """True if the stored PIN should be upgraded on the next successful login.

    Triggers on:
      * empty / missing values (caller is expected to skip the rehash path
        in that case; we still return True so the contract is symmetrical),
      * any non-pbkdf2_sha256 encoding (i.e. legacy plaintext),
      * pbkdf2_sha256 entries with fewer iterations than today's baseline.
    """
    if not stored or not stored.startswith(PBKDF2_ALGO + "$"):
        return True
    parts = stored.split("$")
    try:
        iterations = int(parts[1])
    except (IndexError, ValueError):
        return True
    return iterations < PBKDF2_ITERATIONS
