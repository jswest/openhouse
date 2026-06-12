# GH-0010 — `read` command: filings / filing / trades / summary, `--table`

**Date:** 2026-06-11
**Issue:** #10

## Context

M5/M6 (SPEC §12) light up the query surface — `openhouse read` (SPEC §5), the
third verb and the surface a future agent skill will drive (SPEC §8). It is a
**pure function over** `parsed/`: offline, read-only, deterministic, no database
(scanning the JSON in place is milliseconds at this scale, and skipping a load
step means `read` can never disagree with the last `parse`). New module
`openhouse/read.py`, four subcommands, JSON to stdout by default with a `--table`
human view.

This sub-issue lands under the working agreement that arrived alongside it:
**"Every query must be sound or complete — declare which"** (CLAUDE.md). That
agreement, not just the SPEC's filter list, shapes `trades`.

The producer of PTR bodies (#9) runs in parallel on its own branch, so this
module must not couple to its schema class.

## Decision

### The sound `--ticker` vs the completeness-leaning `--asset` split

`trades` exposes **two declared query modes** over transactions, each stating its
guarantee in its `--help` and on stderr at run time:

- **`--ticker <SYM>` is SOUND.** Case-insensitive *exact* match on the `ticker`
  field. **No false positives** — every hit is a real symbol match. It bounds the
  truth **from below** ("at least these"). It never matches an asset *name* and
  never infers a symbol from one: offline we cannot map name→symbol without a
  drifting network dataset, and names are ambiguous (`Apple Hospitality REIT →
  APLE`, not `AAPL`). Its help declares the at-least bound; at run time it also
  emits the **null-ticker residual** — the count of in-range `[ST]`/`[OP]`
  transactions whose `ticker` is null (the filer omitted the symbol), which the
  sound query structurally cannot search.

- **`--asset <substring>` is COMPLETENESS-leaning.** Case-insensitive substring
  over the **verbatim** `asset` text, which *includes* the embedded `(TICKER)
  [TYPE]` (e.g. `"Apple Inc. (AAPL) [ST]"`). It over-matches, so it bounds the
  truth **from above** ("at most these; may include spurious hits to discard").
  This is the tool to reach for when you would rather not miss a trade.

They are deliberately **not fused** into one symbol-or-name query with a
match-basis marker: that yields both error types at once and bounds nothing
(useless, per the agreement). When the two cannot both hold, we **prefer
completeness** (a missed trade is worse than a spurious hit a human can discard) —
which is why `--asset`, not `--ticker`, is the "don't miss it" tool, and why the
sound query's blind spot is surfaced rather than hidden.

The narrowing filters (`--member`, `--owner`, `--type P|S`, `--since/--until`,
`--min-amount`) compose *within* whichever mode is chosen.

### Universal unparsed-residual on every range query

Every range query — `filings`, `trades`, `summary` — prints a **residual** line
to **stderr**: the manifest's count of in-range filings that did *not* parse
(`scanned` + `missing` from `by_pdf_class`, plus `error` from `by_parse_status`),
so the result is explicitly "**complete over the K e-filed filings parsed in
range; M did not parse (scanned X / missing Y / error Z)**". This realizes the
agreement's "state it relative to the parsed set **plus the manifest's count of
what didn't parse** — complete over the known, explicit residual for the unknown."
Results stay on stdout (JSON, the machine contract); residual + guarantee notes go
to stderr; exit stays **0** (a partial range is not an error).

### Consume parsed JSON as dicts by key — no import of #9's schema

`read.py` reads `filings.json`, `ptr/<DocID>.json`, and the two manifests as plain
dicts accessed by key. It imports **no** PTR-transaction schema class: the
on-disk JSON shape is the only contract, and #9 (the body producer) runs in
parallel, so coupling to its class would be a cross-branch hazard. A body is
joined to its filer by **DocID** (the body filename) against `filings.json`.

### No database — scan in place

There is no load/index step. `read` walks the per-year JSON directly (SPEC §5):
at ~2,250 filings + low-tens-of-thousands of transactions per year it is
milliseconds, and it guarantees `read` can never drift from the last `parse`.

### Graceful missing-year degradation

A range where some years are not parsed (`parsed/<year>/filings.json` absent)
answers from the years that exist and reports the skipped years on stderr, exit
0 (SPEC §5). `filing <doc_id>` scans all parsed years for the DocID; a non-PTR or
unparsed-body filing returns metadata with `body: null` and a stderr note — never
an error, never a silent gap.

### CLI seam

`read` owns its own sub-parser (in `read.py`) with the global `--data-dir` /
`--table`. `cli.py` dispatches everything after the `read` token to
`read_mod.run(...)` **before** argparse runs — argparse's `nargs=REMAINDER`
mishandles a leading global flag (`read --table filings 2021`), so slicing `argv`
on the `read` token is the order-robust seam. The top-level parser keeps a
help-only `read` entry so `openhouse --help` still lists it. `current_year` is
injected into `run` (never read from the clock there) so range validation stays
deterministic (SPEC §9). The former `read` stub and `_stub` helper are deleted.

## Scope

New `openhouse/read.py`; `cli.py` seam (intercept + dispatch, stub removed);
hand-authored fixtures under `tests/fixtures/parsed/{2021,2022}/` (filings, PTR
bodies, both manifests) conforming to the real on-disk shapes; `tests/test_read.py`
(32 tests) covering each subcommand, JSON + `--table`, the `--ticker` soundness
(no false positive on a null-ticker `[ST]` whose name contains the symbol) and its
declared bound + null-ticker residual, `--asset` completeness (it finds that same
row), the universal residual line, the `--member`/`--owner`/`--type`/date/amount
filters, the partial-range skipped-year path, and that `read` writes no file and
makes no network call. No body extraction here (that is #9/#12). `parse.py`,
`schemas.py`, and `docs/decisions/README.md` untouched. 165 tests pass.
