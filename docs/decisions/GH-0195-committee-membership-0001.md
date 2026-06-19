# GH-0195 — `reference --committees`: committee membership, current-congress-only

**Date:** 2026-06-19
**Issue:** #195 (part of the influence-context omnibus #196; builds on the
`reference` command #184 and the CC0 reference lane #16 / #75)

## Context

Add committee membership — which House committees/subcommittees a member sits on,
addressable by year or congress — to the top-level `reference` command, from the
same CC0 `congress-legislators` project the command already reads. The §13.5a
probe discipline applies: verify the source before asserting, turn what is learned
into a fixture, never a network-touching test.

## Probe findings (one polite by-hand fetch, 2026-06-19)

- **`committees-current.json`** (HTTP 200) — committee/subcommittee *definitions*:
  a list; House committees carry `type:"house"`, `thomas_id`, `name`, and a
  `subcommittees[]` of `{thomas_id, name}`.
- **`committee-membership-current.json`** (HTTP 200) — *membership* keyed by
  committee thomas code → `[{bioguide, party, rank, title}]`. A subcommittee code
  is `parent_thomas_id + sub_thomas_id` (`HSAG` + `03` → `HSAG03`). Senate (`SS…`)
  and joint codes also appear.
- **`committee-membership-historical.json` → HTTP 404.** It does not exist.
- **`committees-historical.json`** (HTTP 200) carries only *definitions* (each
  with a `congresses[]` list), never membership.

**Verified coverage: CURRENT-CONGRESS-ONLY (the 119th, 2025–26).** Membership rows
carry no congress field; there is no historical-by-congress membership source. This
is the honest residual — coverage is NOT fabricated.

## Decisions

- **Scope to what the source actually provides.** Current congress only,
  hard-coded as `CURRENT_MEMBERSHIP_CONGRESS = 119` in `legislators.py` (the
  membership snapshot carries no congress field, so the number must come from
  outside the data — bump it when the upstream snapshot rolls forward). `--congress
  N` / `--year Y` outside the current congress return `[]`, with the residual still
  declaring the limit. This is a scope honesty call, not a defect: the omnibus's
  "by year or congress" hope is met for the only congress the CC0 source carries.

- **The join lives in `legislators.py`, the reference-data owner.** `CommitteeIndex`
  + `load_committee_index` mirror `LegislatorIndex` / `load_legislator_index`
  (build-once, offline, deterministic; degrade-with-a-signal on a corrupt file).
  Keeping it beside the legislator join means producer (`pull`) and consumer share
  one module and can't drift on the on-disk layout.

- **House-only.** Senate/joint committee codes are filtered out at index build
  (only `type:"house"` definitions map a code to a label), so they can never enter
  the surface — this is the House product.

- **`year_to_congress` mirrors FEC `year_to_cycle`'s *pattern*, lives in
  `legislators.py`.** Pure, wall-clock-free, unit-tested: `(year - 1789) // 2 + 1`
  (1st = 1789–90, 119th = 2025–26). Placed beside the committee logic rather than
  in `cli.py` so `reference.py` imports it without the cli circular-import dance.

- **Committee files ride the existing reference fetch — no new network step.**
  `pull_legislators` now iterates `LEGISLATORS_FILES + COMMITTEE_FILES` over the
  same `LEGISLATORS_URL_TEMPLATE`, same polite floor, same atomic write, same
  idempotent skip, same non-fatal `--no-reference` gate. No second fetch function,
  no new flag (smallest fix that fits).

- **Default output stays byte-stable.** `--committees` is opt-in; a bare
  `reference <str>` is unchanged (verified by a dedicated test). The committee path
  emits an 8-key row `{name, bioguide_id, congress, committee, subcommittee, rank,
  title, party}` and a separate stderr residual; the roster path is untouched.

- **COMPLETE guarantee, explicit residual.** Complete over the cached current
  snapshot — every seat of every matched member in range is returned. The stderr
  residual names the current-congress-only limit and members/seats absent from the
  cache, per CLAUDE.md (prefer completeness; state the residual).

## Scope held

- No fuzzy committee-name resolution beyond the command's existing substring match
  on the *member* (issue non-goal).
- No cross-join to holdings/donors in code (downstream analysis the field enables,
  not built here).
- No historical-membership reconstruction — the source doesn't carry it, so it is
  declared as the residual, never synthesized.
