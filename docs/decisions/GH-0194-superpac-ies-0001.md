# GH-0194 — super-PAC independent expenditures as a separately-footed slice

**Date:** 2026-06-19
**Issue:** #194 (part of the influence-context omnibus #196)

## Context

SPEC §13.7 and the #167 omnibus listed super-PAC independent expenditures (IEs)
as a **non-goal** for Path 1, on the grounds that IE treasury money legally can't
reach the member. That reasoning stands — and is exactly why this issue adds IEs
as a **distinct, separately-footed slice** (pull + parse only, no `read` surface),
never folded into the Path-1 connected-SSF hard money. An IE is *uncoordinated*
outside spending FOR or AGAINST a House candidate; the money does not go to the
member and does not carry Path 1's "disclosed, candidate-side hard money"
guarantee, so summing the two would be a category error.

## Probe facts (2024 cycle, captured to a trimmed fixture)

One polite by-hand probe (the §13.5a discipline) of the FEC IE bulk file:

- **URL:** `https://www.fec.gov/files/bulk-downloads/2024/independent_expenditure_2024.csv`
  — 302s to the same GovCloud S3 host as the four Path-1 zips
  (`cg-519a459a-…s3-us-gov-west-1.amazonaws.com`).
- **A plain headered CSV, NOT a zip.** Comma-delimited, **UTF-8**, header row —
  wholly unlike the four pipe-delimited latin-1 files. ~19.5 MB, ~73,449 data rows
  (30,900 House: ~23,175 support / ~7,631 oppose / ~94 blank; ~5,050 House rows
  carry a blank `cand_id`). Well under the 150 MB park cap.
- **23 columns**; the parse reads by header name: `cand_id`, `spe_id` (spender),
  `spe_nam`, `can_office` (`H` kept), `sup_opp` (`S`/`O`/blank), `exp_amo`,
  `exp_date` (`DD-MON-YY` — *unlike* itpas2's `MMDDYYYY`), `pur`, `tran_id`,
  `image_num`. `spe_id`/`spe_nam` carry surrounding whitespace (stripped).
- The IE file carries **no connected-organization column** — that is joined from
  `cm` by `spe_id` (raw; no industry classification).

## Decisions

- **Its own fetch path (CSV, not zip).** `fec pull` acquires the IE file alongside
  the four zips but down a separate function (`pull_fec_ie_file`) — same polite /
  resumable / atomic-write / manifest contract, but it writes the response body
  straight to `raw/fec/<cycle>/independent_expenditure_<cycle>.csv` (no inner
  member to extract). One extra manifest entry; `count` per cycle is now 5.

- **Its own output file + provenance, never summed.** Parsed IEs go to
  `independent-expenditures.json` with `provenance = "fec_ie"` (vs Path 1's
  `"fec"`). The distinct file and tag are the structural guarantee the two
  footings never blur downstream.

- **Both directions, House-only, completeness-first.** Keep every `office == H`
  IE regardless of the spending committee's type; tag each with `support_oppose` +
  `_raw`. A non-House row is a `not_house_candidate` residual.

- **Unattributed IEs are kept, not dropped.** A House IE with a blank `cand_id`
  has no member to attribute to; per the issue it is **kept anyway**
  (`candidate_id`/`bioguide_id` left `None`) AND recorded in the residual as
  `unresolved_candidate`. Consequence: the IE reconciliation is "kept + filtered =
  raw rows" *with the caveat that an unattributed House IE is counted in both* —
  stated explicitly in the manifest, because a clean partition would mean dropping
  it (CLAUDE.md: never silently drop).

- **bioguide join reuses the same bridge, inverted.** The candidate→bioguide map
  is the inverse of the same CC0 `id.fec[]` ladder the Path-1 links ride — no new
  data source, no name match.

- **Schema bump + fingerprint.** New model `FecIndependentExpenditure` under
  `FEC_SCHEMA_VERSION` `1 → 2`; `schemas.fingerprint` refreshed in the same change
  (the GH-0043 guard auto-discovers all models). The fingerprint was recomputed by
  the release module's pure `live_fingerprint()` and written directly — the
  release **flow** (tag/publish) was not run.

## Tests

`tests/test_fec_parse.py` (IE direction/filter/unattributed/connected-org/bioguide
joins, absent-file skip, date+amount parsing), `tests/test_fec_pull.py` (the mock
transport now serves the IE CSV as a plain body; counts bumped to 5),
`tests/test_fec_acceptance.py`, and `tests/test_schemas.py` (the new model + the
`2` version + distinct provenance). All offline, on the trimmed fixture under
`tests/fixtures/fec/independent_expenditure_2024.csv`. No test touches the network.
