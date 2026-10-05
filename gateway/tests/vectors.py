import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

VECTORS = Path(__file__).resolve().parents[2] / "testvectors"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((VECTORS / name).read_text())
    return data


def named[F: Callable[..., None]](cases: list[dict[str, Any]]) -> Callable[[F], F]:
    """Run a test once per vector, each named after the vector."""
    mark = pytest.mark.parametrize("case", cases, ids=[c["name"] for c in cases])

    def apply(test: F) -> F:
        # pytest types the mark's result as Any; it returns the function it was given.
        return cast("F", mark(test))

    return apply
