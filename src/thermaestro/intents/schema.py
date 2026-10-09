"""The JSON Schema of an intent and of a level, generated from the types, for anything that
writes intents from outside Python. The checked-in copy is `data/intent.schema.json`."""

import json
from importlib import resources
from typing import Any

from pydantic.json_schema import models_json_schema

from .model import Intent, Level

FILE = "intent.schema.json"


def generate() -> dict[str, Any]:
    _, top = models_json_schema([(Intent, "validation"), (Level, "validation")])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Thermaestro intent",
        "description": "What a household wants, and the levels it names.",
        "$defs": top["$defs"],
        "oneOf": [{"$ref": "#/$defs/Intent"}, {"$ref": "#/$defs/Level"}],
    }


def dumps(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def shipped() -> str:
    return resources.files(__package__).joinpath("data", FILE).read_text(encoding="utf-8")
