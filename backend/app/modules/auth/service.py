"""Registration, login, organization bootstrap, invitations."""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppError, Conflict, Forbidden, InvalidState, NotFound, Unauthorized
from app.core.permissions import ROLES
from app.core.security import (create_access_token, hash_password, hash_token, new_opaque_token,
                               validate_password_strength, verify_password)
from app.modules.common import Ctx
from app.modules.models import Branch, Invitation, Membership, Organization, User, Warehouse, utcnow

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RateLimiter:
    """In-process sliding window. Use Redis-backed limiting when running more than one API replica."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window, self.hits = limit, window_s, defaultdict(deque)

    def check(self, key: str) -> None:
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise AppError("Too many attempts. Try again later.", code="rate_limited")
        q.append(now)


auth_limiter = RateLimiter(limit=10, window_s=60)


def register(db: Session, email: str, password: str, full_name: str) -> User:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise InvalidState("Invalid email address.")
    try:
        validate_password_strength(password)
    except ValueError as e:
        raise InvalidState(str(e)) from e
    if db.scalar(select(User).where(User.email == email)):
        raise Conflict("An account with this email already exists.")
    u = User(email=email, password_hash=hash_password(password), full_name=full_name.strip())
    db.add(u)
    db.flush()
    return u


def login(db: Session, email: str, password: str) -> str:
    u = db.scalar(select(User).where(User.email == email.strip().lower()))
    # Same error for unknown user / wrong password / disabled account.
    if not u or u.status != "active" or not verify_password(password, u.password_hash):
        raise Unauthorized("Invalid email or password.")
    s = get_settings()
    return create_access_token(str(u.id), u.token_version, s.secret_key, s.access_token_minutes)


def logout_everywhere(db: Session, user: User) -> None:
    user.token_version += 1  # invalidates every issued token for this user


def change_password(db: Session, user: User, current: str, new: str) -> None:
    if not verify_password(current, user.password_hash):
        raise Unauthorized("Current password is incorrect.")
    try:
        validate_password_strength(new)
    except ValueError as e:
        raise InvalidState(str(e)) from e
    user.password_hash = hash_password(new)
    user.token_version += 1


def create_organization(db: Session, user: User, *, name: str, industry: str = "general_retail", currency: str = "INR",
                        timezone: str = "Asia/Kolkata", branch_name: str = "Main Store", branch_code: str = "MAIN",
                        is_demo: bool = False) -> Organization:
    org = Organization(name=name.strip(), industry=industry, currency=currency.upper(), timezone=timezone,
                       settings={}, is_demo=is_demo)
    db.add(org)
    db.flush()
    db.add(Membership(organization_id=org.id, user_id=user.id, role="owner", branch_ids=[]))
    br = Branch(organization_id=org.id, name=branch_name, code=branch_code)
    db.add(br)
    db.flush()
    db.add(Warehouse(organization_id=org.id, branch_id=br.id, name=f"{branch_name} Stockroom", code=f"{branch_code}-WH1"))
    db.flush()
    return org


def invite(ctx: Ctx, email: str, role: str, branch_ids: list[str] | None = None, days: int = 7) -> tuple[Invitation, str]:
    ctx.require("members.manage")
    if role not in ROLES:
        raise InvalidState(f"Unknown role. Choose from {sorted(ROLES)}.")
    if role == "owner" and ctx.role != "owner":
        raise Forbidden("Only an owner can invite another owner.")
    raw, h = new_opaque_token()
    inv = Invitation(organization_id=ctx.org_id, email=email.strip().lower(), role=role,
                     branch_ids=branch_ids or [], token_hash=h, expires_at=utcnow() + timedelta(days=days))
    ctx.db.add(inv)
    ctx.db.flush()
    ctx.audit("member.invite", "invitation", inv.id, email=inv.email, role=role)
    return inv, raw  # raw token is shown/emailed once; only its hash is stored


def accept_invitation(db: Session, user: User, raw_token: str) -> Membership:
    inv = db.scalar(select(Invitation).where(Invitation.token_hash == hash_token(raw_token)))
    if not inv or inv.accepted_at:
        raise NotFound("Invitation is invalid or already used.")
    exp = inv.expires_at if inv.expires_at.tzinfo else inv.expires_at.replace(tzinfo=utcnow().tzinfo)
    if exp < utcnow():
        raise InvalidState("Invitation has expired.")
    if inv.email != user.email:
        raise Forbidden("This invitation was issued to a different email address.")
    if db.scalar(select(Membership).where(Membership.organization_id == inv.organization_id,
                                          Membership.user_id == user.id)):
        raise Conflict("You are already a member of this organization.")
    m = Membership(organization_id=inv.organization_id, user_id=user.id, role=inv.role, branch_ids=inv.branch_ids)
    inv.accepted_at = utcnow()
    db.add(m)
    db.flush()
    return m
