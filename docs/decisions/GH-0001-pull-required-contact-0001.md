# GH-0001 — `pull` requires an operator contact (name + email)

**Date:** 2026-06-11
**Issue:** #1 (v0.1.0 omnibus — post-assembly hotfix)

## Context

The first real `pull` runs surfaced a problem the fixtures couldn't: the default
User-Agent was `openhouse/<version> (+<repo-url>)` with the contact *optional*
(an email appended only if `--contact`/`OPENHOUSE_CONTACT` was set). The repo URL
is **identical for every operator**, so without a contact every openhouse user is
indistinguishable to the Clerk. When several people pull concurrently, the Clerk
can't tell them apart and may rate-limit or block *all* of them at once — the
exact failure the polite-crawling defaults exist to avoid (the site has 403'd
naive clients before; there is no bulk PDF download to fall back on).

## Decision

A contact is **required** for any `pull` (it always makes network requests, even
`--index-only`):

- `build_user_agent` raises `PullError` (never crawls) when no contact is given,
  or when the contact lacks **either a name or an email**. The check is fail-fast
  — it runs before any request — with a message that explains *why* (shared
  anonymous UA → collective blocking) and *how* (`--contact "Name <email>"` or
  `OPENHOUSE_CONTACT`).
- The header becomes `openhouse/<version> (+<repo>; contact: <Name> <email>)`.
- Email validation is deliberately loose (`[^@\s]+@[^@\s]+\.[^@\s]+`): the point
  is a reachable operator, not RFC-perfect parsing that would reject valid
  addresses.
- `--user-agent` still overrides the whole header — the caller then owns
  identifying themselves, so it bypasses the requirement.

SPEC §3's User-Agent bullet was updated from "optional append" to "required
name + email"; `--help` and the flag text say so.

## Related: per-family progress bar

Shipped in the same hotfix (not its own decision, but recorded for context): the
PDF-download phase now draws a live per-data-type (`ptr`, then `fd`) progress bar
on stderr instead of one line per file — thousands of per-file lines for a real
`pull 2024` were unreadable. The bar is **TTY-only** (a no-op when stderr is piped
or redirected, so logs and test captures stay clean); per-family and per-year
summary lines carry the information in non-interactive runs. `_record_404` no
longer prints per-404 (it would shred the bar); the manifest entry remains the
durable record and the summary reports the not-found count, so "never silently
drop a filing" still holds.

## Scope

No change to routing, resumability, pacing, or the manifest schema. The version
string in the UA is still `0.0.0` (unbumped) — a separate release-time follow-up,
not part of this hotfix.
