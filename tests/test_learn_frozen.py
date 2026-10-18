"""A model kept while heating is held off is still what decisions use."""

from thermaestro.learn import Learned


def model(state: str) -> Learned:
    return Learned(
        name="house.model",
        scope="pump:hp1/cs1",
        kind="model",
        value={"alpha": 0.1},
        state=state,
        starting=None,
    )


def test_a_frozen_model_is_used_as_it_was_proved() -> None:
    assert model("frozen").used == {"alpha": 0.1}
    assert model("proved").used == {"alpha": 0.1}
    assert model("learning").used is None
