"""
utils/audit.py
Append-only audit log helper. Use audit() to record every privileged action.
Never raises — audit failures must never break the user's request.
"""
import json
import logging
from typing import Any, Optional
from app.models.database import MasterSession, AuditLog

logger = logging.getLogger(__name__)


def audit(
    action: str,
    *,
    actor: str = "system",
    actor_role: str = "system",
    slug: str = "",
    target: str = "",
    payload: Any = None,
    request=None,
) -> None:
    """
    Record a privileged action.

    Examples:
      audit("client.create", actor="admin", actor_role="superadmin",
            slug="whiteSugar", target="whiteSugar", payload={"plan":"pro"}, request=req)
      audit("payment.confirm.cash", actor=phone, actor_role="staff",
            slug=slug, target=order_id, payload={"total": total})

    Failures here are logged and swallowed; never break the calling request.
    """
    try:
        ip = ua = ""
        if request is not None:
            try:
                ip = request.client.host if request.client else ""
            except Exception:
                ip = ""
            ua = (request.headers.get("user-agent") or "")[:300]
        try:
            payload_str = json.dumps(payload, default=str)[:5000] if payload is not None else ""
        except Exception:
            payload_str = str(payload)[:5000]

        db = MasterSession()
        try:
            db.add(AuditLog(
                slug=slug or "", actor=actor[:200], actor_role=actor_role[:50],
                action=action[:100], target=str(target)[:200],
                payload=payload_str, ip=ip[:100], user_agent=ua,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"audit log failed action={action} slug={slug}: {e}")


def list_audit(slug: Optional[str] = None, limit: int = 200,
                action_prefix: Optional[str] = None) -> list[dict]:
    """Return recent audit entries (newest first), optionally filtered by slug/action."""
    db = MasterSession()
    try:
        q = db.query(AuditLog).order_by(AuditLog.id.desc())
        if slug is not None:
            q = q.filter(AuditLog.slug == slug)
        if action_prefix:
            q = q.filter(AuditLog.action.like(action_prefix + "%"))
        rows = q.limit(limit).all()
        return [{
            "id": r.id, "slug": r.slug, "actor": r.actor, "actor_role": r.actor_role,
            "action": r.action, "target": r.target, "payload": r.payload,
            "ip": r.ip, "user_agent": r.user_agent,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]
    finally:
        db.close()
