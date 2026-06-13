---
name: openhouse
description: Pull, parse, and query U.S. House financial-disclosure filings (annual FDs + STOCK Act PTRs) from the Office of the Clerk as normalized JSON. Use when asked about a House member's stock trades, financial disclosures, or periodic transaction reports, or to acquire/normalize that data.
---

# openhouse

`openhouse` turns U.S. House of Representatives financial disclosures — annual
Financial Disclosure (FD) statements and STOCK Act Periodic Transaction Reports
(PTRs) — into normalized JSON you can query. Three verbs, one data directory, no
database:

```
openhouse pull <years>    # network: fetch the index + PDFs from the Clerk
openhouse parse <years>   # offline: PDFs + index → normalized JSON, nothing dropped
openhouse read <query>    # offline: ask the parsed data a question
```

`<years>` is `YYYY` or `YYYY-YYYY` (inclusive), e.g. `2024` or `2019-2024`. The
index covers 2008→present; PTRs exist only from 2012 (STOCK Act).

## Legal restriction (binding)

Clerk FD data carries a statutory use restriction: **not for commercial use,
soliciting, or establishing credit ratings** (news/media dissemination
excepted). Do not build or suggest features that point the other way.

## The contract: JSON to stdout, prose to stderr

Every command writes **machine output (JSON) to stdout** and **prose/progress
to stderr**, and exits non-zero on error. So pipe stdout into `jq` and let
stderr scroll. Add `--table` to `read` for a human-readable table instead of
JSON (garnish — the JSON is the contract).

## pull (the only network step)

```
openhouse pull 2024 --contact "Jane Doe <jane@example.com>"
```

`pull` is the **only** command that touches the network. It is polite by design
(sequential, ~2.5 s between requests, descriptive User-Agent) and idempotent /
Ctrl-C-safe — re-runs fetch only what's missing. A `--contact` (name + email)
is **required** so the operator is identifiable; set it inline or via the
`OPENHOUSE_CONTACT` env var. Useful flags: `--index-only`, `--types ptr,fd`,
`--data-dir`, `--force`. Don't lower `--delay` casually — the Clerk has 403'd
naive clients.

## parse (offline normalization)

```
openhouse parse 2024
```

Offline and deterministic. Reads `raw/<year>/`, writes normalized JSON to
`parsed/<year>/`. **Never silently drops a filing**: e-filed PDFs become
structured records; scanned/paper/odd filings are catalogued in
`unparsed-manifest.json` with a reason. `--strict` exits non-zero if any filing
errors.

## read (offline query)

Four subcommands, each `<range>`-scoped except `filing`:

```
openhouse read filings 2024 --member adams --type ptr
openhouse read filing 20024277
openhouse read trades 2023-2024 --ticker NVDA --table
openhouse read summary 2024
```

- `filings <range>` — matching filing-metadata records. Filters: `--type`,
  `--member`, `--state`, `--since`, `--until`.
- `filing <doc_id>` — one filing: metadata + body if parsed.
- `trades <range>` — PTR transactions flattened across the range, filer
  attached. Filters: `--ticker`, `--asset`, `--member`, `--owner`, `--type`,
  `--since`, `--until`, `--min-amount`.
- `summary <range>` — per-year roll-up from the manifests.

**This skill queries EXISTING parsed data with `read` only.** Do not `pull` or
`parse` to satisfy a query — those are acquisition steps, not query steps. If a
`read` comes back empty, it almost always means the wrong data directory, not
missing data: check `--data-dir` / `OPENHOUSE_DATA_DIR` points at a parsed
corpus. A range query against a dir with no parsed years now **fails loudly**
(non-zero exit, error to stderr) rather than returning a misleading empty
result — that error is the signal you are pointed at the wrong place.

### Sound or complete — read the residual line

Every query declares which way to trust it; a zero-result answer is never
ambiguous. The two `trades` symbol queries are the clearest case:

- `--ticker` is the **sound** query: exact (case-insensitive) ticker match, no
  false positives. Results are **at least** the trades in that symbol — every
  hit is real. It cannot see a trade whose filer omitted the symbol.
- `--asset` is the **completeness-leaning** query: substring over the verbatim
  asset text (which embeds `(TICKER) [TYPE]`). Results are **at most** the
  matches; it over-matches, and you discard spurious hits. Reach for this when
  you would rather not miss a trade.

Every range query prints a **residual** line to stderr: the count of in-range
filings that did *not* parse (scanned / missing / not-classified), so the answer
is "complete over the K parsed filings; M did not parse." For `--ticker` it also
reports in-range stock/option transactions whose ticker is null. **Always read
the residual before trusting a count.**

## Data layout

`--data-dir` defaults to `./data`:

```
<data-dir>/
  raw/<year>/      <year>FD.xml, <year>FD.txt, pull-manifest.json, ptr/<DocID>.pdf, fd/<DocID>.pdf
  parsed/<year>/   filings.json, ptr/<DocID>.json, fd/<DocID>.json, parse-manifest.json, unparsed-manifest.json
```

`filings.json` is the year's roll-up index; one JSON per filing body. The
`parse-manifest.json` records counts and the integer schema generation it was
produced at — re-`parse` after upgrading openhouse if that generation moved.

See `reference.md` for record schemas, the FilingType code table, and query
recipes.
