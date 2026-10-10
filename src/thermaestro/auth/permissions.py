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
    "intent.temporary.create": "ask for something for a while: warmer, a bath, a boost",
    "intent.temporary.create.away": "say the house is away until a date",
    "intent.temporary.create.guests": "say there are guests until a date",
    "intent.handsoff.create": "stop Thermaestro changing the pump for up to 48 hours",
    "intent.standing.write": "set what the household always wants: bands, hot water, limits",
    "intent.levels.write": "add, change and remove levels",
    "intent.ranking.write": "change what gives way first",
    "intent.slider.write": "change how much comfort may give for savings",
    "intent.any.end": "end what someone else asked for",
    "plan.read": "see the plan and why",
    "levers.control": "put levers off, in shadow or in control",
}

MQTT_GROUP = "MQTT"
"""The group whose rights requests over MQTT have: none until the administrator gives
some. The broker isn't trusted to say who sent a message."""

STEP_UP = frozenset({"users.manage", "secrets.manage", "plugins.manage", "levers.control"})
"""Changes under these rights need the password entered again."""


def allows(granted: frozenset[str], permission: str) -> bool:
    return ALL in granted or permission in granted


def known(permission: str) -> bool:
    return permission == ALL or permission in PERMISSIONS
