from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from thermaestro.cap import assume, vocabulary
from thermaestro.cap.model import (
    CompetingFeature,
    Envelope,
    Knowledge,
    Lever,
    Param,
    Persistence,
    Range,
    Verify,
    Wear,
)

T = datetime(2026, 9, 29, 22, 28, 4, tzinfo=UTC)
READBACK = Verify(kind="readback", point="hp1/x.test.1")


def lever(**kw: object) -> Lever:
    fields: dict[str, object] = {
        "path": "hp1/cs1/heating.offset",
        "kind": "setting",
        "verify": READBACK,
        "touches": ("x.test.1",),
    }
    return Lever.model_validate(fields | kw)


def test_unknown_carries_no_value_and_a_value_carries_its_knowledge() -> None:
    assert Knowledge[float]().known == "unknown"
    assert Knowledge(value=1.0, known="reported", basis="a forum post").basis == "a forum post"
    with pytest.raises(ValidationError):
        Knowledge(value=1.0)
    with pytest.raises(ValidationError):
        Knowledge[float](known="documented")


def test_sentinels_never_arrive_as_numbers() -> None:
    def envelope(quality: str, value: float | None) -> Envelope:
        return Envelope.model_validate(
            {
                "point": "hp1/outdoor.temp",
                "value": value,
                "t_observed": T,
                "t_received": T,
                "quality": quality,
                "source": "measured",
            }
        )

    assert envelope("not_connected", None).value is None
    assert envelope("stale", 4.5).value == 4.5
    with pytest.raises(ValidationError, match="isn't connected"):
        envelope("not_connected", -3276.8)
    with pytest.raises(ValidationError, match="good value has a value"):
        envelope("good", None)


def test_a_lever_lists_what_it_touches_unless_it_is_unavailable() -> None:
    with pytest.raises(ValidationError, match="touches"):
        lever(touches=())
    feed = lever(path="hp1/cs1/room.temp_input", kind="feed", touches=(), unavailable="no route")
    assert feed.unavailable == "no route"


def test_baselines_by_default_for_settings_and_holds() -> None:
    assert lever().needs_baseline
    assert lever(kind="hold").needs_baseline
    assert not lever(kind="trigger").needs_baseline
    assert not lever(baseline=False).needs_baseline


def test_unknown_yields_the_conservative_defaults() -> None:
    a = assume(
        lever(
            params={"value": Param(type="number")},
            competing_features=(CompetingFeature(name="schedule"),),
        )
    )
    assert not a.works
    assert a.persistence == Persistence(kind="stored")
    assert a.wear == Wear(kind="flash")
    assert a.can_disable == {"schedule": False}
    assert a.ranges == {"value": None}  # only values already observed
    assert a.effect_delay_s is None  # long


def test_reported_knowledge_can_only_make_the_core_more_careful() -> None:
    reported = lever(
        works=Knowledge(value=True, known="reported"),
        wear=Knowledge(value=Wear(kind="none"), known="reported"),
        persistence=Knowledge(value=Persistence(kind="leased", period_s=300), known="reported"),
        competing_features=(
            CompetingFeature(name="smart", can_disable=Knowledge(value=True, known="reported")),
        ),
        effect_delay_s=Knowledge(value=1.0, known="reported"),
        params={
            "value": Param(
                type="number", range=Knowledge(value=Range(min=-10, max=10), known="reported")
            )
        },
    )
    a = assume(reported)
    assert not a.works
    assert a.wear == Wear(kind="flash")
    assert a.persistence == Persistence(kind="leased", period_s=300)  # renewing is careful
    assert a.can_disable == {"smart": False}
    assert a.ranges == {"value": None}
    assert a.effect_delay_s is None


def test_documented_verified_and_user_knowledge_is_used() -> None:
    for known in ("documented", "verified", "user"):
        a = assume(
            lever(
                works=Knowledge(value=True, known=known),
                wear=Knowledge(value=Wear(kind="none"), known=known),
                effect_delay_s=Knowledge(value=5.0, known=known),
                params={
                    "value": Param(
                        type="number", range=Knowledge(value=Range(min=-10, max=10), known=known)
                    )
                },
            )
        )
        assert (a.works, a.wear.kind, a.effect_delay_s) == (True, "none", 5.0)
        assert a.ranges == {"value": Range(min=-10, max=10)}


def test_refuted_works_is_not_working() -> None:
    assert not assume(lever(works=Knowledge(value=True, known="refuted"))).works
    refuted = lever(persistence=Knowledge(value=Persistence(kind="volatile"), known="refuted"))
    assert assume(refuted).persistence.kind == "stored"


@pytest.mark.parametrize(
    ("kind", "name", "unit"),
    [
        ("dhw_tank", "temp.top", "degC"),
        ("unit", "outdoor.temp", "degC"),
        ("unit", "heat.produced{purpose=dhw,by=compressor}", "kWh"),
        ("room", "carbon_dioxide", "ppm"),
        ("room", "temperature#2", "degC"),
        ("ventilation", "airflow", "m3/h"),
        ("room", "dew_point", "degC"),
    ],
)
def test_standard_points(kind: str, name: str, unit: str) -> None:
    standard = vocabulary.point(kind, name)
    assert standard is not None
    assert standard.unit == unit


@pytest.mark.parametrize(
    ("kind", "name", "lever_kind"),
    [
        ("dhw_tank", "block", "hold"),
        ("unit", "dhw.block", "hold"),
        ("dhw_tank", "mode", "setting"),
        ("climate_system", "heating.offset", "setting"),
        ("addition", "policy", "setting"),
        ("climate_system", "room.temp_input", "feed"),
        ("ventilation", "airflow_input", "feed"),
        ("unit", "alarm.reset", "trigger"),
    ],
)
def test_standard_levers(kind: str, name: str, lever_kind: str) -> None:
    standard = vocabulary.lever(kind, name)
    assert standard is not None
    assert lever_kind in standard.kinds


def test_names_outside_the_vocabulary() -> None:
    assert vocabulary.point("unit", "flow.temp") is None
    assert vocabulary.lever("climate_system", "block") is None  # only on its own area's node
    assert vocabulary.is_vendor("x.nibe.47134", "nibe")
    assert not vocabulary.is_vendor("x.nibe.47134", "ctc")
    assert vocabulary.LEVERS["alarm.reset"].user_only
