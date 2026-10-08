"""Durations as people read them: 8069 seconds is "2 h 14 min 29 s"."""

PARTS = (("d", 86400), ("h", 3600), ("min", 60), ("s", 1))


def text(seconds: float) -> str:
    """In days, hours, minutes and seconds, leaving out the parts that are 0. Under a
    minute, a fraction stays as it is: "0.5 s"."""
    if seconds < 60:
        return f"{seconds:g} s"
    rest = round(seconds)
    parts = []
    for unit, size in PARTS:
        n, rest = divmod(rest, size)
        if n:
            parts.append(f"{n} {unit}")
    return " ".join(parts)
