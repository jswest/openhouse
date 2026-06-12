# GH-0009 — PTR body extraction (layout-aware)

**Date:** 2026-06-11
**Issue:** #9

## Context

`parse` (#6) maps the index XML into validated `FilingMetadata` records and
classifies each on-disk PDF (#7) as `efiled` / `scanned` / `missing`, but leaves
every body unextracted. M4 / SPEC §4 step 3 needs each **e-filed PTR** (filing
type `P`) turned into the SPEC §6.3 `transactions[]` body. This issue adds that
extraction, wired into `parse` so each e-filed PTR's body is written during the
existing classification pass. FD schedule bodies are a later issue and untouched.

The source is the only honest one available: real e-filed PTR PDFs. pdfplumber
(adopted in #7, SPEC §9) is the layout-aware extractor — the SPEC §2.2 caveats
(columns run together, amount ranges wrap, headings lose small-cap glyphs) make
naive line-splitting wrong.

## Decision

### Row model: anchor on the header line, fold wrapped asset names back in

pdfplumber joins each transaction into a **header line** —
`[owner] <asset…> <type> <txn-date> <notif-date> <amount range> <cap-gains glyph>` —
but a long asset name wraps onto the *following* line(s) before the row's
`FILINg STATUS:` / `SUBHOLDINg OF:` / `DESCRIPTION:` detail lines. `_PTR_ROW_RE`
anchors on the header line: the **date pair + amount range + cap-gains glyph at
end of line** are an unambiguous signature, so a regex match is reliable without
positional word geometry. After a header, phase-1 folding joins bare
continuation lines into the asset until the first detail line; phase 2 captures
only a `DESCRIPTION:` line; the row ends at the next header. Verified: 57 rows on
the Lee fixture (37 `P` + 12 `S(partial)` + 8 `S`), the multi-line
`Albertsons Companies, Inc. Class A (ACI) [ST]` joined into one asset string.

### Ticker: strict symbol-only, uppercased — `null` is correct, never inferred

`ticker` is **only** the parenthesized `(SYMBOL)` embedded in the asset name,
uppercased. Two load-bearing choices:

- **Strict symbol-only.** A ticker is never inferred from the company name. No
  external name→ticker library is added. This is **precision = 1 by design**:
  every ticker we emit is one the filer literally printed.
- **Uppercasing.** pdfplumber renders the form's small-caps glyphs as mixed
  case — raw extraction yields `(CSgP)`, `(gPC)`, and (elsewhere) `AAPl` /
  `bRK.b`. Uppercasing the captured symbol defeats that artifact deterministically
  (`CSgP`→`CSGP`, `gPC`→`GPC`, `AAPl`→`AAPL`, `bRK.b`→`BRK.B`). We uppercase the
  symbol *only*, never the asset name (which is preserved verbatim).

When an asset carries no parenthesized symbol — corp bonds `[CS]`, govt/pfd
`[GS]`/`[PS]`, `[OT]`, `[VA]`, `[SA]` legitimately have none — `ticker` is
`null`. `null` is the **correct** value, not a defect and not a sentinel string:
the asset class is disambiguated by `asset_type` (the bracketed tag, preserved
raw). The Lowenthal fixture is the canonical null-ticker case (an `SP` Cinemark
`[CS]` sale).

### `S (partial)` and the cap-gains glyph: read from the form's literal rendering

- The form prints the transaction type as `S (partial)`; we normalize to the
  no-space `S(partial)` (SPEC §6.3's enum) while leaving `P` / `S` / `E` as-is.
- The cap-gains checkbox renders as the literal glyph string at the row's end:
  `gfedc` **unchecked** vs `gfedcb` **checked**. `cap_gains_over_200` is simply
  `glyph == "gfedcb"`. (The same `gfedcb` glyph appears once more on the final
  certification line; it is outside any transaction row's header regex, so it is
  never miscounted — 12 checked transaction rows on the Lee fixture, distinct
  from the 13th certification glyph.)

### Body-file shape and path: a fixed cross-issue contract

Each e-filed PTR body is written to **`parsed/<year>/ptr/<DocID>.json`** as an
object with a **single `"transactions"` key** holding the §6.3 array:

```json
{ "transactions": [ <§6.3 transaction object>, ... ] }
```

Filing metadata is **not** duplicated into the body file — `filings.json` is the
single source of truth, joined to bodies by `DocID` (the sibling reader, #10,
codes against exactly this shape/path). The file is byte-stable (`indent=2`,
`sort_keys=True`, trailing newline) like every other `parse` artifact, so
re-parse from `raw/` is deterministic ("schema changes mean re-parse, not
migrate").

### Failure handling: `extract_failed`, never a crash or silent gap

An extraction failure on an e-filed PTR sets `parse_status="error"` and adds an
`unparsed-manifest.json` entry with reason `extract_failed` (SPEC §6.5). This
makes the e-filed PTR the **one** e-filed path that can land in the unparsed
manifest — previously e-filed filings were never there. No body file is written
for a failed row, and the year never crashes (CLAUDE.md: never silently drop a
filing).

### Schema version bumped to 0.3.0

Adding the PTR body is a schema change, so `SCHEMA_VERSION` (and the mirrored
`pyproject.toml` version, GH-0030) move to `0.3.0`. Pre-v1 there is no migration:
bump, re-parse from `raw/`.

## Consequences

- e-filed PTRs now carry structured `transactions[]`; FD bodies remain deferred.
- Tickers are high-precision but not exhaustive — non-symbol asset classes carry
  `null` by design, which downstream consumers must treat as "no symbol", not
  "missing data".
- The header-line regex assumes pdfplumber keeps the date-pair/amount/glyph on
  one physical line. This held across both committed fixtures (8 pages, 58 rows);
  a future PTR whose layout splits that signature would simply not match and
  would extract zero rows for that block — a risk to watch as more years are
  pulled, mitigated by the count assertion in the tests.
