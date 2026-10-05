from importlib.metadata import version


def test_core_and_gateway_share_a_version() -> None:
    # The release workflow tags both packages with one version.
    assert version("thermaestro") == version("thermaestro-gateway")
