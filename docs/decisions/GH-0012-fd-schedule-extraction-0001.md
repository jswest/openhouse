# GH-0012 — FD schedule extraction (A–D structured, E–J raw_text)

**Date:** 2026-06-11
**Issue:** #12

## Context

`parse` (#6) classifies each on-disk PDF and, since #9, extracts e-filed **PTR**
bodies. The remaining hard extraction (SPEC §6, M6) is the **annual FD**: a
schedule-by-schedule document (Schedules A–J, SPEC §6.3). This issue adds e-filed
FD schedule extraction, wired into the existing classification pass so each
e-filed annual FD's body is written during `parse`. PTR extraction and the
efiled/scanned classifier are untouched.

The source is real e-filed FD PDFs: the committed fixture
`tests/fixtures/pdf/efiled_fd_10042852.pdf` (Hon. Bennie G. Thompson, 2020) plus
the 2020/2021 FD corpus already on disk under `data/raw/`. pdfplumber (SPEC §9)
is the layout-aware extractor; the SPEC §2.2 caveats — small-cap glyphs lost,
columns run together, amount ranges wrap across lines — make naive heading-text
matching and line-splitting wrong.

## Decision

### Segment by schedule letter, never by heading text (SPEC §2.2)

The form's small-cap glyphs are lost on extraction and render inconsistently
across PDFs — the same heading appears as `ScHeDule`, `SCheDule`, and `ScheDule`.
Only the `S…edule <LETTER>:` *shape* is stable, so `_FD_HEADING_RE`
(`^Sc?h?e?d?ule\s+([A-Ja-j]):`) captures the **letter** and ignores the rest of
the heading. Each heading opens a segment that runs to the next heading or to the
trailing exclusions/certification block; per-page repeated column-header
furniture is dropped so the column parsers see only rows.

### `None disclosed.` → schedule **absent**, not an empty array

An empty schedule renders the literal `None disclosed.` (SPEC §2.2). Such a
schedule's letter is **omitted** from the body's `schedules` map entirely — never
an empty list. This lets a consumer tell "the filer disclosed nothing" (letter
absent) from a hypothetical extraction gap, and matches the issue's "`None
disclosed.` marks a schedule absent."

### Depth-ordered: A–D structured, E–J raw_text-only; raw_text on *every* item

Per SPEC §6.3's depth-ordering, A–D are column-parsed into structured fields
(amount ranges as `{low, high, label}`, dates as ISO, owner/type tokens, the
`[TYPE]` asset-class tag), and E–J ship as `raw_text`-only line items. **Every**
line item — structured or not — carries a verbatim `raw_text` of its joined row,
so nothing extracted is ever lost to a schema gap. Structured columns are all
`Optional`: the live form leaves many cells blank, and an open-ended value the
range parser can't bucket (`Undetermined`, `Over $1,000,000`) leaves the field
`None` with the original wording preserved in `raw_text` — completeness over the
known, explicit residual in the text (CLAUDE.md).

### Column-interleave repair on wrapped value ranges (Schedule A)

The hardest §2.2 artifact: a Schedule A asset whose value range's high bound
wraps to the row's *end*, with the income column interleaved between the two
halves — `BancorpSouth Bank [BA] JT $100,001 - Interest $201 - $1,000 gfedc
$250,000`. A naive "first complete `$lo - $hi`" picks the **income** bucket as
the value. `_schedule_a_amounts` detects the dangling low bound (`$100,001 -`
with a non-`$` token after) and pairs it with the trailing standalone `$250,000`,
reconstructing `$100,001 - $250,000` as the value and the interleaved
`$201 - $1,000` as income. When the pattern doesn't apply it falls back to the
two complete buckets in order; an un-untanglable row leaves the field `None` with
`raw_text` intact.

### Extension cover sheets: `NotAnFdBody`, not an extraction failure

Filing types other than `P` all route to the `fd` family, which includes
**extensions** (DocID-prefix `3`) — e-filed PDFs that are cover sheets with *no*
schedule headings, not annual-FD bodies. `extract_fd_schedules` raises a distinct
`NotAnFdBody` when no headings are present; `parse` catches it and writes **no
body** (the filing still lives in `filings.json` with `pdf_class="efiled"`,
`parse_status="ok"`). This is deliberately **not** `extract_failed`: the PDF read
cleanly, it simply isn't a schedule document. A genuinely unreadable PDF still
raises `PdfExtractError` → `parse_status="error"` + unparsed `extract_failed`,
exactly as for PTRs (never a silent drop, never a misleading empty body).

### Body-file shape and path: a fixed contract mirroring the PTR body

Each e-filed annual-FD body is written to **`parsed/<year>/fd/<DocID>.json`**
(SPEC §6.4) as an object with a **single `"schedules"` key** holding the §6.3
letter→items map:

```json
{ "schedules": { "A": [ <§6.3 item>, … ], "C": [ … ], "E": [ { "raw_text": "…" }, … ] } }
```

Filing metadata is **not** duplicated — `filings.json` is the single source of
truth, joined by DocID, exactly as the PTR body (#9) decided. Byte-stable
(`indent=2`, `sort_keys=True`, trailing newline) so re-parse from `raw/` is
deterministic.

### Schema version → 4 (re-parse, not migrate)

Adding the FD body is a parsed-schema change, so `SCHEMA_VERSION` moves from 3 to
4 (the integer generation that *is* the release minor, GH-0037). Pre-v1 there is
no migration: bump, delete old code, re-parse from `raw/`.

## Consequences

- e-filed annual FDs now carry a structured schedule body; A–D are column-parsed,
  E–J ship as `raw_text` (deeper E–J structure is a tracked post-v1 issue, per
  the issue's scope note).
- Structured A–D fields are best-effort over a layout-variable form; the
  authoritative, always-present datum is each item's verbatim `raw_text`. A
  consumer needing a guarantee reads `raw_text`; the structured columns are a
  convenience that degrade to `None` rather than to a wrong value.
- The heading-letter segmentation assumes pdfplumber keeps each `S…edule
  <LETTER>:` heading on its own line; this held across the committed fixture and
  the 2020 corpus (success rate reported in the parse manifest), a risk to watch
  as older years (2008–2011, SPEC §10) are pulled.
- FD extraction runs in the same `parse` classification pass as PTR extraction;
  no new command, flag, or pass was added (CLAUDE.md "smallest fix that fits").
