# GH-0016 — Bioguide identity join (two-tier `filer_id` ladder)

**Date:** 2026-06-12
**Issue:** #16 (part of the v0.5.0 omnibus, #47)

## Context

The Clerk index carries **no member ID** — only name strings that vary across
years ("Alma Shealey Adams" vs "Alma S. Adams"). #6 mitigated with a normalized
name slug (`filer_id`), explicitly *not* an identity, and flagged collisions
heuristically (same key, different district or differing last-name/suffix). That
heuristic bounds nothing: it neither pins a filer to a real person nor tells a
`read --member` user whether a hit is real. This sub-issue replaces the guess
with a join to a stable identity where one exists, and an honest "unverified"
signal where one doesn't.

## Decision

### Two-tier identity ladder (`bioguide:` / `name:`)

`filer_id` is now a two-rung key, and the matched id is also surfaced on a new
`bioguide_id` field (schema generation **5**):

1. **`bioguide:<id>`** — the filer's House seat (normalized last name + state +
   district) matched a single legislator in the CC0
   `@unitedstates/congress-legislators` bulk files. A stable identity across
   years and name spellings.
2. **`name:<name_key>`** — the last resort, the old #6 slug, used when no seat
   matched. A **bounded, unverified name-string claim**, never an identity.

The middle `fec:` tier was **considered and rejected** as scope creep — the
ladder is bioguide-or-name, nothing between. Where a bioguide matches we do **not**
mint a synthesized id alongside it.

### The join is conservative — completeness over a false positive

`legislators.py` builds an offline `(norm_last, state, district) → bioguide`
index from the two bulk files. A seat key that two distinct bioguides share (e.g.
two same-last-name reps holding one district across time) is marked **ambiguous**
and matches **nothing** — a `bioguide:` key is therefore never a false positive.
A missing state/district, an unknown seat, or an ambiguous seat all fall back to
`name:`. No bioguide is ever synthesized. Name normalization is NFKD diacritic-
stripping + lowercase but *not* the punctuation-collapsing `index.slug`, so the
reference's "González-Colón" keys the same as the Clerk's "Gonzalez-Colon".

### The one declared network exception

The two bulk files are CC0 (public domain) — no conflict with the Clerk FD use
restriction, which governs *disclosure* data, not this reference set. They are
fetched **once** by `pull` (`pull_legislators`, same polite floor as every other
fetch) into `raw/reference/` and joined **offline** by `parse`. This is the
single declared exception to "`pull` is the only network step." `pull
--no-reference` skips it; with no reference cached the join simply matches
nothing and every filer falls back to `name:` (the enrichment is optional, never
a gate). `parse` stays fully offline and deterministic.

### `_detect_identity_warnings` recomputed against `bioguide_id`

The old "two people share a slug" collision check is **retired**. The actionable
signal is now *unmatched* identity: one `identity_warnings` entry per distinct
`name:`-keyed `filer_id` (those with `bioguide_id is None`), carrying its distinct
raw names, DocIDs, and districts, with `reason: "unmatched_no_bioguide"`. A
bioguide-matched filer is never warned. This tells a `read --member` user exactly
when a match is an unverified name-string claim.

### Schema bump, not a migration

`SCHEMA_VERSION` 4 → 5 (also the minor of `v0.5.<patch>`). Per CLAUDE.md a schema
change means re-parse, not migrate: bump the int, re-run `parse` from `raw/`. No
migration code.

## Tests

Offline, against a hand-authored fixture subset under `tests/fixtures/reference/`
(real-shape legislator records for Allen GA12, Adams NC12, Norton DC, González-
Colón PR, plus two same-seat John Smiths for the ambiguity guard). No test
touches the network; `pull_legislators` is exercised through `httpx.MockTransport`.
`test_legislators.py` covers the join (match, diacritics/case, ambiguity,
unknown seat, empty index); `test_parse.py` covers attach/fallback/warning and
end-to-end; `test_pull.py` covers the fetch + idempotent skip.

## Notes / caveats

A README `## Caveats` section documents that `name:` identity is a bounded,
unverified claim and that any staff↔member bridge by name alone is a *tagged,
unverified* claim — a starting point for a human, never a settled fact. No "v1"
branding.
