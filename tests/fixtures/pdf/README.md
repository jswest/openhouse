# PDF classification fixtures

Real report-body PDFs from the Clerk's **2020** bulk data, promoted from a live
`openhouse pull 2020` so the classifier (`openhouse/pdf.py`, issue #7) can be
tested **offline** — tests never touch the Clerk (SPEC §3, the polite-crawling
contract). Each file is named `<class>_<family>_<DocID>.pdf` and its expected
classification is the ground truth a test asserts against.

| File | DocID | Family | Expected `pdf_class` | Extraction (pdfplumber) |
|---|---|---|---|---|
| `efiled_fd_10042852.pdf` | 10042852 | fd (annual) | `efiled` | 4 pages, ~1,348 chars/page |
| `efiled_ptr_20016766.pdf` | 20016766 | ptr | `efiled` | 1 page, ~1,127 chars |
| `efiled_ptr_20017980.pdf` | 20017980 | ptr | `efiled` | 7 pages, 57 transaction rows |
| `efiled_ptr_wrap_20013811.pdf` | 20013811 | ptr | `efiled` | 1 page, 3 amount-wrapped rows |
| `scanned_fd_8217722.pdf` | 8217722 | fd (annual) | `scanned` | 1 page, **0** chars (image-only) |
| `scanned_ptr_8217326.pdf` | 8217326 | ptr | `scanned` | 1 page, **0** chars (image-only) |

The DocID prefix matches SPEC §2.2: 8-digit `1`/`2` → e-filed (text-extractable);
7-digit `8` → paper/scanned (image-only, 0 chars). Text extraction is the
*authoritative* test; the prefix is a fast pre-filter only.

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
