"""The JSON Schema of every message, generated from the types, for plugins written in
other languages. The checked-in copy is `data/thermaestro-cap-<version>.schema.json`."""

import json
from importlib import resources
from typing import Any

from .messages import MESSAGES, PROTOCOL, VERSION

FILE = f"{PROTOCOL}-{VERSION}.schema.json"


def generate() -> dict[str, Any]:
    schema = _readable_definitions(MESSAGES.json_schema())
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{PROTOCOL} {VERSION}",
        "description": "One message between the Thermaestro core and a plugin; over a socket, "
        "one message per line.",
        **schema,
    }


def _readable_definitions(schema: dict[str, Any]) -> dict[str, Any]:
    # Generic models' definitions are keyed by an internal name (`Knowledge_float_`);
    # their titles carry the readable one (`KnowledgeFloat`). Key them by the title.
    renames = {
        key: d["title"]
        for key, d in schema["$defs"].items()
        if key.startswith("Knowledge_") and d.get("title", key) not in schema["$defs"]
    }
    text = json.dumps(schema)
    for old, new in renames.items():
        text = text.replace(f'"#/$defs/{old}"', f'"#/$defs/{new}"')
    renamed: dict[str, Any] = json.loads(text)
    renamed["$defs"] = dict(sorted((renames.get(k, k), d) for k, d in renamed["$defs"].items()))
    return renamed


def dumps(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def shipped() -> str:
    return resources.files(__package__).joinpath("data", FILE).read_text(encoding="utf-8")
