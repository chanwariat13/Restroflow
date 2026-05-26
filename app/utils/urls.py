"""
utils/urls.py
Shared helper for resolving the public base URL of this RestroFlow instance.

Why this exists
---------------
Several places (admin approve, staff approve, client QR-code generation, the
WhatsApp bot) need to embed a link back into this service for customers to
follow — e.g. the menu URL the guest receives on WhatsApp after approval, or
the table-registration URL printed on the QR code on each table.

The previous code hardcoded "https://restroflow.coolify.yeshikasingh.cloud"
as the fallback, which silently broke every QR code and approval link
whenever the deployment moved domains. This helper centralises the
resolution so we only have one knob to turn.

Resolution order
----------------
1. The ``PUBLIC_BASE_URL`` env var, if set (e.g. "https://restro.example.com").
   This is the recommended setting for production deployments — set it once
   in your env config and forget about it.
2. The current request's ``base_url`` (FastAPI/Starlette resolves this from
   the Host header / proxy headers when ``ProxyHeadersMiddleware`` is
   active). This is a sensible fallback for dev and for instances that sit
   behind a single domain.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import Request


def get_public_base_url(request: Optional[Request] = None) -> str:
    """Return the public base URL for this RestroFlow instance, no trailing slash.

    Prefers the ``PUBLIC_BASE_URL`` env var; falls back to ``request.base_url``
    when an inbound request is available. Returns an empty string if neither
    is available — callers should guard for that case.
    """
    env_url = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if env_url:
        return env_url
    if request is not None:
        try:
            return str(request.base_url).rstrip("/")
        except Exception:
            return ""
    return ""
