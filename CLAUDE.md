# CLAUDE.md

Instructions for Claude working in this repo. Terse on purpose. One companion:

- [`SPEC.md`](./SPEC.md) — the contract: commands, data model, on-disk layout,
  and **verified facts about the Clerk's data source** (URL routing, XML edge
  cases, FilingType codes). Read it before changing anything structural; update
  it when behavior diverges. Decisions worth recording go one-per-file under
  [`docs/decisions/`](./docs/decisions/).

openhouse pulls U.S. House financial disclosures (annual FDs + PTRs) from the
Clerk into normalized JSON: `pull` (network) → `parse` (offline) → `read`
(offline query). Three verbs, one data directory, no database.

## Working agreements

- **Never silently drop a filing.** Unknown filing type, unreadable PDF,
  failed extraction → a record with explicit `pdf_class`/`parse_status` plus a
  manifest entry, never a gap. Preserve raw values (FilingType letter,
  `raw_text` fallbacks) alongside anything normalized.
- **Every query must be sound or complete — declare which.** A query with no
  false positives bounds the truth from below ("as few as" — at least these); one
  with no false negatives bounds it from above ("as many as" — at most these). A
  query that yields both in unknown amounts bounds nothing and is useless. State
  the guarantee, and state it relative to the parsed set plus the manifest's
  count of what didn't parse (complete over the known, explicit residual for the
  unknown). When you can't have both, prefer completeness: a missed trade is
  worse than a spurious hit a human can discard.
- **`pull` is the only network step.** `parse` and `read` must work offline,
  deterministically. No wall-clock in core logic — one timestamp captured at
  command entry, threaded into manifests.
- **JSON to stdout; prose/progress to stderr; non-zero exit on error.** Machine
  output is the contract (`jq`-composable, agent-consumable); `--table` is
  garnish for humans.
- **Polite crawling defaults are load-bearing** — sequential, 2.5 s between
  requests (grounded in congress.gov's published `Crawl-delay: 2`; the House
  publishes no policy of its own), descriptive User-Agent with contact flow,
  backoff. Don't strip them to go faster; the Clerk's site has 403'd naive
  clients, and there is no bulk PDF download to fall back on.
- **Schema changes mean re-parse, not migrate.** Pre-v1 there is no backwards
  compatibility: bump the schema version recorded in `parse-manifest.json`,
  delete old code, re-run `parse` from `raw/`. That re-parse is cheap and
  offline by design — keep it that way.
- **Legal restriction is a design constraint.** Clerk FD data: no commercial
  use, no solicitation, no credit decisions. README and `--help` say so; never
  add features that point the other way.
- **When unsure, stop and ask.** Most choices here trace to SPEC.md or a prior
  conversation. If something is ambiguous or uncovered, ask rather than wing it.
- **Smallest fix that fits.** No abstractions, flags, or layers unless asked.

## Workflow rails

- **Never commit or push on `main`.** The `guard-main-write.sh` hook enforces
  this; a block means you're on the wrong branch. Issue work happens in a
  **sibling worktree** (`../openhouse-issue-<N>-<slug>`) — never nested in the
  repo, never `git checkout -b` on `main`.
- **Pre-commit gates, every commit, in order:** `uv run pytest` (must pass) →
  `simplify-refactor` agent over the touched files → apply what's worth taking
  → re-run `uv run pytest` → commit.
- **Use `/ship #<N>`** for the full issue→PR loop. Claude opens the PR with a
  `Closes #<N>` line; **a human merges** — never merge yourself.

## Tooling

- **`uv`** for dependencies (not pip/venv); run code with `uv run python`.
- Tests: **`uv run pytest`** (the whole suite).
- Live-data probes during development hit the real Clerk site — keep them
  rare, polite, and out of the test suite (tests run on checked-in fixtures
  under `tests/fixtures/`).
- **`ship` and `ultraship` are vendored from a local skill drawer, not authored
  here.** `.claude/skills/{ship,ultraship}/` are byte-for-byte copies pressed from
  the drawer — never hand-edit them (the skill self-checks against its `.stamp`
  and will flag drift). Change behavior in the drawer and re-press; everything
  openhouse-specific lives in `.claude/ship.toml` / `.claude/ultraship.toml`,
  which the press never touches. (`release` is still repo-local — not yet
  vendored.)
