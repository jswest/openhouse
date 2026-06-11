# GH-0004 — `pull` PDFs: FilingType routing, resumability, pull-manifest

**Date:** 2026-06-11
**Issue:** #4

## Context

After #3, `pull` fetched and extracted the annual index ZIP but left the
per-DocID PDF bodies unimplemented (a stderr "not yet implemented" notice behind
the `# issue #4` seam in `pull()`). The index alone has no filing contents, so
`pull` must fetch every referenced PDF — ~2,250 for 2024 — resumably, politely,
and with a complete `pull-manifest.json` (SPEC §11 bullet 1). This issue fills
the seam; it does **not** parse PDF contents (that is M4+).

## Decisions

### §2.2 routing — by FilingType, never DocID

`openhouse/index.py` (new, enumeration-only) parses `<YEAR>FD.xml` with stdlib
`xml.etree` into minimal `(DocID, FilingType, year)` targets and exposes a
`family` property implementing the verified SPEC §2.2 rule: `FilingType == 'P'`
→ `ptr` (`ptr-pdfs/<year>/<DocID>.pdf` → `raw/<year>/ptr/<DocID>.pdf`); every
other type → `fd` (`financial-pdfs/<year>/<DocID>.pdf` → `raw/<year>/fd/`).
Routing keys on the **raw** FilingType letter, which is preserved verbatim on
every target and in every manifest entry, so an unrecognized letter still routes
(to `fd`) and is never silently dropped.

### `index.py` scope — enumeration only, full mapping deferred

`index.py` yields only the three fields the download loop needs. The full
metadata→record mapping (`filer_id`, name normalization, `StateDst` parsing, the
§6.1 record, collision warnings) is the `parse` milestone (M4) and is
deliberately not built here — keeping #4 to "acquire bytes." A `<Member>` with
no `DocID` is skipped by the loop (no body to fetch); that same XML row will be
recorded as metadata by M4, so it is not a dropped filing.

### Resumability via present-and-size-consistent check

A target whose destination file already exists and is **non-empty** is treated
as present-and-size-consistent and skipped with no network request. We do not
re-download to compare bytes (that would defeat the point). A truncated transfer
leaves a zero-byte file, which fails the non-empty check and is refetched on the
next run — so Ctrl-C mid-PDF is safe. `--force` re-downloads regardless;
`--types ptr,fd` (default both) filters which families are fetched. Skipped and
filtered targets cost no request and therefore consume no pacing delay; the
polite floor is otherwise applied before every network GET, and the across-year
delay is held by `pull()`'s existing per-year sleep.

### Entry-time timestamp threading (wall-clock-free core)

Per SPEC §9, the manifest `fetched_at` is captured **once** at command entry:
`cli.main()` makes a single `datetime.now()` read and derives both the
range-validation `current_year` and the `fetched_at` ISO string from it, then
threads `fetched_at` through `pull()` → `pull_pdfs_year()` into every manifest
entry. Core logic never calls the clock per file; `pull(..., fetched_at=...)`
defaults to one `datetime.now()` only so an ad-hoc call still works, and tests
inject a fixed timestamp and assert every entry carries it.

### 404 is non-fatal but recorded — a gap, never silent

Some index rows have no PDF. `polite_get` gained an `allow_not_found` flag: with
it set, a 404 is **returned** (not raised) so the PDF loop records it in
`pull-manifest.json` with `status: 404`, `bytes: 0`, `sha256: null`, and its URL
— a recorded gap — and continues. Without the flag (the index ZIP path) a 404 is
still a hard `PullError`. 403 and exhausted 429/5xx backoff remain fatal
everywhere; #3's politeness is reused untouched.

### Manifest shape (SPEC §6.5)

`raw/<year>/pull-manifest.json` is `{year, fetched_at, count, filings}` where
`filings` maps DocID → `{doc_id, filing_type, family, url, status, bytes,
sha256, fetched_at}`. It is loaded and merged on each run so a resumed year never
loses prior entries (notably recorded 404s).

## Scope

`openhouse/index.py` (new), the PDF path in `openhouse/pull.py`, and `--types`
in `cli.py`. No PDF content parsing; `parse`/`read` remain stubs. All proven
offline against `tests/fixtures/2024FD-trimmed.xml` and fabricated PDF bytes via
`httpx.MockTransport` — the live `pull 2024` is a daylight follow-up.
