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
`data/raw/{2020,2021,2022}/fd/` (read-only; no writes under `data/`), per Fable's
acceptance pass:

| Year | FD bodies extracted (before → after) |
|---|---|
| 2020 | **1190 → 1242** |
| 2021 | **739 → 1242** |
| 2022 | **67 → 555** |

**Zero regression.** All 1,996 previously-extracted bodies remain byte-identical;
all extension (`X`) cover sheets and every W/D/G doc still raise `NotAnFdBody`.
This resolves **#51** (NUL-rendered FDs mis-filed as benign no-body cover sheets).

The raw_text scrub does not change this: re-extracting all **1190** baseline 2020
FD bodies with the scrub applied and diffing against the v0.4.0 baseline parse at
`parsed/2020/fd/` yields **0 mismatches, 0 errors** — byte-for-byte identical.

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

Full suite: **234 passed** (231 prior + 3 new).

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
