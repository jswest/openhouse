# GH-0169 — FEC identity bridge: member → FEC candidate_id → committee_id (offline, CC0)

**Date:** 2026-06-18
**Issue:** #169 (part of the FEC omnibus #167; builds on the #168 scaffold and the
#16/#122 bioguide identity ladder)

## Context

#168 defined the `FecMemberCandidateLink` record shape (`bioguide_id`,
`candidate_id`, `committee_id`) but populated nothing. The linchpin of the FEC
lane is anchoring a House member (the Clerk lane's identity) to their FEC
candidate id(s) and principal-committee id, so PAC receipts can be attributed to a
member. SPEC §13.2 states the join "already exists": the CC0
`@unitedstates/congress-legislators` records carry an `id.fec` **array** (members
hold multiple FEC candidate ids across cycles), and those bulk files are *already
fetched* by `pull` and *already loaded* by `parse` for the bioguide seat join
(§6.2). This sub-issue builds the **offline half** of the bridge and leaves the
one network step (candidate → committee) as a documented seam for #170.

## Decision

### Reuse the existing identity ladder — no new data source, no network

`id.fec` is folded into `LegislatorIndex` as `by_fec` (`bioguide → ordered,
deduped tuple of FEC candidate ids`) in the same `_index_records` pass that builds
the seat indexes — it rides records already on disk under `raw/reference/`. A new
`LegislatorIndex.fec_candidate_ids(bioguide)` accessor mirrors `match` /
`classify_seat`: it returns `()` for a bioguide with no FEC id (the *unresolved*
case), never a synthesized id. This is a deterministic offline extension of the
§6.2 ladder, not a name match. **No new network dependency** — the issue's
guardrail (candidate→committee resolution is deferred to #170) is honored: this
pass touches no OpenFEC endpoint and runs entirely on the cached CC0 files.

### The link join + residual — mirroring §6.2 `identity_warnings`

`build_fec_member_links(bioguides, legislators)` takes the distinct bioguides
`parse` already pinned and returns `(links, warnings)`:

- One `FecMemberCandidateLink` per `(bioguide, candidate_id)` pair — a member with
  several FEC ids across cycles yields several links (the array is not collapsed).
- A member with **no** FEC id is never given a guessed one: it lands in the
  residual `warnings`, one `{"bioguide_id", "reason"}` entry classified
  `no_fec_id` — the §6.2 `identity_warnings` shape and conservatism (sound over
  complete; a missed link is recoverable from the residual, a fabricated candidate
  id is not — CLAUDE.md).

Output is deterministic: first-appearance bioguide order, then `id.fec` order;
repeated bioguides (a member filed many times) collapse to one.

### The committee_id seam left for #170

The `candidate_id → principal campaign committee_id` step is the OpenFEC network
call (`/candidate/{id}/committees/`) and belongs to #170. The #168 model types
`committee_id` as a required `str`, so this pass populates it with the sentinel
`UNRESOLVED_COMMITTEE = ""` (an empty string — never a fabricated `C########`),
the documented seam #170's network resolution fills. The #168 schema is **not
extended** here (no scope creep — the prompt's park-if-ambiguous rule).

The residual reason vocabulary is declared stable across both waves as
`FEC_LINK_REASONS = ("no_fec_id", "ambiguous_committee", "unmatched")`:
`no_fec_id` is the only reason this offline pass emits; `ambiguous_committee` (>1
principal committee, no single pick) and `unmatched` (OpenFEC has no committee for
the candidate id) are the seams #170's network step will exercise.

## Alternatives considered

- **Extend the schema with an `Optional[str]` committee / a `resolved` flag.**
  Rejected as out-of-scope schema churn — the empty-string sentinel signals
  unresolved without reshaping the #168 contract; #170 owns any such change.
- **A separate FEC module for the join.** Rejected — the join is a pure offline
  CC0 extension of the *same* ladder, so it lives next to it in `legislators.py`
  (smallest fix that fits; no new layer).

## Consequences

- The FEC member→candidate link is populated offline today; the committee column
  is an explicit, typed seam (`""`) #170 fills over the network.
- No schema version moved (`FEC_SCHEMA_VERSION` stays `1`; the record shape is
  unchanged) and `schemas.fingerprint` is untouched.
- Test fixtures under `tests/fixtures/reference/` gained `id.fec` arrays covering a
  multi-id member, a single-id member, an in-array duplicate (dedup), and a no-id
  member (→ `no_fec_id` residual). No network in any test.
