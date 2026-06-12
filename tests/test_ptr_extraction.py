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

from openhouse.pdf import PdfExtractError, extract_ptr_transactions
from openhouse.parse import _classify_records
from openhouse.index import build_filing_records

PDF_FIXTURES = Path(__file__).parent / "fixtures" / "pdf"

LEE = PDF_FIXTURES / "efiled_ptr_20017980.pdf"
LOWENTHAL = PDF_FIXTURES / "efiled_ptr_20016766.pdf"


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
