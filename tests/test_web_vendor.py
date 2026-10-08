import hashlib
import json
import re
from pathlib import Path

import thermaestro

WEB = Path(thermaestro.__file__).parent / "web"
VENDOR = WEB / "static" / "vendor"


def manifest() -> dict[str, dict[str, str]]:
    data = json.loads((VENDOR / "vendor.json").read_text())
    return {
        name: file
        for package in data["packages"].values()
        for name, file in package["files"].items()
    }


def test_vendored_files_match_their_pinned_hashes() -> None:
    for name, file in manifest().items():
        digest = hashlib.sha256((VENDOR / name).read_bytes()).hexdigest()
        assert digest == file["sha256"], name


def test_nothing_unlisted_is_vendored() -> None:
    present = {p.name for p in VENDOR.iterdir() if p.is_file()} - {"vendor.json"}
    assert present == set(manifest())


def test_the_pages_load_only_vendored_or_own_files() -> None:
    static = WEB / "static"
    own = {p.relative_to(static).as_posix() for p in static.rglob("*") if p.is_file()}
    for template in (WEB / "templates").glob("*.html"):
        for src in re.findall(r'(?:src|href)="(/static/[^"]+)"', template.read_text()):
            name = src.removeprefix("/static/")
            if name.startswith("vendor/"):
                assert name.removeprefix("vendor/") in manifest(), (template.name, src)
            else:
                assert name in own, (template.name, src)
        assert not re.search(r'(?:src|href)="(?:https?:)?//', template.read_text()), template.name


def test_htmx_runs_without_eval() -> None:
    base = (WEB / "templates" / "base.html").read_text()
    config = re.search(r"<meta name=\"htmx-config\" content='([^']+)'>", base)
    assert config
    settings = json.loads(config.group(1))
    assert settings["allowEval"] is False
    assert settings["allowScriptTags"] is False
    assert settings["includeIndicatorStyles"] is False
