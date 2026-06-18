# GH-0172 — `fec read`: donors / pac with declared guarantees and residuals

**Date:** 2026-06-18
**Issue:** #172 (part of the FEC lane omnibus #167; depends on #171 parse, #169 identity)

## Context

The FEC lane's query stage — the analogue of the Clerk lane's `read` (§5). A pure
function over `parsed/fec/<cycle>/` (what `fec parse` wrote): two subcommands,
`donors` (PACs → member) and `pac` (member ← org), inverses over the same kept
Path-1 contribution set. Fully offline; no network, no wall clock, no writes.

## Decisions

- **Mirror `read.py`'s idioms, do NOT share code with it.** `fec_read.py` is a
  faithful copy of the clerk `read.py` shape (a `*Error` exception, `_load_*`
  helpers, `_resolve_*` present/skipped split, `_require_present` loud-on-empty,
  `_warn_schema_drift`, `_render_table`/`_emit`, a `build_read_parser` with a
  shared `--data-dir`/`--table` parent using SUPPRESS defaults, and a `run()`
  entry that maps the error to a clean exit). The duplication is deliberate:
  CLAUDE.md says "smallest fix that fits; no new abstractions"; the two lanes are
  schema-independent (`FEC_SCHEMA_VERSION` vs `SCHEMA_VERSION`) and coupling them
  through a shared helper would be a cross-lane hazard with no payoff.

- **`--member` matches the `bioguide_id`, the only identity the link carries.**
  The member-links carry `bioguide_id` + `candidate_id` + `committee_id` — no
  name. So `donors <member>` matches a case-insensitive substring over the
  member-link `bioguide_id` (§6.2 semantics: a full bioguide pins exactly the
  member; a fragment is a fuzzy name-string-style match over what is present). No
  name→bioguide resolution is attempted against the FEC tree — the link has no
  name to match and we never synthesize one (CLAUDE.md).

- **`donors` rolls up to organization via `org_rollup_key` (reused from
  fec_parse).** Key = `connected_organization_name` else committee `name`, exactly
  as #171 wrote it — imported from `fec_parse` so the read key can never drift
  from the parse key. Each entry carries the contributing committee's normalized
  `organization_type`, so `--org-type` slices the tagged set. Sorted by total
  desc, org-name asc tie-break (deterministic).

- **`--org-type` is validated against the §13.3 label table.** An unknown class
  fails loudly (exit 2) rather than silently matching nothing — a typo is a user
  error worth surfacing, not a "complete over zero" trap.

- **`pac <org>` is a fuzzy substring over the rollup key, reverse-mapping
  recipient committee → bioguide.** Selecting contributing committees whose
  `org_rollup_key` *contains* `<org>` is completeness-leaning (a fragment like
  `NATIONAL` legitimately also catches `INTER NATIONAL …` — documented in
  `--help`). A receipt to a committee with no member link is reported as an
  `unattributed` count on stderr, never dropped.

- **Every response declares its guarantee + residual, tied to the manifest
  counts.** Complete over the `contributions_kept` Path-1 receipts; the residual
  names `contributions_filtered` split into `not_connected_ssf` +
  `unresolved_committee`, the affiliation-not-collapsed caveat (§13.8), and the
  §13.7 framing ("disclosed candidate-side slice, not total influence; no dark
  money / super-PAC IE / soft money"). The numbers are read straight from each
  cycle's `fec-parse-manifest.json` — never recomputed in `read` (single source of
  truth; `read` can't disagree with the last `parse`).

- **Graceful degradation + loud-on-nothing, like clerk `read`.** An un-parsed
  cycle in a range is a clean skip reported on stderr (answer from the rest); a
  data dir with **no** parsed cycle for the range raises `FecReadError` (exit 1)
  rather than report a misleading empty roll-up that reads like a trustworthy zero.

- **`read` owns its own sub-parser; CLI hands it the remainder.** `main()`
  intercepts `fec read` and calls `fec_read.run(raw_argv[2:], …)` verbatim (the
  donors/pac subcommands + flags before/after), exactly as `clerk read` does — a
  top-level REMAINDER arg could not express the before/after `--data-dir`/`--table`
  grammar. The `_FEC_STUB_VERBS` tuple (which listed `donors`/`pac` as top-level
  stubs — they were always `read` subcommands) is renamed `_FEC_VERBS =
  ("pull", "parse", "read")` and the lane's "scaffolded / not yet implemented"
  help text is corrected to reflect that Path-1 pull/parse/read are now real.

## Scope held

- No new flags beyond `--org-type` (the issue's one optional filter), no date
  windowing on `read` (the cycle is the window; `fec parse` already scopes by
  cycle), no industry rollup (§13.7 non-goal), no affiliation collapse (§13.8
  declared limitation, surfaced in the residual not hidden).
