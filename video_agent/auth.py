"""Database-backed, tenant-bound API key authentication."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import secrets
import re

from fastapi import Header, HTTPException, Request
from sqlalchemy import select

from .db import ApiKey, ApiKeyAudit, Session


ALL_SCOPES = frozenset({"uploads:write", "videos:write", "videos:read", "questions:ask"})
PBKDF2_ITERATIONS = 600_000
SAFE_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


@dataclass(frozen=True)
class AuthContext:
    key_id: str
    tenant_id: str
    scopes: frozenset[str]


def hash_secret(secret: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_secret(secret: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def issue_key(*, tenant_id: str, name: str, scopes: list[str], expires_at=None,
              rotated_from_id: str | None = None, session=None) -> tuple[ApiKey, str]:
    if not SAFE_TENANT.fullmatch(tenant_id):
        raise ValueError("invalid tenant identifier")
    unknown = set(scopes) - ALL_SCOPES
    if unknown:
        raise ValueError(f"unknown scopes: {', '.join(sorted(unknown))}")
    prefix = f"vak_{secrets.token_hex(6)}"
    secret = secrets.token_urlsafe(32)
    record = ApiKey(name=name, tenant_id=tenant_id, prefix=prefix, secret_hash=hash_secret(secret),
                    scopes=sorted(set(scopes)), expires_at=expires_at, rotated_from_id=rotated_from_id)
    if session is not None:
        session.add(record)
        session.flush()
    return record, f"{prefix}.{secret}"


def _audit(session, request: Request, *, event: str, success: bool, key=None, detail=None):
    session.add(ApiKeyAudit(key_id=key.id if key else None, tenant_id=key.tenant_id if key else None,
        event=event, success=success, request_id=request.headers.get("X-Request-Id"),
        method=request.method, path=request.url.path,
        remote_addr=request.client.host if request.client else None, detail=detail or {}))


def _deny(session, request, status, message, *, event="authenticate", key=None, detail=None):
    _audit(session, request, event=event, success=False, key=key, detail=detail)
    session.commit()  # Authentication failures are still durable security events.
    raise HTTPException(status, message, headers={"WWW-Authenticate": "ApiKey"} if status == 401 else None)


def require_scopes(*required: str):
    required_set = frozenset(required)

    def authenticate(request: Request, x_api_key: str = Header(default=""),
                     x_tenant_id: str | None = Header(default=None)) -> str:
        prefix, separator, secret = x_api_key.partition(".")
        if not separator:
            prefix = "legacy_" + hashlib.sha256(x_api_key.encode()).hexdigest()[:12]
            secret = x_api_key
        with Session.begin() as session:
            key = session.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
            now = datetime.now(timezone.utc)
            valid = bool(key and verify_secret(secret, key.secret_hash))
            if not valid:
                _deny(session, request, 401, "invalid API key",
                      detail={"reason": "invalid_key", "prefix": prefix[:24]})
            if key.revoked_at is not None:
                _deny(session, request, 401, "revoked API key", key=key, detail={"reason": "revoked"})
            if key.expires_at is not None and key.expires_at <= now:
                _deny(session, request, 401, "expired API key", key=key, detail={"reason": "expired"})
            if x_tenant_id is not None and x_tenant_id != key.tenant_id:
                _deny(session, request, 403, "API key is not valid for requested tenant", key=key,
                      detail={"reason": "tenant_mismatch"})
            missing = required_set - set(key.scopes)
            if missing:
                _deny(session, request, 403, "insufficient scope", event="authorize", key=key,
                      detail={"missing_scopes": sorted(missing)})
            key.last_used_at = now
            _audit(session, request, event="authenticate", success=True, key=key,
                   detail={"scopes": sorted(required_set)})
            return key.tenant_id

    return authenticate


def ensure_bootstrap_key(secret: str, tenant_id: str) -> None:
    """Import the deprecated plain API_KEY once without storing its plaintext."""
    if not secret:
        return
    if not SAFE_TENANT.fullmatch(tenant_id):
        raise ValueError("invalid bootstrap tenant identifier")
    prefix = "legacy_" + hashlib.sha256(secret.encode()).hexdigest()[:12]
    with Session.begin() as session:
        if session.scalar(select(ApiKey.id).where(ApiKey.prefix == prefix)):
            return
        session.add(ApiKey(name="bootstrap compatibility key", tenant_id=tenant_id,
            prefix=prefix, secret_hash=hash_secret(secret), scopes=sorted(ALL_SCOPES)))
