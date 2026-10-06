from pydantic import ValidationError


class StoreError(Exception):
    """Something stored can't be used. The message names the file and the field."""


def name_fields(where: str, e: ValidationError) -> StoreError:
    """A validation error as one line per field, never quoting the value given: it may
    be a secret typed into the wrong place."""
    lines = []
    for err in e.errors(include_input=False, include_url=False):
        field = ".".join(str(p) for p in err["loc"]) or "(the whole)"
        lines.append(f"{where}: {field}: {err['msg']}")
    return StoreError("\n".join(lines))
