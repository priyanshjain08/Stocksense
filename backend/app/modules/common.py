"""Request context (tenant + role + branch scope) and shared helpers."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import Forbidden, NotFound
from app.core.permissions import GLOBAL_ROLES, has_permission
from app.modules.models import AuditLog, Branch, DocumentCounter, Variant, Warehouse


@dataclass
class Ctx:
    db: Session
    user_id: uuid.UUID
    org_id: uuid.UUID
    role: str
    branch_ids: list[str]  # explicit branch restriction; empty for global roles

    def require(self, permission: str) -> None:
        if not has_permission(self.role, permission):
            raise Forbidden(f"Your role ({self.role}) lacks permission '{permission}'.")

    @property
    def allowed_branches(self) -> set[str] | None:
        """None => all branches in the organization."""
        if self.role in GLOBAL_ROLES:
            return None
        return set(self.branch_ids)

    def check_branch(self, branch_id: uuid.UUID) -> Branch:
        """Branch must belong to the active org AND be within the user's branch scope."""
        b = self.db.scalar(select(Branch).where(Branch.id == branch_id, Branch.organization_id == self.org_id))
        if not b:
            raise NotFound("Branch not found.")
        allowed = self.allowed_branches
        if allowed is not None and str(b.id) not in allowed:
            raise Forbidden("You do not have access to this branch.")
        return b

    def check_warehouse(self, warehouse_id: uuid.UUID) -> Warehouse:
        w = self.db.scalar(select(Warehouse).where(Warehouse.id == warehouse_id,
                                                   Warehouse.organization_id == self.org_id))
        if not w:
            raise NotFound("Warehouse not found.")
        self.check_branch(w.branch_id)
        return w

    def get_variant(self, variant_id: uuid.UUID) -> Variant:
        v = self.db.scalar(select(Variant).where(Variant.id == variant_id, Variant.organization_id == self.org_id))
        if not v:
            raise NotFound(f"Variant {variant_id} not found in this organization.")
        return v

    def audit(self, action: str, entity_type: str, entity_id, **meta) -> None:
        self.db.add(AuditLog(organization_id=self.org_id, actor_user_id=self.user_id, action=action,
                             entity_type=entity_type, entity_id=str(entity_id) if entity_id else None, meta=meta))


def D(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def money(v) -> Decimal:
    return D(v).quantize(Decimal("0.01"))


def next_number(ctx: Ctx, key: str, prefix: str) -> str:
    """Concurrency-safe document number: row-locked counter, e.g. S-000042. Numbers may skip if a transaction
    rolls back, but are never duplicated."""
    q = select(DocumentCounter).where(DocumentCounter.organization_id == ctx.org_id,
                                      DocumentCounter.key == key).with_for_update()
    row = ctx.db.scalar(q)
    if not row:
        try:
            with ctx.db.begin_nested():
                ctx.db.add(DocumentCounter(organization_id=ctx.org_id, key=key, value=0))
                ctx.db.flush()
        except IntegrityError:
            pass
        row = ctx.db.scalar(q)
    row.value += 1
    ctx.db.flush()
    return f"{prefix}-{row.value:06d}"
