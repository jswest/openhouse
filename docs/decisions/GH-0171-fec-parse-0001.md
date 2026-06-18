# GH-0171 — `fec parse`: bulk → normalized Path-1 records + residual manifest

**Date:** 2026-06-18
**Issue:** #171 (part of the FEC lane omnibus #167; depends on #168 schemas, #170 pull)

## Context

The FEC lane's offline normalization stage — the analogue of the Clerk lane's
`parse`. It reads the four pipe-delimited bulk files `fec pull` (#170) extracted
to `raw/fec/<cycle>/` (`cn.txt`, `ccl.txt`, `cm.txt`, `itpas2.txt`) and writes
normalized JSON to `parsed/fec/<cycle>/`. No network — the only FEC network step
is `pull`.

## Decisions

- **Column positions come from SPEC §13.5a (the #170 by-hand probe), not a live
  fetch.** The data-dictionary column orders were already verified into the SPEC;
  this issue needed no network at all. Positions confirmed against the trimmed
  fixtures: `cm` ORG_TP col 12 / CONNECTED_ORG_NM col 13 / CMTE_TP col 9; `ccl`
  CMTE_DSGN col 5; `itpas2` contributor col 0 / IMAGE_NUM col 4 / TRANSACTION_DT
  col 13 / TRANSACTION_AMT col 14 / OTHER_ID (recipient) col 15 / TRAN_ID col 17.

- **latin-1 decode.** Bulk files are latin-1 (§13.5a), not UTF-8 — decoding as
  UTF-8 would mojibake or raise on an accented committee name.

- **Path-1 filter keys on the contributing committee's `organization_type ∈
  {C,T,L,M,V,W}`, joined via `cm`.** Two residual reasons, never a silent drop:
  `unresolved_committee` (contributor absent from `cm` — can't classify) and
  `not_connected_ssf` (in `cm` but not a connected SSF). Labor (`L`) is kept and
  tagged so `read` can slice corporate vs labor.

- **Org rollup key = `connected_organization_name`, fallback to committee
  `name`.** `connected_organization_name` IS populated in bulk `cm` (#170
  corrected the API-side "it's null" artifact).

- **`itpas2` is canonical — dedup by `transaction_id`, no double-entry
  cross-check.** The API-era recipient-side cross-check does not apply to bulk
  (there is no separate recipient file). A literal repeated `TRAN_ID` is the only
  double-count risk; first occurrence wins. A blank `TRAN_ID` is never deduped.

- **$10k/PAC/cycle invariant is a sanity FLAG, not a drop.** A contributor→
  recipient cycle total over $10k is reported in the manifest (with the org
  rollup key) but every contribution stays kept — the breach may legitimately
  reflect an un-collapsed affiliated pair (below).

- **Affiliation is a DECLARED LIMITATION, not faked.** Bulk `cm` has no
  affiliated-committee column (§13.5a), so `FecCommittee.affiliation` stays `None`
  and the affiliated-PAC collapse is impossible from bulk. The limitation is
  stated verbatim in both the parse manifest and the unparsed manifest, and in
  SPEC §13.8: a member's totals may count a parent + subsidiary PAC as two orgs.

- **`ccl` fills the #169 committee seam offline.** `build_fec_member_links`
  (#169) emits links with the `UNRESOLVED_COMMITTEE` sentinel; `resolve_links`
  fills it from the candidate→principal-committee map (`ccl` designation `P`). A
  candidate with no `P` linkage is left unresolved in the residual
  (`no_principal_committee`) rather than given a guessed committee — sound over
  complete. Unresolved links are NOT written to `member-links.json` (no sentinel
  leaks downstream).

- **Emit only the contributing connected-SSF committees, not the full `cm`
  master.** The master is tens of thousands of rows; only the committees behind a
  kept contribution are written, keeping the relevant slice visible.

## Outputs

`parsed/fec/<cycle>/`: `committees.json`, `contributions.json`,
`member-links.json`, `fec-parse-manifest.json`, `fec-unparsed-manifest.json`.
Byte-stable (`indent=2`, `sort_keys`, trailing newline) so a re-parse from the
same `raw/` is deterministic.

## Tests

`tests/test_fec_parse.py` (offline): the Path-1 filter (labor kept + tagged), the
`not_connected_ssf` and `unresolved_committee` residuals, the $10k flag (kept, not
dropped), `transaction_id` dedup, the `ccl` committee-seam resolution (Adams
A000370 → H4NC12100 → C00546358, seeding the CC0 reference fixtures), an
end-to-end pass over the real fixtures, deterministic re-run, a clean skip on
missing files, and the non-zero "nothing parsed" exit. `test_cli.py` updated: the
old "not yet implemented" assertion for `fec parse` is replaced by a real-dispatch
test.
