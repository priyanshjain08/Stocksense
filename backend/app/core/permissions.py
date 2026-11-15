"""Role -> permission model. Enforced server-side on every protected route."""
from __future__ import annotations

ALL = {
    "org.manage", "members.manage", "branches.manage", "products.read", "products.write",
    "inventory.read", "inventory.adjust", "inventory.count_approve", "inventory.transfer", "sales.read", "sales.create",
    "sales.cancel", "sales.return", "suppliers.read", "suppliers.write", "purchasing.read",
    "purchasing.create", "purchasing.approve", "purchasing.receive", "forecast.read", "forecast.run",
    "replenishment.read", "replenishment.decide", "analytics.read", "audit.read",
}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": set(ALL),
    "admin": ALL - {"org.manage"},
    "store_manager": {"products.read", "inventory.read", "inventory.adjust", "inventory.transfer", "sales.read",
                      "sales.create", "sales.cancel", "sales.return", "suppliers.read", "purchasing.read",
                      "forecast.read", "replenishment.read", "analytics.read"},
    "inventory_manager": {"products.read", "products.write", "inventory.read", "inventory.adjust", "inventory.count_approve",
                          "inventory.transfer", "purchasing.read", "purchasing.receive", "forecast.read",
                          "replenishment.read", "analytics.read", "sales.read"},
    "purchasing_manager": {"products.read", "inventory.read", "suppliers.read", "suppliers.write",
                           "purchasing.read", "purchasing.create", "purchasing.approve", "purchasing.receive",
                           "forecast.read", "forecast.run", "replenishment.read", "replenishment.decide",
                           "analytics.read"},
    "staff": {"products.read", "inventory.read", "sales.read", "sales.create", "sales.return"},
    "analyst": {"products.read", "inventory.read", "sales.read", "suppliers.read", "purchasing.read",
                "forecast.read", "replenishment.read", "analytics.read"},
}
# Roles that are not restricted to specific branches.
GLOBAL_ROLES = {"owner", "admin", "analyst", "purchasing_manager"}
ROLES = set(ROLE_PERMISSIONS)


def has_permission(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())
