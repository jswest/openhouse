"""Real-filing regression fixtures for the GH-0143 column-content omnibus.

Each test pins a column-*content* extraction bug that the v0.8.0 fixes
(#130/#131/#133/#134) and #132 did **not** generalize to, surfaced by
parse-validation sweep run-202606150610 (schema-8 calibration). Unlike the
synthetic shapes the prior PRs were validated against — which is *why* the bugs
reopened — these assert against the real failing PDFs, committed under
``tests/fixtures/pdf/`` (ground truth in that dir's README).

They are the omnibus's executable success condition: every test is
``xfail`` against the current (still-buggy) parser and flips to pass (XPASS) as
its fix lands. ``strict=False`` keeps the suite green during incremental work;
tighten to strict / drop the marker as each fix merges. Expected values are the
vision-verified PDF truth from ``reports/run-202606150610/calibration.jsonl``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openhouse.pdf import extract_fd_schedules

PDF_FIXTURES = Path(__file__).parent / "fixtures" / "pdf"

# Failing filings (doc_id ↔ fixture), schema-8 corpus, sweep run-202606150610.
SCHIFF = PDF_FIXTURES / "efiled_fd_colcontent_10054507.pdf"   # C/D/H/I (#131/#134/#133/#130)
HARRIS = PDF_FIXTURES / "efiled_fd_schedeh_10054295.pdf"      # H header + E (#133, E/F strand)
WALTZ = PDF_FIXTURES / "efiled_fd_schedcdh_10057260.pdf"      # C/D/H (#131/#134/#133)
WILLIAMS = PDF_FIXTURES / "efiled_fd_schede_10059583.pdf"     # E splitter (E/F strand)
BROWNLEY = PDF_FIXTURES / "efiled_fd_schedh_10059679.pdf"     # H header (#133)
LOPEZ = PDF_FIXTURES / "efiled_fd_schedj_10061936.pdf"        # J header (#133)
MOORE = PDF_FIXTURES / "efiled_fd_incomecols_10062886.pdf"    # two-income A columns (#132)

_HEADER_PREFIXES = ("Trip Details", "Source (Name and Address)")


def _sched(pdf: Path, letter: str) -> list[dict]:
    return extract_fd_schedules(pdf).schedules.get(letter, [])


def _no_header_rows(rows: list[dict]) -> bool:
    """No item is a captured column-header line (the #133 failure shape)."""
    return not any(
        (r.get("raw_text") or "").startswith(_HEADER_PREFIXES) for r in rows
    )


# --- #131 — Schedule C owner-prefixed Type bleeds into Source -----------------


@pytest.mark.xfail(reason="#131 incomplete: open-vocab Type, Spouse bleeds into source", strict=False)
def test_schiff_schedule_c_spouse_not_in_source():
    c = _sched(SCHIFF, "C")
    # PDF: Source="Naborforce", Type="Spouse wage". Parser leaks "Spouse" into source.
    assert c[0]["source"] == "Naborforce"


@pytest.mark.xfail(reason="#131 incomplete: 'Spouse salary' bleeds into source", strict=False)
def test_waltz_schedule_c_spouse_not_in_source():
    c = _sched(WALTZ, "C")
    # PDF: Source="Ghyabi Management and Consulting", Type="Spouse salary".
    assert c[2]["source"] == "Ghyabi Management and Consulting"


# --- #134 — Schedule D non-standard date collapses the row --------------------


@pytest.mark.xfail(reason="#134 incomplete: 'Various dates in 2022' collapses the D row", strict=False)
def test_schiff_schedule_d_no_column_collapse():
    d = _sched(SCHIFF, "D")
    # PDF: creditor="UBS Financial Services Inc", date="Various dates in 2022",
    # amount "$100,001 - $250,000". Parser swallows date+type+amount into creditor.
    row = d[0]
    assert row["creditor"] == "UBS Financial Services Inc"
    assert row["amount_range"] is not None
    assert row["amount_range"]["label"] == "$100,001 - $250,000"


@pytest.mark.xfail(reason="#134 incomplete: 'Jan 2022' leaks 'Jan' into creditor", strict=False)
def test_waltz_schedule_d_month_not_in_creditor():
    d = _sched(WALTZ, "D")
    # PDF: creditor="SoFi", date="Jan 2022". Parser yields creditor="SoFi Jan".
    assert d[5]["creditor"] == "SoFi"


# --- #133 — Schedule H/J column-header emitted as a data row ------------------


def test_schiff_schedule_h_no_header_row():
    h = _sched(SCHIFF, "H")
    assert _no_header_rows(h)
    assert len(h) == 3  # PDF has 3 trips, not 4


def test_harris_schedule_h_no_header_row():
    h = _sched(HARRIS, "H")
    assert _no_header_rows(h)
    assert len(h) == 3


def test_waltz_schedule_h_no_header_row():
    h = _sched(WALTZ, "H")
    assert _no_header_rows(h)
    assert len(h) == 1  # only Government of India


def test_brownley_schedule_h_no_header_and_real_row():
    h = _sched(BROWNLEY, "H")
    assert _no_header_rows(h)
    assert len(h) == 1
    assert "Aspen" in (h[0].get("source") or "")


def test_lopez_schedule_j_no_header_and_split():
    j = _sched(LOPEZ, "J")
    assert _no_header_rows(j)
    assert len(j) == 1
    assert "Protect Kids Colorado" in (j[0].get("source") or "")
    assert "Project Management" in (j[0].get("description") or "")


# --- #130 — trailing schedule absorbs the appendix into fabricated rows -------


def test_schiff_schedule_i_single_real_row():
    i = _sched(SCHIFF, "I")
    # PDF: exactly one real entry; parser fabricates 12 appendix rows (13 total)
    # and leaves the real row's fields null.
    assert len(i) == 1
    assert "Los Angeles Times" in (i[0].get("source") or "")


# --- E/F strand (NEW; #17 territory) — position/organization unsplit ----------


@pytest.mark.xfail(reason="E/F splitter: leading identical-position block unsplit", strict=False)
def test_williams_schedule_e_first_block_split():
    e = _sched(WILLIAMS, "E")
    # E[0..6] share "Sole Director & President"; only these leading rows are unsplit.
    assert e[0]["position"] == "Sole Director & President"
    assert e[0]["organization"] == "JRW Corporation"


@pytest.mark.xfail(reason="E/F splitter + comment-as-row on Schedule E", strict=False)
def test_harris_schedule_e_split_and_no_comment_row():
    e = _sched(HARRIS, "E")
    assert e[0]["position"] is not None
    assert e[0]["organization"] is not None
    # The "C : not compensated nonprofit" comment must fold in, not be its own row.
    assert len(e) == 1


# --- #132 — Schedule A two-income (candidate/new-filer) column corruption -----


@pytest.mark.xfail(reason="#132 confirmed: current-year 'None' + preceding range misaligned", strict=False)
def test_moore_schedule_a_two_income_alignment():
    a = _sched(MOORE, "A")
    # A[9..12] (Robinhood): PDF type "Capital Gains", current-year "None",
    # preceding-year "$201 - $1,000".
    for idx in (9, 10, 11, 12):
        row = a[idx]
        assert row["income_type"] == "Capital Gains"
        assert row["income_amount"] is None  # current-year cell is "None"
        assert row["income_preceding"] == {"low": 201, "high": 1000, "label": "$201 - $1,000"}
