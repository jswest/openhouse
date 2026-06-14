# GH-0100 — Schedule A wrapped-`[TYPE]` / `⇒`-subholding row anchoring

**Date:** 2026-06-14
**Issue:** #100 (the lone holdout of omnibus #104 / v0.6.3, shipped standalone)

## Context

Schedule A anchors each asset row on a disjunction of column signatures
(GH-0070): a glyph-terminated line, a `[TYPE]` tag **immediately followed by** the
value column, a `⇒` subholding arrow **immediately followed by** the value
column, or a tag-less line carrying a *dangling* value low (`$lo - <word>`). All
four require the anchoring token and the value column to sit on the **same
physical line**. pdfplumber's column reflow routinely breaks that adjacency: when
an asset name runs long, the value prints on the name's first line while the
`[TYPE]` tag — and, on a `⇒` cluster, the subholding's own name and the arrow —
wrap onto the line(s) below. No signal fired, so the row folded silently into the
row above: the asset vanished with no standalone record, its value bucket
sometimes mis-captured as the host row's income, and `parse_status` stayed `ok`
with no residual. A *missed holding a human can't know to look for* — the worst
failure class under "never silently drop" — and a regression of the GH-0070 fix
that had headlined "0 Schedule A collapses".

Confirmed on the 2026-06-13 sweep across five filings (Pascrell 10046898,
Ivanovskis 10050888, Harris 10054295, Pou 10068928, Suozzi 10048000) in three
distinct layouts: (1) value line then wrapped-tag line; (2) the `⇒` subholding
cluster whose value, arrow, name, and tag scatter across four lines; (3) a
**None/Over-value** row whose tag wrapped, carrying no `$lo -` for any signal to
see.

## Decision

Recover the rows by **broadening the anchor**, and make any *irreducible* merge
**loud** rather than silent. Keep the change inside the anchoring/grouping layer
so the GH-0098/GH-0099 column parsers (which were specced to depend on this fix
but shipped ahead of it) operate on correctly-bounded rows without being rewritten.

### 1. Tag-less value-low anchor (`_FD_A_VALUE_LOW_RE`)

A tag-less, glyph-less, arrow-less line that opens the value column with a
`$lo -` low bound anchors a row. This generalizes the old dangling-low signal
(which required a *word* after the dash) to **complete `$lo - $hi` buckets** too.
A wrapped *high* bound is a bare `$N` with no trailing dash, so a continuation
line never trips it. This one signal recovers both the wrapped-`[TYPE]` member
row (layout 1) and the buried `⇒` subholding cluster (layout 2): the umbrella
line's value low anchors the row and the wrapped tag/name/arrow fold in as
continuations, where the existing column parser untangles them.
`_FD_DANGLING_LOW_RE` is left untouched — `_fd_amount_entries` still uses its
narrower form for wrapped-high pairing.

### 2. One-line lookahead for the None/Over-value row (`_group_items` `starts_before`)

A None/Over-value row whose tag wrapped (layout 3) carries no value low for
signal 1. `_group_items` gains an optional one-line lookahead: a value-bearing,
non-detail line whose **next** meaningful line is a *bare wrapped tag-tail*
(`_is_wrapped_tag_tail` — only a real asset-type code `[MF]`/`[BA]`, no value, no
glyph, no arrow) opens a row. Requiring a value signature on the body line is what
stops it from splitting a `⇒` subholding's pure-name continuation line (which
carries no value and must fold into its value line above).

### 3. Owner-before-value (`_FD_OWNER_BEFORE_VALUE_RE`)

On a wrapped-tag row the owner token (`SP`/`DC`/`JT`) prints *before* the value
low rather than after the tag, so the after-tag/after-arrow owner regexes missed
it (owner `None`, the token bleeding into the asset name). Added as a third
fallback, anchored to the value low so it can't match an owner-looking token
inside a name. A normal row matches the after-tag form first.

### 4. `schedule_incomplete` residual (the backstop)

A row that still fuses two assets after anchoring — **≥2 real asset-type codes in
one `raw_text`** (`_schedule_merge_residual`, gated by `_FD_ASSET_TYPE_CODE_RE` so
a `[VOO]` ticker or `[1]` footnote in a name is not miscounted) — is flagged on a
new in-memory `FdBody.incomplete_schedules`. `parse` reads it and appends a
`schedule_incomplete` entry to `unparsed-manifest.json` while keeping the body and
`parse_status: ok` — mirroring the GH-0113 `date_out_of_range` residual exactly.
So a merge no anchor can separate (e.g. a None-value subholding past the
lookahead's reach) is **loud, never a silent drop** — its `raw_text` is intact.
The hard GH-0070 collapse/severe guard (whole-filing `extract_failed`) is
unchanged; the residual is the softer, body-preserving tier below it. `read`'s
residual line is manifest-count-based and does not enumerate per-reason entries —
the same (acknowledged) limitation `date_out_of_range` already has; left
consistent, a shared follow-up.

### Schema bump

`SCHEMA_VERSION` **7→8** (Generation 8 note in `schemas.py`); recovered rows
change parsed output, so a re-parse from `raw/` is required — which the bump
forces. `schemas.fingerprint` refreshed (`release.py --write-fingerprint`) so the
GH-0043 drift guard stays green.

## Result + the GH-0098 / GH-0099 assessment

Row recovery on the five filings (anchored Schedule A rows, before → after):
Ivanovskis 7→9, Pascrell 38→40, Pou 27→34, Harris 30→32, Suozzi 82→83 — every
named drop recovered, zero rows left fused. Full suite **412 passed**.

Because #98 and #99 merged *ahead* of #100 on the unfixed row-anchor base, their
behavior on the newly-recovered rows was audited:

- **#98 (amount cross-pairing) is sound.** Zero inverted value/income ranges on
  any recovered row, before or after — the recovered wrapped rows do not
  reintroduce the cross-column contamination #98 fixed.
- **#99 (column bleed) has a pre-existing, cosmetic gap on wrapped rows.** Value
  literals (`None`/`Over`) and income-type words (`Tax-Deferred`) leak into the
  asset *name* on wrapped/`⇒` rows because #99's column-zone detection anchors on
  the `]`/`⇒`/glyph that these rows lack. This is **pre-existing** (present on
  main for the rows that already anchored — Pou's 6, Suozzi's 18) and **does not
  affect amounts** (the leaked income-type is a tax status with no dollar figure;
  value/income buckets are correct). #100 extends that surface by a handful of
  newly-recovered None-value rows and, via the owner-before-value fix, *removes*
  one bleed case — net neutral-to-positive on names, decisive on completeness.

The dependency-ordering risk did not produce a correctness bug. Extending #99's
column-zone detection to wrapped-tag rows (strip the value-literal / income-type
column on a row whose anchor is the value low, not the `]`/glyph) is filed as a
follow-up — cosmetic name-field only, deliberately out of #100's anchoring scope
to keep the blast radius off the fragile #98/#99 code.

## Tests (all offline, synthetic pages mirroring the real layouts)

`tests/test_fd_extraction.py`: `..._wrapped_type_tag_row_anchors_separately`
(layout 1 + value-not-mis-captured-as-income + owner recovered),
`..._none_value_wrapped_tag_row_anchors_via_lookahead` (layout 3),
`..._buried_subholding_cluster_reconstructed` (layout 2, asserting the pure-name
line does **not** over-split), `..._unsplit_merge_flags_residual` (the
`schedule_incomplete` backstop), `..._ticker_in_name_is_not_a_merge_residual`
(`[VOO]` beside `[ST]` is not a merge).

## Consequences

- Re-parse from `raw/` required (the bump forces it); offline and cheap.
- New `schedule_incomplete` reason coexists with a written body + `parse_status:
  ok` (the second such reason after `date_out_of_range`).
- Follow-up: extend GH-0099 column stripping to wrapped-tag rows (name-field
  polish; amounts already correct).
