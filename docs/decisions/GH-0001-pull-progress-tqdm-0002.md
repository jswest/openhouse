# GH-0001 — `pull` progress bar uses `tqdm` (measured ETA)

**Date:** 2026-06-11
**Issue:** #1 (v0.1.0 omnibus — post-assembly hotfix)

Supersedes the "Related: per-family progress bar" note in
[GH-0001-pull-required-contact-0001.md](GH-0001-pull-required-contact-0001.md),
which shipped a hand-rolled stderr bar.

## Context

The first hotfix added a hand-rolled per-data-type progress bar (no dependency,
wall-clock-free). It showed position (`123/451`) but **no time estimate**. For a
real `pull 2024` (~2,250 PDFs at the 2.5 s polite floor ≈ 95 min, and the full
2008→present corpus ≈ 26 h) the ETA is the number that actually matters, and a
fixed-pace arithmetic estimate can't account for real-world variance (slow wifi,
per-request latency).

## Decision

Adopt **`tqdm`** (added to `dependencies`) for the PDF-download bar, one per data
type (`ptr`, then `fd`). It gives a **measured** ETA + rate + elapsed out of the
box, adapting to actual throughput — including a resume, where instant skips race
the bar ahead and the rate self-corrects when it reaches the not-yet-downloaded
filings.

Trade-offs accepted:

- **+1 runtime dependency** on a deliberately lean list (now `pydantic`, `httpx`,
  `tqdm`). Judged worth it: this is a long-running CLI and the measured ETA beats
  fixed-pace arithmetic once real network time is in play.
- **A wall-clock read inside the download loop** (tqdm times each iteration). This
  is display-only — never threaded into any artifact — so it does not violate the
  SPEC §9 determinism rule, which governs `parse`/`read` core logic, not a UI
  element in the network step.

`disable=None` keeps the old behaviour of auto-suppressing the bar when stderr is
not a TTY (piped/redirected), so logs and test captures stay clean; the
end-of-year summary line carries the totals there.

## Scope

UI only. No change to routing, pacing (the 2.5 s floor is unchanged), resumability,
the manifest, or the required-contact rule. SPEC §3's progress bullet updated.
