"""Offline tests for e-filed PTR body extraction (``openhouse/pdf.py``, #9).

Extraction targets the SPEC §6.3 ``transactions[]`` schema. These run against
the committed fixtures in ``tests/fixtures/pdf/`` (ground truth in that dir's
README) and a corrupt case built at test time — no Clerk, no binary corrupt
fixture checked into the repo.

Two fixtures exercise the two ends of the contract:

- ``efiled_ptr_20017980.pdf`` (Hon. Susie Lee, 2021) — 57 rows over 7 pages:
  multi-line wrapped asset names, ``S (partial)``, the cap-gains flag set and
  unset, ``JT`` owner, and many tickers (incl. small-caps glyph cases).
- ``efiled_ptr_20016766.pdf`` (Hon. Alan Lowenthal, 2020) — the null-ticker
  case: a single ``SP`` Cinemark ``[CS]`` sale with no parenthesized symbol and
  a ``DESCRIPTION:`` line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openhouse.pdf import (
    PdfExtractError,
    _wrapped_range_tail_follows,
    extract_ptr_transactions,
)
from openhouse.parse import _classify_records
from openhouse.index import build_filing_records

PDF_FIXTURES = Path(__file__).parent / "fixtures" / "pdf"


def test_wrapped_range_tail_guard_distinguishes_exact_from_wrapped_range():
    # GH-0049 soundness guard (critic): a range whose " - $HIGH" tail wrapped off
    # the header looks like an exact row. The guard peeks the next content line —
    # a leading dash means a wrapped range, so the exact reading is refused (the
    # row falls to extract_failed rather than fabricating a point).
    assert _wrapped_range_tail_follows(["- $15,000"], 0, 1) is True
    # Furniture / glyph / blank lines are skipped before the dash is found.
    assert _wrapped_range_tail_follows(["", "gfedc", "- $15,000"], 0, 3) is True
    # A genuine exact value: the next content line is a detail/description or the
    # next row — never a dash tail.
    assert _wrapped_range_tail_follows(["DESCRIPTION: a thing"], 0, 1) is False
    # Nothing follows → not a wrapped range.
    assert _wrapped_range_tail_follows([], 0, 0) is False

LEE = PDF_FIXTURES / "efiled_ptr_20017980.pdf"
LOWENTHAL = PDF_FIXTURES / "efiled_ptr_20016766.pdf"
GAETZ_WRAP = PDF_FIXTURES / "efiled_ptr_wrap_20013811.pdf"
ADERHOLT_NULGLYPH = PDF_FIXTURES / "efiled_ptr_nulglyph_20022132.pdf"


# --- 20017980 (Lee): 57 rows, the curated rows --------------------------------


def test_lee_transaction_count():
    # Ground truth: 7 pages, 57 date-pair rows (37 P + 12 S(partial) + 8 S).
    txns = extract_ptr_transactions(LEE)
    assert len(txns) == 57


def _one(txns, *, ticker):
    matches = [t for t in txns if t.ticker == ticker]
    assert matches, f"no transaction with ticker {ticker!r}"
    return matches


def test_lee_multiline_albertsons_sale():
    # Asset name wraps: "Albertsons Companies, Inc. Class A" + "(ACI) [ST]".
    txns = extract_ptr_transactions(LEE)
    aci = _one(txns, ticker="ACI")[0]
    assert aci.owner == "JT"
    assert aci.ticker == "ACI"
    assert aci.asset_type == "ST"
    assert aci.transaction_type == "S"
    assert aci.asset == "Albertsons Companies, Inc. Class A (ACI) [ST]"
    assert aci.amount_range.label == "$1,001 - $15,000"
    assert aci.amount_range.low == 1001
    assert aci.amount_range.high == 15000


def test_lee_ametek_s_partial_with_cap_gains():
    # The form prints "S (partial)" → normalized to "S(partial)"; cap-gains set
    # (the row ends ``gfedcb``).
    txns = extract_ptr_transactions(LEE)
    ame = _one(txns, ticker="AME")[0]
    assert ame.transaction_type == "S(partial)"
    assert ame.cap_gains_over_200 is True
    assert ame.asset_type == "ST"


def test_lee_clean_purchase_row():
    # A clean P purchase with a parenthesized ticker; cap-gains unset.
    txns = extract_ptr_transactions(LEE)
    amrc = _one(txns, ticker="AMRC")[0]
    assert amrc.transaction_type == "P"
    assert amrc.cap_gains_over_200 is False
    assert amrc.asset_type == "ST"


def test_lee_tickers_are_uppercased():
    # Defeating pdfplumber's small-caps glyph: raw (CSgP)/(gPC) → CSGP/GPC. No
    # extracted ticker is left in mixed-case glyph form.
    txns = extract_ptr_transactions(LEE)
    tickers = [t.ticker for t in txns if t.ticker]
    assert tickers, "expected at least one ticker"
    assert all(t == t.upper() for t in tickers)
    # The small-caps cases specifically uppercase.
    assert "CSGP" in tickers
    assert "GPC" in tickers
    # At least one [ST] row's ticker is all-caps (e.g. ACI), not AAPl-style.
    st_tickers = [t.ticker for t in txns if t.asset_type == "ST" and t.ticker]
    assert st_tickers and all(t == t.upper() for t in st_tickers)


def test_lee_cap_gains_split():
    # 12 rows flag cap-gains > $200 (gfedcb), the rest do not (gfedc).
    txns = extract_ptr_transactions(LEE)
    assert sum(1 for t in txns if t.cap_gains_over_200) == 12


# --- 20016766 (Lowenthal): the null-ticker case -------------------------------


def test_lowenthal_single_null_ticker_with_description():
    txns = extract_ptr_transactions(LOWENTHAL)
    assert len(txns) == 1
    t = txns[0]
    assert t.owner == "SP"
    # A corp-bond [CS] asset carries no parenthesized symbol → ticker is None,
    # not a sentinel; disambiguated by asset_type.
    assert t.ticker is None
    assert t.asset_type == "CS"
    assert t.transaction_type == "S"
    assert t.asset == "Cinemark USA Inc [CS]"
    assert t.description == "Maturity date is 12/15/2022, interest rate is 5.125%"


# --- 20013811 (Gaetz): amount-column wrap + small-caps (issue #46) ------------
#
# Before #46 this PDF — and ~2/3 of all 2020 e-filed PTRs — failed the
# completeness guard and was dropped wholesale: (1) the amount column wraps,
# leaving the header line ending ``$LOW - <glyph>`` with the ``$HIGH`` bound on
# the following line; and (2) the detail/status anchors render in small-caps with
# per-filing-inconsistent case (``FILING STaTUS:`` / ``SUBHoLDING oF:`` /
# ``DESCRIPTIoN:``), so fixed-case matching missed the row boundaries and the
# status-block count.


def test_gaetz_wrap_extracts_all_three_rows():
    # All three rows have a wrapped $HIGH bound; case-insensitive status counting
    # must agree with the row count (else the completeness guard would raise).
    txns = extract_ptr_transactions(GAETZ_WRAP)
    assert len(txns) == 3


def test_gaetz_wrap_amount_ranges_fold_in_the_wrapped_high():
    # The $HIGH bound spilled to the next line ($50,000 / $100,000) must fold back
    # into the range — never a fabricated half-range, never a dropped row.
    txns = extract_ptr_transactions(GAETZ_WRAP)
    labels = {(t.amount_range.low, t.amount_range.high) for t in txns}
    assert (15001, 50000) in labels
    assert (50001, 100000) in labels
    for t in txns:
        assert t.amount_range.label == f"${t.amount_range.low:,} - ${t.amount_range.high:,}"


def test_gaetz_wrap_smallcaps_anchors_and_description():
    # Small-caps DESCRIPTIoN:/SUBHoLDING oF: must still bound the row and a
    # small-caps DESCRIPTIoN: line must still be captured. The first row is an
    # ``E`` (exchange) with a description; the wrapped $HIGH line must not be
    # mistaken for the description or leak into the asset name.
    txns = extract_ptr_transactions(GAETZ_WRAP)
    fbsi = [t for t in txns if t.ticker == "FBSI"]
    assert len(fbsi) == 2  # an E and an S on First Bancshares
    exchange = [t for t in fbsi if t.transaction_type == "E"]
    assert exchange, "expected the E (exchange) row"
    assert exchange[0].asset == "First Bancshares, Inc. (FBSI) [ST]"
    assert exchange[0].description == (
        "Due to an acquisition, 1,285 shares of stock were received."
    )
    assert exchange[0].cap_gains_over_200 is False
    # The closely-held [PS] sale has a small-caps LoCaTIoN:/DESCRIPTIoN: and a
    # null ticker (no parenthesized symbol).
    ps = [t for t in txns if t.asset_type == "PS"]
    assert len(ps) == 1
    assert ps[0].ticker is None
    assert ps[0].transaction_type == "S"
    assert ps[0].cap_gains_over_200 is True
    assert ps[0].description == (
        "5,000 shares of First Florida Bank stock (closely held) were sold."
    )


# --- 20022132 (Aderholt): the glyphs-lost (NUL) PTR rendering ------------------
#
# The Clerk's PTR generator cut over to the SPEC §2.2 NUL rendering around
# 2022-04: small-caps labels extract as NUL runs (``FILING STATUS:`` →
# ``F\x00{5} S\x00{5}:``) and the cap-gains checkbox glyphs vanish from the text
# layer entirely. Before the fix, no row matched (the trailing-glyph anchor never
# fired) AND no status block was counted (the status regex was NUL-blind), so the
# completeness guard passed 0 == 0 and every post-April-2022 PTR silently parsed
# as {"transactions": []} with status "ok".


def test_aderholt_nulglyph_extracts_the_tesla_sale():
    txns = extract_ptr_transactions(ADERHOLT_NULGLYPH)
    assert len(txns) == 1
    t = txns[0]
    assert t.owner == "DC"
    assert t.asset == "Tesla, Inc. (TSLA) [ST]"
    assert t.ticker == "TSLA"
    assert t.asset_type == "ST"
    assert t.transaction_type == "S"
    assert str(t.transaction_date) == "2022-12-05"
    assert str(t.notification_date) == "2022-12-05"
    assert (t.amount_range.low, t.amount_range.high) == (1001, 15000)
    # The checkbox glyph is not in the text layer at all → unknown, never a
    # fabricated boolean.
    assert t.cap_gains_over_200 is None
    assert t.description is None


def test_aderholt_nulglyph_raw_fields_carry_no_nuls():
    # NUL furniture must never leak into the emitted fields (scrubbed/absent).
    txns = extract_ptr_transactions(ADERHOLT_NULGLYPH)
    for t in txns:
        for value in (t.asset, t.description or ""):
            assert "\x00" not in value


# --- synthetic glyphless cases (NUL-pattern pages, _fake_pdfplumber below) -----

_NUL_STATUS = "F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New"
_NUL_SUBHOLDING = "S\x00\x00\x00\x00\x00\x00\x00\x00\x00 O\x00:"
_NUL_DESCRIPTION = "D\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00:"


def test_glyphless_wrapped_high_bound_is_recovered(monkeypatch):
    # In the NUL rendering an amount-column wrap leaves the header ending
    # ``$15,001 -`` bare (no glyph after the dash); the $HIGH bound on the next
    # line must still fold in, and the NUL status line must bound the row.
    page = "\n".join(
        [
            "T\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",  # TRANSACTIONS title
            "Flipside Crypto, Inc. [OT] S 03/22/2022 03/23/2022 $15,001 -",
            "$50,000",
            _NUL_STATUS,
            f"{_NUL_SUBHOLDING} Flipside Crypto Investor Holdings, LLC",
            f"{_NUL_DESCRIPTION} Sold equity interest back to company.",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    txns = extract_ptr_transactions(Path("synthetic.pdf"))
    assert len(txns) == 1
    assert (txns[0].amount_range.low, txns[0].amount_range.high) == (15001, 50000)
    assert txns[0].asset == "Flipside Crypto, Inc. [OT]"
    assert txns[0].cap_gains_over_200 is None
    assert txns[0].description == "Sold equity interest back to company."


def test_glyphless_rows_bound_each_other(monkeypatch):
    # Two NUL-rendering rows: each ends at the next header/status line; the
    # status-block count (NUL-aware) matches the row count, so no guard trip.
    page = "\n".join(
        [
            "DC Tesla, Inc. (TSLA) [ST] S 12/05/2022 12/05/2022 $1,001 - $15,000",
            _NUL_STATUS,
            "Apple Inc. (AAPL) [ST] P 12/06/2022 12/07/2022 $15,001 - $50,000",
            _NUL_STATUS,
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    txns = extract_ptr_transactions(Path("synthetic.pdf"))
    assert [t.ticker for t in txns] == ["TSLA", "AAPL"]
    assert all(t.cap_gains_over_200 is None for t in txns)


def test_glyphless_zero_extraction_fails_loudly(monkeypatch):
    # The regression at the heart of the bug: a glyphless body whose rows don't
    # match (here a one-sided "Over $1,000,000") must NOT return a silent
    # {"transactions": []} with status ok — the NUL-aware status count (1) vs
    # the row count (0) trips the completeness guard into extract_failed.
    page = "\n".join(
        [
            "JT Tesla Inc. (TSLA) [ST] S 12/17/2022 01/01/2023 Over $1,000,000",
            _NUL_STATUS,
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    with pytest.raises(PdfExtractError):
        extract_ptr_transactions(Path("synthetic.pdf"))


def test_glyphless_row_form_is_gated_on_nul_detection(monkeypatch):
    # In an intact-rendering document (no NULs anywhere) a row line WITHOUT the
    # trailing checkbox glyph must still fail loudly, exactly as before the fix:
    # the glyph-free row variants are consulted only for documents detected as
    # glyphless, so the legacy soundness anchor is not weakened.
    page = "\n".join(
        [
            "JT Apple Inc. (AAPL) [ST] P 12/16/2020 01/01/2021 $1,001 - $15,000",
            "FILINg STATUS: New",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    with pytest.raises(PdfExtractError):
        extract_ptr_transactions(Path("synthetic.pdf"))


def test_trailer_description_does_not_bleed_into_last_row(monkeypatch):
    # Real glyphless layouts (e.g. 2022/20020708) carry an ASSET CLASS DETAILS
    # appendix after the table footnote, with its own D\x00…: detail lines. The
    # footnote ends the table — the appendix description must not overwrite the
    # row's own description.
    page = "\n".join(
        [
            "Flipside Crypto, Inc. [OT] S 03/22/2022 03/23/2022 $15,001 - $50,000",
            _NUL_STATUS,
            f"{_NUL_DESCRIPTION} The row's own description.",
            "* For the complete list of asset type abbreviations, please visit"
            " https://fd.house.gov/reference/asset-type-codes.aspx.",
            "A\x00\x00\x00\x00 C\x00\x00\x00\x00 D\x00\x00\x00\x00\x00\x00",
            f"{_NUL_DESCRIPTION} Appendix text that must not bleed in.",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    txns = extract_ptr_transactions(Path("synthetic.pdf"))
    assert len(txns) == 1
    assert txns[0].description == "The row's own description."


# --- extraction-failure path → PdfExtractError --------------------------------


def test_corrupt_pdf_raises_extract_error(tmp_path):
    bogus = tmp_path / "20000001.pdf"
    bogus.write_text("this is plainly not a PDF body\n")
    with pytest.raises(PdfExtractError):
        extract_ptr_transactions(bogus)


# --- parse integration: body file written; failure → error + unparsed ---------

_ONE_PTR_XML = """<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
  <Member>
    <Last>Lee</Last><First>Susie</First><Suffix></Suffix>
    <FilingType>P</FilingType><StateDst>NV03</StateDst>
    <Year>2021</Year><FilingDate>1/11/2021</FilingDate><DocID>20017980</DocID>
  </Member>
</FinancialDisclosure>
"""


def _seed_one_ptr(data_dir: Path, pdf_src: Path, *, year: int = 2021) -> None:
    raw = data_dir / "raw" / str(year)
    (raw / "ptr").mkdir(parents=True, exist_ok=True)
    (raw / f"{year}FD.xml").write_text(_ONE_PTR_XML)
    (raw / "ptr" / "20017980.pdf").write_bytes(pdf_src.read_bytes())


def test_parse_writes_ptr_body_file(tmp_path):
    import json

    _seed_one_ptr(tmp_path, LEE)
    records = build_filing_records(tmp_path / "raw" / "2021" / "2021FD.xml", 2021)
    parsed_dir = tmp_path / "parsed" / "2021"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    unparsed = _classify_records(
        records,
        data_dir=tmp_path,
        types=["ptr", "fd"],
        year=2021,
        parsed_dir=parsed_dir,
    )
    body_path = parsed_dir / "ptr" / "20017980.json"
    assert body_path.exists()
    body = json.loads(body_path.read_text())
    # Exact contract shape: a single "transactions" key holding the array.
    assert list(body.keys()) == ["transactions"]
    assert len(body["transactions"]) == 57
    assert records[0].parse_status == "ok"
    assert not unparsed


def test_parse_extract_failure_sets_error_and_unparsed(tmp_path):
    # A present-but-corrupt efiled PTR → parse_status="error" + extract_failed.
    # Note: classify() must read it as efiled first; a non-PDF body would be
    # caught by classify. To exercise the *body* extraction failure path we use
    # the pdf.py contract directly: the record routes to extract_failed when
    # extraction raises. Here we corrupt by replacing the file with garbage,
    # which classify() turns into extract_failed before body extraction runs.
    raw = tmp_path / "raw" / "2021"
    (raw / "ptr").mkdir(parents=True, exist_ok=True)
    (raw / "2021FD.xml").write_text(_ONE_PTR_XML)
    (raw / "ptr" / "20017980.pdf").write_text("not a pdf\n")
    records = build_filing_records(raw / "2021FD.xml", 2021)
    parsed_dir = tmp_path / "parsed" / "2021"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    unparsed = _classify_records(
        records,
        data_dir=tmp_path,
        types=["ptr", "fd"],
        year=2021,
        parsed_dir=parsed_dir,
    )
    assert records[0].parse_status == "error"
    assert records[0].pdf_class is None
    assert {"reason": "extract_failed", "doc_id": "20017980", "filer_id": records[0].filer_id} in [
        {"reason": u["reason"], "doc_id": u["doc_id"], "filer_id": u["filer_id"]}
        for u in unparsed
    ]
    assert not (parsed_dir / "ptr" / "20017980.json").exists()


def test_out_of_range_date_records_residual_without_dropping_filing(tmp_path, monkeypatch):
    # GH-0113 end-to-end: a PTR whose extraction surfaces an out-of-range date
    # (the per-row anomaly flag is a set ``date_raw``) is NOT dropped — the body is
    # written and the filing stays ``ok``, but the anomaly is surfaced in the
    # unparsed manifest with reason ``date_out_of_range`` so the residual line
    # accounts for it (never a silent gap — CLAUDE.md). Offline: a real efiled PDF
    # (classifies efiled) with extraction stubbed to return a flagged transaction.
    import json

    from openhouse.schemas import AmountRange, PtrTransaction

    _seed_one_ptr(tmp_path, LEE)
    flagged = PtrTransaction(
        owner="self",
        asset="Apple Inc. (AAPL) [ST]",
        ticker="AAPL",
        asset_type="ST",
        asset_type_raw="ST",
        transaction_type="S",
        transaction_date=None,
        date_raw="04/30/3031",
        notification_date=None,
        notification_date_raw="05/02/3031",
        amount_range=AmountRange(low=1001, high=15000, label="$1,001 - $15,000"),
    )
    monkeypatch.setattr(
        "openhouse.parse.extract_ptr_transactions", lambda _p, **_kw: [flagged]
    )
    records = build_filing_records(tmp_path / "raw" / "2021" / "2021FD.xml", 2021)
    parsed_dir = tmp_path / "parsed" / "2021"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    unparsed = _classify_records(
        records,
        data_dir=tmp_path,
        types=["ptr", "fd"],
        year=2021,
        parsed_dir=parsed_dir,
        max_year=2026,
    )
    # Filing intact: body written, status ok (not dropped to extract_failed).
    assert records[0].parse_status == "ok"
    body = json.loads((parsed_dir / "ptr" / "20017980.json").read_text())
    assert body["transactions"][0]["date_raw"] == "04/30/3031"
    # Residual entry accounts for the anomaly.
    assert any(
        u["reason"] == "date_out_of_range" and u["doc_id"] == "20017980"
        for u in unparsed
    )


# --- critic regressions: page-break wrap, partial-extraction guard, ticker slot -


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
    # extract_ptr_transactions joins page text across pages, so a synthetic
    # multi-page layout exercises page-break behavior offline (no PDF needed).
    monkeypatch.setattr(
        "openhouse.pdf.pdfplumber.open", lambda _path: _FakePdf(pages)
    )


def test_asset_wrap_across_page_break_is_not_dropped(monkeypatch):
    # The row header ends page 1; its asset-name continuation "(ACI) [ST]" lands
    # on page 2 *after* the repeated table-header furniture. The furniture must
    # not end the wrap (else ticker/asset_type silently null — a residual-
    # invisible blind spot the sound-or-complete contract forbids).
    page1 = (
        "JT Albertsons Companies, Inc. Class A S 12/16/2020 01/01/2021 "
        "$1,001 - $15,000 gfedc"
    )
    page2 = "\n".join(
        [
            "ID Owner Asset Transaction Date Notification Amount Cap.",
            "Type Date Gains >",
            "$200?",
            "(ACI) [ST]",
            "FILINg STATUS: New",
            "SUBHOLDINg OF: DSL Living Trust",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page1, page2])
    txns = extract_ptr_transactions(Path("synthetic.pdf"))
    assert len(txns) == 1
    assert txns[0].ticker == "ACI"
    assert txns[0].asset_type == "ST"
    assert txns[0].asset == "Albertsons Companies, Inc. Class A (ACI) [ST]"


def test_partial_extraction_raises_rather_than_silently_dropping(monkeypatch):
    # Two rows each with a FILINg STATUS block, but the second's amount is a
    # one-sided "Over $1,000,000" the header regex can't match → it would be
    # silently skipped. The status-block guard turns that into a loud
    # extract_failed instead of a too-short body with status "ok".
    page = "\n".join(
        [
            "JT Apple Inc. (AAPL) [ST] P 12/16/2020 01/01/2021 $1,001 - $15,000 gfedc",
            "FILINg STATUS: New",
            "JT Tesla Inc. (TSLA) [ST] S 12/17/2020 01/01/2021 Over $1,000,000 gfedc",
            "FILINg STATUS: New",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    with pytest.raises(PdfExtractError):
        extract_ptr_transactions(Path("synthetic.pdf"))


def test_wrapped_high_across_page_break_is_recovered(monkeypatch):
    # The header line ends page 1 with only the low bound (``$15,001 - gfedc``);
    # the repeated per-page furniture and a stray glyph remnant land on page 2
    # BEFORE the wrapped ``$50,000`` high bound. The recovery must skip the
    # furniture/glyph and still fold the high bound in (else the row drops and the
    # status-block guard fails the whole PDF) — issue #46's page-break edge.
    page1 = "JT Intuit Inc. (INTU) [ST] S 12/30/2019 01/10/2020 $15,001 - gfedc"
    page2 = "\n".join(
        [
            "ID Owner Asset Transaction Date Notification Amount Cap.",
            "Type Date Gains >",
            "$200?",
            "gfedc",
            "$50,000",
            "FILINg STATUS: New",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page1, page2])
    txns = extract_ptr_transactions(Path("synthetic.pdf"))
    assert len(txns) == 1
    assert txns[0].amount_range.low == 15001
    assert txns[0].amount_range.high == 50000
    assert txns[0].asset == "Intuit Inc. (INTU) [ST]"


def test_wrapped_high_on_asset_wrap_line_keeps_both(monkeypatch):
    # The wrapped $HIGH can share its line with an asset-name wrap (the high at
    # the line end, the asset tail before it). Both must be kept: the range folds
    # the high in AND the asset tail folds into the name.
    page = "\n".join(
        [
            "Alibaba Group Holding S 02/20/2020 03/03/2020 $15,001 - gfedcb",
            "ADS (BABA) [ST] $50,000",
            "FILINg STATUS: New",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    txns = extract_ptr_transactions(Path("synthetic.pdf"))
    assert len(txns) == 1
    assert (txns[0].amount_range.low, txns[0].amount_range.high) == (15001, 50000)
    assert txns[0].asset == "Alibaba Group Holding ADS (BABA) [ST]"
    assert txns[0].ticker == "BABA"


def test_smallcaps_type_letter_is_normalized(monkeypatch):
    # The transaction-type glyph can render lower-case (``s``/``p``/``e``) and
    # ``s (partial)``; all normalize to the schema's canonical upper-case form.
    page = "\n".join(
        [
            "Apple Inc. (AAPL) [ST] p 01/02/2020 01/13/2020 $1,001 - $15,000 gfedc",
            "FILINg STATUS: New",
            "Black Knight (BKI) [ST] s (partial) 01/02/2020 01/13/2020 $1,001 - $15,000 gfedc",
            "FILINg STATUS: New",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    txns = extract_ptr_transactions(Path("synthetic.pdf"))
    assert [t.transaction_type for t in txns] == ["P", "S(partial)"]


def test_asset_type_is_normalized_with_raw_preserved(monkeypatch):
    # The Clerk's PDFs render the bracketed [TYPE] tag with inconsistent casing
    # (sT/Cs/gS all occur — pdfplumber's small-caps glyph artifact). ``asset_type``
    # is normalized (uppercased, trimmed) so consumers need not defensively
    # upper() it, while the verbatim tag is preserved in ``asset_type_raw``
    # (raw alongside normalized — GH-0114).
    page = "\n".join(
        [
            "Apple Inc. (AAPL) [sT] P 01/02/2020 01/13/2020 $1,001 - $15,000 gfedc",
            "FILINg STATUS: New",
            "Some Corp Bond [Cs] S 01/02/2020 01/13/2020 $1,001 - $15,000 gfedc",
            "FILINg STATUS: New",
            "US Treasury [gS] P 01/02/2020 01/13/2020 $1,001 - $15,000 gfedc",
            "FILINg STATUS: New",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    txns = extract_ptr_transactions(Path("synthetic.pdf"))
    assert [t.asset_type for t in txns] == ["ST", "CS", "GS"]
    assert [t.asset_type_raw for t in txns] == ["sT", "Cs", "gS"]


def test_transposed_year_transaction_date_is_flagged_not_accepted(monkeypatch):
    # GH-0113: a transposed-digit year (``3031`` for ``2031``) parses via strptime
    # exactly as readily as a sane year, so without a sanity range it would emit as
    # a valid year-3031 date — silent bad data in a temporal-analysis tool. The
    # date must instead be rejected: structured field None, raw string preserved,
    # and the rest of the row (asset, type, amount) intact. ``max_year`` is the
    # entry year + 1, threaded down — here a fixed offline value, never wall-clock.
    page = "\n".join(
        [
            "JT Apple Inc. (AAPL) [ST] S 04/30/3031 05/02/3031 $1,001 - $15,000 gfedc",
            "FILINg STATUS: New",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    txns = extract_ptr_transactions(Path("synthetic.pdf"), max_year=2026)
    assert len(txns) == 1
    txn = txns[0]
    # NOT emitted as a valid year-3031 date — the whole point of the guard.
    assert txn.transaction_date is None
    assert txn.notification_date is None
    # Raw string preserved as the per-row anomaly flag.
    assert txn.date_raw == "04/30/3031"
    assert txn.notification_date_raw == "05/02/3031"
    # The rest of the filing is intact — never dropped over one bad date.
    assert txn.asset == "Apple Inc. (AAPL) [ST]"
    assert txn.ticker == "AAPL"
    assert txn.transaction_type == "S"
    assert (txn.amount_range.low, txn.amount_range.high) == (1001, 15000)


def test_near_future_typo_year_is_flagged_but_in_range_year_kept(monkeypatch):
    # A near-future typo (``2220`` for ``2022``) is out of range and rejected, but
    # a sound date on a *different* row in the same body parses normally — the
    # guard is per-date, never a whole-filing drop. Confirms the upper bound is
    # entry-year-relative: 2023 is accepted under max_year=2026, 2220 is not.
    page = "\n".join(
        [
            "JT Tesla Inc. (TSLA) [ST] P 04/07/2220 04/08/2220 $1,001 - $15,000 gfedc",
            "FILINg STATUS: New",
            "JT Ford (F) [ST] P 09/19/2023 09/20/2023 $1,001 - $15,000 gfedc",
            "FILINg STATUS: New",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    txns = extract_ptr_transactions(Path("synthetic.pdf"), max_year=2026)
    assert len(txns) == 2
    # Row 0: transposed near-future year — rejected, raw preserved.
    assert txns[0].transaction_date is None
    assert txns[0].date_raw == "04/07/2220"
    # Row 1: a sound in-range date — kept, no anomaly flag.
    assert txns[1].transaction_date is not None
    assert txns[1].transaction_date.year == 2023
    assert txns[1].date_raw is None


def test_truncated_wrapped_high_still_raises(monkeypatch):
    # A row whose $HIGH bound never materializes (header ends ``$15,001 -`` and the
    # next content is the row's own detail line, no money token) must NOT fabricate
    # a half-range — it drops, and the status-block guard surfaces extract_failed.
    page = "\n".join(
        [
            "Apple Inc. (AAPL) [ST] S 12/30/2019 01/10/2020 $15,001 - gfedc",
            "FILINg STATUS: New",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    with pytest.raises(PdfExtractError):
        extract_ptr_transactions(Path("synthetic.pdf"))


# --- #49: exact-dollar amount form ----------------------------------------


def test_exact_dollar_amount_extracts_soundly(monkeypatch):
    # A row whose amount column is a single EXACT dollar value ($894.97), not a
    # $LOW - $HIGH bucket (#49). It must extract — represented as an exact point,
    # NOT coerced into a fake {low: 894.97, high: 894.97} range — with low/high
    # left None and the verbatim "$894.97" preserved as the label.
    page = "\n".join(
        [
            "JT Apple Inc. (AAPL) [ST] S 12/16/2020 01/01/2021 $894.97 gfedcb",
            "FILINg STATUS: New",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    txns = extract_ptr_transactions(Path("synthetic.pdf"))
    assert len(txns) == 1
    amt = txns[0].amount_range
    assert amt.exact == 894.97
    assert amt.low is None and amt.high is None
    assert amt.label == "$894.97"
    # Serialized JSON carries `exact`, never a fabricated low/high pair.
    dumped = amt.model_dump(mode="json")
    assert dumped == {"exact": 894.97, "label": "$894.97"}


def test_exact_and_range_rows_coexist_in_one_pdf(monkeypatch):
    # A range row and an exact-dollar row in the same body both parse, and each
    # bounds the other (an exact row is a row boundary too, like a range row).
    page = "\n".join(
        [
            "Apple Inc. (AAPL) [ST] P 01/02/2020 01/13/2020 $1,001 - $15,000 gfedc",
            "FILINg STATUS: New",
            "Ford Motor Co (F) [ST] S 02/02/2020 02/13/2020 $500 gfedc",
            "FILINg STATUS: New",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    txns = extract_ptr_transactions(Path("synthetic.pdf"))
    assert len(txns) == 2
    assert (txns[0].amount_range.low, txns[0].amount_range.high) == (1001, 15000)
    assert txns[0].amount_range.exact is None
    # A whole-dollar exact value (no cents) is accepted too.
    assert txns[1].amount_range.exact == 500.0
    assert txns[1].amount_range.low is None


def test_one_sided_amount_still_fails_loudly(monkeypatch):
    # A genuinely-malformed amount that is NEITHER a bucket NOR a bare exact dollar
    # value ("Over $1,000,000") must still surface as extract_failed — never coerced
    # into an exact value or a fabricated range (#49 keeps the loud residual).
    page = "\n".join(
        [
            "Tesla Inc. (TSLA) [ST] S 12/17/2020 01/01/2021 Over $1,000,000 gfedc",
            "FILINg STATUS: New",
        ]
    )
    _fake_pdfplumber(monkeypatch, [page])
    with pytest.raises(PdfExtractError):
        extract_ptr_transactions(Path("synthetic.pdf"))


def test_ticker_is_the_symbol_adjacent_to_the_type_tag():
    # The ticker is the paren group immediately before [TYPE], not the first
    # parenthetical — "(The) (KO) [ST]" → KO, never the fabricated "THE" (which
    # would be a --ticker false positive).
    from openhouse.pdf import _ticker_from_asset

    assert _ticker_from_asset("Coca-Cola Company (The) (KO) [ST]") == "KO"
    assert _ticker_from_asset("Apple Inc. (AAPL) [ST]") == "AAPL"
    assert _ticker_from_asset("Cinemark USA Inc [CS]") is None
    # No [TYPE] tag at all → fall back to the last paren group.
    assert _ticker_from_asset("Some Holding (BAR)") == "BAR"
