# PDF classification fixtures

Real report-body PDFs from the Clerk's **2020** bulk data, promoted from a live
`openhouse pull 2020` so the classifier (`openhouse/pdf.py`, issue #7) can be
tested **offline** — tests never touch the Clerk (SPEC §3, the polite-crawling
contract). Each file is named `<class>_<family>_<DocID>.pdf` and its expected
classification is the ground truth a test asserts against.

| File | DocID | Family | Expected `pdf_class` | Extraction (pdfplumber) |
|---|---|---|---|---|
| `efiled_fd_10042852.pdf` | 10042852 | fd (annual) | `efiled` | 4 pages, ~1,348 chars/page |
| `efiled_fd_nulglyph_10049721.pdf` | 10049721 | fd (annual, **2021**) | `efiled` | 2 pages; small-caps glyphs extract as **NUL runs** |
| `efiled_fd_candidate_10035478.pdf` | 10035478 | fd (**candidate**, 2020) | `efiled` | 4 pages; C-form Schedule A: **no checkbox column**, 3 income columns |
| `efiled_fd_subwrap_10039965.pdf` | 10039965 | fd (annual, 2020) | `efiled` | 8 pages; every A row a subholding with the [TYPE] wrapped off the glyph line |
| `efiled_fd_directb_10043047.pdf` | 10043047 | fd (annual, 2020) | `efiled` | 2 pages; Schedule B rows directly held (no ⇒), unpadded dates, glyph-interposed amount wrap |
| `efiled_ptr_20016766.pdf` | 20016766 | ptr | `efiled` | 1 page, ~1,127 chars |
| `efiled_ptr_20017980.pdf` | 20017980 | ptr | `efiled` | 7 pages, 57 transaction rows |
| `efiled_ptr_nulglyph_20022132.pdf` | 20022132 | ptr (**2022**) | `efiled` | 1 page; small-caps glyphs extract as **NUL runs**, checkbox glyphs absent |
| `efiled_ptr_wrap_20013811.pdf` | 20013811 | ptr | `efiled` | 1 page, 3 amount-wrapped rows |
| `scanned_fd_8217722.pdf` | 8217722 | fd (annual) | `scanned` | 1 page, **0** chars (image-only) |
| `scanned_ptr_8217326.pdf` | 8217326 | ptr | `scanned` | 1 page, **0** chars (image-only) |

The DocID prefix matches SPEC §2.2: 8-digit `1`/`2` → e-filed (text-extractable);
7-digit `8` → paper/scanned (image-only, 0 chars). Text extraction is the
*authoritative* test; the prefix is a fast pre-filter only.

## Annual-FD glyphs-lost ground truth (SPEC §2.2 NUL rendering)

- **`efiled_fd_nulglyph_10049721.pdf`** — Hon. Alma Adams, **2021** annual report
  (`data/raw/2021/fd/10049721.pdf`). The dominant 2021+ rendering: the form's
  small-caps furniture (schedule headings, section titles, `LOCATION:`/
  `DESCRIPTION:` labels) extracts as **U+0000 NUL runs**, one per lost glyph —
  `Schedule A:` becomes `S\x00{7} A:`. Filer-entered content stays in a regular
  font and extracts intact. Ground truth: schedules **A, C, E, F** populated
  (A: 2 assets incl. a value range wrapped via the dangling-low interleave;
  C: 2 earned-income rows; E: 1 position; F: 2 agreements), B/D/G/H/I `None
  disclosed.` → absent, no Schedule J on this form. The NUL furniture folded into
  a recovered row is scrubbed out of the emitted `raw_text` (each NUL run → one
  space, whitespace collapsed, ends stripped — issue #52), so no item's
  `raw_text` contains `U+0000`; the content (asset names, amounts, dates) is
  unchanged. The scrub is a no-op on NUL-free text, so intact-rendering bodies
  (all of 2020) are byte-identical to before.

## PTR body-extraction ground truth (issue #9, SPEC §6.3)

The two e-filed PTRs double as body-extraction fixtures (`tests/test_ptr_extraction.py`):

- **`efiled_ptr_20017980.pdf`** — Hon. Susie Lee, 2021 (`data/raw/2021/ptr/20017980.pdf`).
  **57** transaction rows across 7 pages: 37 `P`, 12 `S(partial)`, 8 `S`. Exercises
  multi-line wrapped asset names (`Albertsons Companies, Inc. Class A` / `(ACI) [ST]`),
  `S (partial)` (normalized to `S(partial)`), the cap-gains glyph both set
  (`gfedcb`, 12 rows) and unset (`gfedc`), the `JT` owner, `SUBHOLDINg OF` detail
  lines, and small-caps tickers (`(CSgP)`→`CSGP`, `(gPC)`→`GPC`).
- **`efiled_ptr_20016766.pdf`** — Hon. Alan Lowenthal, 2020: the **null-ticker**
  case. A single `SP` Cinemark `[CS]` (corp-bond) **sale** with no parenthesized
  symbol (`ticker: null`, correct — not a sentinel) and a `DESCRIPTION:` line.
- **`efiled_fd_candidate_10035478.pdf`** — Mary Patricia Hackett (candidate,
  IN02), 2020 (`data/raw/2020/fd/10035478.pdf`): the **Candidate-form Schedule A**
  case (GH-0070 mode 1). The C/H form variant has **no "Tx. > $1,000?" checkbox
  column** — no `gfedc` glyph anywhere — and prints **three** amount columns
  (value, income current year, income preceding year). Ground truth: **20**
  Schedule A rows (2 direct [BA] accounts, 12 retirement-plan ⇒ subholdings, the
  [OL] S-corp whose [TYPE] wrapped off its anchor line, the [IP] None-value
  royalties row, 3 John Hancock ⇒ subholdings, 1 Trust ⇒ subholding). Before the
  GH-0070 anchors the whole schedule collapsed into **one** merged item with
  `parse_status: "ok"`.
- **`efiled_fd_subwrap_10039965.pdf`** — 2020 annual Report
  (`data/raw/2020/fd/10039965.pdf`): the **wrapped-[TYPE] subholding** case
  (GH-0070 mode 2). Every Schedule A row is `Welch account ⇒ …` with the
  [TYPE]-tagged subholding name wrapped onto the next line, so tag+glyph never
  share a line and the old anchor matched zero rows (full collapse). Ground
  truth: **152** rows (= the segment's ⇒-line count); includes single- and
  double-wrap amount columns (`(TWIEX)` row: both value and income highs
  wrapped).
- **`efiled_ptr_nulglyph_20022132.pdf`** — Hon. Robert B. Aderholt, **2022**
  (`data/raw/2022/ptr/20022132.pdf`): the **glyphs-lost (NUL) PTR** case. The
  Clerk's PTR generator cut over to the SPEC §2.2 NUL rendering around
  **2022-04**: the form's small-caps furniture extracts as NUL runs (`FILING
  STATUS:` → `F\x00{5} S\x00{5}:`) and the `gfedc`/`gfedcb` cap-gains checkbox
  glyphs **vanish from the text layer entirely**. Ground truth: **1** row — a
  `DC` Tesla, Inc. `(TSLA) [ST]` sale, transaction + notification date
  12/05/2022, `$1,001 - $15,000`, no `DESCRIPTION:` line. With the checkbox
  unrecoverable, `cap_gains_over_200` is `null` ("unknown" — never a fabricated
  boolean). Before the glyphless PTR fix this rendering matched zero rows AND
  zero status blocks, so the completeness guard passed 0 == 0 and every
  post-April-2022 PTR silently parsed as `{"transactions": []}` with status
  "ok".
- **`efiled_ptr_wrap_20013811.pdf`** — Hon. Matt Gaetz, 2020 (issue #46): the
  **amount-column wrap + small-caps** case. **3** rows, each with the `$HIGH`
  bound wrapped onto the next line (`$15,001 -` … `$50,000`; `$50,001 -` …
  `$100,000`) and *every* detail anchor rendered in small-caps with
  per-filing-inconsistent case (`FILING STaTUS:`, `SUBHoLDING oF:`,
  `DESCRIPTIoN:`, `LoCaTIoN:`). Also covers the `E` (exchange) type, cap-gains
  both set and unset, a small-caps `DESCRIPTIoN:` line that must still be
  captured, and a null-ticker `[PS]` row. Before #46 this PDF (and ~2/3 of all
  2020 e-filed PTRs) failed the completeness guard and was dropped wholesale.

**Legal (SPEC §1).** Clerk FD data carries a statutory use restriction: not for
commercial use, soliciting, or establishing credit ratings. These files are
checked in solely as test fixtures for an open-source parser.

## GH-0143 column-content regression fixtures (schema-8, sweep run-202606150610)

Real annual-FD PDFs (2022–2024) where the v0.8.0 column-content fixes
(#130/#131/#133/#134) and #132 did **not** generalize — the failing cases the
prior PRs' synthetic shapes missed. Asserted by
`tests/test_fd_regression_gh143.py` (each `xfail` until its fix lands). Expected
values are the vision-verified PDF truth in
`reports/run-202606150610/calibration.jsonl`.

| File | DocID | Yr | Pins (bug) |
|---|---|---|---|
| `efiled_fd_colcontent_10054507.pdf` | 10054507 | 2022 | Schiff — all four: C Spouse bleed (#131), D collapse on "Various dates in 2022" (#134), H header-as-row (#133), I absorbs appendix (#130) |
| `efiled_fd_schedeh_10054295.pdf` | 10054295 | 2022 | Harris — H header-as-row (#133); E unsplit + comment-as-row (E/F strand) |
| `efiled_fd_schedcdh_10057260.pdf` | 10057260 | 2023 | Waltz — C "Spouse salary" bleed (#131), D "Jan 2022" leak (#134), H header (#133); #130 holds here |
| `efiled_fd_schede_10059583.pdf` | 10059583 | 2023 | Williams — E[0..6] position/organization unsplit (E/F strand); all four fixes hold |
| `efiled_fd_schedh_10059679.pdf` | 10059679 | 2023 | Brownley — H header-as-row + H[1] field loss (#133) |
| `efiled_fd_schedj_10061936.pdf` | 10061936 | 2024 | Lopez — J header-as-row + J[1] unsplit (#133) |
| `efiled_fd_incomecols_10062886.pdf` | 10062886 | 2023 | Moore — two-income Schedule A column corruption (#132) |

## GH-0166 column/row-reconstruction regression fixtures (schema-11)

Real annual-FD PDFs for the #166 omnibus core wave — the generalized fix to the
shared two-layer column/row reconstruction in `openhouse/pdf.py`. Asserted by
`tests/test_fd_regression_gh166.py`; values are the PDF truth read directly from
the cited filings. Four are new here; the rest are reused from the GH-0143 set
above (one PDF exercises several sub-issues).

| File | DocID | Yr | Pins (bug) |
|---|---|---|---|
| `efiled_fd_awrap_10068928.pdf` | 10068928 | 2024 | Pou — Schedule A value/income token-interleave: value+preceding both wrap, an earlier high glues into a false bucket (#160.2) |
| `efiled_fd_atype_10063197.pdf` | 10063197 | 2023 | Lieu — Schedule A wrapped income-type 2nd line `Capital Gains,`/`Dividends` (#160.1) |
| `efiled_fd_aexact_10066320.pdf` | 10066320 | 2024 | Smith — Schedule A exact (non-range) income `$4,425.09` + value-literal whose `[TYPE]` wrapped past it (#160.3) |
| `efiled_fd_cf_10068086.pdf` | 10068086 | 2024 | Raskin — Schedule C `Pension Distribution` source\|type boundary (#162); Schedule F parties/terms populated (#163) |
| `efiled_fd_colcontent_10054507.pdf` | 10054507 | 2022 | Schiff — A income-type wrap (#160.1); D wrapped `Various dates in`+comment bleed (#165); H multi-line source+itinerary (#164); I activity/date split (#164 sibling) |
| `efiled_fd_schede_10059583.pdf` | 10059583 | 2023 | Williams — open-ended `Over $50,000,000` value → null (#76 extension to FD Schedule A) |
| `efiled_fd_incomecols_10062886.pdf` | 10062886 | 2023 | Moore — C `Speech fee`/`Speech/panel fee` boundary (#162); F phantom row-split on inline date (#163) |
| `efiled_fd_schedeh_10054295.pdf` | 10054295 | 2022 | Harris — H multi-line source `Conservative Partnership Institute, Inc` + itinerary (#164) |
| `efiled_fd_schedcdh_10057260.pdf` | 10057260 | 2023 | Waltz — H multi-line source `Government of India (MECEA)` + full itinerary (#164) |
