"""Offline end-to-end tests for the ``parse`` command (#6).

``parse`` reads only ``raw/<year>/`` and writes ``parsed/<year>/`` — fully
offline, deterministic, re-runnable from raw with identical bytes. These build a
small constructed index under a tmp ``data_dir`` and assert the written
``filings.json`` / ``parse-manifest.json`` and the identity-collision heuristic.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from openhouse.parse import _detect_identity_warnings, parse, parse_year
from openhouse.index import build_filing_records
from openhouse.schemas import SCHEMA_VERSION

FIXTURES = Path(__file__).parent / "fixtures"
PARSE_XML = FIXTURES / "parse" / "2024FD.xml"

FETCHED_AT = "2026-06-11T12:00:00"


def _seed_year(data_dir: Path, year: int, xml_src: Path) -> None:
    """Lay the index XML where ``parse`` expects it: raw/<year>/<year>FD.xml."""
    raw = data_dir / "raw" / str(year)
    raw.mkdir(parents=True, exist_ok=True)
    shutil.copy(xml_src, raw / f"{year}FD.xml")


# --- identity warnings (SPEC §6.2) ----------------------------------------


def test_identity_warning_on_different_districts():
    records = build_filing_records(PARSE_XML, 2024)
    warnings = {w["filer_id"]: w for w in _detect_identity_warnings(records)}
    smith = warnings["tx.smith.john"]
    assert sorted(smith["doc_ids"]) == ["2001", "2002"]
    assert smith["districts"] == [5, 9]  # two districts → two people


def test_identity_warning_on_suffix_slug_collision():
    records = build_filing_records(PARSE_XML, 2024)
    warnings = {w["filer_id"]: w for w in _detect_identity_warnings(records)}
    # The "." suffix slugs to empty, so both share fl.jones.robert, but the raw
    # suffix differs → the slug collided rather than matched.
    assert "fl.jones.robert" in warnings
    assert sorted(warnings["fl.jones.robert"]["doc_ids"]) == ["3001", "3002"]


def test_same_person_many_filings_same_district_is_not_a_warning():
    records = build_filing_records(PARSE_XML, 2024)
    warnings = {w["filer_id"] for w in _detect_identity_warnings(records)}
    # Adams files three times (O, P, X) in NC12 — normal, not a collision.
    assert "nc.adams.alma" not in warnings


def test_lone_filing_never_warns():
    records = build_filing_records(PARSE_XML, 2024)
    warnings = {w["filer_id"] for w in _detect_identity_warnings(records)}
    assert "unk.doe.maryam" not in warnings


# --- parse_year / parse end-to-end ----------------------------------------


def test_parse_year_writes_filings_and_manifest(tmp_path):
    _seed_year(tmp_path, 2024, PARSE_XML)
    summary = parse_year(2024, data_dir=tmp_path, fetched_at=FETCHED_AT)

    filings_path = tmp_path / "parsed" / "2024" / "filings.json"
    manifest_path = tmp_path / "parsed" / "2024" / "parse-manifest.json"
    assert filings_path.exists()
    assert manifest_path.exists()

    filings = json.loads(filings_path.read_text())
    assert len(filings) == 8  # every <Member>, none dropped
    assert summary["total"] == 8

    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["generated_at"] == FETCHED_AT  # threaded, not a fresh clock
    assert manifest["year"] == 2024
    assert manifest["counts"]["total"] == 8
    # Filing-type tally: O x5 (1001,2001,2002,3001,3002), P/X/W x1 each.
    assert manifest["counts"]["by_filing_type"]["O"] == 5
    assert manifest["counts"]["by_filing_type"]["P"] == 1
    assert manifest["counts"]["by_filing_type"]["X"] == 1
    assert manifest["counts"]["by_filing_type"]["W"] == 1
    # Two collisions: Smith (districts) and Jones (suffix slug).
    warned = {w["filer_id"] for w in manifest["identity_warnings"]}
    assert warned == {"tx.smith.john", "fl.jones.robert"}


def test_parse_year_is_deterministic(tmp_path):
    _seed_year(tmp_path, 2024, PARSE_XML)
    parse_year(2024, data_dir=tmp_path, fetched_at=FETCHED_AT)
    first = (tmp_path / "parsed" / "2024" / "filings.json").read_bytes()
    first_manifest = (tmp_path / "parsed" / "2024" / "parse-manifest.json").read_bytes()
    # Re-run from the same raw → byte-identical output.
    parse_year(2024, data_dir=tmp_path, fetched_at=FETCHED_AT)
    assert (tmp_path / "parsed" / "2024" / "filings.json").read_bytes() == first
    assert (
        tmp_path / "parsed" / "2024" / "parse-manifest.json"
    ).read_bytes() == first_manifest


def test_parse_missing_year_is_clean_skip(tmp_path, capsys):
    # No raw/2025 seeded → clean skip, not a crash.
    result = parse_year(2025, data_dir=tmp_path, fetched_at=FETCHED_AT)
    assert result is None
    assert not (tmp_path / "parsed" / "2025").exists()
    err = capsys.readouterr().err
    assert "missing" in err


def test_parse_command_returns_zero_and_emits_stdout_summary(tmp_path, capsys):
    _seed_year(tmp_path, 2024, PARSE_XML)
    rc = parse(
        [2024, 2025],  # 2025 is absent → skipped, not an error
        data_dir=tmp_path,
        types=["ptr", "fd"],
        strict=False,
        fetched_at=FETCHED_AT,
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "parse"
    assert out["generated_at"] == FETCHED_AT
    assert out["skipped_years"] == [2025]
    assert [y["year"] for y in out["years"]] == [2024]
    assert out["years"][0]["total"] == 8
