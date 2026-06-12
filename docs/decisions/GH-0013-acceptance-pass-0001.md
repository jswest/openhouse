# GH-0013 — Acceptance pass (parse → read, 2020–2022) + promote fixtures

**Date:** 2026-06-12
**Issue:** #13 (Part of #11)

## Context

SPEC §11 defines done; this issue verifies the assembled `omnibus/v0.4.0`
parse→read pipeline end-to-end, **offline**, over the real on-disk
`raw/{2020,2021,2022}` Clerk corpus (not committed). Verification ran against a
complete three-year parse produced by the current omnibus code (schema_version
4), with a one-year re-parse for the idempotency check. This record captures the
concrete numbers so the omnibus PR and #16 (filer_id dedup) can cite them.

The base under test is omnibus HEAD `aee2e80`, which includes #12 (FD schedule
A–D structured / E–J raw_text), #46 (PTR amount-wrap recovery), #52 (NUL-glyph
FD rendering), #40 (read residual accounting), and the critic-fixes batch.

## Decision / findings

### 1. Every filing accounted for — manifest reconciles, no gaps

For each year `efiled + scanned + missing + extract_failed == total`, and every
non-`ok` filing has an explicit `unparsed-manifest.json` entry (scanned filings
catalogued, extraction failures recorded — never silently dropped, per the
working agreement).

| Year | total | e-filed | scanned | missing | ok | error | FD bodies | PTR bodies |
|------|------:|--------:|--------:|--------:|----:|------:|----------:|-----------:|
| 2020 | 2930  | 2420    | 412     | 1       | 2833 | 97   | 1257      | 500        |
| 2021 | 2717  | 2326    | 362     | 0       | 2688 | 29   | 1296      | 536        |
| 2022 | 2742  | 2358    | 382     | 0       | 2740 | 2    | 1376      | 503        |

Reconciliation holds exactly: 2420+412+1+97 = 2930; 2326+362+0+29 = 2717;
2358+382+0+2 = 2742. `ok` counts the classified e-filed + scanned population;
`error` is the `extract_failed` residual. Body files are written only for
schedule/trade-bearing e-filed types — annual FD (O/A) and PTR (P); extension
(X), amendment (W), and cover-sheet types legitimately carry no body and are
`ok` with no file (verified against `parse.py:_ANNUAL_FD_CODES` design).

**FD success rate (annual O/A, schedule-bearing):** 480/480 (2020), 544/544
(2021), 491/491 (2022) = **100%** body-written, with only a handful of genuinely
empty schedules (members with nothing reportable: 3 / 4 / 0). FD extraction is
near-complete across all three years (#12 + #52 confirmed).

**PTR success rate:** 500/500, 536/536, 503/503 = **100%** of e-filed type-P
filings produced a transaction body. The `extract_failed` entries are all
type-P, `pdf_class=None` — PTRs whose layout the parser deliberately fails-loud
on rather than emitting partial data (verified: e.g. 2022 DocID 20020423 raises
`PdfExtractError: matched 45 rows but found 0 'FILINg STATUS:' blocks`). 2020's
higher count (97) is concentrated in a few prolific 2020 filers (Cisneros, etc.).

### 2. `read trades` — sound JSON + `--table`, filters narrow, guarantee declared

`read trades {2020,2021,2022}` and the range `2020-2022` all emit valid JSON to
stdout and render `--table`. Trade counts: 6325 / 5322 / 938 (per year), 12585
(range, all three years present).

Each query prints its sound-or-complete guarantee to **stderr** (JSON stays clean
on stdout). The residual line is scoped to the type-P PTR population `read trades`
actually flattens, e.g. 2021:

> `residual: complete over the 536 e-filed PTR (type-P) filings parsed in range; 391 did not parse (scanned 362 / missing 0 / not_classified 29, of which error 29) and are not represented in these results.`

Filter spot-checks (2021): `--ticker AAPL` → 78 hits, all `ticker == AAPL`
(SOUND, with the null-ticker blind-spot residual declared); `--owner SP` → 905,
all `owner == SP`; `--member pelosi` → 27, all `ca.pelosi.nancy`. The #46
recovered PTR (DocID 20013811) flows through to `read trades` with its wrapped
`$50,001 - $100,000` bound intact; the #52 NUL-glyph FD body (DocID 10049721)
extracts real Schedules A/C/E/F.

### 3. Idempotency + determinism

2022 re-parsed into a fresh `--data-dir` (worktree code, schema 4) and compared
byte-for-byte against the existing parse:

- All 1376 FD body files: `diff -rq` clean (**0 differences**).
- All 503 PTR body files: `diff -rq` clean (**0 differences**).
- `filings.json`: **byte-identical**.
- `parse-manifest.json` and `unparsed-manifest.json`: identical **except the
  single `generated_at` entry-timestamp** (the one permitted difference) — once
  that field is excluded both are byte-identical.

No wall-clock leaks into body output; re-parse is deterministic by design.

### 4. Offline guarantee

Verified by monkeypatching `socket.socket.connect`/`connect_ex` to raise, then
running `read trades` (rc 0) and `parse_year` over the fixture tree to completion
— neither opened a socket. Network is confined to `pull` by design; `parse` and
`read` import no network client on their path.

### 5. filer_id dedup rate across 2020–2022 (SPEC §10 item 3 → #16)

Method: union the `filer_id` keys from all three `filings.json`; compare the
per-year-distinct sum to the union size; tally cross-year recurrence, raw-name
variation absorbed, and the `identity_warnings` collisions.

- 8389 filings → **2792 distinct filer_ids** (union).
- Per-year: 2930→1389, 2717→1371, 2742→1420 distinct (≈2.0 filings/member).
- Cross-year dedup collapses **4180 per-year keys → 2792** = 1388 merged
  (**33.2% reduction**). 935 filer_ids (33.5%) recur across ≥2 years; 453 appear
  in all three.
- 69 filer_ids absorbed **>1 distinct raw-name spelling** (the slug doing its
  job — "Donald Sternoff Beyer Jr." vs "…Honorable Beyer Jr").
- **34 identity-collision warnings** (3 / 3 / 28 by year). 2022's spike is the
  post-2021-redistricting "same filer_id, two adjacent districts" pattern
  (e.g. 49→50, 17→32) — correctly surfaced per §6.2, not a defect.

This is the measurement #16 consumes: `filer_id` gets ~⅓ cross-year collapse and
absorbs spelling variation, but is a name-key, not identity — the 34 warnings
bound where a key may span two people (bioguide join is the post-v1 fix).

## Fixtures + acceptance test

The omnibus fixture basis was already complete from the feature PRs and needed no
*additional* trimmed sample:

- `efiled_fd_10042852.pdf` (Thompson, type O) — FD Schedules A/C/E/F (#12).
- `efiled_fd_nulglyph_10049721.pdf` (#52) — NUL-glyph FD rendering.
- `efiled_ptr_20017980.pdf`, `efiled_ptr_20016766.pdf`,
  `efiled_ptr_wrap_20013811.pdf` (#46) — PTR wrap / null-ticker / curated rows.
- scanned FD/PTR PDFs + `2024FD-trimmed.xml` index.

Added **`tests/test_acceptance.py`** — the one missing piece: an end-to-end test
that seeds a raw tree from the committed FD + PTR fixtures, runs the real
`parse_year`, then drives `read.run` over the produced `parsed/`. It asserts
manifest reconciliation with no gaps, the FD schedule body surfacing through
`read filing`, the #46 wrapped-bound trade surfacing through `read trades` with
its residual, and `read` determinism. Every other test exercised a single stage;
this closes the parse→read loop against fixtures. Suite: **243 passed** (was 239).

## Parked (out of scope / not fixed here)

- The live-`pull` §11 bullets (any year) — offline-scoped issue, parked per #11.
- 2023+ completeness and the `2019–2024` range bullet — partial pulls; not on
  disk as complete years.
- Exact-dollar PTR amounts (#49) — the amount-range labels are extracted; the
  exact-dollar residual is a separate follow-up.
- True identity resolution (bioguide join) — the 34 warnings bound the ambiguity;
  resolution is post-v1 (#16 consumes this dedup rate).

Nothing acceptance-blocking was found; no production code was changed.
