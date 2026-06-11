"""Offline tests for the index enumeration that drives the PDF download loop (#4).

``index.enumerate_targets`` parses ``<YEAR>FD.xml`` into the minimal
``(DocID, FilingType, year)`` targets the §4 download loop needs — nothing more
(the full metadata→record mapping is the parse milestone). These run against the
checked-in trimmed fixture, which carries the SPEC §2.1 edge-case rows.
"""

from __future__ import annotations

from pathlib import Path

from openhouse.index import IndexTarget, enumerate_targets

FIXTURES = Path(__file__).parent / "fixtures"
TRIMMED_XML = FIXTURES / "2024FD-trimmed.xml"


def test_enumerate_yields_all_rows_with_raw_filing_type():
    targets = list(enumerate_targets(TRIMMED_XML, 2024))
    by_doc = {t.doc_id: t for t in targets}
    # All five fixture rows, including the 4-digit DocID and the DC/PR rows.
    assert set(by_doc) == {
        "10066961",
        "20024277",
        "10067000",
        "30022163",
        "7940",
    }
    # Raw FilingType letters preserved verbatim.
    assert by_doc["10066961"].filing_type == "O"
    assert by_doc["20024277"].filing_type == "P"
    assert by_doc["7940"].filing_type == "W"
    # Year threaded onto every target.
    assert all(t.year == 2024 for t in targets)


def test_family_routes_p_to_ptr_else_fd():
    by_doc = {t.doc_id: t for t in enumerate_targets(TRIMMED_XML, 2024)}
    # The lone P row routes to ptr; every other type routes to fd (§2.2).
    assert by_doc["20024277"].family == "ptr"
    for doc_id in ("10066961", "10067000", "30022163", "7940"):
        assert by_doc[doc_id].family == "fd"


def test_member_without_docid_is_skipped(tmp_path):
    xml = (
        '<FinancialDisclosure>'
        '<Member><FilingType>O</FilingType><DocID>111</DocID></Member>'
        '<Member><FilingType>P</FilingType><DocID></DocID></Member>'  # no DocID
        '<Member><FilingType>C</FilingType></Member>'  # no DocID tag at all
        '</FinancialDisclosure>'
    )
    path = tmp_path / "2024FD.xml"
    path.write_text(xml)
    targets = list(enumerate_targets(path, 2024))
    assert [t.doc_id for t in targets] == ["111"]


def test_index_target_family_property():
    assert IndexTarget("x", "P", 2024).family == "ptr"
    assert IndexTarget("x", "O", 2024).family == "fd"
    assert IndexTarget("x", "", 2024).family == "fd"
