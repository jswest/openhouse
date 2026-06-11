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
| `scanned_fd_8217722.pdf` | 8217722 | fd (annual) | `scanned` | 1 page, **0** chars (image-only) |
| `scanned_ptr_8217326.pdf` | 8217326 | ptr | `scanned` | 1 page, **0** chars (image-only) |

The DocID prefix matches SPEC §2.2: 8-digit `1`/`2` → e-filed (text-extractable);
7-digit `8` → paper/scanned (image-only, 0 chars). Text extraction is the
*authoritative* test; the prefix is a fast pre-filter only.

**Legal (SPEC §1).** Clerk FD data carries a statutory use restriction: not for
commercial use, soliciting, or establishing credit ratings. These files are
checked in solely as test fixtures for an open-source parser.
