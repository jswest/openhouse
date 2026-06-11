# GH-0003 — `pull --index-only`: polite client + index acquisition

**Date:** 2026-06-11
**Issue:** #3

## Context

`pull` was a stub. This issue gives it its first real behavior: for each year in
range, download the Clerk's `<YEAR>FD.zip` and extract `<YEAR>FD.xml` +
`<YEAR>FD.txt` into `raw/<year>/` (SPEC §2.1, §6.4) through the polite client
settled in the #1 thread (SPEC §3). PDF-body downloading is deferred to #4;
cross-year FilingType enumeration is explicitly out of scope (the verified 2024
12-code table from #2 already lives in `schemas.py`).

## Decisions

### Polite-crawling defaults (the floor, not a knob)

`pull.py` is built around SPEC §3's politeness floor: sequential
(`--concurrency 1`), 2.5 s between requests (`--delay 2.5`), a descriptive
User-Agent logged to stderr at startup, exponential backoff on 429/5xx. The
grounding is in SPEC §3 — the House publishes no crawl policy (no robots.txt on
either Clerk domain), there is no bulk PDF download, and the nearest published
legislative-branch standard is congress.gov's `Crawl-delay: 2`; we default just
above it. `--delay` / `--concurrency` exist as deliberate, documented overrides;
when either is set away from the default, `pull` logs that politeness was
overridden. The defaults are never weakened to make anything faster — tests mock
the network instead (see below).

### 403 hard error vs 429/5xx backoff (the split)

`polite_get` treats the two server signals as opposites:

- **403** → raise `PullError` immediately, on the first response, with no retry
  and no backoff. A 403 means the server is *refusing* us; retrying is the
  opposite of polite. The message names the likely causes (UA, pacing) and the
  caller exits non-zero. Tests assert exactly one request and zero sleeps.
- **429 / 5xx** → exponential backoff (`backoff_base * 2**attempt`) up to
  `MAX_RETRIES`, then `PullError`. These mean "retry later", so we do.
- other 4xx (e.g. 404) → `PullError`, not retried (a retry won't help).

### Injectable sleep + mock-transport offline testing (testability)

Two seams keep the whole module offline- and fast-testable without weakening any
default:

- **The network** is reached only through an injected `httpx.Client`. The
  orchestrator `pull()` creates a real client (with the UA header) only when none
  is passed; tests pass an `httpx.Client(transport=httpx.MockTransport(handler))`
  so **no test ever touches the live Clerk**. This honors the run-wide
  offline-is-non-negotiable rule directly.
- **The politeness sleep** is an injected callable (`sleep=time.sleep` by
  default). Tests pass `list.append` to *record* backoff/pacing waits, or a no-op
  to skip them — so the suite asserts the politeness behavior (e.g. "two
  retriable failures → `[1.0, 2.0]` backoffs", "one 2.5 s pace between two years,
  none before the first") without ever waiting 2.5 s.

No wall-clock lives in core logic; the only time read is the injectable sleep for
pacing/backoff. (The `fetched_at` manifest timestamp threaded from command entry
is a #4 concern — `pull-manifest.json` is not written in this issue.)

### Fabricated trimmed-index fixture

`tests/fixtures/2024FD-trimmed.{xml,txt}` carry the SPEC §2.1 edge cases — a
type-`W` row with empty `StateDst` and empty `FilingDate`, `DC00` and `PR00`
rows, a 4-digit `DocID` (`7940`), plus a normal e-filed PTR (`P`) and annual
(`O`). `tests/test_pull.py::make_index_zip` builds an in-memory ZIP (stdlib
`zipfile`) from those bytes under the canonical `2024FD.xml` / `2024FD.txt`
member names, so the mocked httpx response returns real ZIP bytes the extractor
unpacks. Nothing is downloaded.

### `cli.py` shape for the #4 PDF-half stub

`cli.py` dispatches `pull` to `pull_mod.pull(...)` after the shared year-range
parse (still exit 2 on a bad range, via the #2 parser). `--contact` falls back to
`OPENHOUSE_CONTACT`. The orchestrator `pull()` loops years, paces, and calls
`pull_index_year` for each; the PDF path slots in inside that same loop, gated on
`not index_only`, right after the per-year index pull — there is a marked seam
comment there. For now, `pull` **without** `--index-only` fetches the index
(so #4's PDF enumeration always has the XML on disk) and prints a clear "PDF body
download is not yet implemented (issue #4)" notice, then exits 0. This was the
smaller change than requiring `--index-only`, and it leaves #4 a clean place to
add the download loop, the `--types ptr,fd` flag, and `pull-manifest.json`
without reshaping the orchestrator.

## Scope

Index acquisition only. No PDF download, no `--types`, no `pull-manifest.json`
(all #4). Cross-year FilingType enumeration remains out of scope; the verified
2024 table in `schemas.py` is reused unchanged, and SPEC §2.3 / §10 already note
the full enumeration is pending — left as-is.
