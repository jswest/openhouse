# GH-0006 — Metadata mapping, filer_id, and the `parse` skeleton

**Date:** 2026-06-11
**Issue:** #6

## Context

M4 (SPEC §12) is `parse`: the offline, deterministic transform of `raw/<year>/`
into normalized JSON. This sub-issue (#6) builds the **metadata half** — every
`<Member>` in `<year>FD.xml` becomes a schema-validated `FilingMetadata` record
(SPEC §6.1) with a computed `filer_id` (SPEC §6.2), zero rows dropped, plus
identity-collision warnings. The per-PDF classification + body-extraction pass
(efiled/scanned/missing, schedules, transactions) is deliberately out of scope:
that is sub-issue #7 (v0.3.0). No PDF is opened here; `pdfplumber` is not imported.

`index.py` already had `enumerate_targets` for `pull` (it skips no-DocID rows
because `pull` only fetches bodies). That stays untouched; the new metadata path
is additive and never skips a `<Member>`.

## Decision

### `filer_id` rule (SPEC §6.2), in `index.py`

`compute_filer_id` implements
`lower(state) "." slug(Last) "." slug(first_token(First)) ["." slug(Suffix)]`:

- `slug()` lowercases, NFKD-decomposes and drops combining marks (diacritics),
  replaces punctuation with whitespace, and collapses whitespace runs to a single
  `-`. Empty input → `""`. So `Gonzalez-Colon` → `gonzalez-colon`, `Núñez` →
  `nunez`, `Maryam.` → `maryam`.
- Only the **first whitespace token** of `First` participates — `Alma S.` and
  `Alma Shealey` both yield `…alma`, which is the whole point (middle names /
  initials are the main cross-year variation).
- The suffix segment is appended **only when `slug(Suffix)` is non-empty**. A
  punctuation-only suffix (e.g. `.`) slugs away and is not appended — which is
  exactly what produces the suffix-slug-collision the warning catches.
- Empty/missing `StateDst` → state segment `unk`.

This is a normalized *key*, not a true member ID. Bioguide-join identity is a
post-v1 enrichment (SPEC §10).

### Collision heuristic, in `parse.py`

Records are grouped by `filer_id`. The same person filing many times per year
(annual + PTRs + extension, same name, same district) is **normal and not
warned**. A group is flagged only when it shows a signal that one key covers two
*different* people:

- **different districts** within the year (a person never spans two districts), or
- a differing raw **last name** or **suffix** among records sharing the key (the
  slug collided rather than matched).

District-`None` (empty `StateDst`) is its own bucket, so an empty-state row and a
real-state row at the same key count as two districts. Each warning carries the
colliding `filer_id`, the distinct raw names (first-appearance order, so the
output is deterministic), the involved `doc_ids`, and the distinct districts. It
goes to **stderr** and into `parse-manifest.json` under `identity_warnings`, so
`read --member` users learn when a name is ambiguous.

### `build_filing_records` never drops a row

Unlike `enumerate_targets`, this maps **every** `<Member>` — a row with no DocID
still yields a record (`doc_id=""`, `source_pdf=None`): it has metadata even if it
has no body to fetch. `source_pdf` is the relative path the body would live at,
routed by FilingType (`P` → `ptr`, else `fd`, the §2.2 rule). `pdf_class` is left
`None` (that is #7's classification); `parse_status` is `"ok"`. `StateDst` parses
to `{raw, state=first-2-chars, district=int(rest) or 0}` or `None` when empty;
`FilingDate` (`M/D/YYYY`) parses to a `date` or `None`. XML order is preserved.

### `parse` skeleton + flags-now-for-#7

`parse.parse(years, *, data_dir, types, strict, fetched_at)` is the public entry.
The signature and the `cli.py` wiring are **final** so #7 extends `parse.py`
without touching `cli.py`: the `parse` subparser already declares `--data-dir`,
`--types`, and `--strict`, and `main()` already parses types and dispatches into
`parse_mod.parse(...)`, catching `ParseError` → `error:` + exit 1 (mirroring
`pull`). In #6, `types`/`strict` are accepted but inert (no PDFs touched).

A missing `raw/<year>/<year>FD.xml` is a **clean per-year skip** (stderr message,
no crash), so a multi-year range survives a not-yet-pulled year. Per year we write
`parsed/<year>/filings.json` (a JSON array, `model_dump(mode="json")` so dates
serialize ISO) and `parsed/<year>/parse-manifest.json`. Both are
`json.dumps(..., indent=2, sort_keys=True)` + trailing newline → **byte-identical
on re-run from the same raw**. A compact combined JSON summary is printed to
**stdout** (CLAUDE.md "JSON to stdout"); progress/warnings to stderr.

### Manifest shape (room for #7)

```json
{
  "schema_version": "0.2.0",
  "generated_at": "<fetched_at, threaded — not a fresh clock read>",
  "year": 2024,
  "counts": { "total": N, "by_filing_type": { "<code>": n } },
  "identity_warnings": [ … ]
}
```

`SCHEMA_VERSION` is a new module constant in `schemas.py` (a schema change means
re-parse, not migrate — CLAUDE.md). The `counts` object is deliberately a dict so
#7 ADDS `by_pdf_class` (efiled/scanned/missing) and a parse-status tally beside
`by_filing_type` without reshaping anything written here; `generated_at` is the
single entry-time `fetched_at` threaded from the CLI (SPEC §9: no wall-clock in
core logic), keeping `parse` deterministic.

### `filer_id` added to `FilingMetadata`

The field was intentionally absent at scaffold time (GH-0002) because its
derivation lived in a later milestone. It is now a required `str` on the record
(SPEC §6.1 shows it). `test_schemas.py`'s base kwargs gained a `filer_id`.

## Scope

Metadata + `filer_id` + identity warnings + the `parse` skeleton and its final
CLI wiring. **No** PDF handling, classification, or body extraction (that is #7).
No `read` work. Existing tests untouched and green (95 → 115 with the new
`test_metadata.py` / `test_parse.py`).
