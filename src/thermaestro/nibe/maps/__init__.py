"""Nibe register maps: which registers each model has, and how their words decode."""

from thermaestro.nibe.maps.codec import (
    Decoded,
    EncodeError,
    Status,
    UnknownSize,
    decode,
    encode,
    words,
)
from thermaestro.nibe.maps.model import (
    Disagreement,
    ModelMap,
    Register,
    RegisterMap,
    Size,
    from_json,
    load,
    load_file,
)

__all__ = [
    "Decoded",
    "Disagreement",
    "EncodeError",
    "ModelMap",
    "Register",
    "RegisterMap",
    "Size",
    "Status",
    "UnknownSize",
    "decode",
    "encode",
    "from_json",
    "load",
    "load_file",
    "words",
]
