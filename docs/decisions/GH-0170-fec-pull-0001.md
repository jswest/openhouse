# GH-0170 — `fec pull`: bulk-data-only acquisition (cn/ccl/cm/pas2)

**Date:** 2026-06-18
**Issue:** #170 (part of the FEC lane omnibus #167)

## Context

The FEC lane's only network step. Path 1 (connected-SSF PAC money, SPEC §13)
needs four FEC bulk files per 2-year cycle: candidate master (`cn`),
candidate-committee linkage (`ccl`), committee master (`cm`), and
committee→candidate contributions (`pas2`, whose line-11C rows are the PAC→member
money). The operator decision (#170) is **bulk-data-only**: the OpenFEC `/v1` API
is robots-disallowed for crawling and rate-limited, so it is never used in
production code or tests.

## Decisions

- **Reuse the Clerk lane's polite primitives verbatim.** `fec_pull.py` imports
  `build_user_agent` / `polite_get` / `PullError` from `pull.py` rather than
  re-deriving them: same required-contact User-Agent flow, same 403-hard-stop and
  429/5xx exponential backoff. No new abstraction — the FEC module is a thin,
  FEC-shaped caller of the existing floor.

- **10 s polite delay between file fetches**, grounded in `fec.gov/robots.txt`
  → `Crawl-delay: 10` for `*` (which does not disallow `/files/`). More
  conservative than the Clerk's 2.5 s, grounded the same way (the host's own
  published policy). Sequential, one file at a time. The first file of a run is
  not delayed; every file after it is (the Clerk lane's "pace before every request
  but the first").

- **Follow redirects, record the final URL.** The by-hand probe confirmed the
  `www.fec.gov/files/bulk-downloads/<cycle>/<file>` URLs **302 to an AWS GovCloud
  S3 host** (`cg-…s3-us-gov-west-1.amazonaws.com`). The client follows redirects;
  the manifest records both the requested `www.fec.gov` URL and the final storage
  URL per file.

- **Inner zip member is the bare stem, not the cycle-suffixed zip name.** Verified
  by probe: `cn24.zip` → `cn.txt`, `ccl24.zip` → `ccl.txt`, `cm24.zip` →
  `cm.txt`, `pas224.zip` → `itpas2.txt` (the contributions file's inner member is
  the irregular `itpas2.txt`). This is a hard fact baked into `FEC_BULK_FILES` —
  an initial assumption that the inner name mirrored the cycle-suffixed zip
  (`cn24.txt`) was **wrong** and corrected against the real download.

- **Manifest `fec-pull-manifest.json` per cycle**, written in a `finally` so an
  interrupted run never loses what it fetched. Per file: requested URL, final URL,
  status, zip byte size, extracted byte size, sha256, and the single
  command-entry `fetched_at` (no wall-clock in logic). Idempotent/resumable: a
  present inner `.txt` whose on-disk size matches the recorded manifest entry is
  skipped with no network request; a size mismatch (partial transfer) is
  re-downloaded; `--force` re-downloads regardless.

- **STOP/PARK on anything surprising (operator instruction).** A non-zip
  response, a missing expected inner member, a 403, or a file larger than a 150 MB
  cap raises `PullError` rather than pushing through. `pas2` is the largest file
  (the real 2024 `pas224.zip` is ~24.7 MB — expected, well under the cap).

## Probe facts (2024 cycle, captured to fixtures)

| file | zip bytes | inner | inner rows |
|------|-----------|-------|-----------|
| `cn24.zip`   | 356,434    | `cn.txt`     | 9,799   |
| `ccl24.zip`  | 94,242     | `ccl.txt`    | 8,620   |
| `cm24.zip`   | 883,466    | `cm.txt`     | 20,938  |
| `pas224.zip` | 24,683,218 | `itpas2.txt` | 703,597 |

Final redirect host: `cg-519a459a-0ea3-42c2-b7bc-fa1143481f74.s3-us-gov-west-1.amazonaws.com`.

Adams (`H4NC12100`) → principal committee `C00546358` (ccl designation `P`),
confirmed. The three connected-SSF contributors to that committee — `C00002469`
(Machinists, org_type `L`), `C00130773` (Multifamily Housing, `T`), `C00792127`
(FICO, `C`) — each gave $5,000 line-11C receipts, all present in the trimmed
`itpas2.txt` / `cm.txt` fixtures so #171's Path-1 filter is exercised across all
three sponsor classes.

## Tests

`tests/test_fec_pull.py` runs fully offline via `httpx.MockTransport` against
in-memory zips built from the trimmed real fixtures under `tests/fixtures/fec/`.
The mock reproduces the live 302→storage-host flow. No test touches the network.
