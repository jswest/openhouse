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
PDF_FIXTURES = FIXTURES / "pdf"

FETCHED_AT = "2026-06-11T12:00:00"


# Constructed index whose DocIDs match the committed PDF fixtures, plus one row
# with NO file on disk (→ missing). FilingType "P" routes a row to the ptr
# family; a non-P type (here "O") routes to fd (SPEC §2.2).
CLASSIFY_XML = """<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
  <Member>
    <Last>Efiled</Last><First>Fiona</First><Suffix></Suffix>
    <FilingType>O</FilingType><StateDst>NY01</StateDst>
    <Year>2020</Year><FilingDate>5/15/2020</FilingDate><DocID>10042852</DocID>
  </Member>
  <Member>
    <Last>Efiled</Last><First>Peter</First><Suffix></Suffix>
    <FilingType>P</FilingType><StateDst>NY02</StateDst>
    <Year>2020</Year><FilingDate>1/08/2020</FilingDate><DocID>20016766</DocID>
  </Member>
  <Member>
    <Last>Scanned</Last><First>Frank</First><Suffix></Suffix>
    <FilingType>O</FilingType><StateDst>NY03</StateDst>
    <Year>2020</Year><FilingDate>5/15/2020</FilingDate><DocID>8217722</DocID>
  </Member>
  <Member>
    <Last>Scanned</Last><First>Paula</First><Suffix></Suffix>
    <FilingType>P</FilingType><StateDst>NY04</StateDst>
    <Year>2020</Year><FilingDate>1/08/2020</FilingDate><DocID>8217326</DocID>
  </Member>
  <Member>
    <Last>Gone</Last><First>Greg</First><Suffix></Suffix>
    <FilingType>O</FilingType><StateDst>NY05</StateDst>
    <Year>2020</Year><FilingDate>5/15/2020</FilingDate><DocID>30099999</DocID>
  </Member>
</FinancialDisclosure>
"""

# (DocID, family) → fixture filename. The "missing" DocID 30099999 is absent.
_FIXTURE_FILES = {
    ("10042852", "fd"): "efiled_fd_10042852.pdf",
    ("20016766", "ptr"): "efiled_ptr_20016766.pdf",
    ("8217722", "fd"): "scanned_fd_8217722.pdf",
    ("8217326", "ptr"): "scanned_ptr_8217326.pdf",
}


def _seed_classify_year(data_dir: Path, year: int = 2020) -> None:
    """Lay the constructed index + copy fixture PDFs to raw/<year>/<family>/."""
    raw = data_dir / "raw" / str(year)
    raw.mkdir(parents=True, exist_ok=True)
    (raw / f"{year}FD.xml").write_text(CLASSIFY_XML)
    for (doc_id, family), fname in _FIXTURE_FILES.items():
        fam_dir = raw / family
        fam_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(PDF_FIXTURES / fname, fam_dir / f"{doc_id}.pdf")


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


# --- PDF classification end-to-end (#7) ------------------------------------


def _filings_by_doc(data_dir: Path, year: int = 2020) -> dict:
    filings = json.loads(
        (data_dir / "parsed" / str(year) / "filings.json").read_text()
    )
    return {f["doc_id"]: f for f in filings}


def test_parse_classifies_pdfs_and_writes_unparsed_manifest(tmp_path):
    _seed_classify_year(tmp_path)
    summary = parse_year(2020, data_dir=tmp_path, fetched_at=FETCHED_AT)

    # filings.json carries the authoritative pdf_class per filing.
    by_doc = _filings_by_doc(tmp_path)
    assert by_doc["10042852"]["pdf_class"] == "efiled"
    assert by_doc["20016766"]["pdf_class"] == "efiled"
    assert by_doc["8217722"]["pdf_class"] == "scanned"
    assert by_doc["8217326"]["pdf_class"] == "scanned"
    assert by_doc["30099999"]["pdf_class"] == "missing"
    for doc in by_doc.values():
        assert doc["parse_status"] == "ok"

    # unparsed-manifest has the scanned + missing entries with reasons, NOT efiled.
    unparsed = json.loads(
        (tmp_path / "parsed" / "2020" / "unparsed-manifest.json").read_text()
    )["unparsed"]
    reasons = {u["doc_id"]: u["reason"] for u in unparsed}
    assert reasons == {
        "8217722": "scanned",
        "8217326": "scanned",
        "30099999": "missing",
    }
    assert "10042852" not in reasons
    assert "20016766" not in reasons

    # parse-manifest counts reconcile to the total.
    manifest = json.loads(
        (tmp_path / "parsed" / "2020" / "parse-manifest.json").read_text()
    )
    counts = manifest["counts"]
    assert counts["total"] == 5
    assert counts["by_pdf_class"] == {"efiled": 2, "scanned": 2, "missing": 1}
    assert counts["not_classified"] == 0
    assert counts["by_parse_status"] == {"ok": 5, "error": 0}
    total_classified = sum(counts["by_pdf_class"].values()) + counts["not_classified"]
    assert total_classified == counts["total"]

    assert summary["has_error"] is False


def test_parse_classification_is_deterministic(tmp_path):
    _seed_classify_year(tmp_path)
    parse_year(2020, data_dir=tmp_path, fetched_at=FETCHED_AT)
    parsed = tmp_path / "parsed" / "2020"
    first_f = (parsed / "filings.json").read_bytes()
    first_m = (parsed / "parse-manifest.json").read_bytes()
    first_u = (parsed / "unparsed-manifest.json").read_bytes()
    parse_year(2020, data_dir=tmp_path, fetched_at=FETCHED_AT)
    assert (parsed / "filings.json").read_bytes() == first_f
    assert (parsed / "parse-manifest.json").read_bytes() == first_m
    assert (parsed / "unparsed-manifest.json").read_bytes() == first_u


def test_parse_types_partial_run_reconciles(tmp_path):
    # Only classify the ptr family; fd rows stay unclassified but still counted.
    _seed_classify_year(tmp_path)
    parse_year(2020, data_dir=tmp_path, types=["ptr"], fetched_at=FETCHED_AT)

    by_doc = _filings_by_doc(tmp_path)
    # ptr rows classified; fd rows left None (out of scope this run).
    assert by_doc["20016766"]["pdf_class"] == "efiled"
    assert by_doc["8217326"]["pdf_class"] == "scanned"
    assert by_doc["10042852"]["pdf_class"] is None
    assert by_doc["8217722"]["pdf_class"] is None
    assert by_doc["30099999"]["pdf_class"] is None

    counts = json.loads(
        (tmp_path / "parsed" / "2020" / "parse-manifest.json").read_text()
    )["counts"]
    assert counts["by_pdf_class"] == {"efiled": 1, "scanned": 1, "missing": 0}
    assert counts["not_classified"] == 3
    assert sum(counts["by_pdf_class"].values()) + counts["not_classified"] == 5

    # Out-of-scope fd rows are not deemed "unparsed".
    unparsed = json.loads(
        (tmp_path / "parsed" / "2020" / "unparsed-manifest.json").read_text()
    )["unparsed"]
    reasons = {u["doc_id"]: u["reason"] for u in unparsed}
    assert reasons == {"8217326": "scanned"}


def test_parse_extract_failed_marks_error(tmp_path):
    # A present-but-corrupt PDF → parse_status error + unparsed reason extract_failed.
    _seed_classify_year(tmp_path)
    # Clobber one efiled fixture with non-PDF bytes (built at test time).
    (tmp_path / "raw" / "2020" / "fd" / "10042852.pdf").write_text("not a pdf\n")

    summary = parse_year(2020, data_dir=tmp_path, fetched_at=FETCHED_AT)
    by_doc = _filings_by_doc(tmp_path)
    assert by_doc["10042852"]["parse_status"] == "error"
    assert by_doc["10042852"]["pdf_class"] is None

    unparsed = json.loads(
        (tmp_path / "parsed" / "2020" / "unparsed-manifest.json").read_text()
    )["unparsed"]
    reasons = {u["doc_id"]: u["reason"] for u in unparsed}
    assert reasons["10042852"] == "extract_failed"

    counts = json.loads(
        (tmp_path / "parsed" / "2020" / "parse-manifest.json").read_text()
    )["counts"]
    assert counts["by_parse_status"]["error"] == 1
    # error filing isn't double-counted as a pdf_class; reconciles via not_classified.
    assert sum(counts["by_pdf_class"].values()) + counts["not_classified"] == 5
    assert summary["has_error"] is True


def test_strict_returns_nonzero_on_error(tmp_path, capsys):
    _seed_classify_year(tmp_path)
    (tmp_path / "raw" / "2020" / "fd" / "10042852.pdf").write_text("not a pdf\n")
    rc = parse(
        [2020], data_dir=tmp_path, types=["ptr", "fd"], strict=True, fetched_at=FETCHED_AT
    )
    assert rc != 0


def test_strict_returns_zero_without_error(tmp_path, capsys):
    _seed_classify_year(tmp_path)
    rc = parse(
        [2020], data_dir=tmp_path, types=["ptr", "fd"], strict=True, fetched_at=FETCHED_AT
    )
    assert rc == 0


def test_no_strict_returns_zero_even_with_error(tmp_path, capsys):
    _seed_classify_year(tmp_path)
    (tmp_path / "raw" / "2020" / "fd" / "10042852.pdf").write_text("not a pdf\n")
    rc = parse(
        [2020], data_dir=tmp_path, types=["ptr", "fd"], strict=False, fetched_at=FETCHED_AT
    )
    assert rc == 0


# --- integrator fixes (critic pass 1) -------------------------------------


def test_parse_all_years_missing_returns_nonzero(tmp_path, capsys):
    # Nothing pulled at all → `parse` must not report success (exit 0), or a
    # `parse … && next-step` proceeds on no output. (A *partial* skip still 0.)
    rc = parse(
        [2024, 2025], data_dir=tmp_path, types=["ptr", "fd"], strict=False,
        fetched_at=FETCHED_AT,
    )
    assert rc != 0
    out = json.loads(capsys.readouterr().out)
    assert out["years"] == []
    assert out["skipped_years"] == [2024, 2025]


def test_unparsed_entries_carry_filer_id(tmp_path):
    # filer_id on every unparsed entry keeps no-DocID rows (doc_id == "") joinable.
    _seed_classify_year(tmp_path)
    parse_year(2020, data_dir=tmp_path, fetched_at=FETCHED_AT)
    unparsed = json.loads(
        (tmp_path / "parsed" / "2020" / "unparsed-manifest.json").read_text()
    )["unparsed"]
    assert unparsed  # scanned + missing entries exist
    for entry in unparsed:
        assert set(entry) == {"doc_id", "filer_id", "reason"}
        assert entry["filer_id"]  # non-empty


def test_types_partial_run_emits_stderr_note(tmp_path, capsys):
    _seed_classify_year(tmp_path)
    parse([2020], data_dir=tmp_path, types=["ptr"], strict=False, fetched_at=FETCHED_AT)
    assert "--types excludes fd" in capsys.readouterr().err
