"""The bundler: what leaves the source tree, and in what shape.

The bundle is the integration alone — ``libkp`` is a manifest requirement Home
Assistant installs from PyPI, not something that ships inside the component.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import build


@pytest.fixture(autouse=True)
def dist_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build into the test's own directory, never the working tree's dist/."""
    dist = tmp_path / "dist"
    monkeypatch.setattr(build, "DIST", dist)
    return dist


def test_the_component_ships_with_its_brand(tmp_path: Path) -> None:
    """The manifest, the translations and the icons Home Assistant shows."""
    build.build()
    staged = tmp_path / "dist" / "custom_components" / "kemper"

    assert (staged / "manifest.json").is_file()
    assert (staged / "translations" / "en.json").is_file()
    assert (staged / "brand" / "icon.png").is_file()


def test_no_library_is_vendored(tmp_path: Path) -> None:
    """libkp arrives from PyPI; a copy inside the component would shadow it."""
    build.build()
    staged = tmp_path / "dist" / "custom_components" / "kemper"

    assert not (staged / "libkp").exists()
    assert not list(staged.rglob("_generated.py"))


def test_nothing_that_should_not_ship_ships(tmp_path: Path) -> None:
    """Bytecode caches and the integration's tests stay behind."""
    build.build()
    staged = tmp_path / "dist" / "custom_components" / "kemper"

    assert not list(staged.rglob("__pycache__"))
    assert not list(staged.rglob("*.pyc"))
    assert not list(staged.rglob("tests"))


def test_the_zip_unpacks_into_a_config_directory(tmp_path: Path) -> None:
    """Its paths are relative to the configuration directory, ready to unzip."""
    archive = build.build()
    assert archive.name == f"kemper-{build.version()}.zip"

    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert "custom_components/kemper/manifest.json" in names
    assert "custom_components/kemper/translations/en.json" in names
    assert "custom_components/kemper/brand/icon.png" in names


def test_install_replaces_an_existing_copy(tmp_path: Path) -> None:
    """Installing twice leaves one copy, not a merge of two."""
    build.build()
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    target = build.install(config_dir)
    stale = target / "stale.py"
    stale.write_text("# left over from an older version\n", encoding="utf-8")

    build.install(config_dir)
    assert (target / "manifest.json").is_file()
    assert not stale.exists()
