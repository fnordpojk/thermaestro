from importlib.metadata import requires, version


def test_core_and_gateway_share_a_version() -> None:
    # The release workflow tags both packages with one version.
    assert version("thermaestro") == version("thermaestro-gateway")


def test_the_core_pins_the_gateway_to_its_own_version() -> None:
    assert f"thermaestro-gateway=={version('thermaestro')}" in (requires("thermaestro") or [])
