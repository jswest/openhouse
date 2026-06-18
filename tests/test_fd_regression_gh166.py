"""Real-filing regression fixtures for the GH-0166 column/row-reconstruction omnibus.

Each test pins a column-/row-reconstruction bug that the prior per-schedule fixes
(#99/#100, #101/#131, #146/#103, #134/#102, #150/#103, #76) did **not**
generalize — the family the GH-0166 generalized fix to the shared two-layer
reconstruction in ``openhouse/pdf.py`` resolves once at the root. Asserted
against the real failing PDFs committed under ``tests/fixtures/pdf/`` (the cited
filings from sub-issues #160/#162/#163/#164/#165 + the #76 extension), so the
"keeps coming undone" pattern is guarded by executable truth, not synthetic
shapes.
"""

from __future__ import annotations

from pathlib import Path

from openhouse.pdf import extract_fd_schedules

PDF = Path(__file__).parent / "fixtures" / "pdf"

# doc_id ↔ fixture. Several were already committed for GH-0143; the four new ones
# carry the GH-0166-specific shapes.
POU = PDF / "efiled_fd_awrap_10068928.pdf"        # A value/income multi-wrap (#160.2)
LIEU = PDF / "efiled_fd_atype_10063197.pdf"        # A wrapped income-type 2nd line (#160.1)
SMITH = PDF / "efiled_fd_aexact_10066320.pdf"      # A exact (non-range) income (#160.3)
SCHIFF = PDF / "efiled_fd_colcontent_10054507.pdf" # A income-type wrap; D; H; I (#160/#164/#165)
WILLIAMS = PDF / "efiled_fd_schede_10059583.pdf"   # A open-ended Over $50M value (#76)
RASKIN = PDF / "efiled_fd_cf_10068086.pdf"         # C source|type; F parties/terms (#162/#163)
MOORE = PDF / "efiled_fd_incomecols_10062886.pdf"  # C Speech fee; D; F phantom split (#162/#163/#165)
HARRIS = PDF / "efiled_fd_schedeh_10054295.pdf"    # H multi-line source + itinerary (#164)
WALTZ = PDF / "efiled_fd_schedcdh_10057260.pdf"    # H multi-line source + itinerary (#164)


def _sched(pdf: Path, letter: str) -> list[dict]:
    return extract_fd_schedules(pdf).schedules.get(letter, [])


# --- #160.1 — Schedule A wrapped income-type second line ----------------------


def test_lieu_a_wrapped_income_type_second_line():
    a = _sched(LIEU, "A")
    # PDF row 0: Income Type(s) is two stacked lines "Capital Gains," / "Dividends";
    # the 2nd line wrapped to the row tail (dropped, comma left dangling, Dividends
    # folded into the asset). Both lines must rejoin and leave the asset clean.
    row = a[0]
    assert row["income_type"] == "Capital Gains, Dividends"
    assert row["asset"] == "3148/Large Cap Growth Fund - A"
    assert "Dividends" not in row["asset"]


def test_schiff_a_wrapped_income_type_and_value_wrap():
    a = _sched(SCHIFF, "A")
    # A[2] (Franklin Growth Opportunities): value high ($100,000) AND the income
    # type 2nd line (Dividends) both wrapped to the tail.
    row = a[2]
    assert row["income_type"] == "Capital Gains, Dividends"
    assert row["value_of_asset"]["label"] == "$50,001 - $100,000"
    assert row["asset"] == "Franklin Templeton Franklin Growth Opportunities Fund Class A (FGRAX)"


# --- #160.2 — Schedule A value/income token-interleave (multi-column wrap) -----


def test_pou_a_value_income_interleave():
    a = _sched(POU, "A")
    # A[0] (30th Street Mortgage Recording): value AND preceding both wrapped, so
    # an earlier column's wrapped high glued into a false bucket. The general
    # resolver must untangle all three columns in order.
    row = a[0]
    assert row["value_of_asset"]["label"] == "$500,001 - $1,000,000"
    assert row["income_amount"]["label"] == "$5,001 - $15,000"
    assert row["income_preceding"]["label"] == "$15,001 - $50,000"


# --- #160.3 — Schedule A exact (non-range) income value -----------------------


def test_smith_a_exact_income_value():
    a = _sched(SMITH, "A")
    # A[0]: value "Undetermined" (its [TYPE] tag wrapped past it), income type
    # "Royalties", income an EXACT $4,425.09 (not a range). Value null; income exact.
    row = a[0]
    assert row["value_of_asset"] is None
    assert row["income_type"] == "Royalties"
    assert row["income_amount"] == {"exact": 4425.09, "label": "$4,425.09"}


# --- #76 extension — open-ended "Over $X" value on FD Schedule A --------------


def test_williams_a_over_50m_value_is_null():
    a = _sched(WILLIAMS, "A")
    # The "Over $50,000,000" value cannot be a {low, high} bucket: it must be null
    # (slot held), never a fabricated range. (#76 widened to FD Schedule A value.)
    over_rows = [r for r in a if "Over $50,000,000" in r["raw_text"]]
    assert over_rows, "expected the JRW Corporation Over-$50M row"
    assert all(r["value_of_asset"] is None for r in over_rows)


# --- #162 — Schedule C source|type boundary -----------------------------------


def test_raskin_c_pension_distribution_boundary():
    c = _sched(RASKIN, "C")
    # C[2]: source ends "...Pension Plan", type is the multi-word "Pension
    # Distribution" — the boundary must not bleed "Pension" into the type.
    row = c[2]
    assert row["source"] == "Barbara Raskin Pension Plan"
    assert row["income_type"] == "Pension Distribution"


def test_moore_c_speech_fee_boundary():
    c = _sched(MOORE, "C")
    # The 4 speaking-fee rows: type "Speech fee" / "Speech/panel fee", source the org.
    speech = [r for r in c if (r["income_type"] or "").lower().startswith("speech")]
    assert len(speech) == 4
    uky = next(r for r in c if "University of Kentucky" in (r["source"] or ""))
    assert uky["source"] == "University of Kentucky"
    assert uky["income_type"] == "Speech fee"


# --- #161 — Schedule C two amount columns (candidate/new-filer form) ----------


def test_moore_c_candidate_two_amount_columns():
    # Moore's C is a Candidate/New-Filer form: two Amount columns ("Current Year
    # to Filing" then "Preceding Year"). They must land in separate fields, never
    # the pre-#161 space-joined string ("$258,468.14 $406,169.85").
    c = _sched(MOORE, "C")
    blackbaud = next(r for r in c if "Blackbaud" in (r["source"] or ""))
    assert blackbaud["amount"] == "$258,468.14"
    assert blackbaud["amount_preceding"] == "$406,169.85"
    # current present / preceding N/A
    micro = next(r for r in c if (r["source"] or "") == "Microsoft")
    assert micro["amount"] == "$350.00"
    assert micro["amount_preceding"] == "N/A"
    # current N/A / preceding present
    uky = next(r for r in c if "University of Kentucky" in (r["source"] or ""))
    assert uky["amount"] == "N/A"
    assert uky["amount_preceding"] == "$562.20"
    # both N/A
    ngps = next(r for r in c if "NineGPS" in (r["source"] or ""))
    assert ngps["amount"] == "N/A"
    assert ngps["amount_preceding"] == "N/A"


def test_raskin_c_member_form_single_amount_unchanged():
    # The member annual form has ONE Amount column: amount stays a single value
    # and amount_preceding is None (no second column on this form) — #161 must
    # not regress the standard form.
    c = _sched(RASKIN, "C")
    pension = next(r for r in c if "Pension Plan" in (r["source"] or ""))
    assert pension["amount"] == "$23,082.00"
    assert pension["amount_preceding"] is None
    assert all(r["amount_preceding"] is None for r in c)


# --- #163 — Schedule F parties/terms + no phantom row-split -------------------


def test_moore_f_no_phantom_split_on_inline_date():
    f = _sched(MOORE, "F")
    # The Terms text holds an inline "08/23/2023." that previously anchored a
    # phantom 3rd row. Exactly two agreements; the inline date stays in Terms.
    assert len(f) == 2
    assert f[0]["date"] == "August 2021"
    assert f[0]["parties"] == "Blackbaud Inc."
    assert "08/23/2023" in (f[0]["terms"] or "")


def test_raskin_f_parties_and_terms_populated():
    f = _sched(RASKIN, "F")
    assert f[0]["parties"] == "Myself & American University"
    assert f[0]["terms"] == "401(k) plan"
    assert f[0]["date"] == "1990"


# --- #164 — Schedule H multi-line source + itinerary --------------------------


def test_harris_h_multiline_source_and_itinerary():
    h = _sched(HARRIS, "H")
    row = h[0]
    assert row["source"] == "Conservative Partnership Institute, Inc"
    assert row["location"] == "Baltimore - Jacksonville - Baltimore"


def test_schiff_h_multiline_source_and_itinerary():
    h = _sched(SCHIFF, "H")
    forum = next(r for r in h if (r["source"] or "").startswith("Forum"))
    assert forum["source"] == "Forum Club of Palm Beaches"
    assert forum["location"] == "Washington, DC - Palm Beach, FL - Washington, DC"
    rancho = next(r for r in h if (r["source"] or "").startswith("Rancho"))
    assert rancho["source"] == "Rancho Mirage Writers Festival"


def test_waltz_h_multiline_source_and_full_itinerary():
    h = _sched(WALTZ, "H")
    row = h[0]
    assert row["source"] == "Government of India (MECEA)"
    assert row["location"] == (
        "Washington, DC - Mumbai, India - Hyderabad, India - "
        "Delhi, India - Washington, DC"
    )


# --- #164 sibling — Schedule I activity/date folded out of source -------------


def test_schiff_i_activity_date_split_from_source():
    i = _sched(SCHIFF, "I")
    assert len(i) == 1
    row = i[0]
    assert row["source"] == "Los Angeles Times Festival of Books"
    assert row["activity"] == "Article"
    assert row["date"] == "04/22/2022"
    assert row["amount"] == "$500.00"


# --- #165 — Schedule D wrapped date_incurred + comment-line bleed -------------


def test_schiff_d_wrapped_date_and_comment_bleed():
    d = _sched(SCHIFF, "D")
    # D[0]: "Various dates in" + wrapped "2022"; the year must rejoin the date and
    # leave the type clean, with the amount recovered.
    assert d[0]["date_incurred"] == "Various dates in 2022"
    assert d[0]["liability_type"] == "Margin loan on portfolio"
    assert d[0]["amount_range"]["label"] == "$100,001 - $250,000"
    # D[1]: a "C :" COMMENTS line must not bleed into liability_type.
    assert d[1]["liability_type"] == "Mortgage on private residence in Potomac, MD"
    assert "C :" not in (d[1]["liability_type"] or "")


# --- Resolver bound — pathological many-column row must degrade, not hang ------


def test_fd_amount_entries_caps_split_search():
    """A row-merge artifact (many ``$lo - $hi`` buckets + a count-breaking stray
    bare ``$N``) must not drive a 2**N split enumeration. The cap routes it to the
    degrade reading instead. Synthetic by necessity — the point is a shape real
    filings don't (and mustn't be able to) produce a hang from (GH-0166)."""
    from openhouse.pdf import _FD_MAX_SPLIT_OPENERS, _fd_amount_entries

    n = _FD_MAX_SPLIT_OPENERS + 12  # far past the cap; 2**n would peg the CPU
    cols = " ".join(["$1,001 - $15,000"] * n) + " $9,999"  # stray bare ⇒ inconsistent
    entries, _ = _fd_amount_entries(cols)
    # Degrade keeps every opener as its inline bucket — slot held, nothing dropped.
    ranges = [r for _, _, r in entries if r is not None]
    assert len(ranges) == n
