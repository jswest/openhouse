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


# ---------------------------------------------------------------------------
# Schema-drift guard (GH-0043) — pure fingerprint() + check_drift().
# ---------------------------------------------------------------------------

from typing import Optional  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402


class _ModelV1(BaseModel):
    """A first-shape model (this docstring is noise the fingerprint must ignore)."""

    asset: str
    owner: Optional[str] = None


# Same NAME as _ModelV1 (so the by-name fingerprint key matches) but different
# prose: a different docstring and a Field(description=...). Both are documentation
# noise — the structure (fields, types, optionality) is identical to _ModelV1.
class _ModelV1RewordedDoc(BaseModel):
    """Totally different prose — but the structure is identical to ``_ModelV1``."""

    asset: str = Field(description="now has a Field description, still noise")
    owner: Optional[str] = None


_ModelV1RewordedDoc.__name__ = "_ModelV1"


class _ModelV2AddedField(BaseModel):
    """Same intent, but with one extra field — a real structural change."""

    asset: str
    owner: Optional[str] = None
    ticker: Optional[str] = None


def test_fingerprint_is_pure_and_stable():
    # Same models, called twice → identical hash (no wall-clock, no ordering noise).
    assert release.fingerprint([_ModelV1]) == release.fingerprint([_ModelV1])
    # Order of the model list doesn't matter.
    assert release.fingerprint([_ModelV1, _ModelV2AddedField]) == release.fingerprint(
        [_ModelV2AddedField, _ModelV1]
    )


def test_fingerprint_ignores_docstring_and_description_noise():
    # A reworded docstring / added Field(description=...) is NOT structural drift.
    assert release.fingerprint([_ModelV1]) == release.fingerprint([_ModelV1RewordedDoc])


def test_fingerprint_changes_on_structural_change():
    # Adding a field changes the shape → the fingerprint must move.
    assert release.fingerprint([_ModelV1]) != release.fingerprint([_ModelV2AddedField])


def test_check_drift_changed_model_static_version_is_drift():
    # The model fingerprint changed since the last tag, but SCHEMA_VERSION (4)
    # still matches the tag's minor → refuse to tag.
    msg = release.check_drift("new-fp", "old-fp", schema_version=4, last_tag="v0.4.0")
    assert msg is not None
    assert "SCHEMA_VERSION" in msg


def test_check_drift_version_bump_is_clean():
    # Fingerprint changed AND the schema int moved past the tag's minor → the
    # bump is the acknowledgement; no drift.
    assert (
        release.check_drift("new-fp", "old-fp", schema_version=5, last_tag="v0.4.0")
        is None
    )


def test_check_drift_non_schema_edit_is_clean():
    # Fingerprint unchanged (a non-schema edit, e.g. release.py or docs) at a
    # static version → no drift.
    assert (
        release.check_drift("same-fp", "same-fp", schema_version=4, last_tag="v0.4.0")
        is None
    )


def test_check_drift_no_prior_tag_or_fingerprint_is_clean():
    # Nothing to compare against → never drift (baseline release).
    assert release.check_drift("fp", None, schema_version=4, last_tag="v0.4.0") is None
    assert release.check_drift("fp", "old", schema_version=4, last_tag=None) is None


def test_committed_fingerprint_matches_live_models():
    # The committed openhouse/schemas.fingerprint must track the current models,
    # so a real release of the unchanged schema reads "no drift". A failure here
    # means someone changed schemas.py without refreshing the fingerprint.
    assert release.recorded_fingerprint() == release.live_fingerprint()
