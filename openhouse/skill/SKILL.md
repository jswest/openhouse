---
name: openhouse
description: Pull, parse, and query U.S. House financial-disclosure filings (annual FDs + STOCK Act PTRs) from the Office of the Clerk, and connected-PAC contributions to House members' campaign committees from the FEC, as normalized JSON. Use when asked about a House member's stock trades, financial disclosures, periodic transaction reports, or which PACs/organizations funded their campaign — or to acquire/normalize that data.
---

# openhouse

`openhouse` turns U.S. House of Representatives financial disclosures — annual
Financial Disclosure (FD) statements and STOCK Act Periodic Transaction Reports
(PTRs) — into normalized JSON you can query. Three verbs, one data directory, no
database. The CLI is source-scoped — the same three verbs hang off each source:
the House-Clerk pipeline under `clerk`, and the FEC PAC-money pipeline under
`fec`:

```
openhouse <source> pull <range>     # network: fetch from the source
openhouse <source> parse <range>    # offline: source data → normalized JSON, nothing dropped
openhouse <source> read <query>     # offline: ask the parsed data a question
```

```
openhouse clerk pull <years>    # the index + PDFs from the Clerk
openhouse fec   pull <years>    # the FEC bulk files for the matching cycle(s)
```

For `clerk`, `<years>` is `YYYY` or `YYYY-YYYY` (inclusive), e.g. `2024` or
`2019-2024`; the index covers 2008→present, PTRs exist only from 2012 (STOCK
Act). For `fec`, you also pass a **year** (or range) — it resolves to the
enclosing **two-year election cycle** (the even end year): `2023` and `2024`
both resolve to cycle `2024`. The resolution is always echoed on stderr.

The two sources sit on different legal footings (see below) and never share a
data tree (`raw/clerk/…` vs `raw/fec/…`); records carry a `provenance` tag
(`"clerk"` vs `"fec"`) so the footings stay distinguishable downstream.

## Legal restriction (binding)

**Clerk FD data** carries a statutory use restriction: **not for commercial use,
soliciting, or establishing credit ratings** (news/media dissemination
excepted). Do not build or suggest features that point the other way.

**FEC data is public domain** (a federal-government work), so it carries no
commercial-use bar — *but* one statutory restriction applies: [52 U.S.C.
§30111(a)](https://www.law.cornell.edu/uscode/text/52/30111), the **"sale or
use" restriction** — contributor information may **not** be sold or used to
solicit contributions or for any commercial purpose. The two footings are
distinct; never mix them, and never point a feature the other way.

## The contract: JSON to stdout, prose to stderr

Every command writes **machine output (JSON) to stdout** and **prose/progress
to stderr**, and exits non-zero on error. So pipe stdout into `jq` and let
stderr scroll. Add `--table` to `read` for a human-readable table instead of
JSON (garnish — the JSON is the contract).

## clerk pull (the only network step)

```
openhouse clerk pull 2024 --contact "Jane Doe <jane@example.com>"
```

`pull` is the **only** command that touches the network. It is polite by design
(sequential, ~2.5 s between requests, descriptive User-Agent) and idempotent /
Ctrl-C-safe — re-runs fetch only what's missing. A `--contact` (name + email)
is **required** so the operator is identifiable; set it inline or via the
`OPENHOUSE_CONTACT` env var. Useful flags: `--index-only`, `--types ptr,fd`,
`--data-dir`, `--force`. Don't lower `--delay` casually — the Clerk has 403'd
naive clients.

## clerk parse (offline normalization)

```
openhouse clerk parse 2024
```

Offline and deterministic. Reads `raw/clerk/<year>/`, writes normalized JSON to
`parsed/clerk/<year>/`. **Never silently drops a filing**: e-filed PDFs become
structured records; scanned/paper/odd filings are catalogued in
`unparsed-manifest.json` with a reason. `--strict` exits non-zero if any filing
errors.

## clerk read (offline query)

Four subcommands, each `<range>`-scoped except `filing`:

```
openhouse clerk read filings 2024 --member adams --type ptr
openhouse clerk read filing 20024277
openhouse clerk read trades 2023-2024 --ticker NVDA --table
openhouse clerk read summary 2024
```

- `filings <range>` — matching filing-metadata records. Filters: `--type`,
  `--member`, `--state`, `--since`, `--until`.
- `filing <doc_id>` — one filing: metadata + body if parsed.
- `trades <range>` — PTR transactions flattened across the range, filer
  attached. Filters: `--ticker`, `--asset`, `--member`, `--owner`, `--type`,
  `--since`, `--until`, `--min-amount`.
- `summary <range>` — per-year roll-up from the manifests.

**This skill queries EXISTING parsed data with `clerk read` only.** Do not
`clerk pull` or `clerk parse` to satisfy a query — those are acquisition steps, not query steps. If a
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

## fec pull / parse / read (the PAC-money lane)

The `fec` source answers a narrow, sound question: **which connected-PAC
organizations gave itemized money to a member's campaign committee, by cycle.**
It is the analogue of the clerk lane — same three verbs, same JSON-to-stdout
contract, same offline `parse`/`read`.

```
openhouse fec pull 2024 --contact "Jane Doe <jane@example.com>"
openhouse fec parse 2024
openhouse fec read donors A000370 2024            # PACs → member
openhouse fec read pac MACHINISTS 2024            # org's PAC → members it gave to
```

- **`fec pull`** is the only network step. It fetches the FEC **bulk** files for
  the cycle (candidate master `cn`, candidate-committee linkage `ccl`, committee
  master `cm`, committee→candidate contributions `pas2`/`itpas2`). It is
  bulk-data-only (the OpenFEC API is never crawled), polite by design (sequential,
  **10 s** between files — fec.gov's own `Crawl-delay`), `--contact`-required, and
  idempotent. The CC0 `congress-legislators` reference set (the member↔FEC anchor)
  is fetched by `clerk pull`; run it once if you have not.
- **`fec parse`** is offline. It Path-1-filters `itpas2` to **connected
  separate-segregated-fund (SSF) committee** contributions — corporate (`C`),
  trade (`T`), labor (`L`), membership (`M`), cooperative (`V`), and
  corporation-without-capital-stock (`W`) — tags each by `organization_type`,
  rolls receipts up to the connected organization, and resolves member→candidate→
  principal-committee from the CC0 join. **Nothing is dropped**: a contribution
  whose committee isn't a connected SSF, or isn't in `cm` at all, lands in
  `fec-unparsed-manifest.json` with a reason (`not_connected_ssf` /
  `unresolved_committee`).
- **`fec read donors <member> <year>`** rolls the kept receipts up to organization
  for one member (matched by `bioguide_id` — a full id pins the member, a fragment
  fuzzy-matches), sorted by total desc. `--org-type <class>` slices to one SSF
  class (`labor`, `corporation`, `trade`, `membership`, `cooperative`,
  `corp_without_stock`); an unknown class fails loudly (exit 2).
- **`fec read pac <org> <year>`** is the inverse: an org's PAC → the members it
  supported (fuzzy substring over the org rollup key).

### Sound or complete — the FEC residual line

Every `fec read` answer prints its guarantee + residual to stderr, tied straight
to the cycle's `fec-parse-manifest.json` counts: **complete over the N kept
Path-1 itemized receipts**, with the residual naming the filtered count split by
reason. It also restates the framing — this is the *disclosed candidate-side
slice*, **not total influence** (no dark money, no super-PAC independent
expenditures, no soft money) — and the **affiliation-not-collapsed** limitation
(bulk `cm` has no affiliation column, so a parent and its subsidiary PACs are
*not* merged). Read it before trusting a roll-up.

## Data layout

`--data-dir` defaults to `~/.openhouse` (precedence: `--data-dir`, then `$OPENHOUSE_DATA_DIR`, then `~/.openhouse`):

```
<data-dir>/
  raw/clerk/<year>/      <year>FD.xml, <year>FD.txt, pull-manifest.json, ptr/<DocID>.pdf, fd/<DocID>.pdf
  parsed/clerk/<year>/   filings.json, ptr/<DocID>.json, fd/<DocID>.json, parse-manifest.json, unparsed-manifest.json
  raw/fec/<cycle>/       cn.txt, ccl.txt, cm.txt, itpas2.txt, fec-pull-manifest.json
  parsed/fec/<cycle>/    committees.json, contributions.json, member-links.json, fec-parse-manifest.json, fec-unparsed-manifest.json
  raw/reference/         congress-legislators bulk (CC0; shared member↔FEC anchor)
```

`filings.json` is the year's roll-up index; one JSON per filing body. The
`parse-manifest.json` (clerk) and `fec-parse-manifest.json` (fec) record counts
and the integer schema generation each was produced at — re-run the matching
`parse` after upgrading openhouse if that generation moved.

See `reference.md` for record schemas, the FilingType code table, and query
recipes.
