"""
services/payment.py
Payment utility functions for UPI QR code generation and Razorpay order creation.
"""
import hashlib
import hmac
import logging
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


def generate_upi_qr_url(upi_id: str, upi_name: str, amount: float, order_ref: str) -> str:
    """
    Build a UPI deep link and return a QR code image URL via qrserver.com.
    The customer's browser loads this directly as an img src.
    """
    upi_link = (
        f"upi://pay?pa={quote(upi_id)}"
        f"&pn={quote(upi_name)}"
        f"&am={amount:.2f}"
        f"&tn={quote(order_ref)}"
        f"&cu=INR"
    )
    encoded = quote(upi_link, safe="")
    return f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&margin=10&data={encoded}"


async def create_razorpay_order(
    key_id: str,
    key_secret: str,
    amount_paise: int,
    receipt: str,
    notes: dict | None = None,
) -> dict | None:
    """
    Create a Razorpay order via their API (standard checkout flow).
    Returns the JSON response dict or None on error.
    """
    if not key_id or not key_secret:
        logger.error("Razorpay key_id or key_secret not configured")
        return None

    payload = {
        "amount": int(amount_paise),
        "currency": "INR",
        "receipt": receipt,
        "notes": notes or {},
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.razorpay.com/v1/orders",
                json=payload,
                auth=(key_id, key_secret),
            )
            if resp.status_code == 200:
                return resp.json()
            logger.error(
                "Razorpay order creation failed: status=%s body=%s",
                resp.status_code, resp.text,
            )
            return None
    except Exception as e:
        logger.error("Razorpay order creation exception: %s", e)
        return None


def verify_razorpay_payment_signature(
    order_id: str, payment_id: str, signature: str, key_secret: str
) -> bool:
    """
    Verify the Razorpay checkout payment signature.
    Computes HMAC-SHA256 of "{order_id}|{payment_id}" with key_secret,
    compares to signature using constant-time comparison.
    """
    if not order_id or not payment_id or not signature or not key_secret:
        return False
    try:
        message = f"{order_id}|{payment_id}"
        expected = hmac.new(
            key_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False
