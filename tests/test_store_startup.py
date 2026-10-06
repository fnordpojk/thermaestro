from pathlib import Path

import pytest

from thermaestro.store import Layout, StoreError, load_startup


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "thermaestro.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_missing_file_means_every_default(tmp_path: Path) -> None:
    startup = load_startup(tmp_path / "thermaestro.toml")
    assert startup.web.listen == "0.0.0.0"  # noqa: S104
    assert (startup.web.port, startup.paths.state) == (8080, None)


def test_the_file_is_read(tmp_path: Path) -> None:
    startup = load_startup(write(tmp_path, '[web]\nport = 8443\n[paths]\nstate = "/srv/t"\n'))
    assert startup.web.port == 8443
    layout = startup.layout(Layout(Path("/etc/thermaestro"), Path("/var/lib/thermaestro")))
    assert layout.database == Path("/srv/t/thermaestro.db")


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("[web]\nport = 70000\n", "web.port"),
        ('[web]\nport = "eighty"\n', "web.port"),
        ("[web]\nlisten_on = 1\n", "web.listen_on"),
        ("[gui]\n", "gui"),
    ],
)
def test_an_error_names_the_field(tmp_path: Path, text: str, names: str) -> None:
    with pytest.raises(StoreError, match=rf"thermaestro.toml: {names}: "):
        load_startup(write(tmp_path, text))


def test_broken_toml_says_where(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match=r"thermaestro.toml: .*line 2"):
        load_startup(write(tmp_path, "[web]\nport = = 1\n"))


def test_the_layout_follows_systemd_and_docker() -> None:
    assert Layout.from_environment({}) == Layout(
        Path("/etc/thermaestro"), Path("/var/lib/thermaestro")
    )
    docker = Layout.from_environment(
        {"CONFIGURATION_DIRECTORY": "/config", "STATE_DIRECTORY": "/data"}
    )
    assert (docker.startup, docker.secrets) == (
        Path("/config/thermaestro.toml"),
        Path("/data/secrets.json"),
    )
    several = Layout.from_environment({"STATE_DIRECTORY": "/var/lib/thermaestro:/var/lib/other"})
    assert several.maps == Path("/var/lib/thermaestro/maps")
