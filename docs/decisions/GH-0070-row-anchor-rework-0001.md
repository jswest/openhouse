# GH-0070 — row anchors keyed on sometimes-absent signals: rework + guards

**Decision.** Row segmentation across the extractors stops gating on any
single rendering-dependent signal and anchors on a **disjunction of column
signatures**; Schedules A/B gain a **completeness guard** that fails loudly on
catastrophic merging; the PTR extractor gains glyphless row forms (absorbed
from the exploratory `fix/ptr-glyphless-2022` worktree). `SCHEMA_VERSION`
**5→6** (nullable `PtrTransaction.cap_gains_over_200`,
`ScheduleAItem.income_preceding`); re-parse from `raw/`, no migration.

**Why.** One species of bug, five instances: a `_group_items`-style row anchor
keyed on a signal the form only sometimes emits silently merges every
unanchored row into the previous item — with `parse_status: "ok"`. Measured
before the fix: 955 of 5,132 parsed FDs (19%) had their whole Schedule A
collapsed into one item, 686 more partially merged; Schedule B merged in 31%
of filings carrying one; D in 9%; F anchored zero rows in 8%; and **100% of
2023–2024 PTRs parsed as `{"transactions": []}`** because the NUL rendering
dropped the checkbox glyph the row regex *and* the completeness guard both
keyed on (0 == 0 passed; the cutover is ~2022-04 — 2023–24 is the fully
affected, fully measured window).

**The anchors now.**
- *Schedule A*: `[TYPE]`+glyph, OR `[TYPE]`+value-column signature, OR
  arrow+value signature, OR a glyph-terminated line (the checkbox is the last
  column; a stranded glyph-only remnant folds instead), OR a tag-less dangling
  value low. A bare `⇒` is **not** an anchor (it lands on wrapped continuation
  lines among wrapped high bounds — Rochester-trust layout).
- *Schedule B*: date+type+amount signature, OR glyph-terminated line, OR
  arrow+type+amount (the Date column can hold `Semi-Annually`); unpadded dates
  (`05/5/2020`); a detached `(partial)` upgrades S.
- *Schedules D/F*: bare-year dates anchor — D only with the amount column's
  own range start on the line (a year can sit next to a wrapped bare high
  bound on a continuation; only a real row opens its range).
- *PTR*: glyphless row variants consulted only when the document carries NULs;
  `cap_gains_over_200: null` = unknown, never fabricated; NUL-aware labels
  keep the FILINg STATUS guard loud; truncate at the table footnote.
- Case-mangling hits content tokens (`Sp`, `[oT]`, `none`): owner/literal
  matchers are case-insensitive, captured owners normalized upper.

**Amount columns are positional entries** (`_fd_amount_entries`): complete
buckets, `Over $X` slot-holders, and dangling lows re-paired with bare wrapped
highs **in column order, only when the counts match exactly** — ambiguity
degrades to `None`, never a fabricated bound. Value/income/preceding assign by
position; a literal `None`/`Undetermined` value holds its slot and shifts the
buckets right.

**The guard is deliberately approximate.** The per-row `[TYPE]`-tag invariant
drifts by a row or two on ~30% of real documents (tag-less rows, brackets in
filer text, header fragments — measured on a 300-doc stratified sample), so an
exact-count guard would `extract_failed` a third of the corpus. It fires only
on **total collapse** (1 item, ≥3 tags) and **severe merge** (items×2 ≤ tags, tags ≥4):
~0.7% of the sample, zero false collapses. Small drift passes — verbatim
`raw_text` still carries every line. The PTR lesson is structural: a
completeness guard must count rows from a signal **independent** of the row
anchor, or it inherits the anchor's blindness.

**Cost accepted.** Off-by-a-row merges below the severe threshold stay silent
(raw_text-complete, structured fields possibly off on the merged row); F's
bare-year anchor has no corroborating column and could in principle split a
terms continuation that begins with a year. Both are precision/recall trades
made toward completeness, per the working agreement.

Supersedes nothing; extends GH-0052 (NUL renderings) and GH-0046 (PTR wrap).
The canonical Thompson fixture's Schedule A ground truth was itself wrong (25
pinned, 27 real — two `[DB]` rows silently merged); now guard-pinned at 27.
