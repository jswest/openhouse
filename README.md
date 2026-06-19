# openhouse

<img src="docs/assets/logo.png" alt="openhouse logo: an open-house yard sign reading 'openhouse'" width="200">

Pull, parse, and query **U.S. House of Representatives financial disclosure
filings** — annual Financial Disclosure statements and STOCK Act Periodic
Transaction Reports — from the Office of the Clerk, into normalized JSON you
can actually ask questions of.

> **Status: pre-v1.** The pipeline works end to end; the parsed schema is not
> yet frozen, so a version bump can mean a re-parse rather than a migration.
> The CLI is source-scoped: verbs live under a source noun — `clerk` for House
> disclosures, `fec` for FEC campaign-contribution data (connected-PAC money to
> a member's campaign committee). Design contract: [SPEC.md](./SPEC.md).

## What it does

A three-command pipeline plus an accuracy-review tool, over one data directory:

```sh
openhouse clerk pull 2024 --contact "Jane Doe <jane@example.com>"
                             # network: fetch the year's index + every PDF (resumable, polite)
openhouse clerk parse 2024   # offline: PDFs + index → normalized JSON, nothing dropped
openhouse clerk read trades 2024 --ticker NVDA --table
                             # offline: ask the parsed data a question
openhouse clerk inspect 2024 --sample 0.05
                             # offline: review a sample beside the PDFs, score accuracy
```

`pull` **requires** a `--contact "Name <email>"` (or the `OPENHOUSE_CONTACT`
env var): it goes into the User-Agent so the Clerk can identify the operator.
The Clerk blocks anonymous shared clients, so without it `pull` errors out
before making a request. Full flag reference is in [Usage](#usage).

- **`pull`** is the only network step. Idempotent and Ctrl-C-safe — re-runs
  fetch only what's missing. Multi-year ranges (`openhouse clerk pull 2019-2024`)
  throughout.
- **`parse`** never touches the network and never silently drops a filing:
  machine-readable (e-filed) PDFs become structured records; scanned/paper
  filings are detected and catalogued for a future OCR pass; anything odd lands
  in a manifest with a reason.
- **`read`** answers questions from the parsed JSON — filings by member or
  type, a single filing's full contents, stock transactions flattened across
  years, per-year summaries. Every query states whether its results are
  exhaustive (an upper bound — "at most these") or a floor (a lower bound —
  "at least these"), so a zero-result answer is never ambiguous. It errors
  loudly when the data directory is empty or unparsed — "nothing here to query"
  is an error, never silently returned as "no matches."
  JSON to stdout for machines and `jq`; `--table` for humans.
- **`inspect`** measures whether the parse is *right*, not just whether it ran.
  It samples a reproducible, stratified slice of the parsed filings, opens a
  small local web app showing each one beside its source PDF, and records a
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

The CLI is **source-scoped**: a source noun (`clerk` / `fec`) sits above the
pipeline verbs, so the House-Clerk commands are `openhouse clerk <verb>` and
the FEC commands are `openhouse fec <verb>`. The `ready` and `reference`
commands stay top-level. `clerk pull` is the only command that touches the
network; all other commands are offline and deterministic.

**Data directory** (precedence, highest first): the `--data-dir` flag, then the
`$OPENHOUSE_DATA_DIR` env var, then the `~/.openhouse` default. All three verbs
honour the same precedence; `raw/clerk/` lives under it after `clerk pull`,
`parsed/clerk/` after `clerk parse`. FEC data lives under `raw/fec/` and
`parsed/fec/`.

**Environment variables:**

- `OPENHOUSE_CONTACT` — your `Name <email>` for `pull`'s User-Agent; saves
  repeating `--contact`. Required for `clerk pull` one way or the other.
- `OPENHOUSE_DATA_DIR` — the data directory, overridden only by `--data-dir`.

### Migrating from the pre-namespace layout

The CLI used to expose bare verbs (`openhouse pull 2024`) and stored data at
`raw/<year>/` + `parsed/<year>/`. It is now source-scoped: run
`openhouse clerk <verb>`, and the data lives under `raw/clerk/<year>/` +
`parsed/clerk/<year>/`. If you have a store from before the change, relocate it
once with an **offline `mv`** (this moves bytes — it does **not** re-download):

```sh
mv ~/.openhouse/raw/<year>    ~/.openhouse/raw/clerk/<year>
mv ~/.openhouse/parsed/<year> ~/.openhouse/parsed/clerk/<year>
```

openhouse detects a legacy `raw/<year>/` and prints this `mv` once as a stderr
nudge — it never moves your data for you. After the move, re-run
`openhouse clerk parse <year>`: the `mv` relocates the parsed files but not the
`source_pdf` path baked into each record, and the schema version bumped (9→10),
so `clerk read` warns until a re-parse refreshes the tree. (The shared
CC0 `raw/reference/` set is *not* relocated.)

### clerk pull (network)

Fetches the annual index ZIP and per-filing PDFs into `<data>/raw/clerk/`. Polite by
default (sequential, 2.5 s between requests, identifiable User-Agent); re-runs
skip what's already on disk. Years are `YYYY` or `YYYY-YYYY`.

```sh
openhouse clerk pull 2024 --contact "Jane Doe <jane@example.com>"
openhouse clerk pull 2020-2024 --types ptr        # only PTRs, five years
openhouse clerk pull 2024 --index-only            # index metadata, no PDF bodies
openhouse clerk pull 2024 --member Pelosi         # only that filer's PDFs (substring match)
openhouse clerk pull 2024 --doc-id 20024277       # one filing's PDF (requires a single year)
openhouse clerk pull 2020-2024 --newest-first     # process 2024 first, 2020 last
```

`--member` and `--doc-id` are mutually exclusive. By default `pull` also makes a
one-time CC0 `congress-legislators` fetch into `raw/reference/` so `parse` can
pin verified member IDs (bioguide IDs from a public-domain legislator registry);
`--no-reference` skips it (every filer then falls back to a name-only key, and
`read --bioguide` finds nothing).

### clerk parse (offline)

Turns the raw artifacts into normalized JSON under `<data>/parsed/clerk/`. Classifies
each PDF, extracts metadata and PTR transactions, joins filer identity, and
writes a parse-manifest recording what did and didn't parse — never a silent
gap. Re-parsing is cheap by design.

```sh
openhouse clerk parse 2024
openhouse clerk parse 2020-2024 --types ptr
openhouse clerk parse 2024 --strict               # non-zero exit if any filing errors
```

### clerk read (offline)

Queries the parsed JSON. JSON to stdout (one object per line for lists,
`jq`-composable); `--table` for a human-readable view. Four sub-verbs:

```sh
openhouse clerk read filings 2024 --type ptr --state NY     # matching filing-metadata records
openhouse clerk read filing 20024001                        # one filing: metadata + body
openhouse clerk read trades 2024 --ticker NVDA --table      # PTR transactions, filer attached
openhouse clerk read holdings 2024 --asset nvda --table     # Schedule A assets from annual FDs
openhouse clerk read summary 2020-2024                       # per-year roll-up from the manifests
```

Identity filters on `filings`, `trades`, and `holdings`:

- `--member <substring>` — case-insensitive substring over the filer id and raw
  names. Fuzzy name matching, **not** verified identity (SPEC §6.2).
- `--bioguide <id>` — exact match on a member's verified congressional ID (the
  same stable identifier used by congress.gov). A sound query (no false
  positives); the precise alternative to `--member`. Needs reference-enriched
  data, so it only matches filings parsed from a pull made *without*
  `--no-reference`.

On `trades`, `--ticker` is a sound query (no false positives — exact symbol
match) while `--asset` leans toward completeness (substring over the verbatim
asset text, no false negatives). On `holdings`, `--asset` is the only asset-text
filter — Schedule A items carry no separate parsed ticker field; use
`--asset <SYMBOL>` to find by symbol (see
`docs/decisions/GH-0200-read-holdings-0001.md`). `read` errors with a non-zero
exit when the target years aren't parsed under the data dir, so an empty result
is never mistaken for "no matches" (run `parse` first).

### reference (offline)

Look up legislators by name or ID. Searches the union of current and historical
legislators cached in `raw/reference/` (populated by `clerk pull`). Matching is
case- and diacritic-insensitive for names (so `gonzalez` matches
`González-Colón`) and case-insensitive for IDs.
A top-level command, not scoped to `clerk` or `fec`.

```sh
openhouse reference Adams --table           # all legislators named Adams
openhouse reference A000370                 # look up by id
openhouse reference gonzalez               # diacritic-insensitive name search
```

**Guarantee:** complete over the cached congress-legislators set (current ∪
historical) — every matching record is returned, none dropped. The residual is
members absent from the on-disk cache (e.g. sworn in after the last `clerk
pull`); re-pull to refresh. JSON to stdout; `--table` for human-aligned columns
(name, id, chamber, state). No matches → empty result, exit 0. No reference
data on disk → non-zero exit with a pointer to `clerk pull`.

## What's coming

The `clerk` pipeline ships end to end for any year range since 2008; stable
filer identity (via the public-domain `congress-legislators` registry) and a
Claude Code [agent skill](./openhouse/skill/SKILL.md) are in place. The `fec`
lane is in place for connected-PAC contributions. Still pending:

- **OCR for the scanned/handwritten backlog** ([#15](https://github.com/jswest/openhouse/issues/15)),
  already detected and catalogued by `parse` so nothing is lost in the meantime.

## Caveats

**Filer identity has two levels of confidence, and only one is verified.** The
Clerk index carries no member ID — only name strings that vary across years
("Alma Shealey Adams" vs "Alma S. Adams"). `parse` resolves each filer through
two levels and records which it used:

- **Verified ID** (`bioguide:<id>`) — the filer's House seat (normalized last
  name + state + district) matched a single record in the public-domain
  [`@unitedstates/congress-legislators`](https://github.com/unitedstates/congress-legislators)
  registry. This is a **stable identity**: the same ID across years and name
  spellings is the same person. The match is conservative — a seat that resolves
  to two legislators (same last name, same seat across time) matches *nothing*
  rather than guess, so a verified ID is never a false positive.
- **Name key** (`name:<normalized-slug>`) — the last resort, used when no House
  seat matched (a candidate who never took a seat, a delegate edge case, a name
  the registry doesn't carry, an ambiguous seat). **This is an unverified
  name-string label, not a stable identity.** Two different people can share one
  `name:` key; `parse` emits an `identity_warnings` entry (and a stderr line) for
  every name-keyed filer precisely so a `read --member` user knows the match is
  unverified.

The public-domain registry is fetched once by `pull` into `raw/reference/` and
joined **offline** by `parse`. `pull --no-reference` skips it, in which case
every filer falls back to a name key.

**The staff↔member bridge, if any, is name-keyed and unverified.** Where a
filing is bridged to a member by name alone (no verified ID), treat it as a
tagged, unverified claim — a starting point for a human to confirm, never a
settled fact. `openhouse` never synthesizes a verified ID from a name guess.

**The FEC lane covers connected-PAC contributions — not total campaign money.**
`fec read` answers exactly one question: which corporate, trade, or labor PAC
organizations gave to a member's principal campaign committee, by two-year
cycle. This is a disclosed, itemized slice of campaign finance, not a measure
of total influence. Out of scope:

- Individual-donor itemization, leadership-PAC flows, and joint-fundraising
  transfers are not included.
- Organizations are tagged by their FEC type (corporation / trade / labor /
  membership / cooperative / corp-without-stock), not mapped to sectors or
  industries.
- Super-PAC independent expenditures, dark money, and soft money are not
  included — only direct, itemized contributions to the candidate's principal
  campaign committee.

Two further limitations are declared on every `fec read` response (on stderr):

- **Disclosed-slice caveat.** The roll-up is complete over the *itemized*
  receipts the FEC discloses for the cycle; it cannot see money that was never
  itemized or never disclosed. The stderr residual states the count and reason
  for anything filtered (PACs without a connected organization, unresolved
committees).
- **Labor is included and tagged.** Labor PAC money is institutional PAC money
  like any other — it is kept and tagged `labor`, never silently dropped, so
  `--org-type labor` slices exactly it.
- **Parent and subsidiary PACs are reported separately.** FEC data carries no
  affiliation column, so a parent organization and its subsidiary PACs appear as
  separate entries — `openhouse` never merges them from data it does not have.

## Use restriction

Clerk financial-disclosure data may **not** be used for commercial purposes
(news/media dissemination excepted), for solicitation, or to establish credit
ratings — this is statutory ([5 U.S.C. §
13107](https://www.law.cornell.edu/uscode/text/5/13107), Ethics in Government
Act as amended), not a request. `openhouse` is a research and transparency
tool; don't build a commercial product on its output.

FEC data (the `fec` source — [SPEC §13](./SPEC.md)) sits on a *different*
legal footing: it is **public domain** (a federal-government work), with one
statutory bar — [52 U.S.C. §
30111(a)](https://www.law.cornell.edu/uscode/text/52/30111): contributor
information may **not** be sold or used to solicit contributions or for any
commercial purpose. Records carry a `provenance` tag (`"fec"` vs `"clerk"`) so
the two footings stay distinguishable downstream.

## Development

Python 3.11+, managed with [`uv`](https://docs.astral.sh/uv/):

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

A schema bump means existing `parsed/clerk/` data must be re-parsed — re-run
`openhouse clerk parse <years>`; the dry run says so when it applies.

## Refreshing a local install

Rebuild the installed CLI and re-press the agent skill:

```sh
# after cutting a release (tag exists) → clean 0.X.Y on --version
uv tool install --force .

# tracking day-to-day main (past the last tag) → 0.<schema>.<patch+1>.devN
uv tool install --force --no-cache --editable .

openhouse --version          # confirm the version moved
openhouse ready                  # re-press the skill if SKILL.md changed
openhouse clerk parse <years>    # re-parse only if SCHEMA_VERSION moved
```

Install **after** tagging — the version comes from the git tag via `hatch-vcs`,
so a build off an untagged `HEAD` reports `.devN`. `ready --check` reports
status without writing and exits non-zero on `stale`/`hand-edited`/`absent` —
a status code, not an error. A *hand-edited* verdict means the installed skill
diverged from `openhouse/skill/`; diff before letting `ready` overwrite.

Design contract: [SPEC.md](./SPEC.md). License: [MIT](./LICENSE.txt).
