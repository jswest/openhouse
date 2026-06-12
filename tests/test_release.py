"""Tests for the release helper (`.claude/skills/release/release.py`).

The release script lives beside its `SKILL.md` (the GH-0020 pattern), not as an
installed module, so it's loaded by path. Only the pure functions — version
arithmetic and notes assembly — are exercised here; the git/gh side effects are
left to manual use via the `/release` skill.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_RELEASE_PATH = (
    Path(__file__).resolve().parents[1]
    / ".claude" / "skills" / "release" / "release.py"
)
_spec = importlib.util.spec_from_file_location("release", _RELEASE_PATH)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def test_parse_version_tag():
    assert release.parse_version_tag("v0.3.2") == (0, 3, 2)
    assert release.parse_version_tag("0.3.2") == (0, 3, 2)
    assert release.parse_version_tag("v0.3.0+g123abc") == (0, 3, 0)


def test_parse_version_tag_rejects_malformed():
    with pytest.raises(ValueError):
        release.parse_version_tag("v0.3")


def test_compute_next_version_baseline():
    # No prior tag → baseline at the current schema, patch 0.
    assert release.compute_next_version(3, None) == "0.3.0"


def test_compute_next_version_patch_bump():
    # Same schema as the last tag → patch increments.
    assert release.compute_next_version(3, "v0.3.0") == "0.3.1"
    assert release.compute_next_version(3, "v0.3.4") == "0.3.5"


def test_compute_next_version_schema_bump_resets_patch():
    # Schema moved since the last tag → new minor, patch resets to 0.
    assert release.compute_next_version(4, "v0.3.7") == "0.4.0"


def test_compute_next_version_carries_major():
    assert release.compute_next_version(3, "v1.3.2") == "1.3.3"
    assert release.compute_next_version(4, "v1.3.2") == "1.4.0"


def test_schema_moved():
    assert release.schema_moved(3, None) is False
    assert release.schema_moved(3, "v0.3.0") is False
    assert release.schema_moved(4, "v0.3.0") is True


def test_build_release_notes_baseline():
    notes = release.build_release_notes([], schema_from=None, schema_to=None)
    assert "Baseline release." in notes
    assert "⚠️" not in notes


def test_build_release_notes_lists_commits():
    notes = release.build_release_notes(
        ["fix: a thing", "feat: another"], schema_from=None, schema_to=None
    )
    assert "- fix: a thing" in notes
    assert "- feat: another" in notes


def test_build_release_notes_schema_bump_banner():
    notes = release.build_release_notes(
        ["feat: new schedule"], schema_from=3, schema_to=4
    )
    assert "3 → 4" in notes
    assert "re-run `openhouse parse`" in notes
    assert "- feat: new schedule" in notes
