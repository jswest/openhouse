# GH-0052 — FD NUL-glyph heading recovery + raw_text scrub

**Date:** 2026-06-12
**Issue:** #52 (also resolves #51)

## Context

From 2021 on, most e-filed annual FDs render the Clerk form's small-caps
furniture with no `/ToUnicode` map: pdfplumber extracts each lost glyph as a
literal `U+0000` (one NUL per glyph), so `Schedule A:` comes out as
`S\x00{7} A:`. NUL is not `\s`, is not removed by `str.strip`, and is invisible
in most viewers — so the FD heading matcher (`_FD_HEADING_RE`), keyed on the
letters of `Schedule`, went blind and raised `NotAnFdBody` on fully-populated
bodies, landing them in `extract_failed` despite all the content being present.

### Root cause — glyph loss to NULs (SPEC §2.2)

Two renderings of the same form exist in the wild:

- **letters survive, case-mangled** (dominant through 2020): `ScheDule` /
  `ScHeDule` / `SCheDuLe` — the letter sequence is intact, only case varies.
- **glyphs lost to NULs** (dominant 2021 on): every small-caps glyph extracts as
  `U+0000`.

The structural invariant verified across all 2020–2022 e-filed FDs:
**NULs appear only in the form's own small-caps furniture** — headings, section
titles, the `LOCATION:`/`DESCRIPTION:` labels, the exclusions/certification
titles — and *never* in filer-entered content, which is set in a regular font.

## Decision

`openhouse/pdf.py` only; no schema change.

### Content-safe NUL-run anchors (the core fix, Fable's commit `7fe3c67`)

Because NULs cannot occur in content, each furniture-keyed matcher gains a
**NUL-run branch that cannot collide with content**, while its letters-survive
branch stays byte-identical (so intact documents take exactly the same code
paths as before):

- `_FD_HEADING_RE` — accept `S\x00+ <LETTER>:` alongside `Sc?h?e?d?ule`. Requiring
  the NUL run keeps it collision-proof, and the small-caps appendix title that
  also starts `S\x00` (`Schedules A and B Asset Class Details`) carries no
  `<LETTER>:` so it never opens a fake schedule.
- `_FD_TRAILER_RE` — accept the NUL-rendered Exclusions / Certification titles so
  trailer text cannot leak into the last schedule.
- `LOCATION:` / `DESCRIPTION:` labels (also small-caps) — NUL-tolerant in both the
  detail-line guard and the structured-field extraction; the *values* after the
  colon are regular-font content and survive intact.
- Schedule A row anchor — the tx-over-$1,000 checkbox glyph (`gfedc`/`gfedcb`) is
  absent from the NUL rendering's text layer entirely, so the proven glyph anchor
  can never fire there. For those documents only, a row anchors on its own column
  signature (`[TYPE]` tag then `$lo -` / `None` / `Undetermined`, or a wrapped-
  subholding `⇒` line). Intact documents keep the glyph anchor byte-for-byte.

A document-level `glyphless = any("\x00" in ln for ln in lines)` flag selects the
NUL row anchor — a precise marker, since NULs never occur in an intact body.

### raw_text scrub (this issue's refinement)

The NUL-run anchors recover the bodies, but recovered line items' `raw_text`
still carries the folded-in NUL furniture (literal `U+0000` bytes — invisible but
real in the JSON). One centralized helper scrubs it:

```python
def _scrub_raw_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\x00", " ")).strip()
```

Each NUL run becomes a single space, consecutive whitespace collapses to one
space, ends strip; every other character is left verbatim. It is applied **only**
to the value assigned to `raw_text` (five sites: the A/B/C/D structured parsers
and the E–J `_parse_raw_schedule`), never earlier in the pipeline — the field-
extraction regexes read the un-scrubbed `raw` blob and depend on the NULs being
present.

This is deliberately, **mildly lossy**: the exact furniture rendering (how many
NULs sat where) is dropped. That is the accepted trade — `raw_text` becomes
readable while still carrying all the *content* (asset names, amounts, dates,
`None disclosed.`). Unsegmentable bodies still raise `extract_failed`; nothing is
silently dropped (CLAUDE.md).

**No-op on NUL-free text (load-bearing).** `_group_items` joins pre-stripped
parts with a single space, so any NUL-free `raw_text` is already in the scrub's
fixed-point form — replacing nothing, collapsing single spaces to single spaces,
stripping already-trimmed ends. The scrub is therefore byte-identical on every
intact-rendering body. This is what preserves the 2020 no-regression guarantee,
and it is enforced by a unit test (`test_scrub_raw_text_is_noop_on_nul_free_text`)
and by the doc-by-doc check below.

## Before / after recovery (real data, offline)

Measured by running `extract_fd_schedules` over every e-filed FD under
`data/raw/{2020,2021,2022}/fd/` (read-only; no writes under `data/`) — a full
6,346-doc sweep, re-measured fresh for the critic fixes (the earlier table
understated 2021/2022):

| Year | FD bodies extracted (before → after) |
|---|---|
| 2020 | **1190 → 1257** |
| 2021 | **739 → 1296** |
| 2022 | **67 → 1376** |

(3,929 FD bodies recovered total; the remaining 2,417 docs are extensions / cover
sheets / W·D·G filings that still correctly raise `NotAnFdBody`.)

**Zero regression.** All 1,996 previously-extracted bodies remain byte-identical
(0 mismatch over the full baseline parse at the v0.4.0 accept-data snapshot,
including all **1190** baseline 2020 bodies — byte-for-byte); all extension (`X`)
cover sheets and every W/D/G doc still raise `NotAnFdBody`. This resolves **#51**
(NUL-rendered FDs mis-filed as benign no-body cover sheets).

### Critic-found defect fixes (PR #53 review, all `openhouse/pdf.py`)

A post-implementation 6,346-doc critic sweep surfaced three real defects in the
NUL path. All three are fixed here, each with a regression test and re-verified by
a before/after sweep.

**🔴 Comments-label trailer false-fire (`_FD_TRAILER_RE`).** The per-row
`COMMENTS:` detail label renders `C\x00{7}: <filer text>` in NUL docs and matched
the `^[EC]\x00` trailer branch, so `_segment_schedules` treated it as end-of-body
and **dropped every following content line** until the next heading — **16,901
content lines silently lost across 376 docs / 533 occurrences**, status still
`ok`. The two legitimate NUL trailers (`E\x00{9} …`, `C\x00{12} …`) are never
followed by a colon; the comments label always is. Fix: a **possessive** negative
lookahead `[EC]\x00++(?!:)` — possessive so greedy backtracking can't shrink the
NUL run to expose a non-colon char and defeat the guard. The comments line now
folds into the row's `raw_text` exactly as the intact-glyph `COMMENTS:` label
does; the real trailers still end the body. Net effect: **+7,375 structured items
retained across 349 docs**.

**🟡 Glyphless Schedule-A anchor missed `Over $X` and exact-dollar values
(`_FD_A_ROW_AFTER_TYPE_RE`).** The value-column signature accepted only
`$lo -`/`None`/`Undetermined`, so rows whose value column is `Over $50,000,000` (a
real form bucket) or an exact-dollar value (`$96,550.00`) didn't anchor and merged
into the prior item. Extended the alternation with `Over\b` and `\$[\d,]+\.\d{2}`
(collision-free against continuation lines: wrapped highs are bare `$X` at EOL with
no decimals/`Over`). **28 such rows across 17 docs now anchor** as structured
items (was 0).

**🟡 NULs leaked into STRUCTURED string fields.** `_scrub_raw_text` was applied
only to `raw_text`, but `asset`, `location`, `description`, `income_type`,
`source`, `creditor`, and `liability_type` are sliced from the *un-scrubbed* `raw`
blob, so folded furniture put literal `\x00` into those JSON fields — **626 fields
across 352 docs**. A new **NUL-gated** helper scrubs them:

```python
def _scrub_field(s):
    return _scrub_raw_text(s) if s and "\x00" in s else s
```

The gate is load-bearing: a blanket `\s+`-collapse is unsafe because legitimate
filer values (notably 431 baseline `income_type` values) carry meaningful double
spaces that must stay byte-identical, so the transform fires *only* when a NUL is
actually present. `_scrub_field` is applied to every structured string assignment
**including the `or raw`/`or _scrub_field(raw)` fallbacks** (a Schedule-D row whose
creditor slice is empty falls back to the whole blob, which can carry a folded
comments label). After the fix: **0 NUL bytes in any emitted structured field or
`raw_text` across all 3,929 recovered bodies** (was 626).

## Tests (`tests/test_fd_extraction.py`, all offline)

Fable added the real fixture `tests/fixtures/pdf/efiled_fd_nulglyph_10049721.pdf`
(Hon. Alma Adams, 2021) with README ground truth, plus segmentation / Schedule-A
row / trailer / NUL-label / appendix-title / extension-cover-sheet tests. This
issue adds three:

- `test_nulglyph_raw_text_is_scrubbed_of_nul_bytes` — no recovered item's
  `raw_text` contains `\x00`, and content still survives (real fixture).
- `test_scrub_raw_text_is_noop_on_nul_free_text` — the byte-identity guarantee:
  the scrub leaves representative NUL-free rows unchanged.
- `test_scrub_raw_text_collapses_nul_runs_and_whitespace` — interior / leading /
  trailing NUL runs collapse and strip as specified.

The three critic fixes add five more (offline, synthetic NUL bodies):

- `test_nul_comments_label_does_not_end_schedule` (🔴) — a `C\x00{7}: …` comments
  line mid-Schedule-D followed by a real liability row: the row is still emitted
  (not dropped), the comment text folds into `raw_text`, and a real
  certification NUL trailer still ends the body.
- `test_nulglyph_schedule_a_anchors_over_and_exact_dollar` (🟡) — `Over $X` and
  exact-dollar A rows now anchor as distinct structured items.
- `test_scrub_field_is_nul_gated` (🟡) — `_scrub_field` scrubs only when a NUL is
  present; a NUL-free double-spaced value is returned byte-identical.
- `test_nulglyph_structured_fields_have_no_nul_bytes` (🟡) — `asset` / `location`
  / `description` / `income_type` carry no `\x00`, content preserved.
- `test_nul_schedule_d_creditor_fallback_is_scrubbed` (🟡) — the `or raw`
  fallback path is also scrubbed.

Full suite: **239 passed** (234 prior + 5 new).

## Consequences / residual left deliberately

- **Cap-gains checkbox is unrecoverable in NUL documents → `None`.** The
  tx-over-$1,000 / cap-gains glyph is absent from the NUL rendering's text layer
  entirely, so `cap_gains_over_200` is `None` for glyphless Schedule-B rows
  (honest absence, not a fabricated `False`). It cannot be recovered without the
  glyph and is out of scope here.
- **Merged-row residual is pre-existing and separate.** A small number of
  glyphless Schedule-A rows whose column signature is ambiguous can still merge
  into one salvaged blob; this is the general row-segmentation residual, not
  introduced by the NUL work, and is left for a follow-up.
- The scrub is intentionally lossy on furniture rendering (documented above); the
  exact NUL layout is not recoverable from the scrubbed `raw_text`. The
  un-scrubbed bytes are not retained anywhere — by design, since they carry no
  content.
