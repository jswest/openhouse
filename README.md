# openhouse

<img src="docs/assets/logo.png" alt="openhouse logo: an open-house yard sign reading 'openhouse'" width="200">

Pull, parse, and query **U.S. House of Representatives financial disclosure
filings** — annual Financial Disclosure statements and STOCK Act Periodic
Transaction Reports — from the Office of the Clerk, into normalized JSON you
can actually ask questions of.

> **Status: pre-v1.** The design is fully specified and verified against live
> Clerk data ([SPEC.md](./SPEC.md)); implementation is underway. Nothing below
> works yet — this README previews where it's going.

## What it will do

A three-command pipeline plus an accuracy-review tool, over one data directory:

```sh
openhouse pull 2024          # fetch the year's filing index + every PDF (resumable, polite)
openhouse parse 2024         # offline: PDFs + index → normalized JSON, nothing dropped
openhouse read trades 2024 --ticker NVDA --table
                             # offline: ask the parsed data a question
openhouse inspect 2024 --sample 0.05
                             # offline: review a sample beside the PDFs, score accuracy
```

- **`pull`** is the only network step. Idempotent and Ctrl-C-safe — re-runs
  fetch only what's missing. Multi-year ranges (`openhouse pull 2019-2024`)
  throughout.
- **`parse`** never touches the network and never silently drops a filing:
  machine-readable (e-filed) PDFs become structured records; scanned/paper
  filings are detected and catalogued for a future OCR pass; anything odd lands
  in a manifest with a reason.
- **`read`** answers questions from the parsed JSON — filings by member or
  type, a single filing's full contents, stock transactions flattened across
  years, per-year summaries. Every query tells you which way to trust it:
  whether its results are exhaustive (an upper bound — "at most these") or a
  floor (a lower bound — "at least these"), so a zero-result answer is never
  ambiguous. JSON to stdout for machines and `jq`; `--table` for humans.
- **`inspect`** measures whether the parse is *right*, not just whether it ran.
  It samples a reproducible, stratified slice of the `ok` filings, opens a small
  local web app showing each one beside its source PDF, and records a
  precision/recall verdict per filing — surfacing silent recall failures (e.g.
  scanned PTRs that extract zero trades) and emitting an accuracy scorecard.
  Offline; the browser is the only socket. JSON scorecard to stdout.

## What's in the data

Every filing the Clerk indexes from 2008 onward: Members' and candidates'
annual disclosures (assets, income, liabilities, positions, agreements, gifts,
travel — Schedules A–J), and securities-trade reports (PTRs) from 2012 onward
under the [STOCK Act](https://www.govinfo.gov/app/details/PLAW-112publ105),
including amendments, extensions, and terminations as metadata.

Source: the [Clerk of the House's Financial Disclosure
portal](https://disclosures-clerk.house.gov/FinancialDisclosure) (Legislative
Resource Center), which also offers a human-friendly search if you just want to
look at one filing. The [House Committee on
Ethics](https://ethics.house.gov/financial-disclosure) oversees the disclosure
program and documents the filing requirements.

## What's coming, roughly in order

1. The three commands above, end to end, for any year range since 2008.
2. Stable filer identity via a CC0 `congress-legislators` bioguide join, with a
   `name:`-key fallback and explicit warnings whenever a filer is name-keyed only
   (see [Caveats](#caveats)).
3. A Claude Code agent skill, so an AI agent can drive `pull`/`parse`/`read`
   directly.
4. OCR for the scanned/handwritten backlog (already detected and catalogued by
   `parse`).

## Caveats

**Identity is a two-tier claim, and only one tier is verified.** The Clerk index
carries no member ID — only name strings that vary across years ("Alma Shealey
Adams" vs "Alma S. Adams"). `parse` resolves each filer through a two-rung ladder
and records which rung it used:

- `bioguide:<id>` — the filer's House seat (normalized last name + state +
  district) matched a single record in the public-domain
  [`@unitedstates/congress-legislators`](https://github.com/unitedstates/congress-legislators)
  bulk files (CC0). This is a **stable identity**: the same `filer_id` across
  years and name spellings is the same person. The match is conservative — a
  seat that resolves to two legislators (same last name, same seat across time)
  matches *nothing* rather than guess, so a `bioguide:` key is never a false
  positive.
- `name:<normalized-slug>` — the last resort, used when no House seat matched
  (a candidate who never took a seat, a delegate edge case, a name the reference
  set doesn't carry, an ambiguous seat). **This is a bounded, unverified
  name-string claim, not an identity.** Two different people can share one
  `name:` key; `parse` emits an `identity_warnings` entry (and a stderr line) for
  every `name:`-keyed filer precisely so a `read --member` user knows the match
  is unverified.

The CC0 reference set is fetched once by `pull` into `raw/reference/` and joined
**offline** by `parse` — it is the single declared exception to "`pull` is the
only network step," and being CC0 it carries none of the Clerk data's use
restriction. `pull --no-reference` skips it, in which case every filer falls back
to a `name:` key.

**The staff↔member bridge, if any, is name-keyed and unverified.** Where a filing
is bridged to a member by name alone (no bioguide), treat it as a *tagged,
unverified* claim — a starting point for a human to confirm, never a settled
fact. `openhouse` never synthesizes a bioguide id and never folds a name-only
guess into one.

## Use restriction

Clerk financial-disclosure data may **not** be used for commercial purposes
(news/media dissemination excepted), for solicitation, or to establish credit
ratings — this is statutory ([5 U.S.C. §
13107](https://www.law.cornell.edu/uscode/text/5/13107), Ethics in Government
Act as amended), not a request. `openhouse` is a research and transparency
tool; don't build a commercial product on its output.

## Development

Python 3.12+, managed with [`uv`](https://docs.astral.sh/uv/):

```sh
uv run pytest        # tests
```

## Releasing

Versions are `v0.<SCHEMA_VERSION>.<patch>` — the minor **is** `SCHEMA_VERSION`,
the integer parsed-schema generation in
[`openhouse/schemas.py`](./openhouse/schemas.py). Patch increments at the same
schema and resets to `0` when the schema bumps; the package version is derived
from the git tag by `hatch-vcs`, so cutting a release writes only the tag. The
release script computes the number from `SCHEMA_VERSION` + the last tag — never
pass a version by hand.

Cut a release from a clean, synced `main`:

```sh
# dry run — prints last tag, schema, the computed next version, and notes
uv run python .claude/skills/release/release.py

# publish — annotated tag + push + GitHub Release (after the dry run looks right)
uv run python .claude/skills/release/release.py --tag --push
```

A schema bump means existing `parsed/` data must be re-parsed — re-run
`openhouse parse <years>`; the dry run says so when it applies.

## Refreshing a local install

After pulling `main` or cutting a release:

```sh
openhouse ready                       # re-press the agent skill if SKILL.md changed
uv tool install --force --editable .  # refresh the CLI and its --version string
openhouse parse <years>               # re-parse only if SCHEMA_VERSION moved
```

A bare editable install already runs current code; the reinstall is mainly to
refresh the cached `--version`. The data directory defaults to `~/.openhouse`
(override with `--data-dir` or `OPENHOUSE_DATA_DIR`).

Design contract: [SPEC.md](./SPEC.md). License: [MIT](./LICENSE.txt).
