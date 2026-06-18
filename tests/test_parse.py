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
from openhouse.legislators import load_legislator_index
from openhouse.schemas import SCHEMA_VERSION

FIXTURES = Path(__file__).parent / "fixtures"
PARSE_XML = FIXTURES / "parse" / "2024FD.xml"
TRIMMED_XML = FIXTURES / "2024FD-trimmed.xml"
REFERENCE_DIR = FIXTURES / "reference"
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
    raw = data_dir / "raw" / "clerk" / str(year)
    raw.mkdir(parents=True, exist_ok=True)
    (raw / f"{year}FD.xml").write_text(CLASSIFY_XML)
    for (doc_id, family), fname in _FIXTURE_FILES.items():
        fam_dir = raw / family
        fam_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(PDF_FIXTURES / fname, fam_dir / f"{doc_id}.pdf")


def _seed_year(data_dir: Path, year: int, xml_src: Path) -> None:
    """Lay the index XML where ``parse`` expects it: raw/<year>/<year>FD.xml."""
    raw = data_dir / "raw" / "clerk" / str(year)
    raw.mkdir(parents=True, exist_ok=True)
    shutil.copy(xml_src, raw / f"{year}FD.xml")


def _seed_reference(data_dir: Path) -> None:
    """Copy the CC0 legislators fixture into raw/reference/ for the offline join."""
    ref = data_dir / "raw" / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    for path in REFERENCE_DIR.glob("*.json"):
        shutil.copy(path, ref / path.name)


def _fixture_legislators(tmp_path):
    """The fixture legislator index, loaded through the public offline loader."""
    _seed_reference(tmp_path)
    return load_legislator_index(tmp_path)


# --- bioguide identity join + warnings (#16) ------------------------------


def test_bioguide_attaches_on_matched_house_seat(tmp_path):
    # The trimmed fixture's real members match the CC0 legislators fixture by
    # (last, state, district): Allen GA12, Adams NC12, González PR — all
    # non-candidate filings, so the seat join pins each to its bioguide. (Norton's
    # 2024 filing is a candidate report and is covered separately — see
    # test_candidate_report_never_bioguide_matched; the historical-file match path
    # itself is exercised in test_legislators.py.)
    records = build_filing_records(
        TRIMMED_XML, 2024, _fixture_legislators(tmp_path)
    )
    by_doc = {r.doc_id: r for r in records}
    assert by_doc["10066961"].bioguide_id == "A000372"  # Allen GA12
    assert by_doc["10066961"].filer_id == "bioguide:A000372"
    assert by_doc["20024277"].bioguide_id == "A000370"  # Adams NC12
    # González-Colón spelled with diacritics in the reference, plain in the FD.
    assert by_doc["30022163"].bioguide_id == "G000582"
    # Norton's 2024 filing is a candidate report → name-keyed, not bioguide.
    assert by_doc["10067000"].bioguide_id is None
    assert by_doc["10067000"].filer_id.startswith("name:")


def test_unmatched_filer_falls_back_to_name_key_and_warns(tmp_path):
    records = build_filing_records(
        TRIMMED_XML, 2024, _fixture_legislators(tmp_path)
    )
    doe = next(r for r in records if r.doc_id == "7940")
    # No StateDst → no seat → no bioguide; last-resort name: key + a warning.
    assert doe.bioguide_id is None
    assert doe.filer_id == "name:unk.doe.maryam"
    warnings = {
        w["filer_id"]: w for w in _detect_identity_warnings(records, _fixture_legislators(tmp_path))
    }
    assert "name:unk.doe.maryam" in warnings
    # No StateDst → no seat key was even possible → no_district (GH-0122).
    assert warnings["name:unk.doe.maryam"]["reason"] == "no_district"


def test_matched_filer_never_warns(tmp_path):
    records = build_filing_records(
        TRIMMED_XML, 2024, _fixture_legislators(tmp_path)
    )
    warned = {w["filer_id"] for w in _detect_identity_warnings(records)}
    # Allen matched a bioguide — pinned identity, never a name-keyed warning.
    assert "bioguide:A000372" not in warned


def test_ambiguous_seat_does_not_false_match(tmp_path):
    # Two distinct John Smiths share TX-5 in the fixture → the seat key is
    # ambiguous → no bioguide (completeness over a false positive).
    xml = (
        "<FinancialDisclosure><Member>"
        "<Last>Smith</Last><First>John</First><Suffix></Suffix>"
        "<FilingType>O</FilingType><StateDst>TX05</StateDst>"
        "<DocID>5001</DocID></Member></FinancialDisclosure>"
    )
    path = tmp_path / "x.xml"
    path.write_text(xml)
    records = build_filing_records(path, 2024, _fixture_legislators(tmp_path))
    assert records[0].bioguide_id is None
    assert records[0].filer_id == "name:tx.smith.john"


def test_candidate_report_never_bioguide_matched(tmp_path):
    # A candidate report (FilingType "C") is filed by someone RUNNING for a seat,
    # not its holder — a surname+seat collision with the real rep must NOT pin the
    # candidate to that member's bioguide. It would be a *silent* false positive,
    # since _detect_identity_warnings only flags UNmatched filers. A member filing
    # on the same seat key still matches; the candidate falls back to name:.
    member = (
        "<Member><Last>Allen</Last><First>Rick</First><Suffix></Suffix>"
        "<FilingType>{t}</FilingType><StateDst>GA12</StateDst>"
        "<DocID>{d}</DocID></Member>"
    )
    xml = (
        "<FinancialDisclosure>"
        + member.format(t="O", d="9001")  # member filing → matches the seat
        + member.format(t="C", d="9002")  # candidate report → must NOT match
        + "</FinancialDisclosure>"
    )
    path = tmp_path / "cand.xml"
    path.write_text(xml)
    records = build_filing_records(path, 2024, _fixture_legislators(tmp_path))
    by_doc = {r.doc_id: r for r in records}
    assert by_doc["9001"].bioguide_id == "A000372"  # member: pinned
    assert by_doc["9002"].bioguide_id is None  # candidate: not pinned
    assert by_doc["9002"].filer_id.startswith("name:")


def test_no_reference_index_keeps_everyone_name_keyed():
    # With no legislators index every filer is name-keyed and warned.
    records = build_filing_records(PARSE_XML, 2024)
    assert all(r.bioguide_id is None for r in records)
    assert all(r.filer_id.startswith("name:") for r in records)
    warned = {w["filer_id"] for w in _detect_identity_warnings(records)}
    # Distinct name keys, each warned once (no bioguide to pin any of them).
    assert "name:tx.smith.john" in warned
    assert "name:nc.adams.alma" in warned


# --- GH-0122: suspicious vs expected non-matches --------------------------


def test_suspicious_when_seat_occupied_but_name_mismatched(tmp_path):
    # GA-12 is held by Allen (A000372). A filer at GA-12 whose last name is a
    # variant/typo ("Allenn") matches no seat key, but the seat IS on record —
    # the actionable "we likely missed a real member" signal (GH-0122).
    legislators = _fixture_legislators(tmp_path)
    xml = (
        "<FinancialDisclosure><Member>"
        "<Last>Allenn</Last><First>Rick</First><Suffix></Suffix>"
        "<FilingType>O</FilingType><StateDst>GA12</StateDst>"
        "<DocID>6001</DocID></Member></FinancialDisclosure>"
    )
    path = tmp_path / "s.xml"
    path.write_text(xml)
    records = build_filing_records(path, 2024, legislators)
    assert records[0].bioguide_id is None
    warnings = {
        w["filer_id"]: w for w in _detect_identity_warnings(records, legislators)
    }
    w = warnings["name:ga.allenn.rick"]
    assert w["reason"] == "suspicious"
    # Carries the occupied seat + its roster holder so an operator can eyeball it.
    assert w["seats"] == [
        {
            "state": "GA",
            "district": 12,
            "holders": [{"bioguide": "A000372", "last": "Allen"}],
        }
    ]


def test_unmatched_reasons_classify_each_bucket(tmp_path):
    # One filer per non-suspicious bucket, all unmatched, classified distinctly.
    legislators = _fixture_legislators(tmp_path)
    member = (
        "<Member><Last>{last}</Last><First>{first}</First><Suffix></Suffix>"
        "<FilingType>{t}</FilingType><StateDst>{sd}</StateDst><DocID>{d}</DocID></Member>"
    )
    xml = (
        "<FinancialDisclosure>"
        # candidate at an occupied seat → expected (candidate), never suspicious.
        + member.format(last="Allen", first="Rick", t="C", sd="GA12", d="7001")
        # two John Smiths share TX-5 → the seat key is nulled → ambiguous_seat.
        + member.format(last="Smith", first="John", t="O", sd="TX05", d="7002")
        # a seat no fixture rep holds → unknown_seat.
        + member.format(last="Nobody", first="Nora", t="O", sd="WY01", d="7003")
        + "</FinancialDisclosure>"
    )
    path = tmp_path / "r.xml"
    path.write_text(xml)
    records = build_filing_records(path, 2024, legislators)
    reasons = {w["filer_id"]: w["reason"] for w in _detect_identity_warnings(records, legislators)}
    assert reasons["name:ga.allen.rick"] == "candidate"
    assert reasons["name:tx.smith.john"] == "ambiguous_seat"
    assert reasons["name:wy.nobody.nora"] == "unknown_seat"
    # None of these expected non-matches carry a seats detail (only suspicious does).
    assert all("seats" not in w for w in _detect_identity_warnings(records, legislators))


def test_identity_report_warns_only_on_suspicious(tmp_path, capsys):
    # End-to-end: a suspicious filer (GA-12, name variant) alongside an expected
    # one (no district). The summary line tallies both; only the suspicious one is
    # named per-line on stderr (GH-0122).
    _seed_reference(tmp_path)
    xml = (
        "<FinancialDisclosure>"
        "<Member><Last>Allenn</Last><First>Rick</First><Suffix></Suffix>"
        "<FilingType>O</FilingType><StateDst>GA12</StateDst><DocID>8001</DocID></Member>"
        "<Member><Last>Doe</Last><First>Maryam</First><Suffix></Suffix>"
        "<FilingType>O</FilingType><StateDst></StateDst><DocID>8002</DocID></Member>"
        "</FinancialDisclosure>"
    )
    raw = tmp_path / "raw" / "clerk" / "2024"  # no PDFs → both filings classify missing
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "2024FD.xml").write_text(xml)
    parse_year(2024, data_dir=tmp_path, fetched_at=FETCHED_AT)
    err = capsys.readouterr().err
    # One collapsed summary line carrying the per-reason breakdown.
    assert "2024: identity — 0 matched, 2 unmatched" in err
    assert "1 suspicious" in err
    # The suspicious filer is named; the expected (no_district) one is not.
    assert "SUSPICIOUS identity" in err
    assert "name:ga.allenn.rick" in err
    assert "GA-12" in err and "Allen (A000372)" in err
    assert "name:unk.doe.maryam" not in err


# --- parse_year / parse end-to-end ----------------------------------------


def test_parse_year_writes_filings_and_manifest(tmp_path):
    _seed_year(tmp_path, 2024, PARSE_XML)
    summary = parse_year(2024, data_dir=tmp_path, fetched_at=FETCHED_AT)

    filings_path = tmp_path / "parsed" / "clerk" / "2024" / "filings.json"
    manifest_path = tmp_path / "parsed" / "clerk" / "2024" / "parse-manifest.json"
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
    # No legislators reference seeded in this tmp data_dir → every filer is
    # name-keyed. The distinct name keys are the four people in the fixture (Adams
    # files 3x but shares one key).
    by_filer = {w["filer_id"]: w for w in manifest["identity_warnings"]}
    assert set(by_filer) == {
        "name:nc.adams.alma",
        "name:tx.smith.john",
        "name:fl.jones.robert",
        "name:unk.doe.maryam",
    }
    # With no reference index loaded, a seatless filer is no_district; a real seat
    # reads as unknown_seat (no known holder), never suspicious (GH-0122).
    assert by_filer["name:unk.doe.maryam"]["reason"] == "no_district"
    assert by_filer["name:nc.adams.alma"]["reason"] == "unknown_seat"
    # match_summary rolls the per-filer reasons up to an identity-level tally.
    ms = manifest["match_summary"]
    assert ms["matched"] == 0
    assert ms["unmatched"] == 4
    assert ms["by_reason"] == {
        "candidate": 0,
        "no_district": 1,
        "unknown_seat": 3,
        "ambiguous_seat": 0,
        "suspicious": 0,
    }
    assert ms["suspicious"] == []


def test_parse_year_is_deterministic(tmp_path):
    _seed_year(tmp_path, 2024, PARSE_XML)
    parse_year(2024, data_dir=tmp_path, fetched_at=FETCHED_AT)
    first = (tmp_path / "parsed" / "clerk" / "2024" / "filings.json").read_bytes()
    first_manifest = (tmp_path / "parsed" / "clerk" / "2024" / "parse-manifest.json").read_bytes()
    # Re-run from the same raw → byte-identical output.
    parse_year(2024, data_dir=tmp_path, fetched_at=FETCHED_AT)
    assert (tmp_path / "parsed" / "clerk" / "2024" / "filings.json").read_bytes() == first
    assert (
        tmp_path / "parsed" / "clerk" / "2024" / "parse-manifest.json"
    ).read_bytes() == first_manifest


def test_parse_missing_year_is_clean_skip(tmp_path, capsys):
    # No raw/2025 seeded → clean skip, not a crash.
    result = parse_year(2025, data_dir=tmp_path, fetched_at=FETCHED_AT)
    assert result is None
    assert not (tmp_path / "parsed" / "clerk" / "2025").exists()
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
        (data_dir / "parsed" / "clerk" / str(year) / "filings.json").read_text()
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
        (tmp_path / "parsed" / "clerk" / "2020" / "unparsed-manifest.json").read_text()
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
        (tmp_path / "parsed" / "clerk" / "2020" / "parse-manifest.json").read_text()
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
    parsed = tmp_path / "parsed" / "clerk" / "2020"
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
        (tmp_path / "parsed" / "clerk" / "2020" / "parse-manifest.json").read_text()
    )["counts"]
    assert counts["by_pdf_class"] == {"efiled": 1, "scanned": 1, "missing": 0}
    assert counts["not_classified"] == 3
    assert sum(counts["by_pdf_class"].values()) + counts["not_classified"] == 5

    # Out-of-scope fd rows are not deemed "unparsed".
    unparsed = json.loads(
        (tmp_path / "parsed" / "clerk" / "2020" / "unparsed-manifest.json").read_text()
    )["unparsed"]
    reasons = {u["doc_id"]: u["reason"] for u in unparsed}
    assert reasons == {"8217326": "scanned"}


def test_parse_extract_failed_marks_error(tmp_path):
    # A present-but-corrupt PDF → parse_status error + unparsed reason extract_failed.
    _seed_classify_year(tmp_path)
    # Clobber one efiled fixture with non-PDF bytes (built at test time).
    (tmp_path / "raw" / "clerk" / "2020" / "fd" / "10042852.pdf").write_text("not a pdf\n")

    summary = parse_year(2020, data_dir=tmp_path, fetched_at=FETCHED_AT)
    by_doc = _filings_by_doc(tmp_path)
    assert by_doc["10042852"]["parse_status"] == "error"
    assert by_doc["10042852"]["pdf_class"] is None

    unparsed = json.loads(
        (tmp_path / "parsed" / "clerk" / "2020" / "unparsed-manifest.json").read_text()
    )["unparsed"]
    reasons = {u["doc_id"]: u["reason"] for u in unparsed}
    assert reasons["10042852"] == "extract_failed"

    counts = json.loads(
        (tmp_path / "parsed" / "clerk" / "2020" / "parse-manifest.json").read_text()
    )["counts"]
    assert counts["by_parse_status"]["error"] == 1
    # error filing isn't double-counted as a pdf_class; reconciles via not_classified.
    assert sum(counts["by_pdf_class"].values()) + counts["not_classified"] == 5
    assert summary["has_error"] is True


def test_strict_returns_nonzero_on_error(tmp_path, capsys):
    _seed_classify_year(tmp_path)
    (tmp_path / "raw" / "clerk" / "2020" / "fd" / "10042852.pdf").write_text("not a pdf\n")
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
    (tmp_path / "raw" / "clerk" / "2020" / "fd" / "10042852.pdf").write_text("not a pdf\n")
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
        (tmp_path / "parsed" / "clerk" / "2020" / "unparsed-manifest.json").read_text()
    )["unparsed"]
    assert unparsed  # scanned + missing entries exist
    for entry in unparsed:
        assert set(entry) == {"doc_id", "filer_id", "reason"}
        assert entry["filer_id"]  # non-empty


def test_types_partial_run_emits_stderr_note(tmp_path, capsys):
    _seed_classify_year(tmp_path)
    parse([2020], data_dir=tmp_path, types=["ptr"], strict=False, fetched_at=FETCHED_AT)
    assert "--types excludes fd" in capsys.readouterr().err


def test_stale_body_removed_when_filing_degrades(tmp_path):
    # GH-0070: a body written by an earlier parse generation must not survive a
    # run in which the filing produces no body (here: extraction failure) — a
    # stale body beside an extract_failed manifest entry masquerades as data.
    from openhouse.parse import _classify_records
    from openhouse.schemas import FilingMetadata, Filer, FilingTypeInfo

    rec = FilingMetadata(
        doc_id="10009999",
        year=2020,
        filer=Filer(first="Pat", last="Example"),
        filer_id="name:example.pat",
        filing_type=FilingTypeInfo.from_code("O"),
        source_pdf="raw/clerk/2020/fd/10009999.pdf",
    )
    data_dir = tmp_path
    (data_dir / "raw/clerk/2020/fd").mkdir(parents=True)
    (data_dir / "raw/clerk/2020/fd/10009999.pdf").write_bytes(b"%PDF-1.4 truncated")
    parsed_dir = data_dir / "parsed/clerk/2020"
    stale = parsed_dir / "fd/10009999.json"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"schedules": {"A": []}}\n')

    unparsed = _classify_records(
        [rec], data_dir=data_dir, types=["fd"], year=2020, parsed_dir=parsed_dir
    )
    assert rec.parse_status == "error"
    assert any(e["reason"] == "extract_failed" for e in unparsed)
    assert not stale.exists(), "stale body must be removed, not left as data"
