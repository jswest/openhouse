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
import re
from pathlib import Path

import pytest

from openhouse.index import build_filing_records
from openhouse.parse import _classify_records
from openhouse.pdf import (
    NotAnFdBody,
    PdfExtractError,
    _parse_schedule_e,
    _scrub_field,
    _scrub_raw_text,
    extract_fd_schedules,
)


def test_schedule_parser_skips_nul_only_furniture_line():
    # A NUL-only line (glyphless furniture — NUL isn't stripped by str.strip) must
    # not become an empty-raw_text item; it carries no filer content, so _group_items
    # skips it (matching _salvage_raw). Critic 🟢 finding.
    assert _parse_schedule_e(["\x00\x00\x00"]) == []
    # A real row beside furniture still yields exactly one item.
    items = _parse_schedule_e(["\x00\x00", "Board Member  Acme Corp"])
    assert len(items) == 1
    assert items[0].raw_text == "Board Member Acme Corp"

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
    # 27 asset rows on this form. The pre-GH-0070 anchor counted 25: the two
    # "Public Employees' Retirement System of Mississippi … [DB]" rows wrap
    # the [TYPE] tag off the glyph-terminated row line and silently merged
    # into the Prudential row. The glyph-terminated-line anchor recovers them
    # and the [TYPE]-count completeness guard pins the total.
    assert len(a) == 27
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


# --- GH-0100: wrapped-[TYPE] / ⇒-subholding rows are not silently dropped ------
# Each fixture mirrors a real drop from the 2026-06-13 parse-validation sweep,
# glyphless as on the Candidate/New-Filer forms where the row's value line carries
# no checkbox to anchor on.


def test_schedule_a_wrapped_type_tag_row_anchors_separately(monkeypatch):
    # The asset name + [TYPE] tag wraps so the value prints on the first physical
    # line and the tag (``[GS]``) on the next; the row must anchor on its own value
    # low, not fold into the row above (Pascrell 10046898 PASSAIC bond — a #70
    # regression). Before the fix the bond vanished and its $1,001-$15,000 was
    # mis-captured as the preceding row's income.
    page = "\n".join(
        [
            'ScheDule a: aSSetS anD "unearneD" income',
            "asset owner value of asset income income tx. >",
            "Morgan Stanley [BA] JT $250,001 - Interest $2,501 -",
            "$500,000 $5,000",
            "Passaic County Authority Lease JT $1,001 - $15,000 Interest $201 - $1,000",
            "Refunding Bond [GS]",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    a = extract_fd_schedules(Path("synthetic.pdf")).schedules["A"]
    assert len(a) == 2
    ms = next(i for i in a if i["asset"].startswith("Morgan Stanley"))
    bond = next(i for i in a if "Refunding Bond" in i["asset"])
    # the bond is its own structured row now, with its own value and type
    assert bond["asset_type"] == "GS"
    assert bond["owner"] == "JT"
    assert bond["value_of_asset"] == {
        "low": 1001,
        "high": 15000,
        "label": "$1,001 - $15,000",
    }
    # and its value is NOT mis-captured as Morgan Stanley's income
    assert ms["value_of_asset"] == {
        "low": 250001,
        "high": 500000,
        "label": "$250,001 - $500,000",
    }
    assert ms["income_amount"] == {
        "low": 2501,
        "high": 5000,
        "label": "$2,501 - $5,000",
    }


def test_schedule_a_none_value_wrapped_tag_row_anchors_via_lookahead(monkeypatch):
    # A None-value row whose [TYPE] tag wrapped carries no ``$lo -`` value low to
    # anchor on; the one-line lookahead at the wrapped tag-tail (``[MF]``) recovers
    # it (Harris 10054295's 403b holdings). The row is no longer dropped — its
    # verbatim text and type survive.
    page = "\n".join(
        [
            'ScheDule a: aSSetS anD "unearneD" income',
            "asset owner value of asset income income tx. >",
            "403b American Century Global Gold [MF] None Tax-Deferred",
            "403b American Century International Opportunities None Tax-Deferred",
            "[MF]",
            "403b CREF Inflation Linked Bond [MF] $100,001 - Tax-Deferred",
            "$250,000",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    a = extract_fd_schedules(Path("synthetic.pdf")).schedules["A"]
    assert len(a) == 3
    intl = next(i for i in a if "International Opportunities" in i["asset"])
    assert intl["asset_type"] == "MF"
    assert "International Opportunities" in intl["asset"]
    assert "[MF]" in intl["raw_text"]


def test_schedule_a_buried_subholding_cluster_reconstructed(monkeypatch):
    # A ⇒ subholding cluster whose value prints on the umbrella-name line, the
    # arrow on the next, the subholding's own name on the third, and its [TYPE] tag
    # on the fourth (Pou 10068928's deferred-comp plan). Each subholding becomes
    # its own row anchored on its value low — and the pure-name line must NOT split
    # off on its own (it carries no value column), so the count is exactly two.
    page = "\n".join(
        [
            'ScheDule a: aSSetS anD "unearneD" income',
            "asset owner value of asset income income tx. >",
            "New Jersey State Employees Deferred $15,001 - $50,000 Tax-Deferred",
            "Compensation Plan ⇒",
            "BNY Mellon Small Cap Value Fund Class I (STSVX)",
            "[MF]",
            "New Jersey State Employees Deferred $1,001 - $15,000 Tax-Deferred",
            "Compensation Plan ⇒",
            "DCP Equity Fund [OT]",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    a = extract_fd_schedules(Path("synthetic.pdf")).schedules["A"]
    assert len(a) == 2
    bny = next(i for i in a if "BNY Mellon" in i["asset"])
    assert bny["asset_type"] == "MF"
    assert bny["value_of_asset"] == {
        "low": 15001,
        "high": 50000,
        "label": "$15,001 - $50,000",
    }
    dcp = next(i for i in a if "DCP Equity" in i["asset"])
    assert dcp["asset_type"] == "OT"
    assert dcp["value_of_asset"] == {
        "low": 1001,
        "high": 15000,
        "label": "$1,001 - $15,000",
    }


def test_schedule_a_unsplit_merge_flags_residual(monkeypatch):
    # When two assets fuse into one anchored row that no signal can split (≥2 real
    # [TYPE] codes in one row), the schedule is flagged ``incomplete_schedules`` so
    # ``parse`` emits a ``schedule_incomplete`` residual — the buried asset is loud
    # (its raw_text is intact), never a silent drop.
    page = "\n".join(
        [
            'ScheDule a: aSSetS anD "unearneD" income',
            "asset owner value of asset income income tx. >",
            "Brokerage Account ⇒ $1,001 - $15,000 None",
            "First Buried Fund [MF]",
            "Second Buried Fund [ST]",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    assert body.incomplete_schedules == ["A"]
    raw = " ".join(i["raw_text"] for i in body.schedules["A"])
    assert "First Buried Fund" in raw and "Second Buried Fund" in raw
    assert "[MF]" in raw and "[ST]" in raw


def test_schedule_a_ticker_in_name_is_not_a_merge_residual(monkeypatch):
    # A real second bracket that is a ticker (``[VOO]``) or footnote, not a type
    # code, must NOT trip the merge residual — only a second *real* asset-type code
    # means a fused row (GH-0100).
    page = "\n".join(
        [
            'ScheDule a: aSSetS anD "unearneD" income',
            "asset owner value of asset income income tx. >",
            "Vanguard S&P 500 [VOO] [MF] $1,001 - $15,000 Dividends $1 - $200",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    assert body.incomplete_schedules == []
    assert len(body.schedules["A"]) == 1


# --- Schedule C: earned income, structured ------------------------------------


def test_schedule_c_items():
    body = extract_fd_schedules(THOMPSON)
    c = body.schedules["C"]
    assert len(c) == 4
    miss = next(i for i in c if i["raw_text"].startswith("State of Mississippi Member"))
    assert miss["amount"] == "$11,195.00"
    assert miss["raw_text"].startswith("State of Mississippi Member Retirement")


def test_schedule_c_multiword_type_does_not_bleed_into_source():
    # GH-0101: the Type column is multi-word on the real form and folds the owner
    # column in front of it. A last-whitespace split bled the Type's leading
    # word(s) into ``source`` and truncated ``income_type`` to its final word.
    # The vocabulary split must keep the full Type out of the source. Asserted on
    # the committed Thompson fixture (real-fixture reproduction, not synthetic).
    c = extract_fd_schedules(THOMPSON).schedules["C"]

    # "State of Mississippi Member Retirement Plan $11,195.00": pre-fix source
    # gained "Member Retirement", income_type was truncated to "Plan".
    member = next(i for i in c if i["raw_text"].startswith("State of Mississippi Member"))
    assert member["source"] == "State of Mississippi"
    assert member["income_type"] == "Member Retirement Plan"

    # "... Benefit Payment Services Spouse Pension N/A": pre-fix source gained
    # "Spouse", income_type was just "Pension".
    pension = next(i for i in c if "Benefit Payment Services" in i["raw_text"])
    assert pension["source"] == "The Northern Trust Company, Benefit Payment Services"
    assert pension["income_type"] == "Spouse Pension"

    # "AXA Equitable Annuity Spouse Annuity Plan N/A": "Annuity" also appears in
    # the SOURCE (company name) — the tail-anchored Type must not swallow it.
    annuity = next(i for i in c if i["raw_text"].startswith("AXA"))
    assert annuity["source"] == "AXA Equitable Annuity"
    assert annuity["income_type"] == "Spouse Annuity Plan"


def test_schedule_c_spouse_pension_prefixed_type_does_not_bleed_into_source():
    # GH-0131: closed #101 fixed the multi-word and ``Spouse``-prefixed Type cases,
    # but two prefixed-Type shapes still bled their leading token into ``source``:
    #   - an all-caps owner token (``SPOUSE``) — #101 only listed ``Spouse``, so the
    #     all-caps owner fell through to the last-token split and lodged in source;
    #   - a Type that *begins with* ``Pension`` and runs two words (``Pension Plan``)
    #     — ``Pension Plan`` was not a vocabulary phrase, so the split truncated
    #     income_type to ``Plan`` and shoved ``Pension`` into source.
    # The named filings (10068086, 5 rows; 10057260) are not checked into
    # tests/fixtures/, so we reproduce their row shapes synthetically (the #101/#97
    # convention), asserting the CORRECT source AND income_type for each row.
    from openhouse.pdf import _parse_schedule_c

    # 10068086 — five Schedule C rows mixing the regressing shapes with the cases
    # #101 already handled (those must stay green here too).
    rows = _parse_schedule_c(
        [
            "Acme Industries SPOUSE Salary $50,000.00",            # all-caps owner + Type
            "Teachers Retirement System Pension Plan N/A",        # Type begins with Pension
            "State of Mississippi Member Retirement Plan $11,195.00",  # #101: owner + 2-word Type
            "AXA Equitable Annuity Spouse Annuity Plan N/A",      # #101: phrase also in source
            "Consulting LLC Professional Services $25,000.00",    # #101: 2-word Type, no owner
        ]
    )
    assert len(rows) == 5
    by_src = {r.source: r for r in rows}

    assert by_src["Acme Industries"].income_type == "SPOUSE Salary"
    assert by_src["Acme Industries"].amount == "$50,000.00"

    assert by_src["Teachers Retirement System"].income_type == "Pension Plan"
    assert by_src["Teachers Retirement System"].amount == "N/A"

    assert by_src["State of Mississippi"].income_type == "Member Retirement Plan"
    assert by_src["AXA Equitable Annuity"].income_type == "Spouse Annuity Plan"
    assert by_src["Consulting LLC"].income_type == "Professional Services"

    # 10057260 — a SPOUSE-prefixed pension row: the owner token stays in the Type.
    (row,) = _parse_schedule_c(["Northern Trust Company SPOUSE Pension N/A"])
    assert row.source == "Northern Trust Company"
    assert row.income_type == "SPOUSE Pension"
    assert row.amount == "N/A"


def test_schedule_c_unknown_multiword_type_degrades_safely(monkeypatch):
    # An UNKNOWN Type (not in the vocabulary) falls back to the single-token
    # split rather than being dropped — and the verbatim row survives in
    # raw_text either way (CLAUDE.md: never silently drop).
    from openhouse.pdf import _parse_schedule_c

    rows = _parse_schedule_c(["Weird Co Mystery Compensation $5.00"])
    assert len(rows) == 1
    row = rows[0]
    assert row.amount == "$5.00"
    assert row.income_type == "Compensation"  # last-token fallback
    assert row.source == "Weird Co Mystery"
    assert row.raw_text == "Weird Co Mystery Compensation $5.00"


# --- Schedules E–J: structured columns, every item keeps verbatim raw_text ----


def test_schedule_e_positions_structured():
    body = extract_fd_schedules(THOMPSON)
    e = body.schedules["E"]
    assert e, "Schedule E (positions) has data on this form"
    # Columns are position/organization; every item still carries raw_text.
    for item in e:
        assert set(item.keys()) == {"position", "organization", "raw_text"}
        assert item["raw_text"].strip()
    blb = next(i for i in e if "BLB Properties" in i["raw_text"])
    assert blb["position"] == "President"
    assert blb["organization"] == "BLB Properties"
    # A multi-word title splits too.
    board = next(i for i in e if "Housing Assistance Council" in i["raw_text"])
    assert board["position"] == "Board Member"
    assert board["organization"] == "Housing Assistance Council"


def test_schedule_f_agreements_structured():
    body = extract_fd_schedules(THOMPSON)
    f = body.schedules["F"]
    assert f
    for item in f:
        assert set(item.keys()) == {"date", "parties", "terms", "raw_text"}
        assert item["raw_text"].strip()
    # The agreement date splits off the leading "Month YYYY"; the wrapped terms
    # fold into one verbatim raw_text.
    first = f[0]
    assert first["date"] == "February 2008"
    assert "State of Mississippi" in first["raw_text"]
    assert "payable to beneficiaries." in first["raw_text"]


def test_every_ej_item_keeps_raw_text():
    # The binding invariant: every E–J line item carries verbatim raw_text, so a
    # column the parser cannot read loses nothing (CLAUDE.md).
    body = extract_fd_schedules(THOMPSON)
    for letter in ("E", "F"):
        for item in body.schedules.get(letter, []):
            assert item["raw_text"].strip()


def test_schedule_g_h_i_j_structured_synthetic(monkeypatch):
    # A synthetic FD exercising the G/H/I/J parsers, whose populated forms the
    # committed fixtures lack (G–I render "None disclosed."; J is absent).
    page = "\n".join(
        [
            "ScheDule g: giftS",
            "Source Description Value",
            "Acme Foundation Crystal vase $450.00",
            "ScheDule H: travel PaymentS anD reimburSementS",
            "Source Dates Location Items",
            "Policy Institute 06/01/2020 - 06/03/2020 Aspen, CO Lodging, airfare",
            "ScheDule i: PaymentS maDe to cHarity in lieu of Honoraria",
            "Source Activity Date Amount",
            "State University Lecture 03/15/2020 $2,000.00",
            "ScheDule J: comPenSation in exceSS of $5,000 PaiD bY one Source",
            "Source Brief Description of Duties",
            "Old Employer LLC Senior advisory and consulting duties",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    assert sorted(body.schedules) == ["G", "H", "I", "J"]

    g = body.schedules["G"]
    assert len(g) == 1
    assert set(g[0].keys()) == {"source", "description", "value", "raw_text"}
    assert g[0]["value"] == "$450.00"
    assert g[0]["source"] == "Acme Foundation Crystal vase"
    assert g[0]["raw_text"].strip()

    h = body.schedules["H"]
    assert len(h) == 1
    assert set(h[0].keys()) == {"source", "dates", "location", "items", "raw_text"}
    assert "Aspen, CO" in h[0]["raw_text"]

    i = body.schedules["I"]
    assert len(i) == 1
    assert set(i[0].keys()) == {"source", "activity", "date", "amount", "raw_text"}
    assert i[0]["amount"] == "$2,000.00"
    assert i[0]["source"].startswith("State University")

    j = body.schedules["J"]
    assert len(j) == 1
    assert set(j[0].keys()) == {"source", "description", "raw_text"}
    assert "advisory and consulting" in j[0]["raw_text"]


def test_schedule_e_editor_title_structures(monkeypatch):
    # GH-0103: Pan 2023/10055778 Schedule E row 2 is ``Editor Telos Press`` —
    # the same shape as a row 1 ``Treasurer`` that splits fine, but ``Editor``
    # was absent from the position-title vocabulary, so position/organization
    # came back null while raw_text stayed intact (inconsistency in one schedule).
    # The PDF isn't a committed fixture; the title-list mechanism it exercises is
    # validated on the THOMPSON real fixture (test_schedule_e_positions_structured).
    page = "\n".join(
        [
            "ScheDule e: PoSitionS",
            "Position Name of Organization",
            "Treasurer Telos Press",
            "Editor Telos Press",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    e = extract_fd_schedules(Path("synthetic.pdf")).schedules["E"]
    assert len(e) == 2
    # Row 1 structures (as it always did) AND row 2 now structures the same way —
    # consistent within the schedule, not null-while-its-twin-splits.
    assert e[0]["position"] == "Treasurer"
    assert e[1]["position"] == "Editor"
    assert e[1]["organization"] == "Telos Press"
    assert e[1]["raw_text"] == "Editor Telos Press"


def test_schedule_h_banner_skipped_and_itinerary_coalesced(monkeypatch):
    # GH-0103: Green 2020/10040812 Schedule H shreds — the parser neither skips
    # the column-header banner (``Source Dates Location Items``) nor coalesces the
    # multi-line itinerary, so one trip becomes garbled header/fragment rows. The
    # PDF isn't a committed fixture; this reproduces the shape synthetically: a
    # banner + a trip whose Location/Items wrap across three physical lines.
    page = "\n".join(
        [
            "ScheDule H: travel PaymentS anD reimburSementS",
            "Source Dates Location Items",
            "Policy Institute 06/01/2020 - 06/03/2020 Aspen, Colorado",
            "Lodging, airfare, and conference",
            "registration fees",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    h = extract_fd_schedules(Path("synthetic.pdf")).schedules["H"]
    # The banner is dropped (not an item); the three-line trip coalesces into ONE
    # structured item rather than shredding into a header + fragment rows.
    assert len(h) == 1
    assert set(h[0].keys()) == {"source", "dates", "location", "items", "raw_text"}
    assert h[0]["source"] == "Policy Institute"
    assert h[0]["dates"] == "06/01/2020 - 06/03/2020"
    # The wrapped Location/Items lines are folded into the one row's raw_text.
    assert "Aspen, Colorado" in h[0]["raw_text"]
    assert "registration fees" in h[0]["raw_text"]
    assert "Source Dates Location Items" not in h[0]["raw_text"]


def test_schedule_h_yearless_slash_in_continuation_does_not_anchor(monkeypatch):
    # GH-0103 critic: the Dates anchor must not fire on a *yearless* ``M/D`` slash
    # buried in a wrapped Location/Items line (``1/2 day``, ``9/11 Memorial``).
    # Pre-fix that split one trip into two items and fabricated a ``dates`` value
    # (``1/2``) the filer never wrote — violating degrade-not-fabricate and the
    # very coalescing this schedule's parser exists to do.
    page = "\n".join(
        [
            "ScheDule H: travel PaymentS anD reimburSementS",
            "Source Dates Location Items",
            "Heritage Foundation 06/01/2020 - 06/03/2020 Washington, DC",
            "Lodging, 1/2 day conference, meals, and a",
            "visit to the 9/11 Memorial Museum",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    h = extract_fd_schedules(Path("synthetic.pdf")).schedules["H"]
    # ONE trip — the yearless fragments do not anchor spurious extra items.
    assert len(h) == 1
    assert h[0]["dates"] == "06/01/2020 - 06/03/2020"
    # No fabricated date: ``1/2`` / ``9/11`` never become a structured ``dates``.
    assert all(item["dates"] != "1/2" and item["dates"] != "9/11" for item in h)
    # The wrapped continuation text is preserved in the single row's raw_text.
    assert "9/11 Memorial Museum" in h[0]["raw_text"]


def test_schedule_h_nul_glyph_header_not_emitted_as_row(monkeypatch):
    # #133 (FABRICATION; verified on 10054295 / 10059679, both NUL-rendered annual
    # FDs): in the glyphs-lost rendering the Schedule H column-header line extracts
    # as NUL runs (``Source Dates Location Items`` → ``S\x00+ D\x00+ L\x00+ I\x00+``),
    # so the intact-letter ``Source Date`` furniture branch misses it and the banner
    # leaks as a phantom raw_text row (structured fields null). The header must be
    # recognized and skipped before row extraction.
    header = f"S{chr(0) * 5} D{chr(0) * 4} L{chr(0) * 7} I{chr(0) * 4}"
    page = "\n".join(
        [
            f"S{chr(0) * 7} H: t{chr(0) * 30}",  # ScheDule H: travel… (NUL heading)
            header,
            "Policy Institute 06/01/2020 - 06/03/2020 Aspen, Colorado",
            "Lodging and conference registration fees",
            f"C{chr(0) * 12} a{chr(0) * 2} S{chr(0) * 8}",  # certification… trailer
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    h = extract_fd_schedules(Path("synthetic.pdf")).schedules["H"]
    # The header is NOT a row: exactly one item (the real trip), no phantom record.
    assert len(h) == 1
    # No item carries the header banner as its raw_text (the fabrication signature).
    assert all(chr(0) not in item["raw_text"] for item in h)
    assert all("Source" not in item["raw_text"] for item in h)
    # The real H row degrades to raw_text as designed: source/dates split off, the
    # by-design Location/Items merge stays null, full row preserved in raw_text.
    assert h[0]["source"] == "Policy Institute"
    assert h[0]["dates"] == "06/01/2020 - 06/03/2020"
    assert h[0]["location"] is None
    assert h[0]["items"] is None
    assert "Aspen, Colorado" in h[0]["raw_text"]


def test_schedule_j_nul_glyph_header_not_emitted_and_row_degrades(monkeypatch):
    # #133 (FABRICATION; verified on 10061936, a NUL-rendered annual FD): Schedule J
    # has no structured parser — every real row degrades to a raw_text-only item by
    # design (its two columns merge with no stable delimiter). The NUL-rendered
    # column header (``Source Description of Duties`` → ``S\x00+ D\x00+ … D\x00+``)
    # must be skipped so it is not salvaged into a phantom raw_text row.
    header = f"S{chr(0) * 5} D{chr(0) * 10} of D{chr(0) * 5}"
    page = "\n".join(
        [
            # ScheDule J: comPenSation… (NUL heading)
            f"S{chr(0) * 7} J: c{chr(0) * 40}",
            header,
            "Acme Corporation Senior advisory and consulting services",
            f"C{chr(0) * 12} a{chr(0) * 2} S{chr(0) * 8}",  # certification trailer
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    j = extract_fd_schedules(Path("synthetic.pdf")).schedules["J"]
    # The header is NOT a row: exactly one item (the real disclosure), no phantom.
    assert len(j) == 1
    assert all(chr(0) not in item["raw_text"] for item in j)
    # The real J row degrades to raw_text as designed: all structured columns null,
    # the full row carried verbatim in raw_text (J has no column parser).
    assert j[0]["raw_text"] == "Acme Corporation Senior advisory and consulting services"
    assert all(v is None for k, v in j[0].items() if k != "raw_text")


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


def test_schedule_b_transposed_year_date_is_flagged_not_accepted(monkeypatch):
    # GH-0113 on Schedule B: a transposed-digit year (``2202``) in the Date column
    # is rejected by the sanity range — structured ``transaction_date`` None, raw
    # string preserved on ``transaction_date_raw`` — while the rest of the row (the
    # type, amount, asset) is intact. ``max_year`` is the entry year + 1, threaded
    # down; a fixed offline value here, never wall-clock.
    page = "\n".join(
        [
            "ScheDule B: tranSactionS",
            "asset owner Date tx. amount cap.",
            "UBS Account (XYZ) [ST] ⇒ SP 09/19/2202 S $1,001 - $15,000 gfedc",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"), max_year=2026)
    b = body.schedules["B"]
    assert len(b) == 1
    # NOT emitted as a valid year-2202 date.
    assert b[0]["transaction_date"] is None
    # Raw string preserved as the per-row anomaly flag.
    assert b[0]["transaction_date_raw"] == "09/19/2202"
    # The rest of the row is intact — never dropped over one bad date.
    assert b[0]["transaction_type"] == "S"
    assert b[0]["owner"] == "SP"
    assert b[0]["amount_range"]["low"] == 1001


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


def test_empty_trailing_schedule_does_not_absorb_appendix(monkeypatch):
    # #97: an empty trailing schedule (``None disclosed.``) must terminate BEFORE
    # the post-table "Asset Class Details" appendix. Otherwise the appendix asset
    # lines are salvaged into fabricated rows for a schedule the filer left blank
    # (Bucshon 2020/10040126: Schedule I "None disclosed." → 6 phantom rows).
    page = "\n".join(
        [
            "ScheDule a: aSSetS anD \"unearneD\" income",
            "asset owner value of asset income income tx. >",
            "Deaconess 401(k) Plan [IH] JT $1,001 - $15,000 None",
            # Trailing Schedule I, marked empty by the filer.
            "ScheDule i: PaymentS maDe to cHarity in lieu of Honoraria",
            "None disclosed.",
            # The post-table appendix (intact / case-mangled glyphs). Its lines
            # are an asset-class legend, NOT Schedule I disclosures.
            "ScheDule a anD B aSSet claSS DetailS",
            "Deaconess 401(k) Plan",
            "Schwab Brokerage Account",
            "Schwab IRA Rollover #1",
            "Schwab Roth IRA",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    # A has its real row; I was "None disclosed." → absent, NOT fabricated rows.
    assert "I" not in body.schedules
    assert sorted(body.schedules) == ["A"]
    # The appendix asset names never surface as disclosed content anywhere.
    all_raw = " ".join(
        item["raw_text"] for items in body.schedules.values() for item in items
    )
    assert "Schwab Brokerage Account" not in all_raw
    assert "Schwab IRA Rollover" not in all_raw


def test_empty_trailing_schedule_nul_appendix_not_fabricated(monkeypatch):
    # #97, glyph-collapse rendering: the small-caps appendix title flattens to its
    # initials ("S A A C D" = Schedule A Asset Class Details), so neither the
    # heading nor the trailer regex fired and the appendix bled into the empty
    # trailing schedule (Pan 2023/10055778: Schedule J "None disclosed." → phantom
    # "Non-federal Retirement Accounts" rows).
    nul = "\x00"
    page = "\n".join(
        [
            f"S{nul * 7} E: P{nul * 8}",
            "Position Name of Organization",
            "Board member Some Nonprofit, Inc.",
            # Trailing Schedule J, empty.
            f"S{nul * 7} J: C{nul * 10}",
            "None disclosed.",
            # Appendix title collapsed to "S A A C D" (no "<LETTER>:", no phrase).
            f"S{nul * 7} A A{nul * 4} C{nul * 4} D{nul * 6}",
            "Non-federal Retirement Accounts",
            "Charles Schwab JT TEN (Owner: JT)",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    # E keeps its real row; J was "None disclosed." → absent, not fabricated.
    assert "J" not in body.schedules
    assert sorted(body.schedules) == ["E"]
    all_raw = " ".join(
        item["raw_text"] for items in body.schedules.values() for item in items
    )
    assert "Non-federal Retirement Accounts" not in all_raw
    assert "Charles Schwab" not in all_raw


def test_empty_trailing_schedule_does_not_absorb_nonwhitelisted_appendix(monkeypatch):
    # #130: the empty-trailing-schedule termination must be HEADING-AGNOSTIC. #97
    # only whitelisted the "Asset Class Details" appendix, so any OTHER post-table
    # appendix (e.g. "Investment Vehicle Details") after a ``None disclosed.``
    # trailing schedule still bled in and was fabricated into Schedule I rows
    # (verified on 10057260 / 10059583 / 10059679 / 10068086 (I), 10068928 (J)).
    page = "\n".join(
        [
            "ScheDule a: aSSetS anD \"unearneD\" income",
            "asset owner value of asset income income tx. >",
            "Vanguard 500 Index Fund [MF] JT $1,001 - $15,000 None",
            # Trailing Schedule I, marked empty by the filer.
            "ScheDule i: PaymentS maDe to cHarity in lieu of Honoraria",
            "None disclosed.",
            # A post-table appendix whose title is NOT "Asset Class Details" — its
            # lines are an investment-vehicle key, NOT Schedule I disclosures.
            "Investment Vehicle Details",
            "Vanguard 500 Index Fund",
            "Fidelity Contrafund",
            "American Funds Growth Fund",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    # A keeps its real row; I was "None disclosed." → absent, NOT fabricated rows.
    assert "I" not in body.schedules
    assert sorted(body.schedules) == ["A"]
    all_raw = " ".join(
        item["raw_text"] for items in body.schedules.values() for item in items
    )
    # The appendix vehicle names never surface as disclosed content anywhere.
    assert "Fidelity Contrafund" not in all_raw
    assert "American Funds Growth Fund" not in all_raw


def test_empty_trailing_schedule_j_does_not_absorb_appendix(monkeypatch):
    # #130 (J variant, verified on 10068928): an empty trailing Schedule J followed
    # by ANY post-table appendix terminates before the appendix — no fabricated J.
    page = "\n".join(
        [
            f"S{chr(0) * 7} E: P{chr(0) * 8}",
            "Position Name of Organization",
            "Board member Some Nonprofit, Inc.",
            # Trailing Schedule J, empty.
            "ScheDule J: comPenSation in exceSS of $5,000 PaiD By one Source",
            "None disclosed.",
            # Non-whitelisted appendix material follows.
            "Investment Vehicle Details",
            "Some Managed Account Program",
            "Charles Schwab JT TEN (Owner: JT)",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    # E keeps its real row; J was "None disclosed." → absent, not fabricated.
    assert "J" not in body.schedules
    assert sorted(body.schedules) == ["E"]
    all_raw = " ".join(
        item["raw_text"] for items in body.schedules.values() for item in items
    )
    assert "Some Managed Account Program" not in all_raw
    assert "Charles Schwab" not in all_raw


def test_populated_trailing_schedule_still_folds_appendix(monkeypatch):
    # #130 guard: the heading-agnostic termination must fire ONLY for an explicitly
    # blank (``None disclosed.``) schedule. A trailing schedule with REAL rows still
    # folds a following appendix into its content rather than dropping it — the
    # "never silently drop a filing" agreement. (Without the ``None disclosed.``
    # gate, a naive "no rows yet" test would wrongly terminate populated schedules.)
    page = "\n".join(
        [
            "ScheDule a: aSSetS anD \"unearneD\" income",
            "asset owner value of asset income income tx. >",
            "Vanguard 500 Index Fund [MF] JT $1,001 - $15,000 None",
            # Trailing Schedule I WITH a real disclosed row (not blank).
            "ScheDule i: PaymentS maDe to cHarity in lieu of Honoraria",
            "Habitat for Humanity Speaking fee 2024 $5,000",
            # Appendix follows a populated schedule → folded in, never dropped.
            "Investment Vehicle Details",
            "Vanguard 500 Index Fund",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    assert "I" in body.schedules  # real content kept, not terminated away
    i_raw = " ".join(item["raw_text"] for item in body.schedules["I"])
    assert "Habitat for Humanity" in i_raw  # the real row survives
    assert "Investment Vehicle Details" in i_raw  # appendix folded, not dropped


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


def test_nulglyph_raw_text_is_scrubbed_of_nul_bytes():
    # The glyphs-lost rendering folds NUL furniture into recovered rows; #52
    # scrubs it so no emitted raw_text carries a literal U+0000 — content stays.
    body = extract_fd_schedules(ADAMS_NUL)
    assert body.schedules  # recovered a body at all
    for letter, items in body.schedules.items():
        for item in items:
            assert "\x00" not in item["raw_text"], f"NUL leaked into schedule {letter}"
            assert item["raw_text"].strip(), "row content must still survive"


def test_scrub_raw_text_is_noop_on_nul_free_text():
    # The scrub MUST leave any NUL-free string byte-identical (this is what keeps
    # every intact-rendering 2020 body unchanged). _group_items joins parts with a
    # single space and the inputs are pre-stripped, so such strings are unchanged.
    for s in [
        "0.5 acre unimproved property [RP] $1,001 - $15,000 Rent $201 - $1,000",
        "State of Mississippi Member Retirement [PE] None",
        "None disclosed.",
        "BLB Properties, LLC ⇒ JT",
        "",
    ]:
        assert _scrub_raw_text(s) == s


def test_scrub_raw_text_collapses_nul_runs_and_whitespace():
    nul = "\x00"
    assert _scrub_raw_text(f"S{nul * 7} A: Assets") == "S A: Assets"
    assert _scrub_raw_text(f"L{nul * 7}: Bolton/Hinds, MS") == "L : Bolton/Hinds, MS"
    # Trailing/leading NUL runs strip; interior collapses to one space.
    assert _scrub_raw_text(f"{nul * 3} Cash [BA] {nul * 2} ") == "Cash [BA]"


# --- #52 critic fixes: comments-label trailer, A anchor, NUL-gated fields ------


def test_nul_comments_label_does_not_end_schedule(monkeypatch):
    # 🔴 The per-row COMMENTS: detail label renders "C\x00{7}: <text>" in NUL docs.
    # It must NOT match the trailer branch (which would end the schedule and drop
    # every following content row); it folds into the row's raw_text instead, like
    # the intact-glyph "Comments:" label. A *real* certification trailer "C\x00{12}"
    # (no colon) must still end the body.
    nul = "\x00"
    page = "\n".join(
        [
            f"S{nul * 7} D: L{nul * 7}",
            "owner creditor Date incurred type amount of",
            "JT PennyMac Loan Services December 2015 Home Mortgage $100,001 -",
            "$250,000",
            # The comments label for that row — must fold in, not end the schedule.
            f"C{nul * 7}: paid off early in 2016",
            # A SECOND liability row AFTER the comments label. Before the fix this
            # whole row (16,901 such content lines across 293 docs) was dropped.
            "SP CapitalOne December 2018 Credit Card $15,001 - $50,000",
            # The real certification trailer (no colon) still ends the body.
            f"C{nul * 12} {nul * 3} S{nul * 8}",
            "I CERTIFY that the statements made are true.",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    d = body.schedules["D"]
    # Both liability rows survive — the post-comments row was NOT dropped.
    creditors = {item["creditor"] for item in d}
    assert "PennyMac Loan Services" in creditors
    assert "CapitalOne" in creditors
    # The comments text folded into the first row's raw_text (scrubbed of NULs).
    all_raw = " ".join(item["raw_text"] for item in d)
    assert "paid off early in 2016" in all_raw
    assert "\x00" not in all_raw
    # The real certification trailer ended the body — its content never leaked in.
    assert "CERTIFY" not in all_raw


def test_nulglyph_schedule_a_anchors_over_and_exact_dollar(monkeypatch):
    # 🟡 The glyphless A row anchor accepted only "$lo -"/None/Undetermined, so a
    # row whose value column is "Over $50,000,000" (a real form bucket) or an exact
    # dollar value ("$96,550.00") did not anchor and merged into the prior item.
    # Both now anchor as their own structured items.
    nul = "\x00"
    page = "\n".join(
        [
            f"S{nul * 7} A: A{nul * 5}",
            "Asset Owner Value of Asset Income Type(s) Income Tx. >",
            "$1,000?",
            "First Asset Holding [IH] JT Over $50,000,000 None",
            "Second Asset Fund [BA] JT $96,550.00 Interest $1,141.00",
            f"E{nul * 9} {nul * 2} S{nul * 5}",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    a = body.schedules["A"]
    assets = [item["asset"] for item in a]
    assert "First Asset Holding" in assets
    assert "Second Asset Fund" in assets
    # Two distinct rows, not one merged blob.
    assert len([x for x in assets if x in ("First Asset Holding", "Second Asset Fund")]) == 2


def test_scrub_field_is_nul_gated():
    # 🟡 Structured fields (asset/location/description/income_type/source) are
    # sliced from the un-scrubbed raw blob, so they can carry literal NULs. Scrub
    # them — but only when a NUL is present. A NUL-free value (notably an
    # income_type with a legitimate double space) is returned byte-identical.
    nul = "\x00"
    assert _scrub_field(f"Cash{nul * 3}Holdings") == "Cash Holdings"
    assert _scrub_field(f"{nul * 5} rental parcel {nul * 2}") == "rental parcel"
    # NUL-free values pass through unchanged — including meaningful double spaces.
    assert _scrub_field("Dividends  and  Interest") == "Dividends  and  Interest"
    assert _scrub_field("Salary") == "Salary"
    assert _scrub_field(None) is None
    assert _scrub_field("") == ""


def test_nulglyph_structured_fields_have_no_nul_bytes(monkeypatch):
    # 🟡 A glyphless row with NUL furniture folded into the asset name and the
    # LOCATION:/DESCRIPTION: details must emit those structured fields free of any
    # literal U+0000 (previously 626+ fields across 352 docs leaked NULs).
    nul = "\x00"
    page = "\n".join(
        [
            f"S{nul * 7} A: A{nul * 5}",
            "Asset Owner Value of Asset Income Type(s) Income Tx. >",
            "$1,000?",
            f"Rental{nul * 2}Property [RP] $1,001 - $15,000 Rent $201 - $1,000",
            f"L{nul * 7}: Bolton/Hinds, MS, US",
            f"D{nul * 10}: rental{nul * 2}parcel",
            f"E{nul * 9} {nul * 2} S{nul * 5}",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    item = body.schedules["A"][0]
    for field in ("asset", "location", "description", "income_type"):
        value = item.get(field)
        if value is not None:
            assert "\x00" not in value, f"NUL leaked into {field}: {value!r}"
    # The NULs folded into the asset name were scrubbed, content preserved.
    assert item["asset"] == "Rental Property"
    assert item["description"] == "rental parcel"


def test_nul_schedule_d_creditor_fallback_is_scrubbed(monkeypatch):
    # 🟡 When a Schedule D row's creditor slice is empty (date leads the row) the
    # field falls back to the whole raw blob. That fallback must also be scrubbed:
    # otherwise a folded comments label ("C\x00{7}: …", retained by fix #1) leaks a
    # literal NUL into the emitted ``creditor``.
    nul = "\x00"
    page = "\n".join(
        [
            f"S{nul * 7} D: L{nul * 7}",
            "owner creditor Date incurred type amount of",
            # Date leads → creditor slice is empty → fallback to raw, with a folded
            # comments label carrying NULs.
            f"January 2022 C{nul * 7}: Student loan debt cosigned for my daughter.",
            f"C{nul * 12} {nul * 3} S{nul * 8}",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    body = extract_fd_schedules(Path("synthetic.pdf"))
    d = body.schedules["D"]
    assert len(d) == 1
    assert "\x00" not in d[0]["creditor"]
    assert "\x00" not in d[0]["raw_text"]
    assert "Student loan debt cosigned" in d[0]["creditor"]


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
    raw = data_dir / "raw" / "clerk" / str(year)
    (raw / "fd").mkdir(parents=True, exist_ok=True)
    (raw / f"{year}FD.xml").write_text(_ONE_FD_XML)
    (raw / "fd" / "10042852.pdf").write_bytes(pdf_src.read_bytes())


def test_parse_writes_fd_body_file(tmp_path):
    _seed_one_fd(tmp_path, THOMPSON)
    records = build_filing_records(tmp_path / "raw" / "clerk" / "2020" / "2020FD.xml", 2020)
    parsed_dir = tmp_path / "parsed" / "clerk" / "2020"
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
    raw = tmp_path / "raw" / "clerk" / "2020"
    (raw / "fd").mkdir(parents=True, exist_ok=True)
    (raw / "2020FD.xml").write_text(xml)
    (raw / "fd" / "10042852.pdf").write_text("placeholder")

    # classify → efiled; extract_fd_schedules → NotAnFdBody (no headings).
    monkeypatch.setattr("openhouse.parse.classify", lambda _p: "efiled")

    def _raise_not_fd(_p, **_kw):
        raise NotAnFdBody("no headings")

    monkeypatch.setattr("openhouse.parse.extract_fd_schedules", _raise_not_fd)

    records = build_filing_records(raw / "2020FD.xml", 2020)
    parsed_dir = tmp_path / "parsed" / "clerk" / "2020"
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
    raw = tmp_path / "raw" / "clerk" / "2020"
    (raw / "fd").mkdir(parents=True, exist_ok=True)
    (raw / "2020FD.xml").write_text(_ONE_FD_XML)  # FilingType O
    (raw / "fd" / "10042852.pdf").write_text("placeholder")

    monkeypatch.setattr("openhouse.parse.classify", lambda _p: "efiled")

    def _raise_not_fd(_p, **_kw):
        raise NotAnFdBody("headings lost")

    monkeypatch.setattr("openhouse.parse.extract_fd_schedules", _raise_not_fd)

    records = build_filing_records(raw / "2020FD.xml", 2020)
    parsed_dir = tmp_path / "parsed" / "clerk" / "2020"
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


# --- GH-0070: rendering-independent Schedule A row anchors ---------------------
#
# Two real fixtures cover the two intact-rendering failure modes the
# glyph-gated anchor had (ground truth in tests/fixtures/pdf/README.md):
# the Candidate-form variant with NO checkbox column (no glyph anywhere), and
# a member form whose every row is a subholding with the [TYPE]-tagged name
# wrapped off the glyph-bearing line. Before GH-0070 both collapsed into a
# single merged Schedule A item with parse_status "ok".

HACKETT_CANDIDATE = PDF_FIXTURES / "efiled_fd_candidate_10035478.pdf"
WELCH_SUBWRAP = PDF_FIXTURES / "efiled_fd_subwrap_10039965.pdf"


def test_candidate_form_schedule_a_anchors_without_glyph():
    a = extract_fd_schedules(HACKETT_CANDIDATE).schedules["A"]
    # 20 asset rows (counted by hand from the PDF text: 2 direct [BA] accounts,
    # 12 retirement-plan subholdings, the [OL] S-corp, the [IP] royalties row,
    # 3 John Hancock subholdings, 1 Trust subholding). Pre-GH-0070: 1.
    assert len(a) == 20
    assert all(item["raw_text"] for item in a)
    # The C-form prints THREE amount columns (value, income current year,
    # income preceding year) — the third lands in income_preceding.
    first = a[0]
    assert first["asset"] == "1st Source Bank Portfolio Account"
    assert first["asset_type"] == "BA"
    assert first["value_of_asset"]["label"] == "$1,001 - $15,000"
    assert first["income_type"] == "Interest"
    assert first["income_amount"]["label"] == "$1 - $200"
    assert first["income_preceding"]["label"] == "$1 - $200"


def test_candidate_form_none_value_row_shifts_columns():
    a = extract_fd_schedules(HACKETT_CANDIDATE).schedules["A"]
    # "Harpercollins … [IP] None Royalties $1 - $200 $201 - $1,000": the value
    # column is the literal None, so the buckets are the two income columns —
    # assigning the first to value would fabricate an asset value.
    royalties = next(i for i in a if i["asset"].startswith("Harpercollins"))
    assert royalties["value_of_asset"] is None
    assert royalties["income_type"] == "Royalties"
    assert royalties["income_amount"]["label"] == "$1 - $200"
    assert royalties["income_preceding"]["label"] == "$201 - $1,000"
    assert royalties["description"] == "Royalties from theological book"


def test_candidate_form_tagless_dangling_row_anchors():
    a = extract_fd_schedules(HACKETT_CANDIDATE).schedules["A"]
    # "Hackett & Associates, PC (S corporation), 100% $250,001 - None" — the
    # [TYPE] tag wrapped off the anchor line entirely; the dangling value low
    # is the only signal left, and its high bound ($500,000) sits right before
    # the folded LOCATION: label.
    scorp = next(i for i in a if i["asset"].startswith("Hackett & Associates"))
    assert scorp["asset_type"] == "OL"
    assert scorp["value_of_asset"]["label"] == "$250,001 - $500,000"
    assert scorp["income_amount"] is None  # income column is the literal None
    assert scorp["location"] == "South Bend, IN, US"


def test_member_form_subholding_rows_anchor_on_arrow():
    a = extract_fd_schedules(WELCH_SUBWRAP).schedules["A"]
    # Every asset row is "Welch account ⇒ …" with the [TYPE]-tagged subholding
    # name wrapped onto the next line — tag+glyph never share a line, so the
    # old anchor matched zero rows. 152 = the segment's ⇒-line count.
    assert len(a) == 152
    assert all(item["raw_text"] for item in a)
    # Single-wrap row: the income high bound wrapped into the row's
    # continuation ("… Dividends $201 - gfedcb <name> $1,000 <name> [EF]").
    first = a[0]
    assert first["asset_type"] == "EF"
    assert first["value_of_asset"]["label"] == "$15,001 - $50,000"
    assert first["income_type"] == "Dividends"
    assert first["income_amount"]["label"] == "$201 - $1,000"
    assert first["income_preceding"] is None  # member form: no third column


def test_member_form_double_wrap_row_repairs_both_bounds():
    a = extract_fd_schedules(WELCH_SUBWRAP).schedules["A"]
    # "Welch account ⇒ $100,001 - Capital Gains, $5,001 - gfedc / American
    # Century International Growth Fund (TWIEX) $250,000 Dividends $15,000 /
    # [MF]" — BOTH the value and income high bounds wrapped. The two dangling
    # lows re-pair with the two bare wrapped highs in column order.
    twiex = next(i for i in a if "TWIEX" in i["raw_text"])
    assert twiex["value_of_asset"]["label"] == "$100,001 - $250,000"
    assert twiex["income_amount"]["label"] == "$5,001 - $15,000"


def test_member_form_subholding_asset_name_has_no_bled_column_text():
    # GH-0099: on a ⇒-subholding row the value / income-type / income / glyph
    # column text renders physically *between* the wrapped name fragments, so
    # ``raw[:type_tag]`` swallowed it into the asset string (asset unusable as a
    # grouping key, owner often dropped). The already-parsed column spans must be
    # lifted back out, leaving the clean subholding name — while the structured
    # value/income fields (verified by the sibling wrap tests) stay intact.
    a = extract_fd_schedules(WELCH_SUBWRAP).schedules["A"]

    # Single-wrap row: "Welch account ⇒ $15,001 - $50,000 Dividends $201 -
    # gfedcb aberdeen … Strategy $1,000 K-1 Free ETF (BCI) [EF]". Pre-fix the
    # asset carried "$15,001 - $50,000 Dividends $201 - gfedcb … $1,000".
    first = a[0]
    assert first["asset"] == (
        "Welch account ⇒ aberdeen Standard Bloomberg all Commodity "
        "Strategy K-1 Free ETF (BCI)"
    )
    # Structured columns the bled text was lifted into (GH-0099 must not regress
    # the GH-0098 wrap repair the names were entangled with).
    assert first["value_of_asset"]["label"] == "$15,001 - $50,000"
    assert first["income_type"] == "Dividends"
    assert first["income_amount"]["label"] == "$201 - $1,000"

    # Double-wrap row: both bounds wrapped; the two bare highs ($250,000 /
    # $15,000) sat inside the name. They land in their structured fields and the
    # asset name keeps only the (TWIEX) holding (plus a residual wrapped income
    # word the sub-pattern leaves — the name no longer carries any $ or owner).
    twiex = next(i for i in a if "TWIEX" in i["raw_text"])
    assert "$" not in twiex["asset"]
    assert twiex["value_of_asset"]["label"] == "$100,001 - $250,000"
    assert twiex["income_amount"]["label"] == "$5,001 - $15,000"

    # None/None row: "Welch account ⇒ None None gfedc Charles Schwab
    # Corporation (SCHW) [ST]". The value-None / income-None literals and glyph
    # are column furniture — strip them, keep the ⇒ subholding marker.
    schwab = next(
        i for i in a if i["raw_text"].startswith("Welch account ⇒ None None")
    )
    assert schwab["asset"] == "Welch account ⇒ Charles Schwab Corporation (SCHW)"

    # No surviving row carries an owner code, dollar range, or glyph in its name.
    for item in a:
        assert not re.search(r"\bgfedcb?\b", item["asset"])
        assert "$" not in item["asset"]
        assert not re.search(r"⇒\s*(?:SP|DC|JT)\b", item["asset"])


def test_schedule_a_wrapped_value_high_does_not_cross_pair_income(monkeypatch):
    # GH-0098: when the value-of-asset high bound wraps and lands *between* the
    # income low and the income high in the de-wrapped row, the greedy bucket
    # regex used to glue ``$incomeLow - $valueHigh`` into a spurious complete
    # bucket — crossing the columns and leaving value dangling against the wrong
    # high. The Clerk's Cline (2021/10054358) Rental Property row: PDF value
    # $250,001 - $500,000, income $15,001 - $50,000. The value high ($500,000)
    # wraps onto the continuation line, ahead of the income high ($50,000):
    #   "Rental Property [RP] JT $250,001 - Rent $15,001 -"
    #   "$500,000 $50,000"
    # Pre-fix this parsed value $250,001 - $50,000 (high < low) and income
    # $15,001 - $500,000 (100x overstated). The two ranges must stay anchored to
    # their originating columns.
    page = "\n".join(
        [
            'ScheDule a: aSSetS anD "unearneD" income',
            "asset owner value of asset income income tx. >",
            "Rental Property [RP] JT $250,001 - Rent $15,001 -",
            "$500,000 $50,000",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    item = extract_fd_schedules(Path("synthetic.pdf")).schedules["A"][0]
    assert item["value_of_asset"] == {
        "low": 250001,
        "high": 500000,
        "label": "$250,001 - $500,000",
    }
    assert item["income_type"] == "Rent"
    assert item["income_amount"] == {
        "low": 15001,
        "high": 50000,
        "label": "$15,001 - $50,000",
    }


def test_schedule_a_both_highs_wrap_in_reading_order_keeps_columns(monkeypatch):
    # GH-0098 (DelBene 2021/10046520, Gryphon Partners Fund V): both the value
    # and income high bounds wrap, in value-high-then-income-high reading order,
    # so ``$incomeLow - $valueHigh`` glues into a spurious bucket. PDF value
    # $1,000,001 - $5,000,000, income $50,001 - $100,000; pre-fix parsed value
    # $1,000,001 - $100,000 (high < low) and income $50,001 - $5,000,000.
    page = "\n".join(
        [
            'ScheDule a: aSSetS anD "unearneD" income',
            "asset owner value of asset income income tx. >",
            "Gryphon Partners Fund V [PE] JT $1,000,001 - Dividends $50,001 -",
            "$5,000,000 $100,000",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    item = extract_fd_schedules(Path("synthetic.pdf")).schedules["A"][0]
    assert item["value_of_asset"]["label"] == "$1,000,001 - $5,000,000"
    assert item["income_amount"]["label"] == "$50,001 - $100,000"


# --- GH-0070: Schedule B anchors for directly-held (arrow-less) rows -----------

DIRECTB = PDF_FIXTURES / "efiled_fd_directb_10043047.pdf"


def test_directly_held_b_rows_anchor_on_column_signature():
    b = extract_fd_schedules(DIRECTB).schedules["B"]
    # 6 transactions, none with a subholding arrow — the old ⇒-only anchor
    # merged all of them into one item. Ground truth counted by hand.
    assert len(b) == 6
    condo = b[0]
    assert condo["asset"] == "DC Condo [RP]"
    assert condo["asset_type"] == "RP"
    assert condo["transaction_date"] == "2020-12-21"
    assert condo["transaction_type"] == "P"
    assert condo["amount_range"]["label"] == "$250,001 - $500,000"


def test_b_row_unpadded_date_and_glyph_interposed_wrap():
    b = extract_fd_schedules(DIRECTB).schedules["B"]
    # "Victoria Rental Property [RP] 04/8/2021 S $1,000,001 - gfedcb" with the
    # $5,000,000 high bound wrapped past the checkbox glyph: the unpadded date
    # must parse and the dangling low must re-pair with the wrapped high.
    victoria = next(i for i in b if i["asset"].startswith("Victoria"))
    assert victoria["transaction_date"] == "2021-04-08"
    assert victoria["transaction_type"] == "S"
    assert victoria["amount_range"] == {
        "low": 1000001,
        "high": 5000000,
        "label": "$1,000,001 - $5,000,000",
    }
    assert victoria["cap_gains_over_200"] is True


def test_subholding_b_rows_still_anchor_on_arrow():
    b = extract_fd_schedules(WELCH_SUBWRAP).schedules["B"]
    # 165 = the segment's ⇒-line count; every row's (possibly unpadded) date
    # must parse.
    assert len(b) == 165
    assert all(i["transaction_date"] for i in b)


# --- GH-0070: bare-year Date anchors for Schedules D and F ---------------------


def test_schedule_d_bare_year_date_anchors_and_extracts(monkeypatch):
    # Real line shapes from filing 10035546: two rows with Month-YYYY dates
    # (amount wrapped) and two with BARE-YEAR dates. The bare-year rows never
    # anchored before GH-0070 and merged into the preceding liability.
    page = "\n".join(
        [
            "ScheDule D: liabilitieS",
            "owner creditor Date incurred type amount of",
            "liability",
            "FedLoan servicing July 2009 College loans $50,001 -",
            "$100,000",
            "City Employees Credit Union Loan 2019 Personal Loan $15,001 - $50,000",
            "City Employees Credit Union LoC 2018 Line of Credit $15,001 - $50,000",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    d = extract_fd_schedules(Path("synthetic.pdf")).schedules["D"]
    assert len(d) == 3
    assert d[0]["date_incurred"] == "July 2009"
    assert d[0]["amount_range"]["label"] == "$50,001 - $100,000"
    assert d[1]["creditor"] == "City Employees Credit Union Loan"
    assert d[1]["date_incurred"] == "2019"
    assert d[1]["liability_type"] == "Personal Loan"
    assert d[2]["date_incurred"] == "2018"
    assert d[2]["amount_range"]["label"] == "$15,001 - $50,000"


def test_schedule_d_bare_year_alone_does_not_anchor(monkeypatch):
    # A bare year WITHOUT the amount column on the same line is no anchor —
    # it could be a wrapped creditor-name fragment. The line folds into the
    # preceding row rather than splitting it.
    page = "\n".join(
        [
            "ScheDule D: liabilitieS",
            "owner creditor Date incurred type amount of",
            "Wells Fargo Home Mortgage June 2018 Mortgage on residence $250,001 -",
            "Established 1999 Branch $500,000",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    d = extract_fd_schedules(Path("synthetic.pdf")).schedules["D"]
    assert len(d) == 1
    assert d[0]["date_incurred"] == "June 2018"
    assert "Established 1999" in d[0]["raw_text"]


def test_schedule_d_wrapped_type_amount_not_lost(monkeypatch):
    # GH-0102: when the Type column wraps to a second physical line, pdfplumber
    # re-flows columns left-to-right and the amount's low bound + dash land
    # *between* the type fragments, with the high bound after the type
    # continuation. Before the fix, _FD_AMOUNT_RE saw no contiguous "$lo - $hi"
    # bucket, so amount_range went silently null and the amount tokens were swept
    # into liability_type. Two real wrapped shapes from the issue:
    #   Bucshon (2020/10040126) BB&T: type "Mortgage on Rental Property,
    #     Washington, DC", amount "$100,001 - $250,000".
    #   Bost (2021/10047859): type "Personal residence", amount
    #     "$250,001 - $500,000".
    page = "\n".join(
        [
            "ScheDule D: liabilitieS",
            "owner creditor Date incurred type amount of",
            # BB&T wrapped row: low bound ends line 1, type continuation + high
            # bound on line 2 (the documented interleave shape).
            "BB&T Bank December 2015 Mortgage on Rental Property, $100,001 -",
            "Washington, DC $250,000",
            # A single-line control row (its amount already parsed correctly).
            "Old National Bank January 2016 Mortgage $15,001 - $50,000",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    d = extract_fd_schedules(Path("synthetic.pdf")).schedules["D"]
    assert len(d) == 2
    wrapped, control = d[0], d[1]
    # Positive: the wrapped amount now resolves to its real bucket, not null.
    assert wrapped["amount_range"] == {
        "low": 100001,
        "high": 250000,
        "label": "$100,001 - $250,000",
    }
    # The type is the clean, rejoined string — the amount tokens are carved back
    # out of the interleave, never left swept into liability_type.
    assert wrapped["liability_type"] == "Mortgage on Rental Property, Washington, DC"
    assert wrapped["creditor"] == "BB&T Bank"
    assert wrapped["date_incurred"] == "December 2015"
    # Control row is unaffected (single-line amount still parses).
    assert control["amount_range"]["label"] == "$15,001 - $50,000"
    assert control["liability_type"] == "Mortgage"


def test_schedule_d_present_but_unparseable_amount_stays_visible(monkeypatch):
    # GH-0102 "never silently drop": when the Amount column holds a low bound + dash
    # but no recoverable high bound, the amount is present-but-unparseable. It must
    # NOT collapse to amount_range:null with the tokens carved away — the present
    # amount stays visible in a structured field (liability_type) so a liability
    # query is not silently unsound, and raw_text carries the verbatim row.
    page = "\n".join(
        [
            "ScheDule D: liabilitieS",
            "owner creditor Date incurred type amount of",
            # Dangling low ("$lo -" followed by a word), no "$hi" anywhere.
            "Some Bank December 2015 Personal Loan $100,001 - undisclosed",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    d = extract_fd_schedules(Path("synthetic.pdf")).schedules["D"]
    assert len(d) == 1
    # No fabricated bucket — the unparseable amount is null (degrade, never fake).
    assert d[0]["amount_range"] is None
    # But the present amount tokens survive in a structured field, not just
    # raw_text — the loss is visible, not silent.
    assert "$100,001 -" in d[0]["liability_type"]
    assert "$100,001 -" in d[0]["raw_text"]


def test_schedule_d_month_day_year_date_consumed_whole(monkeypatch):
    # GH-0134: a single-line Date incurred written as "Month DD, YYYY" must be
    # consumed WHOLE (comma included) into date_incurred. Before the fix
    # _FD_DATE_RE matched only "Month YYYY", so "Month DD," failed it, the
    # bare-year fallback captured only the trailing year, and the "Month DD,"
    # fragment leaked into creditor. Two real rows from the issue:
    #   10063197 (D[1]): "April 15, 2019".
    #   10057260 (D[5]): "January 1, 2020".
    page = "\n".join(
        [
            "ScheDule D: liabilitieS",
            "owner creditor Date incurred type amount of",
            # 10063197 D[1] shape: Month DD, YYYY date, single-line amount.
            "Navient April 15, 2019 Student Loan $15,001 - $50,000",
            # 10057260 D[5] shape: a second Month DD, YYYY row.
            "Wells Fargo January 1, 2020 Mortgage $250,001 - $500,000",
            # Control: the comma-less Month YYYY form is unaffected.
            "Old National Bank January 2016 Mortgage $15,001 - $50,000",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    d = extract_fd_schedules(Path("synthetic.pdf")).schedules["D"]
    assert len(d) == 3
    # The whole "Month DD, YYYY" lands in date_incurred — no fragment leaks.
    assert d[0]["creditor"] == "Navient"
    assert d[0]["date_incurred"] == "April 15, 2019"
    assert d[0]["liability_type"] == "Student Loan"
    assert d[0]["amount_range"]["label"] == "$15,001 - $50,000"
    assert d[1]["creditor"] == "Wells Fargo"
    assert d[1]["date_incurred"] == "January 1, 2020"
    assert d[1]["liability_type"] == "Mortgage"
    assert d[1]["amount_range"]["label"] == "$250,001 - $500,000"
    # Control row (comma-less Month YYYY) still parses as before.
    assert d[2]["creditor"] == "Old National Bank"
    assert d[2]["date_incurred"] == "January 2016"


def test_schedule_d_month_day_year_with_wrapped_amount(monkeypatch):
    # GH-0134 + GH-0102: the Month DD, YYYY date must coexist with the wrapped-Type
    # amount handling — the date is consumed whole AND the interleaved amount is
    # recovered, with a clean creditor and rejoined type.
    page = "\n".join(
        [
            "ScheDule D: liabilitieS",
            "owner creditor Date incurred type amount of",
            "BB&T Bank April 15, 2019 Mortgage on Rental Property, $100,001 -",
            "Washington, DC $250,000",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    d = extract_fd_schedules(Path("synthetic.pdf")).schedules["D"]
    assert len(d) == 1
    assert d[0]["creditor"] == "BB&T Bank"
    assert d[0]["date_incurred"] == "April 15, 2019"
    assert d[0]["liability_type"] == "Mortgage on Rental Property, Washington, DC"
    assert d[0]["amount_range"] == {
        "low": 100001,
        "high": 250000,
        "label": "$100,001 - $250,000",
    }


def test_schedule_f_bare_year_leading_date(monkeypatch):
    # Real line shape from filing 10039877: the agreement Date column is a
    # bare year. Before GH-0070 no row anchored and date stayed None.
    page = "\n".join(
        [
            "ScheDule f: agreementS",
            "Date Parties to terms of agreement",
            "2014 GENERAL MOTORS LLC Continued participation in qualified",
            "retirement plan.",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    f = extract_fd_schedules(Path("synthetic.pdf")).schedules["F"]
    assert len(f) == 1
    assert f[0]["date"] == "2014"
    assert "retirement plan." in f[0]["raw_text"]


# --- GH-0070: Schedule A/B completeness guard ----------------------------------


def test_fd_completeness_guard_fails_loudly_on_unanchorable_rows(monkeypatch):
    # An A segment whose rows defeat every anchor signal (no glyph, no value
    # signature after the [TYPE] tag, no arrow, no dangling low) collapses
    # into one salvaged item — the guard must surface that as extract_failed
    # (3 [TYPE] tags, 1 item), never a plausible-but-wrong body with status ok.
    # The guard fires only on collapse / severe merge: small tag-count drift
    # (tag-less rows, brackets in filer text) passes — see extract_fd_schedules.
    page = "\n".join(
        [
            'ScheDule a: aSSetS anD "unearneD" income',
            "Some Asset [ST]",
            "Another Asset [BA]",
            "Third Asset [MF]",
            "certification anD Signature",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    with pytest.raises(PdfExtractError, match="Schedule A.*3 \\[TYPE\\] tag"):
        extract_fd_schedules(Path("synthetic.pdf"))


def test_fd_completeness_guard_passes_on_clean_fixtures():
    # The guard is exercised by every fixture-driven test above; assert once
    # that it stays quiet on every real fixture — no collapse, no severe merge
    # (deliberately weaker than exact tag equality, which drifts ~30% in the
    # wild; see the guard's comment in extract_fd_schedules).
    for fixture in (THOMPSON, HACKETT_CANDIDATE, WELCH_SUBWRAP, DIRECTB):
        extract_fd_schedules(fixture)  # raises PdfExtractError on collapse
