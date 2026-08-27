"""The manifest, and the two places its claims have to hold.

The integration carries no copy of libkp: the manifest names a release, and
Home Assistant installs it from PyPI before loading the component. That makes
the requirement the seam — so it is pinned exactly, the tests run against that
exact version, and the brand HACS looks for is here.
"""

from __future__ import annotations

import json
from pathlib import Path

import libkp

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "kemper"
MANIFEST = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))


def test_libkp_is_pinned_to_an_exact_release() -> None:
    """A range would let Home Assistant load a libkp nothing here was run against."""
    requirements = MANIFEST["requirements"]
    assert [r for r in requirements if r.startswith("libkp==")] == requirements[:1]


def test_the_pinned_release_is_the_one_the_tests_run_against() -> None:
    """The installed library and the manifest cannot drift apart unnoticed."""
    pinned = next(r for r in MANIFEST["requirements"] if r.startswith("libkp"))
    assert pinned == f"libkp=={libkp.__version__}"


def test_hacs_finds_a_brand() -> None:
    """HACS wants a brand directory with at least an icon; Home Assistant 2026.3+ serves it."""
    assert (COMPONENT / "brand" / "icon.png").is_file()


def test_the_keys_hacs_requires_are_all_here() -> None:
    """https://hacs.xyz/docs/publish/integration/"""
    required = {"domain", "documentation", "issue_tracker", "codeowners", "name", "version"}
    assert required <= MANIFEST.keys()
