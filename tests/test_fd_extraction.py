"""Offline tests for e-filed annual-FD schedule extraction (``openhouse/pdf.py``, #12).

Extraction targets the SPEC §6.3 FD schedule body: schedules A–D **fully
structured**, E–J as ``raw_text``-only line items, every line item carrying
verbatim ``raw_text``, and a ``None disclosed.`` schedule recorded as **absent**.
These run against the committed fixture ``efiled_fd_10042852.pdf`` (Hon. Bennie G.
Thompson, 2020 — ground truth in ``tests/fixtures/pdf/README.md``) and synthetic
page text — no Clerk, no extra binary fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openhouse.index import build_filing_records
from openhouse.parse import _classify_records
from openhouse.pdf import (
    NotAnFdBody,
    PdfExtractError,
    extract_fd_schedules,
)

PDF_FIXTURES = Path(__file__).parent / "fixtures" / "pdf"
THOMPSON = PDF_FIXTURES / "efiled_fd_10042852.pdf"
# Hon. Alma Adams, 2021: the glyphs-lost rendering (SPEC §2.2) — every small-caps
# glyph extracts as a U+0000 NUL, so "Schedule A:" is "S\x00{7} A:" and the
# tx-over-$1,000 checkbox glyph (gfedc) is absent from the text layer entirely.
ADAMS_NUL = PDF_FIXTURES / "efiled_fd_nulglyph_10049721.pdf"
EXTENSION = None  # no extension fixture is committed; synthetic text covers it


# --- segmentation: which schedules are present vs absent ----------------------


def test_thompson_present_and_absent_schedules():
    body = extract_fd_schedules(THOMPSON)
    # A, C, E, F have data; B, D, G, H, I render "None disclosed." → absent;
    # J is absent entirely (no Schedule J on this form).
    assert sorted(body.schedules) == ["A", "C", "E", "F"]
    for absent in ("B", "D", "G", "H", "I", "J"):
        assert absent not in body.schedules


def test_none_disclosed_is_absent_not_empty():
    # A "None disclosed." schedule (B, D here) is omitted entirely, never an
    # empty array — a consumer can tell "disclosed nothing" from "we failed".
    body = extract_fd_schedules(THOMPSON)
    assert "B" not in body.schedules
    assert "D" not in body.schedules


# --- Schedule A: assets & "unearned" income, fully structured -----------------


def test_schedule_a_item_count_and_structure():
    body = extract_fd_schedules(THOMPSON)
    a = body.schedules["A"]
    # 25 asset rows on this form (each [TYPE]-tagged, glyph-terminated).
    assert len(a) == 25
    # First row: a real-property asset, no owner column, a value range, a LOCATION.
    first = a[0]
    assert first["asset"] == "0.5 acre unimproved property"
    assert first["asset_type"] == "RP"
    assert first["owner"] is None
    assert first["value_of_asset"] == {
        "low": 1001,
        "high": 15000,
        "label": "$1,001 - $15,000",
    }
    assert first["location"] == "Bolton/Hinds, MS, US"
    # Verbatim raw_text carries the whole row, glyph and all.
    assert "[RP]" in first["raw_text"]
    assert "gfedc" in first["raw_text"]


def test_schedule_a_owner_and_wrapped_amount():
    body = extract_fd_schedules(THOMPSON)
    a = body.schedules["A"]
    # BancorpSouth Bank [BA] JT $100,001 - $250,000 — the value range wraps onto
    # the next physical line; it must fold back into one structured range.
    bancorp = next(i for i in a if i["asset"].startswith("BancorpSouth"))
    assert bancorp["owner"] == "JT"
    assert bancorp["asset_type"] == "BA"
    assert bancorp["value_of_asset"] == {
        "low": 100001,
        "high": 250000,
        "label": "$100,001 - $250,000",
    }


def test_schedule_a_description_detail_captured():
    body = extract_fd_schedules(THOMPSON)
    a = body.schedules["A"]
    blb = next(i for i in a if i["asset"].startswith("BLB Consulting"))
    assert blb["owner"] == "SP"
    assert blb["description"] is not None
    assert "consulting company" in blb["description"]


def test_schedule_a_income_type_is_populated():
    # #5: income_type sits between the value and income amount buckets (Rent /
    # Interest / Dividends) and must be populated, not left None.
    body = extract_fd_schedules(THOMPSON)
    a = body.schedules["A"]
    rent = next(i for i in a if i["asset"].startswith("2 acres unimproved"))
    assert rent["income_type"] == "Rent"
    assert rent["income_amount"]["label"] == "$201 - $1,000"
    assert rent["value_of_asset"] is not None  # not clobbered by the income word
    # The interleave row (value high bound wrapped to line end) also gets its type.
    bancorp = next(i for i in a if i["asset"].startswith("BancorpSouth"))
    assert bancorp["income_type"] == "Interest"
    # A value-only asset (no income column) keeps income_type None.
    rp = next(i for i in a if i["asset_type"] == "RP" and i["income_amount"] is None)
    assert rp["income_type"] is None


def test_schedule_a_dangling_low_without_high_degrades_value_to_none(monkeypatch):
    # #7: a row whose value low dangles (income column intrudes) but whose high
    # bound never materializes must NOT mis-assign the income bucket to
    # value_of_asset. Per "degrade to None rather than a wrong value", value is
    # None and the first complete bucket is income.
    page = "\n".join(
        [
            'ScheDule a: aSSetS anD "unearneD" income',
            "asset owner value of asset income income tx. >",
            "BancorpSouth Bank [BA] JT $100,001 - Interest $201 - $1,000 gfedc",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    item = body.schedules["A"][0]
    assert item["value_of_asset"] is None  # NOT $201 - $1,000 (that's income)
    assert item["income_type"] == "Interest"
    assert item["income_amount"] == {
        "low": 201,
        "high": 1000,
        "label": "$201 - $1,000",
    }
    assert "$100,001 -" in item["raw_text"]  # raw_text still carries the row


def test_every_schedule_a_item_has_raw_text():
    body = extract_fd_schedules(THOMPSON)
    for item in body.schedules["A"]:
        assert item["raw_text"].strip(), "every line item carries verbatim raw_text"


# --- Schedule C: earned income, structured ------------------------------------


def test_schedule_c_items():
    body = extract_fd_schedules(THOMPSON)
    c = body.schedules["C"]
    assert len(c) == 4
    miss = next(i for i in c if i["source"].startswith("State of Mississippi Member"))
    assert miss["amount"] == "$11,195.00"
    assert miss["raw_text"].startswith("State of Mississippi Member Retirement")


# --- Schedules E–J: raw_text-only line items ----------------------------------


def test_schedule_e_is_raw_text_only():
    body = extract_fd_schedules(THOMPSON)
    e = body.schedules["E"]
    assert e, "Schedule E (positions) has data on this form"
    # Each E item is raw_text-only — exactly one key.
    for item in e:
        assert list(item.keys()) == ["raw_text"]
        assert item["raw_text"].strip()
    assert any("BLB Properties" in i["raw_text"] for i in e)


def test_schedule_f_is_raw_text_only():
    body = extract_fd_schedules(THOMPSON)
    f = body.schedules["F"]
    assert f
    for item in f:
        assert list(item.keys()) == ["raw_text"]


# --- D structured (synthetic, since the fixture's D is "None disclosed.") ------


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakePdf:
    def __init__(self, pages: list[str]) -> None:
        self.pages = [_FakePage(t) for t in pages]

    def __enter__(self) -> "_FakePdf":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _fake_pdfplumber(monkeypatch, pages: list[str]) -> None:
    monkeypatch.setattr(
        "openhouse.pdf.pdfplumber.open", lambda _path: _FakePdf(pages)
    )


def test_schedule_b_and_d_structured_synthetic(monkeypatch):
    # A synthetic annual FD exercising the B (transactions) and D (liabilities)
    # column parsers, whose populated forms the committed fixture lacks.
    page = "\n".join(
        [
            'ScheDule a: aSSetS anD "unearneD" income',
            "asset owner value of asset income income tx. >",
            "Apple Inc. (AAPL) [ST] JT $1,001 - $15,000 Dividends $1 - $200 gfedc",
            "ScheDule B: tranSactionS",
            "asset owner Date tx. amount cap.",
            "UBS Account ⇒ SP 04/21/2020 P $1,001 - $15,000 gfedc",
            "IShares Broad USD Investment Grade Corporate Bond [MF]",
            "ScheDule c: earneD income",
            "None disclosed.",
            "ScheDule D: liabilitieS",
            "owner creditor Date incurred type amount of",
            "PennyMac Loan Services December 2015 Home Mortgage $100,001 -",
            "$250,000",
            "ScheDule e: PoSitionS",
            "None disclosed.",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    assert sorted(body.schedules) == ["A", "B", "D"]
    assert "C" not in body.schedules  # None disclosed → absent
    assert "E" not in body.schedules

    b = body.schedules["B"]
    assert len(b) == 1
    assert b[0]["transaction_date"] == "2020-04-21"
    assert b[0]["transaction_type"] == "P"
    assert b[0]["owner"] == "SP"
    assert b[0]["amount_range"]["low"] == 1001
    assert "UBS Account" in b[0]["asset"]
    assert b[0]["raw_text"].strip()

    d = body.schedules["D"]
    assert len(d) == 1
    assert d[0]["creditor"] == "PennyMac Loan Services"
    assert d[0]["date_incurred"] == "December 2015"
    assert d[0]["liability_type"] == "Home Mortgage"
    # The amount range wraps to the next line and folds back in.
    assert d[0]["amount_range"] == {
        "low": 100001,
        "high": 250000,
        "label": "$100,001 - $250,000",
    }


def test_schedule_b_partial_sale_marker_survives(monkeypatch):
    # #6: "S (partial)" must parse to "S(partial)", not collapse to a bare "S".
    page = "\n".join(
        [
            "ScheDule B: tranSactionS",
            "asset owner Date tx. amount cap.",
            "UBS Account (XYZ) [ST] ⇒ SP 04/21/2020 S (partial) $1,001 - $15,000 gfedc",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    b = body.schedules["B"]
    assert len(b) == 1
    assert b[0]["transaction_type"] == "S(partial)"


def test_schedule_d_pre_anchor_row_is_not_dropped(monkeypatch):
    # A Schedule D liability row whose "Date incurred" is blank does NOT match the
    # date item-start anchor. Before the fix it (and its wrapped amount line) were
    # silently dropped — no raw_text, no manifest entry. Now the pre-anchor lines
    # are salvaged into a leading raw item so the row's verbatim text survives.
    page = "\n".join(
        [
            "ScheDule D: liabilitieS",
            "owner creditor Date incurred type amount of",
            # Undated liability row — no Date incurred, so no anchor match.
            "JT CapitalOne Mortgage $100,001 -",
            "$250,000",
            # A normal dated row after it, which DOES anchor.
            "SP PennyMac Loan Services December 2015 Home Mortgage $15,001 - $50,000",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    d = body.schedules["D"]
    # Both rows present: the salvaged pre-anchor row + the dated row. Nothing lost.
    assert len(d) == 2
    all_raw = " ".join(item["raw_text"] for item in d)
    # The undated row's verbatim text (creditor + both wrapped amount halves)
    # survives somewhere in the parsed items.
    assert "CapitalOne Mortgage" in all_raw
    assert "$100,001 -" in all_raw and "$250,000" in all_raw
    # The dated row still parses structured as before.
    dated = next(i for i in d if i["date_incurred"] == "December 2015")
    assert dated["creditor"] == "PennyMac Loan Services"


# --- glyphs-lost (NUL) rendering: real fixture + synthetic edges ---------------


def test_nulglyph_segmentation_and_absent_schedules():
    # Before the NUL-tolerant heading matcher this fully-populated 2021 annual FD
    # raised NotAnFdBody (→ extract_failed) because "Schedule" rendered as
    # S + seven NULs. Ground truth per the fixtures README: A, C, E, F populated;
    # B/D/G/H/I are "None disclosed." → absent; the form has no Schedule J.
    body = extract_fd_schedules(ADAMS_NUL)
    assert sorted(body.schedules) == ["A", "C", "E", "F"]


def test_nulglyph_schedule_a_structured_rows():
    # The checkbox glyph is not in the text layer, so the glyphless row anchor
    # (the [TYPE]-tag + value-column signature) must split the two asset rows —
    # not merge them into one salvaged blob with mis-attributed amounts.
    body = extract_fd_schedules(ADAMS_NUL)
    a = body.schedules["A"]
    assert len(a) == 2
    assert a[0]["asset"] == "North Carolina Legislative Retirement System Plan"
    assert a[0]["value_of_asset"]["label"] == "$15,001 - $50,000"
    # Second row's value range wraps (dangling low + trailing high) and refolds.
    assert a[1]["asset"] == "TIAA-CREF Annuity Account"
    assert a[1]["value_of_asset"]["label"] == "$250,001 - $500,000"
    for item in a:
        assert item["raw_text"].strip()


def test_nulglyph_trailer_does_not_leak_into_last_schedule():
    # The exclusions/certification block also renders NUL-stripped ("E\x00…" /
    # "C\x00…"); it must still END the last schedule, not be folded into it as
    # fabricated content (Schedule I here is "None disclosed." → absent).
    body = extract_fd_schedules(ADAMS_NUL)
    all_text = " ".join(
        item["raw_text"]
        for items in body.schedules.values()
        for item in items
    )
    assert "IPO" not in all_text
    assert "CERTIFY" not in all_text
    assert "Digitally Signed" not in all_text


def test_nul_location_description_labels(monkeypatch):
    # In the glyphs-lost rendering the LOCATION:/DESCRIPTION: labels themselves
    # are small-caps → "L\x00{7}:" / "D\x00{10}:". The values after the colon are
    # regular-font content and must still be captured structured.
    nul = "\x00"
    page = "\n".join(
        [
            f"S{nul * 7} A: A{nul * 5} {nul * 3} \"U{nul * 7}\" I{nul * 5}",
            "Asset Owner Value of Asset Income Type(s) Income Tx. >",
            "$1,000?",
            "0.5 acre unimproved property [RP] $1,001 - $15,000 Rent $201 - $1,000",
            f"L{nul * 7}: Bolton/Hinds, MS, US",
            f"D{nul * 10}: rental parcel",
            f"E{nul * 9} {nul * 2} S{nul * 5}, D{nul * 8}, {nul * 2} T{nul * 4} I{nul * 10}",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    item = body.schedules["A"][0]
    assert item["location"] == "Bolton/Hinds, MS, US"
    assert item["description"] == "rental parcel"
    assert item["value_of_asset"]["label"] == "$1,001 - $15,000"


def test_nul_appendix_title_is_not_a_heading(monkeypatch):
    # "Schedules A and B Asset Class Details" renders "S\x00{7} A \x00{3} B …" —
    # it starts S+NUL and names a schedule letter, but carries no "<LETTER>:" so
    # it must NOT open a fake schedule; it folds into the last schedule's content
    # exactly as its letters-survive form does on intact documents.
    nul = "\x00"
    page = "\n".join(
        [
            f"S{nul * 7} E: P{nul * 8}",
            "Position Name of Organization",
            "Board member Some Nonprofit, Inc.",
            f"S{nul * 7} A {nul * 3} B A{nul * 4} C{nul * 4} D{nul * 6}",
            "Charles Schwab JT TEN (Owner: JT)",
            f"C{nul * 12} {nul * 3} S{nul * 8}",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    assert sorted(body.schedules) == ["E"]
    raw = " ".join(i["raw_text"] for i in body.schedules["E"])
    assert "Charles Schwab JT TEN" in raw  # appendix folded, not dropped


def test_nul_extension_cover_sheet_still_not_an_fd_body(monkeypatch):
    # A glyphs-lost extension/cover sheet (small-caps titles render as NUL runs
    # but there are no "S… <LETTER>:" headings) must STILL raise NotAnFdBody —
    # the NUL branch must not fabricate a body out of title furniture.
    nul = "\x00"
    page = "\n".join(
        [
            "Filing ID #30011729",
            f"F{nul * 8} D{nul * 9} E{nul * 8} R{nul * 6}",
            "House Members ... are permitted to request an extension ...",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    with pytest.raises(NotAnFdBody):
        extract_fd_schedules(Path("synthetic.pdf"))


# --- failure paths -------------------------------------------------------------


def test_extension_cover_sheet_is_not_an_fd_body(monkeypatch):
    # An e-filed extension/cover sheet has no schedule headings → NotAnFdBody,
    # which the parse caller treats as "no body", not an error.
    page = (
        "Filing ID #30011729\n"
        "Financial Disclosure Extension Request\n"
        "House Members ... are permitted to request an extension ...\n"
    )
    _fake_pdfplumber(monkeypatch, [page])
    with pytest.raises(NotAnFdBody):
        extract_fd_schedules(Path("synthetic.pdf"))


def test_corrupt_pdf_raises_extract_error(tmp_path):
    bogus = tmp_path / "10000001.pdf"
    bogus.write_text("this is plainly not an FD body\n")
    with pytest.raises(PdfExtractError):
        extract_fd_schedules(bogus)


# --- parse integration: body file written at parsed/<year>/fd/<DocID>.json -----

_ONE_FD_XML = """<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
  <Member>
    <Last>Thompson</Last><First>Bennie G.</First><Suffix></Suffix>
    <FilingType>O</FilingType><StateDst>MS02</StateDst>
    <Year>2020</Year><FilingDate>8/12/2021</FilingDate><DocID>10042852</DocID>
  </Member>
</FinancialDisclosure>
"""


def _seed_one_fd(data_dir: Path, pdf_src: Path, *, year: int = 2020) -> None:
    raw = data_dir / "raw" / str(year)
    (raw / "fd").mkdir(parents=True, exist_ok=True)
    (raw / f"{year}FD.xml").write_text(_ONE_FD_XML)
    (raw / "fd" / "10042852.pdf").write_bytes(pdf_src.read_bytes())


def test_parse_writes_fd_body_file(tmp_path):
    _seed_one_fd(tmp_path, THOMPSON)
    records = build_filing_records(tmp_path / "raw" / "2020" / "2020FD.xml", 2020)
    parsed_dir = tmp_path / "parsed" / "2020"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    unparsed = _classify_records(
        records,
        data_dir=tmp_path,
        types=["ptr", "fd"],
        year=2020,
        parsed_dir=parsed_dir,
    )
    body_path = parsed_dir / "fd" / "10042852.json"
    assert body_path.exists()
    body = json.loads(body_path.read_text())
    # Exact contract shape: a single "schedules" key holding the letter map.
    assert list(body.keys()) == ["schedules"]
    assert sorted(body["schedules"]) == ["A", "C", "E", "F"]
    assert records[0].parse_status == "ok"
    assert records[0].pdf_class == "efiled"
    # An efiled annual FD that extracted cleanly is NOT in the unparsed manifest.
    assert not unparsed


def test_parse_extension_writes_no_fd_body(tmp_path, monkeypatch):
    # An efiled fd-family extension (no schedules) stays efiled/ok with no body
    # file and no unparsed entry — present in filings.json, never dropped.
    xml = _ONE_FD_XML.replace("<FilingType>O</FilingType>", "<FilingType>X</FilingType>")
    raw = tmp_path / "raw" / "2020"
    (raw / "fd").mkdir(parents=True, exist_ok=True)
    (raw / "2020FD.xml").write_text(xml)
    (raw / "fd" / "10042852.pdf").write_text("placeholder")

    # classify → efiled; extract_fd_schedules → NotAnFdBody (no headings).
    monkeypatch.setattr("openhouse.parse.classify", lambda _p: "efiled")

    def _raise_not_fd(_p):
        raise NotAnFdBody("no headings")

    monkeypatch.setattr("openhouse.parse.extract_fd_schedules", _raise_not_fd)

    records = build_filing_records(raw / "2020FD.xml", 2020)
    parsed_dir = tmp_path / "parsed" / "2020"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    unparsed = _classify_records(
        records,
        data_dir=tmp_path,
        types=["ptr", "fd"],
        year=2020,
        parsed_dir=parsed_dir,
    )
    assert records[0].parse_status == "ok"
    assert records[0].pdf_class == "efiled"
    assert not (parsed_dir / "fd" / "10042852.json").exists()
    assert not unparsed


def test_parse_annual_fd_with_lost_headings_is_extract_failed(tmp_path, monkeypatch):
    # A genuine annual report (FilingType O) whose body renders with the schedule
    # headings fully lost would raise NotAnFdBody — but for an annual-report type
    # that is a REAL extraction failure (an invisible gap), not a benign cover
    # sheet. It must land in the unparsed manifest as extract_failed (status
    # error), never silently ok with no body.
    raw = tmp_path / "raw" / "2020"
    (raw / "fd").mkdir(parents=True, exist_ok=True)
    (raw / "2020FD.xml").write_text(_ONE_FD_XML)  # FilingType O
    (raw / "fd" / "10042852.pdf").write_text("placeholder")

    monkeypatch.setattr("openhouse.parse.classify", lambda _p: "efiled")

    def _raise_not_fd(_p):
        raise NotAnFdBody("headings lost")

    monkeypatch.setattr("openhouse.parse.extract_fd_schedules", _raise_not_fd)

    records = build_filing_records(raw / "2020FD.xml", 2020)
    parsed_dir = tmp_path / "parsed" / "2020"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    unparsed = _classify_records(
        records,
        data_dir=tmp_path,
        types=["ptr", "fd"],
        year=2020,
        parsed_dir=parsed_dir,
    )
    assert records[0].filing_type.code == "O"
    assert records[0].parse_status == "error"
    assert records[0].pdf_class is None
    assert not (parsed_dir / "fd" / "10042852.json").exists()
    assert [u["reason"] for u in unparsed] == ["extract_failed"]
