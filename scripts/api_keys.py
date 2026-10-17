"""Provision, rotate, revoke, and inspect tenant API keys."""
import argparse
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from video_agent.auth import ALL_SCOPES, issue_key
from video_agent.db import ApiKey, ApiKeyAudit, Session, init_db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--tenant", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--scope", action="append", choices=sorted(ALL_SCOPES))
    create.add_argument("--expires-in-days", type=int)
    rotate = commands.add_parser("rotate")
    rotate.add_argument("key_id")
    rotate.add_argument("--grace-hours", type=int, default=0)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("key_id")
    listing = commands.add_parser("list")
    listing.add_argument("--tenant")
    audit = commands.add_parser("audit")
    audit.add_argument("--tenant")
    audit.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    init_db()
    now = datetime.now(timezone.utc)
    with Session.begin() as session:
        if args.command == "create":
            expires = now + timedelta(days=args.expires_in_days) if args.expires_in_days else None
            record, plaintext = issue_key(tenant_id=args.tenant, name=args.name,
                scopes=args.scope or sorted(ALL_SCOPES), expires_at=expires, session=session)
            session.add(ApiKeyAudit(key_id=record.id, tenant_id=record.tenant_id,
                event="create", success=True, detail={"name": record.name, "scopes": record.scopes}))
            print(f"key_id={record.id}\napi_key={plaintext}")
            print("Save api_key now; it cannot be recovered from the database.")
        elif args.command == "rotate":
            old = session.get(ApiKey, args.key_id)
            if not old or old.revoked_at:
                raise SystemExit("active key not found")
            old.expires_at = now + timedelta(hours=args.grace_hours)
            record, plaintext = issue_key(tenant_id=old.tenant_id, name=f"{old.name} (rotated)",
                scopes=old.scopes, rotated_from_id=old.id, session=session)
            session.add(ApiKeyAudit(key_id=record.id, tenant_id=record.tenant_id,
                event="rotate", success=True,
                detail={"rotated_from_id": old.id, "grace_hours": args.grace_hours}))
            print(f"key_id={record.id}\napi_key={plaintext}\nold_key_expires_at={old.expires_at.isoformat()}")
        elif args.command == "revoke":
            key = session.get(ApiKey, args.key_id)
            if not key:
                raise SystemExit("key not found")
            key.revoked_at = now
            session.add(ApiKeyAudit(key_id=key.id, tenant_id=key.tenant_id,
                event="revoke", success=True, detail={}))
            print(f"revoked={key.id}")
        elif args.command == "list":
            query = select(ApiKey).order_by(ApiKey.created_at.desc())
            if args.tenant:
                query = query.where(ApiKey.tenant_id == args.tenant)
            for key in session.scalars(query):
                print(key.id, key.tenant_id, key.prefix, ",".join(key.scopes),
                      f"expires={key.expires_at or '-'}", f"revoked={key.revoked_at or '-'}")
        else:
            query = select(ApiKeyAudit).order_by(ApiKeyAudit.created_at.desc()).limit(args.limit)
            if args.tenant:
                query = query.where(ApiKeyAudit.tenant_id == args.tenant)
            for event in session.scalars(query):
                print(event.created_at, event.tenant_id or "-", event.event, event.success,
                      event.method or "-", event.path or "-", event.detail)


if __name__ == "__main__":
    main()
