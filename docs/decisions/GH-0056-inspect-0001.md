# GH-0056 — `openhouse inspect`: human accuracy review of parsed filings

## Context

`pull`/`parse`/`read` move filings through the pipeline, and the parse manifest
counts what *didn't* parse — but nothing measured whether the filings that *did*
parse are correct. A `parse_status: ok` filing can still be wrong, most visibly
the scanned PTRs that extract **zero** trades while the PDF plainly shows them (in
2022, 392 of 397 PTR bodies, every one marked `ok`). Those are silent recall
failures — exactly the "never silently drop a filing" hazard — and no command
surfaced them.

## Decision

Add a fourth verb, `openhouse inspect <year> --sample <float>`, that samples
reviewable filings, opens a small local web app showing each beside its source
PDF, and records a precision/recall verdict — yielding an accuracy scorecard plus
a resumable local labelled set. Offline and deterministic like `parse`/`read`
(the only socket is the operator's browser on `127.0.0.1`); no new Python deps
(stdlib `http.server` + `pdfplumber`).

Load-bearing choices:

- **Precision/recall, applied symmetrically.** The verdict is a 2×2 over the
  sound/complete duality — `is_fully_precise`/`is_fully_recalled` for entries,
  `is_metadata_accurate`/`is_metadata_fully_complete` for metadata — because a
  bare "correct?" boolean measures soundness only and is blind to *missing*
  entries, which are the dominant error here. Entries also carry optional
  magnitude counts (`n_incorrect_entries`/`n_missing_entries`); metadata does not
  (a fixed handful of scalars). A tallied count must agree with its boolean
  (`count > 0 ⟺ boolean false`; `null` = "wrong, didn't tally"), enforced
  server-side.

- **Seeded, monotonic, stratified sampling.** A `hash(seed, doc_id)` rank makes
  the draw reproducible (offline, no wall-clock) and monotonic (`0.2 ⊃ 0.1`, so a
  sample widens without re-review); stratifying by `pdf_class` × is-PTR and taking
  `ceil(sample · n)` per stratum guarantees the hard cells (scanned PTRs) are
  represented rather than drowned out.

- **Snapshot-pinned verdicts.** Each verdict stores a `sha256` of the parsed
  record at review time. Because this repo re-parses rather than migrates, a later
  re-parse that changes a filing flags its label **stale** instead of silently
  blessing changed output. The verdict/labels schema is versioned by
  `LABELS_SCHEMA_VERSION`, **independent of** the parsed-data `SCHEMA_VERSION`: a
  verdict-schema change must not force a full data re-parse.

- **Svelte → static SPA (not SvelteKit) + committed bundle.** The frontend is
  compiled by Vite to a plain static bundle committed under
  `openhouse/inspect/static/` (not `dist/`, which is gitignored), so `inspect`
  runs with **zero Node** at runtime; `web/inspect/` holds contributor-only
  source and `npm run build` regenerates the bundle. SvelteKit's SSR/adapter
  machinery buys nothing for a read-mostly local tool. The Python core
  (sampling/verdict/snapshot/scorecard/labels) is the durable, pytest-covered
  heart; the web layer is a thin shell. Scorecard JSON to stdout, residual to
  stderr — the machine contract holds.

## Scope

Filing mode only. Deferred as named follow-ups: **trade mode** (per-transaction
flagging + structured gold corrections → an auto-scoreable gold set), **CI
scoring** (promoting labels to committed ground truth scored against re-parses),
and **metadata magnitude counts** (intentionally kept as two booleans).

Code: `openhouse/inspect/` (`core`, `verdict`, `labels`, `server`), CLI wiring in
`openhouse/cli.py`, frontend in `web/inspect/`, bundle in
`openhouse/inspect/static/`. Tests: `tests/test_inspect.py`,
`tests/test_inspect_server.py`, `inspect` cases in `tests/test_cli.py`.
