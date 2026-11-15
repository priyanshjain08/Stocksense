from __future__ import annotations

import uuid

import jwt
from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import Forbidden, Unauthorized
from app.core.security import decode_access_token
from app.database.base import get_db
from app.modules.common import Ctx
from app.modules.models import Membership, User


def current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Missing bearer token.")
    try:
        claims = decode_access_token(authorization[7:], get_settings().secret_key)
        user = db.get(User, uuid.UUID(claims["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        raise Unauthorized("Invalid or expired token.")
    if not user or user.status != "active" or user.token_version != claims.get("tv"):
        raise Unauthorized("Session is no longer valid.")
    return user


def ctx_dep(x_organization_id: str | None = Header(default=None), user: User = Depends(current_user),
            db: Session = Depends(get_db)) -> Ctx:
    """Active organization comes from X-Organization-Id and is ALWAYS verified against the user's memberships."""
    if not x_organization_id:
        raise Forbidden("X-Organization-Id header is required.")
    try:
        org_id = uuid.UUID(x_organization_id)
    except ValueError:
        raise Forbidden("Invalid organization id.")
    m = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.organization_id == org_id,
                                           Membership.status == "active"))
    if not m:
        raise Forbidden("You are not a member of this organization.")  # same answer whether or not it exists
    return Ctx(db=db, user_id=user.id, org_id=org_id, role=m.role, branch_ids=list(m.branch_ids or []))
