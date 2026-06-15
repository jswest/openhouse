# openhouse

<img src="docs/assets/logo.png" alt="openhouse logo: an open-house yard sign reading 'openhouse'" width="200">

Pull, parse, and query **U.S. House of Representatives financial disclosure
filings** — annual Financial Disclosure statements and STOCK Act Periodic
Transaction Reports — from the Office of the Clerk, into normalized JSON you
can actually ask questions of.

> **Status: pre-v1.** The three verbs (`pull` / `parse` / `read`) work end to
> end; the parsed schema is not yet frozen, so a version bump can mean a
> re-parse rather than a migration. Design contract: [SPEC.md](./SPEC.md).

## What it does

A three-command pipeline plus an accuracy-review tool, over one data directory:

```sh
openhouse pull 2024 --contact "Jane Doe <jane@example.com>"
                             # network: fetch the year's index + every PDF (resumable, polite)
openhouse parse 2024         # offline: PDFs + index → normalized JSON, nothing dropped
openhouse read trades 2024 --ticker NVDA --table
                             # offline: ask the parsed data a question
openhouse inspect 2024 --sample 0.05
                             # offline: review a sample beside the PDFs, score accuracy
```

`pull` **requires** a `--contact "Name <email>"` (or the `OPENHOUSE_CONTACT`
env var): it goes into the User-Agent so the Clerk can identify the operator.
The Clerk 403s anonymous shared clients, so without it `pull` errors out before
making a request. Full flag reference is in [Usage](#usage).

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
  ambiguous. It also fails loudly when the data dir is empty or unparsed —
  "nothing here to query" is an error, never silently returned as "no matches."
  JSON to stdout for machines and `jq`; `--table` for humans.
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

## Usage

Three verbs over one data directory. `pull` is the only one that touches the
network; `parse` and `read` are offline and deterministic.

**Data directory** (precedence, highest first): the `--data-dir` flag, then the
`$OPENHOUSE_DATA_DIR` env var, then the `~/.openhouse` default. All three verbs
honour the same precedence; `raw/` lives under it after `pull`, `parsed/` after
`parse`.

**Environment variables:**

- `OPENHOUSE_CONTACT` — your `Name <email>` for `pull`'s User-Agent; saves
  repeating `--contact`. Required for `pull` one way or the other.
- `OPENHOUSE_DATA_DIR` — the data directory, overridden only by `--data-dir`.

### pull (network)

Fetches the annual index ZIP and per-filing PDFs into `<data>/raw/`. Polite by
default (sequential, 2.5 s between requests, identifiable User-Agent); re-runs
skip what's already on disk. Years are `YYYY` or `YYYY-YYYY`.

```sh
openhouse pull 2024 --contact "Jane Doe <jane@example.com>"
openhouse pull 2020-2024 --types ptr        # only PTRs, five years
openhouse pull 2024 --index-only            # index metadata, no PDF bodies
openhouse pull 2024 --member Pelosi         # only that filer's PDFs (substring match)
openhouse pull 2024 --doc-id 20024277       # one filing's PDF (requires a single year)
openhouse pull 2020-2024 --newest-first     # process 2024 first, 2020 last
```

`--member` and `--doc-id` are mutually exclusive. By default `pull` also makes a
one-time CC0 `congress-legislators` fetch into `raw/reference/` so `parse` can
pin verified bioguide identities; `--no-reference` skips it (every filer then
falls back to a name-only key, and `read --bioguide` finds nothing).

### parse (offline)

Turns the raw artifacts into normalized JSON under `<data>/parsed/`. Classifies
each PDF, extracts metadata and PTR transactions, joins filer identity, and
writes a parse-manifest recording what did and didn't parse — never a silent
gap. Re-parsing is cheap by design.

```sh
openhouse parse 2024
openhouse parse 2020-2024 --types ptr
openhouse parse 2024 --strict               # non-zero exit if any filing errors
```

### read (offline)

Queries the parsed JSON. JSON to stdout (one object per line for lists,
`jq`-composable); `--table` for a human-readable view. Four sub-verbs:

```sh
openhouse read filings 2024 --type ptr --state NY     # matching filing-metadata records
openhouse read filing 20024001                        # one filing: metadata + body
openhouse read trades 2024 --ticker NVDA --table      # PTR transactions, filer attached
openhouse read summary 2020-2024                       # per-year roll-up from the manifests
```

Identity filters on `filings` and `trades`:

- `--member <substring>` — case-insensitive substring over the filer id and raw
  names. Fuzzy name matching, **not** verified identity (SPEC §6.2).
- `--bioguide <id>` — exact, case-insensitive match on the verified
  `bioguide_id`. A sound query (no false positives); the precise alternative to
  `--member`. Needs reference-enriched data, so it only matches filings parsed
  from a pull made *without* `--no-reference`.

On `trades`, `--ticker` is a sound query (no false positives — exact symbol
match) while `--asset` leans toward completeness (substring over the verbatim
asset text, no false negatives). `read` errors with a non-zero exit when the
target years aren't parsed under the data dir, so an empty result is never
mistaken for "no matches" (run `parse` first).

## What's coming

The three verbs ship end to end for any year range since 2008; stable filer
identity via the CC0 `congress-legislators` bioguide join (with the name-key
fallback and warnings described in [Caveats](#caveats)) is in place, and a
Claude Code [agent skill](./openhouse/skill/SKILL.md) lets an AI agent drive
`pull`/`parse`/`read` directly. Still pending:

- **OCR for the scanned/handwritten backlog** ([#15](https://github.com/jswest/openhouse/issues/15)),
  already detected and catalogued by `parse` so nothing is lost in the meantime.

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

New contributors: [`CONTRIBUTING.md`](./CONTRIBUTING.md) is the standalone
onboarding guide — dev setup, the issue → PR workflow, and the data-model and
legal invariants the code must hold to.

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

Rebuild the installed CLI and re-press the agent skill:

```sh
# after cutting a release (tag exists) → clean 0.X.Y on --version
uv tool install --force .

# tracking day-to-day main (past the last tag) → 0.<schema>.<patch+1>.devN
uv tool install --force --no-cache --editable .

openhouse --version          # confirm the version moved
openhouse ready              # re-press the skill if SKILL.md changed
openhouse parse <years>     # re-parse only if SCHEMA_VERSION moved
```

Install **after** tagging — the version comes from the git tag via `hatch-vcs`,
so a build off an untagged `HEAD` reports `.devN`. `ready --check` reports
status without writing and exits non-zero on `stale`/`hand-edited`/`absent` —
a status code, not an error. A *hand-edited* verdict means the installed skill
diverged from `openhouse/skill/`; diff before letting `ready` overwrite.

Design contract: [SPEC.md](./SPEC.md). License: [MIT](./LICENSE.txt).
