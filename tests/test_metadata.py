"""Offline tests for the metadata mapping (#6): filer_id, build_filing_records.

These exercise the ``parse`` half of ``index.py`` — the full ``<Member>`` →
:class:`FilingMetadata` mapping (SPEC §6.1) and the ``filer_id`` rule (SPEC §6.2)
— against the checked-in trimmed fixture and small constructed XML. Pure offline,
no network.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from openhouse.index import build_filing_records, compute_filer_id, slug

FIXTURES = Path(__file__).parent / "fixtures"
TRIMMED_XML = FIXTURES / "2024FD-trimmed.xml"


# --- slug + filer_id (SPEC §6.2) ------------------------------------------


def test_slug_lowercases_strips_punct_and_collapses_whitespace():
    assert slug("Allen") == "allen"
    assert slug("Maryam.") == "maryam"
    assert slug("Gonzalez-Colon") == "gonzalez-colon"
    assert slug("  Van  Buren  ") == "van-buren"
    assert slug("") == ""


def test_slug_strips_diacritics():
    # NFKD decomposition drops the combining marks but keeps the base letters.
    assert slug("Núñez") == "nunez"
    assert slug("Peña") == "pena"


def test_filer_id_basic_state_last_first():
    assert (
        compute_filer_id(last="Allen", first="Richard", suffix="", state="GA")
        == "ga.allen.richard"
    )


def test_filer_id_uses_only_first_token_of_first():
    # Middle names / initials are dropped (the main cross-year variation).
    assert (
        compute_filer_id(last="Adams", first="Alma S.", suffix="", state="NC")
        == "nc.adams.alma"
    )
    assert (
        compute_filer_id(last="Adams", first="Alma Shealey", suffix="", state="NC")
        == "nc.adams.alma"
    )


def test_filer_id_appends_suffix_only_when_present():
    assert (
        compute_filer_id(last="Smith", first="John", suffix="Jr.", state="TX")
        == "tx.smith.john.jr"
    )
    assert (
        compute_filer_id(last="Smith", first="John", suffix="", state="TX")
        == "tx.smith.john"
    )


def test_filer_id_empty_state_is_unk():
    assert (
        compute_filer_id(last="Doe", first="Maryam.", suffix="", state=None)
        == "unk.doe.maryam"
    )


# --- build_filing_records (SPEC §6.1) -------------------------------------


def test_build_records_maps_all_fields_in_order():
    records = build_filing_records(TRIMMED_XML, 2024)
    # All five fixture rows, in XML order (no row dropped).
    assert [r.doc_id for r in records] == [
        "10066961",
        "20024277",
        "10067000",
        "30022163",
        "7940",
    ]
    allen = records[0]
    assert allen.year == 2024
    assert allen.filer.prefix == "Hon."
    assert allen.filer.first == "Richard W."
    assert allen.filer.last == "Allen"
    assert allen.filer.suffix is None
    assert allen.filer_id == "ga.allen.richard"
    assert allen.state_district.raw == "GA12"
    assert allen.state_district.state == "GA"
    assert allen.state_district.district == 12
    assert allen.filing_type.code == "O"
    assert allen.filing_type.label == "annual_report"
    assert allen.filing_date == date(2025, 4, 29)  # never derived from Year
    assert allen.parse_status == "ok"
    assert allen.pdf_class is None  # left for #7


def test_source_pdf_routes_p_to_ptr_else_fd():
    records = {r.doc_id: r for r in build_filing_records(TRIMMED_XML, 2024)}
    assert records["20024277"].source_pdf == "raw/2024/ptr/20024277.pdf"  # P
    assert records["10066961"].source_pdf == "raw/2024/fd/10066961.pdf"  # O
    assert records["30022163"].source_pdf == "raw/2024/fd/30022163.pdf"  # X


def test_empty_statedst_yields_null_state_district():
    records = {r.doc_id: r for r in build_filing_records(TRIMMED_XML, 2024)}
    # The type-W row (DocID 7940) has an empty StateDst and FilingDate.
    w = records["7940"]
    assert w.state_district is None
    assert w.filing_date is None
    assert w.filer_id == "unk.doe.maryam"  # empty state → unk segment


def test_dc_and_pr_districts_are_zero():
    records = {r.doc_id: r for r in build_filing_records(TRIMMED_XML, 2024)}
    assert records["10067000"].state_district.state == "DC"
    assert records["10067000"].state_district.district == 0  # DC00
    assert records["30022163"].state_district.state == "PR"
    assert records["30022163"].state_district.district == 0  # PR00


def test_no_docid_member_still_yields_a_record(tmp_path):
    xml = (
        "<FinancialDisclosure>"
        "<Member><First>Jane</First><Last>Roe</Last>"
        "<FilingType>W</FilingType><StateDst></StateDst></Member>"
        "</FinancialDisclosure>"
    )
    path = tmp_path / "2024FD.xml"
    path.write_text(xml)
    records = build_filing_records(path, 2024)
    # Never dropped, even with no DocID / no body to fetch.
    assert len(records) == 1
    assert records[0].doc_id == ""
    assert records[0].source_pdf is None
    assert records[0].filer_id == "unk.roe.jane"


def test_unknown_filing_type_preserves_raw_code(tmp_path):
    xml = (
        "<FinancialDisclosure>"
        "<Member><First>Sam</First><Last>Vance</Last>"
        "<FilingType>Z</FilingType><StateDst>OH01</StateDst>"
        "<DocID>999</DocID></Member>"
        "</FinancialDisclosure>"
    )
    path = tmp_path / "2024FD.xml"
    path.write_text(xml)
    records = build_filing_records(path, 2024)
    assert records[0].filing_type.code == "Z"  # raw letter preserved
    assert records[0].filing_type.label == "unknown"
