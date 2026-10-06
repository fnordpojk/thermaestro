"""Rights, named after what they allow. Granted to groups and to users; a token carries a
subset of its user's. Nothing is allowed unless a right says so."""

ALL = "*"

PERMISSIONS: dict[str, str] = {
    "points.read": "see devices, values and their history",
    "settings.read": "see the installation's settings",
    "settings.write": "change the installation's settings",
    "secrets.manage": "enter or replace secrets (never read them back)",
    "plugins.manage": "add, change and remove plugin instances",
    "users.manage": "add, change and remove users and their rights",
    "tokens.own": "create and revoke one's own API tokens",
    "audit.read": "read the audit log",
}

STEP_UP = frozenset({"users.manage", "secrets.manage", "plugins.manage"})
"""Changes under these rights need the password entered again."""


def allows(granted: frozenset[str], permission: str) -> bool:
    return ALL in granted or permission in granted


def known(permission: str) -> bool:
    return permission == ALL or permission in PERMISSIONS
