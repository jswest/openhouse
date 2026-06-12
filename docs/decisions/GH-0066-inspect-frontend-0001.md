# GH-0066 — `inspect` review UI: 50/50 split, parsed records, tray

## Context

The `inspect` reviewer app ([[GH-0056]]) shipped a functional but cramped
layout: a fixed three-column grid (`17rem | 1fr | 24rem`) that let the source
PDF's `1fr` middle column dominate the viewport while the record column was a
narrow `24rem`. The record itself was a raw `JSON.stringify(…, null, 2)` dump,
and a second panel showed the PDF's full extracted text (`raw_text`). For a human
judging precision/recall against the PDF beside them, the PDF was too big to read
the record, and the record was too raw to read at a glance.

## Decision

Rework the front end (Svelte 5 source under `web/inspect/src/`, rebuilt into the
committed `openhouse/inspect/static/` bundle — no runtime Node, per GH-0056).
Pure UI: no server, API, data-model, or dependency change.

Load-bearing choices:

- **PDF and record each get 50% of the viewport.** The workspace is now a
  two-column `1fr 1fr` grid (PDF | record + verdict form). The reviewer reads
  both at once instead of squinting at a `24rem` column.

- **Records render parsed, not as JSON.** Metadata (`filing.filing`) is a labeled
  field list; the body renders as tables — PTR `transactions` as one table, each
  present FD schedule as its own `<h2>` + table. A single generic
  `RecordTable.svelte` drives both: columns are the union of keys across the rows,
  so it adapts to any schedule without ten hardcoded layouts. Amount-range objects
  show their human `label`; nulls render `—` (never blank). Per-row verbatim
  `raw_text` is kept behind a `<details>` toggle, not dropped — the
  "never silently drop" agreement applies to the line item's own source text even
  as the whole-PDF extracted-text panel goes away.

- **The documents queue becomes a collapsible tray.** It leaves the grid and
  overlays the PDF's left edge (`position: absolute`), so expanding or collapsing
  it never disturbs the 50/50 split. Collapsed, it's a thin rail showing only the
  toggle; the PDF pane reserves that rail's width as a left gutter so the rail
  never covers content.

- **The extracted-text panel is removed.** The whole-PDF `raw_text` block is gone
  from the UI; the field stays in the `/api/filing/<doc_id>` payload, just
  unshown. What a reviewer needs is the parsed record beside the PDF, not the
  parser's intermediate text.

No SPEC change — layout is not part of the contract. `LABELS_SCHEMA_VERSION` and
the parsed-data `SCHEMA_VERSION` are untouched.
