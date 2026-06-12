"""Offline tests for the CC0 congress-legislators identity join (#16).

These exercise :mod:`openhouse.legislators` directly against the checked-in
fixture subset under ``tests/fixtures/reference/`` — no network, deterministic.
The fixture holds a handful of real-shape legislator records that line up with
the FD fixtures (Allen GA12, Adams NC12, Norton DC, González-Colón PR) plus two
distinct John Smiths sharing TX-5 to prove the ambiguity guard.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from openhouse.legislators import load_legislator_index

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_DIR = FIXTURES / "reference"


def _seed(tmp_path: Path) -> Path:
    ref = tmp_path / "raw" / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    for path in REFERENCE_DIR.glob("*.json"):
        shutil.copy(path, ref / path.name)
    return tmp_path


def test_match_on_house_seat(tmp_path):
    idx = load_legislator_index(_seed(tmp_path))
    assert idx.match(last="Allen", state="GA", district=12) == "A000372"
    assert idx.match(last="Adams", state="NC", district=12) == "A000370"
    # Historical file is folded in too (Norton, DC at-large = district 0).
    assert idx.match(last="Norton", state="DC", district=0) == "N000147"


def test_match_ignores_diacritics_and_case(tmp_path):
    idx = load_legislator_index(_seed(tmp_path))
    # Reference spells "González-Colón"; the Clerk spells "Gonzalez-Colon".
    assert idx.match(last="Gonzalez-Colon", state="PR", district=0) == "G000582"
    assert idx.match(last="gonzalez-colon", state="pr", district=0) == "G000582"


def test_ambiguous_seat_matches_nothing(tmp_path):
    idx = load_legislator_index(_seed(tmp_path))
    # Two distinct John Smiths share TX-5 → ambiguous → no false-positive match.
    assert idx.match(last="Smith", state="TX", district=5) is None


def test_unknown_or_missing_seat_returns_none(tmp_path):
    idx = load_legislator_index(_seed(tmp_path))
    assert idx.match(last="Allen", state="GA", district=99) is None  # wrong district
    assert idx.match(last="Nobody", state="GA", district=12) is None  # wrong name
    assert idx.match(last="Allen", state=None, district=None) is None  # no seat


def test_missing_reference_files_yield_empty_index(tmp_path):
    # No reference cached → empty index, matches nothing (the join is optional).
    idx = load_legislator_index(tmp_path)
    assert idx.by_seat == {}
    assert idx.match(last="Allen", state="GA", district=12) is None
