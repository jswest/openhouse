# GH-0173 — FEC lane acceptance test: the offline `pull` → `parse` → `read` spine

**Date:** 2026-06-18
**Issue:** #173 (closes the FEC lane omnibus #167; depends on the whole lane #168–#172)

## Context

Each FEC unit suite exercises one stage in isolation (`test_fec_pull` the bulk
download, `test_fec_parse` the Path-1 classifier, `test_fec_read` the query over a
parsed cycle). #173 is the lane's analogue of the Clerk `test_acceptance.py`: one
test module that drives the *real* code of all three stages over one cycle and
asserts a concrete `donors`/`pac` answer comes out with its declared
guarantee/residual. The per-stage architecture (API-vs-bulk, year→cycle,
Path-1 filter, the read guarantee) is already recorded in GH-0170/0171/0172 — this
doc records only the acceptance *approach*.

## Decisions

- **Exercise `fec pull` through the unit suite's mock-transport seam — do not
  stage raw fixtures past it.** `fec pull` is the lane's only network step. The
  issue offered two seams (a `httpx.MockTransport` serving the fixture zips, or
  staging the fixtures into `raw/fec/<cycle>/` and running `parse`→`read`). The
  acceptance test takes the *more faithful* one: it reuses `test_fec_pull`'s
  `make_handler`/`make_client`/`no_sleep` (importing them directly rather than
  re-deriving the in-memory zip plumbing — single source of truth for the offline
  seam, and the live host's 302-to-storage + bare-stem inner-member quirks come
  along for free) and runs the **real** `fec_pull(...)` entry point. So all three
  stages run their production code paths; nothing is hand-staged except what the
  FEC pull legitimately does not fetch (below). **Fully offline — no network.**

- **Stage only the CC0 reference set, because the FEC pull legitimately does not
  fetch it.** `fec parse`'s member→candidate→committee anchor (§13.2) reads
  `congress-legislators` from `raw/reference/`, which is fetched by the *Clerk*
  `pull` (the shared CC0 set), not the FEC pull. The acceptance flow stages it
  from the checked-in reference fixtures before `parse`, mirroring an operator who
  has run `clerk pull` once. This is staging a real prerequisite, not bypassing
  the network seam.

- **Drain the `parse` summary from stdout before reading `read`'s stdout.**
  `fec_parse` (the entry point, unlike the lower-level `parse_cycle` the unit tests
  call) emits its own JSON summary to stdout per the "JSON to stdout" contract.
  Running the real entry point and then `fec read` in the same test would
  concatenate two JSON objects on stdout; the helper calls `capsys.readouterr()`
  to drain the parse summary between stages so each assertion parses exactly one
  object. This keeps the test on the real entry points rather than dropping to the
  quieter internal functions.

- **Assert a real answer, not just a clean exit.** The donors test asserts the
  rolled-up Machinists/Multifamily/FICO totals, the org-type tags (incl. labor
  kept-and-tagged), the desc sort, and the guarantee + residual line on stderr
  tied to the parse-manifest counts (7 kept / 3 filtered). The `pac` inverse test
  asserts the exact bioguide attribution. The parse-stage test asserts the residual
  reconciles (3 `unresolved_committee`, nothing dropped) — the CLAUDE.md "never
  silently drop" invariant, verified end to end.

- **Fixtures are already durable; no new captures.** The trimmed real bulk rows
  (`tests/fixtures/fec/` — cn/ccl/cm/itpas2, incl. labor/trade/corporate
  committees and Adams) and the CC0 reference fixtures were landed by the earlier
  waves and are reused verbatim. #173 adds no fixtures.

## Scope held

- No live-site probe (that is a rare, manual development step, never in the
  suite). No new production code — acceptance is test-only plus docs. No new
  fixtures. The skill/README/decision-doc edits are the doc half of the issue;
  `openhouse ready` (the post-merge skill-install step) is deliberately *not* run
  here — only the package-data skill files are edited.
