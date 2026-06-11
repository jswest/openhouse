# GH-0007 — PDF classifier: efiled / scanned / missing (parse)

**Date:** 2026-06-11
**Issue:** #7

## Context

`parse` (#6) maps the index XML into validated `FilingMetadata` records but
leaves `pdf_class = None` and never touches the PDFs. M4 / SPEC §4 step 3 needs
each on-disk PDF classified — `efiled` (text-extractable, the v1 extraction
target), `scanned` (image-only/paper, catalogued but never OCR'd in v1), or
`missing` (no file on disk) — so the pipeline knows which bodies are worth
extracting (v0.3.0 / #9 / #12) and which become an OCR backlog.

Scope here is **classification only**. An `efiled` PDF is classified `efiled`
with `body: null` and `parse_status: "ok"`; no schedule rows or transactions are
extracted yet.

## Decision

### Text extraction is the authoritative test; DocID prefix is a pre-filter only

`openhouse/pdf.py::classify(path)` opens the PDF with **pdfplumber** and sums the
non-whitespace characters of `page.extract_text()` across pages. SPEC §2.2
verified the populations are unambiguous and far apart: an e-filed page extracts
~1,000 chars of real text, a scanned page extracts exactly **0**. Measured on the
four committed fixtures, the smallest e-filed body yields ~960 non-whitespace
chars; both scanned bodies yield 0.

The DocID-prefix heuristic (8-digit `1`/`2`/`3`/`4` → likely e-filed; 7-digit
`8`/`9` → likely paper) is exposed as a separate `predict_from_doc_id()` helper
and is a **fast pre-filter only** — it never makes the decision. `classify()`
always decides by extraction.

### Threshold: >= 20 non-whitespace chars → efiled

`EFILED_MIN_NONWS_CHARS = 20`. The threshold sits in the wide empty gap between 0
(every scanned body) and ~960 (smallest e-filed body): generous enough to ignore
a stray watermark/scanner-artifact glyph on an otherwise image-only page, yet far
below any real e-filed body. We count **non-whitespace** chars (not raw length)
so layout whitespace can never inflate a near-empty page toward the threshold.

### pdfplumber adopted now (the v0.3.0 body-extraction lib)

Per the director's note and SPEC §9, pdfplumber is the mandated layout-aware
extraction library for the §2.2 caveats (column runs, small-caps headings). It is
added as a runtime dependency now — classification needs the same text extraction
v0.3.0 will use for bodies, so there is no reason to introduce a throwaway
`pypdf` classification path first.

### unparsed-manifest reason taxonomy (incl. `missing`)

`parsed/<year>/unparsed-manifest.json` lists every filing **not** fully usable as
an e-filed body, each with a `doc_id` + `reason` from this closed set (SPEC §6.5):

- `scanned` — image-only PDF (classified by extraction).
- `missing` — no PDF on disk (or a no-DocID index row, which has no body).
- `extract_failed` — present-but-corrupt PDF (pdfplumber raised).
- `unknown_type` — FilingType label is `"unknown"` (a letter not in the §2.3
  table); recorded independently of the PDF outcome, raw code preserved.
- `validation_error` — reserved in the taxonomy; records are pydantic-validated
  at build time in #6, so no per-record validation failure path emits this yet.

E-filed filings are **never** in this manifest. Both `scanned` and `missing`
belong here (per #7 + SPEC §6.5) — they are the OCR/re-pull backlog.

### Counts reconcile to the index total

`parse-manifest.json` gains `counts.by_pdf_class` (`{efiled, scanned, missing}`),
`counts.not_classified`, and `counts.by_parse_status` (`{ok, error}`), alongside
the existing `total` / `by_filing_type` and `schema_version`. The invariant:

```
efiled + scanned + missing + not_classified == total
```

An `extract_failed` filing keeps `pdf_class = None` (it was never classified), so
it lands in `not_classified` for the pdf-class reconciliation while showing up as
`error` in `by_parse_status` — the two breakdowns are orthogonal and both
reconcile to `total`.

### `--types` partial runs

`parse` only classifies filings whose family (`ptr` for FilingType `P`, else
`fd`) is in `--types`. A filing **out of scope** for the run is left
`pdf_class = None`, `parse_status = "ok"`, and is **not** added to the unparsed
manifest — it was out of scope, not unparsed. Out-of-scope rows fall into
`not_classified` so the total still reconciles. (Default is both families.)

### extract_failed / `--strict`

A present-but-corrupt PDF makes `classify()` raise a typed `PdfExtractError`
rather than crash the year; the caller sets `parse_status = "error"` and adds an
`extract_failed` unparsed entry — never a silently dropped filing (CLAUDE.md).
With `--strict`, `parse` returns exit code **1** (`STRICT_ERROR_EXIT`) if any
filing in any year errored; without `--strict`, errors are catalogued and the run
still exits 0. The strict exit (1) is distinct from the argument-validation exit
(2, in `cli.py`).

Everything stays offline and deterministic: classification opens only files
already on disk, and a re-run from the same `raw/` produces byte-identical
`filings.json` / `parse-manifest.json` / `unparsed-manifest.json`.

## Scope

Classification only — no body/field extraction (schedules, transactions are
v0.3.0 / #9 / #12). New `openhouse/pdf.py`; extended `openhouse/parse.py`
(`parse_year` classification pass + manifest builder + `--strict` exit);
`pdfplumber>=0.11` added to `pyproject.toml`. `cli.py` (flags already wired) and
`docs/decisions/README.md` untouched. Tests: `tests/test_pdf.py` (new) +
end-to-end / partial-run / strict cases in `tests/test_parse.py`, all against the
committed fixtures (no Clerk). 130 tests pass.
