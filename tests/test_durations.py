import pytest

from thermaestro.durations import text


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 s"),
        (0.5, "0.5 s"),
        (5, "5 s"),
        (5.0, "5 s"),
        (59, "59 s"),
        (60, "1 min"),
        (601, "10 min 1 s"),
        (1800.0, "30 min"),
        (3600, "1 h"),
        (8069, "2 h 14 min 29 s"),
        (86400, "1 d"),
        (90061, "1 d 1 h 1 min 1 s"),
        (172800 + 59.6, "2 d 1 min"),
    ],
)
def test_parts_that_are_zero_are_left_out(seconds: float, expected: str) -> None:
    assert text(seconds) == expected
