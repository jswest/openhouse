# GH-0046 — PTR extraction: amount-column wrap + small-caps case

**Date:** 2026-06-12
**Issue:** #46

## Context

The #13 acceptance pass over **real** 2020 e-filed PTR PDFs showed the PTR
extractor (`extract_ptr_transactions`, #9) failing **~two-thirds** of filings:
411 of 597 e-filed 2020 PTRs (and 410 of 565 in 2021) raised the completeness
guard and were dropped to `extract_failed`, recovering only 281 transactions in
2020 (420 in 2021). GH-0009 itself predicted this ("a future PTR whose layout
splits that signature would simply not match"). Two real-PDF quirks the two
committed fixtures happened not to exhibit were the cause.

### Root cause 1 — amount-column wrap (SPEC §2.2)

pdfplumber keeps the row's date-pair/amount/glyph signature on one physical line
*most* of the time, but on a sizable minority of filings the amount column wraps:
the header line ends `$LOW - <glyph>` (e.g. `$15,001 - gfedc`) with the `$HIGH`
bound (`$50,000`) spilled onto the **following** line. GH-0009's strict
`(\$[\d,]+ - \$[\d,]+)` group required both bounds on the header line, so every
wrapped row failed the header regex, was skipped, and the
`status_blocks != len(transactions)` guard then failed the whole PDF.

### Root cause 2 — small-caps case-sensitivity (SPEC §2.2)

The detail/status anchors render with small-caps glyphs that land on **different
letters from one filing to the next** — `FILINg STATUS:` / `FIlINg STATuS:` /
`FILING STaTUS:` / `SUBHoLDING oF:` / `DESCRIPTIoN:` (10+ renderings across 2020
alone). GH-0009 matched a single fixed-case literal (`FILINg STATUS:`,
`DESCRIPTION:`), so on the majority of real PTRs the row-boundary detail lines
and — critically — the **status-block count** were missed. (The status count
being *also* missed is why many of these PDFs produced a silently-empty body with
status "ok" rather than a loud failure: 0 transactions matched, 0 status blocks
counted, guard passed. Making the count case-insensitive converts those into the
honest `extract_failed` they always should have been.)

## Decision

Edit `openhouse/pdf.py` only (the row model and folding loop); no schema change.

### Optional high bound, recovered from the continuation line

`_PTR_ROW_RE` now captures the low bound and an **optional** high bound
separately (`(\$[\d,]+)\s+-\s+(\$[\d,]+)?`). When the high bound is absent on the
header line, the extractor recovers it from a following line as the lone money
token there, wherever it sits — at the line start (`$50,000`, asset name did not
wrap) or end (`… (BABA) [ST] $50,000`, asset name wrapped onto the same line);
whatever else is on that line folds back into the asset name as a normal wrap.

**Page-break sub-case (beyond the candidate patch).** When the header wraps
across a *page break*, pdfplumber's repeated per-page furniture (and a stray
`gfedc` glyph remnant from the header's end) lands **between** the header and the
`$HIGH` line. The candidate patch peeked only at the immediately-next line, hit
furniture, and dropped the row. The recovery now skips furniture / lone-glyph /
blank lines (stopping at the next row or this row's own detail line) before
taking the high bound — the same furniture-tolerance the asset-name wrap loop
already had. This is what `_PTR_GLYPH_ONLY_RE` was added for.

### Case-insensitive anchors + small-caps type normalization

`_PTR_DETAIL_RE`, the new status-only `_PTR_STATUS_RE`, and the `DESCRIPTION:`
matcher are all `re.IGNORECASE`; the status-block guard counts with
`_PTR_STATUS_RE`. The transaction-type group also accepts lower-case glyphs
(`[PpSsEe]`), normalized back to canonical `P`/`S`/`E`/`S(partial)`.

### Genuinely-unparseable rows still fail loudly — never a half-range

If the high bound never materializes (not on the header line, not as a
post-furniture money token), the row is **dropped from `transactions[]`** rather
than emitted with a fabricated half-range. The status-block guard then surfaces
the mismatch as `extract_failed` (CLAUDE.md: prefer completeness, never silently
drop, never fabricate). This is exactly the path the **exact-dollar amount**
format takes (see Consequences).

## Before / after recovery (real data, offline)

Measured by running `extract_ptr_transactions` over every e-filed PTR under
`data/raw/{2020,2021}/ptr/` (read-only; no writes under `data/`).

| Year | extract_failed (before → after) | total transactions (before → after) |
|---|---|---|
| 2020 | **411 → 97** | **281 → 6,325** |
| 2021 | **410 → 29** | **420 → 5,322** |

No regression: **zero** PDFs that previously extracted ≥1 transaction now fail or
return a lower count (verified doc-by-doc before vs after). The only PDFs that
move from "succeeded" to `extract_failed` are ones that previously returned a
silently-empty body (0 txns, 0 mis-counted status blocks) — now correctly loud.

## Promoted fixture

`tests/fixtures/pdf/efiled_ptr_wrap_20013811.pdf` (Hon. Matt Gaetz, 2020;
`data/raw/2020/ptr/20013811.pdf`, 54 KB, 1 page, 3 rows). Chosen because it
exhibits **every** root-cause case in one small file: all three rows have a
wrapped `$HIGH` (`$15,001 -`…`$50,000`; `$50,001 -`…`$100,000`); every anchor is
small-caps with inconsistent case (`FILING STaTUS:`, `SUBHoLDING oF:`,
`DESCRIPTIoN:`, `LoCaTIoN:`); plus an `E` (exchange) type, cap-gains both set and
unset, a small-caps `DESCRIPTIoN:` line that must still be captured, and a
null-ticker `[PS]` row. Before #46 this PDF failed the guard wholesale.

## Tests (`tests/test_ptr_extraction.py`, all offline)

- Three real-fixture tests on `efiled_ptr_wrap_20013811.pdf`: all 3 rows
  extracted; the wrapped `$HIGH` folds into the correct `low`/`high`/`label`; the
  small-caps `DESCRIPTIoN:`/`SUBHoLDING oF:` anchors bound the row and the
  description is captured; null `[PS]` ticker; `E` type; cap-gains both polarities.
- Synthetic unit tests (`_FakePdf`): page-break wrapped-`$HIGH` recovery across
  furniture + a stray glyph; wrapped-`$HIGH` sharing the asset-wrap line keeps
  both the range and the asset tail; small-caps type letters (`p`, `s (partial)`)
  normalize; a truncated row whose `$HIGH` never materializes raises
  `PdfExtractError` (no fabricated half-range) rather than silently dropping.

Full suite: **218 passed** (211 prior + 7 new).

## Consequences / residual left deliberately

- **Exact-dollar amounts are out of scope and remain `extract_failed`.** Many of
  the ~97 (2020) / ~29 (2021) still-failing PDFs report an *exact* dollar value
  in the amount column (e.g. `$894.97`) instead of a `$LOW - $HIGH` bucket. The
  `AmountRange` schema (`{low, high, label}`) cannot represent a single exact
  value, and `schemas.py` is outside this issue's blast radius (and outside the
  documented #46 root causes). These rows now surface honestly as `extract_failed`
  in the unparsed manifest — a bounded, visible residual a human/follow-up can
  pick up, never a silent gap. Handling the exact-dollar format is a separate
  schema-touching change worth its own issue.
- The wrapped-high recovery assumes the high bound is the **lone** money token on
  its (post-furniture) continuation line; this held across every 2020/2021
  wrap-continuation observed. A continuation line carrying two money tokens would
  take the first — a risk to watch as more years are pulled, mitigated by the
  status-block count guard (a wrong pairing would not change the *count*, but any
  *structural* miss still fails loudly).
- The header regex's `E (partial)` accommodation is speculative (the domain only
  has `S (partial)`); left in as harmless, not load-bearing.
