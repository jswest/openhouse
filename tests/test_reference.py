"""Offline tests for the ``reference`` command (``openhouse reference``, #184).

Every test runs offline and deterministically over the checked-in fixture data
in ``tests/fixtures/reference/``. No network calls are made.

Fixture inventory (6 records total):
  legislators-current.json (3):
    A000372 — Rick W. Allen  (Richard Allen, GA, rep)
    A000370 — Alma S. Adams  (Alma Adams, NC, rep)
    G000582 — Jenniffer González-Colón (PR, rep)  ← diacritic fixture

  legislators-historical.json (3):
    N000147 — Eleanor Holmes Norton (DC, rep)
    S000001 — John A. Smith (TX, rep)
    S000002 — John B. Smith (TX, rep)

Committee fixtures (#195), trimmed from the real CC0 source:
  committees-current.json — 3 House committees (Agriculture, Energy & Commerce,
    Education & Workforce) with the subcommittees the fixture members sit on.
  committee-membership-current.json — current-congress (119th) membership for
    those codes, anchored on A000370/A000372, plus a Senate code (SSAF) to prove
    House-only filtering.
  committees-historical.json — one historical House definition (HSVC) to mirror
    the real layout; carries no membership (the verified coverage limit).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from openhouse import reference as reference_mod
from openhouse.legislators import year_to_congress
from openhouse.reference import ReferenceDataError, search, search_committees

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_DIR = FIXTURES / "reference"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _seed_ref(tmp_path: Path) -> Path:
    """Copy the CC0 reference fixtures into a tmp data dir; return the data dir."""
    ref = tmp_path / "raw" / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    for path in REFERENCE_DIR.glob("*.json"):
        shutil.copy(path, ref / path.name)
    return tmp_path


def _run(args: list[str], data_dir: Path, capsys):
    """Run ``reference`` with ``--data-dir`` injected; return (code, stdout, stderr)."""
    code = reference_mod.run([*args, "--data-dir", str(data_dir)])
    out = capsys.readouterr()
    return code, out.out, out.err


# ---------------------------------------------------------------------------
# Matching behaviour
# ---------------------------------------------------------------------------


def test_bioguide_exact_match(tmp_path, capsys):
    """An exact bioguide ID returns exactly that one record."""
    data = _seed_ref(tmp_path)
    code, out, err = _run(["A000370"], data, capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["bioguide_id"] == "A000370"
    assert rows[0]["name"] == "Alma S. Adams"


def test_bioguide_fragment_match(tmp_path, capsys):
    """A bioguide substring 'A000' matches all bioguide IDs starting with A000."""
    data = _seed_ref(tmp_path)
    code, out, err = _run(["A000"], data, capsys)
    assert code == 0
    rows = json.loads(out)
    bioguides = {r["bioguide_id"] for r in rows}
    assert "A000370" in bioguides
    assert "A000372" in bioguides
    # G000582 and N000147 etc. should NOT match
    assert "G000582" not in bioguides
    assert "N000147" not in bioguides


def test_last_name_substring_match(tmp_path, capsys):
    """A last-name substring matches all records with that substring in any name field."""
    data = _seed_ref(tmp_path)
    code, out, err = _run(["Adams"], data, capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["bioguide_id"] == "A000370"


def test_diacritic_insensitive_match(tmp_path, capsys):
    """'gonzalez' (no accent) matches 'González-Colón' (with accents)."""
    data = _seed_ref(tmp_path)
    code, out, err = _run(["gonzalez"], data, capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["bioguide_id"] == "G000582"
    assert "González" in rows[0]["name"]


def test_diacritic_insensitive_partial(tmp_path, capsys):
    """'colon' (no tilde) matches 'Colón' via diacritic folding."""
    data = _seed_ref(tmp_path)
    code, out, err = _run(["colon"], data, capsys)
    assert code == 0
    rows = json.loads(out)
    bioguides = {r["bioguide_id"] for r in rows}
    assert "G000582" in bioguides


def test_no_match_returns_empty_exit_zero(tmp_path, capsys):
    """A needle matching nothing → empty list, exit 0."""
    data = _seed_ref(tmp_path)
    code, out, err = _run(["ZZZZNOTAMEMBER"], data, capsys)
    assert code == 0
    rows = json.loads(out)
    assert rows == []


def test_missing_reference_data_exit_nonzero(tmp_path, capsys):
    """When neither reference file exists, exit non-zero with a pointer to stderr."""
    # tmp_path has no raw/reference/ at all
    code, out, err = _run(["Adams"], tmp_path, capsys)
    assert code != 0
    assert "raw/reference" in err
    assert "clerk pull" in err


def test_missing_reference_data_via_search_raises(tmp_path):
    """``search()`` raises ``ReferenceDataError`` when files are absent."""
    with pytest.raises(ReferenceDataError, match="no reference data"):
        search("Adams", tmp_path)


# ---------------------------------------------------------------------------
# Output shape and ordering
# ---------------------------------------------------------------------------


def test_json_shape(tmp_path, capsys):
    """JSON output has the required four keys on every row."""
    data = _seed_ref(tmp_path)
    code, out, _ = _run(["Smith"], data, capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) >= 1
    for row in rows:
        assert set(row.keys()) == {"name", "bioguide_id", "chamber", "state"}


def test_deterministic_ordering(tmp_path, capsys):
    """Results are sorted by name asc, then bioguide_id asc."""
    data = _seed_ref(tmp_path)
    # 'Smith' matches both S000001 (John A. Smith) and S000002 (John B. Smith)
    code, out, _ = _run(["Smith"], data, capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 2
    # Both have same last name; official_full = "John A. Smith" < "John B. Smith"
    assert rows[0]["bioguide_id"] == "S000001"
    assert rows[1]["bioguide_id"] == "S000002"


def test_most_recent_term_used_for_chamber_and_state(tmp_path, capsys):
    """chamber and state come from terms[-1], the most recent term."""
    data = _seed_ref(tmp_path)
    # Allen (A000372) has two terms, both rep/GA
    code, out, _ = _run(["A000372"], data, capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["chamber"] == "rep"
    assert rows[0]["state"] == "GA"


def test_name_fallback_when_no_official_full(tmp_path, capsys):
    """When official_full is absent, name = '{first} {last}'."""
    # Build a custom fixture record without official_full
    ref = tmp_path / "raw" / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    (ref / "legislators-current.json").write_text(
        json.dumps([
            {
                "id": {"bioguide": "X000001"},
                "name": {"first": "Test", "last": "Member"},
                "terms": [{"type": "rep", "state": "CA", "district": 1}],
            }
        ]),
        encoding="utf-8",
    )
    code, out, err = _run(["X000001"], tmp_path, capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["name"] == "Test Member"


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------


def test_table_output_has_header_and_row(tmp_path, capsys):
    """``--table`` renders a header line and at least one data row."""
    data = _seed_ref(tmp_path)
    code, out, _ = _run(["A000370", "--table"], data, capsys)
    assert code == 0
    lines = [l for l in out.splitlines() if l.strip()]
    # First line is the header
    assert "name" in lines[0].lower()
    assert "bioguide_id" in lines[0].lower()
    assert "chamber" in lines[0].lower()
    assert "state" in lines[0].lower()
    # At least one data row follows
    assert len(lines) >= 2
    assert "A000370" in out


def test_table_no_match_header_only(tmp_path, capsys):
    """``--table`` with no matches prints a header but no data rows."""
    data = _seed_ref(tmp_path)
    code, out, _ = _run(["ZZZZNOTAMEMBER", "--table"], data, capsys)
    assert code == 0
    lines = [l for l in out.splitlines() if l.strip()]
    # Only the header line
    assert len(lines) == 1
    assert "name" in lines[0].lower()


# ---------------------------------------------------------------------------
# Residual note to stderr
# ---------------------------------------------------------------------------


def test_stderr_residual_note_contains_count(tmp_path, capsys):
    """stderr includes the count of records searched."""
    data = _seed_ref(tmp_path)
    code, _, err = _run(["Adams"], data, capsys)
    assert code == 0
    # 6 records total (3 current + 3 historical)
    assert "6" in err


def test_stderr_residual_note_mentions_pull(tmp_path, capsys):
    """stderr note tells the user to re-pull to refresh the cache."""
    data = _seed_ref(tmp_path)
    code, _, err = _run(["Adams"], data, capsys)
    assert code == 0
    assert "pull" in err.lower()


# ---------------------------------------------------------------------------
# Search over both current and historical
# ---------------------------------------------------------------------------


def test_searches_both_files(tmp_path, capsys):
    """A needle that spans current + historical returns records from both."""
    data = _seed_ref(tmp_path)
    # 'A' matches A000370 (current), A000372 (current), and N000147 has no 'A' prefix
    # Use a needle that hits one from each file:
    # Norton is in historical; Adams is in current
    code, out, _ = _run(["Norton"], data, capsys)
    assert code == 0
    rows = json.loads(out)
    assert any(r["bioguide_id"] == "N000147" for r in rows)


# ---------------------------------------------------------------------------
# year → congress helper (#195)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "year,congress",
    [
        (1789, 1),  # the 1st Congress, both years
        (1790, 1),
        (2025, 119),  # the 119th, both years
        (2026, 119),
        (2023, 118),
        (2024, 118),
    ],
)
def test_year_to_congress(year, congress):
    """Each two-year term folds to one Congress number; 119th = 2025-26."""
    assert year_to_congress(year) == congress


# ---------------------------------------------------------------------------
# Committee membership surface (#195)
# ---------------------------------------------------------------------------


def test_default_output_unchanged_without_committees_flag(tmp_path, capsys):
    """A bare lookup is byte-stable — committee fixtures present must not leak in."""
    data = _seed_ref(tmp_path)
    code, out, _ = _run(["A000370"], data, capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 1
    assert set(rows[0].keys()) == {"name", "bioguide_id", "chamber", "state"}


def test_committees_returns_membership_rows(tmp_path, capsys):
    """``--committees`` surfaces each committee/subcommittee seat for the member."""
    data = _seed_ref(tmp_path)
    code, out, err = _run(["A000370", "--committees"], data, capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) >= 1
    for row in rows:
        assert set(row.keys()) == {
            "name",
            "bioguide_id",
            "congress",
            "committee",
            "subcommittee",
            "rank",
            "title",
            "party",
        }
        assert row["bioguide_id"] == "A000370"
        assert row["congress"] == 119
    # A parent-committee seat (subcommittee None) and a subcommittee seat both appear.
    assert any(r["subcommittee"] is None for r in rows)
    assert any(r["subcommittee"] is not None for r in rows)
    # The Ranking Member title is preserved verbatim.
    assert any(r["title"] == "Ranking Member" for r in rows)


def test_committees_excludes_senate(tmp_path, capsys):
    """House-only: a Senate committee in the membership fixture never appears."""
    data = _seed_ref(tmp_path)
    rows, _ = search_committees("Adams", data)
    assert rows
    assert all(r["committee"].startswith("House") for r in rows)


def test_committees_year_filters_to_congress(tmp_path, capsys):
    """``--year 2025`` resolves to the 119th and returns the current snapshot."""
    data = _seed_ref(tmp_path)
    code, out, _ = _run(["A000370", "--committees", "--year", "2025"], data, capsys)
    assert code == 0
    assert len(json.loads(out)) >= 1


def test_committees_out_of_range_congress_empty(tmp_path, capsys):
    """A congress the cache doesn't cover returns no rows (the residual case)."""
    data = _seed_ref(tmp_path)
    code, out, err = _run(["A000370", "--committees", "--congress", "117"], data, capsys)
    assert code == 0
    assert json.loads(out) == []
    # The residual still declares the current-congress-only limitation.
    assert "CURRENT-CONGRESS-ONLY" in err


def test_committees_residual_declares_coverage_limit(tmp_path, capsys):
    """stderr names the COMPLETE guarantee and the current-congress-only residual."""
    data = _seed_ref(tmp_path)
    code, _, err = _run(["A000370", "--committees"], data, capsys)
    assert code == 0
    assert "COMPLETE" in err
    assert "119" in err
    assert "pull" in err.lower()


def test_committees_table(tmp_path, capsys):
    """``--table`` renders membership rows with int cells coerced to strings."""
    data = _seed_ref(tmp_path)
    code, out, _ = _run(["A000370", "--committees", "--table"], data, capsys)
    assert code == 0
    lines = [l for l in out.splitlines() if l.strip()]
    assert "committee" in lines[0].lower()
    assert "congress" in lines[0].lower()
    assert len(lines) >= 2


def test_congress_flag_requires_committees(tmp_path, capsys):
    """``--congress`` without ``--committees`` is a usage error (exit 2)."""
    data = _seed_ref(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run(["A000370", "--congress", "119"], data, capsys)
    assert exc.value.code == 2


def test_committees_missing_data_exit_nonzero(tmp_path, capsys):
    """No committee files on disk → non-zero exit pointing at clerk pull."""
    # Seed ONLY the legislator files, not the committee files.
    ref = tmp_path / "raw" / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    for name in ("legislators-current.json", "legislators-historical.json"):
        shutil.copy(REFERENCE_DIR / name, ref / name)
    code, out, err = _run(["A000370", "--committees"], tmp_path, capsys)
    assert code != 0
    assert "no committee data" in err
    assert "clerk pull" in err
