# openhouse reference

Record schemas, the FilingType code table, and query recipes. Accurate to the
current CLI; the on-disk JSON shape is the contract.

## Record shapes

### Filing metadata (always present, from the index)

Every record in `parsed/clerk/<year>/filings.json`:

```json
{
  "doc_id": "20024277",
  "year": 2024,
  "filer": { "prefix": "Hon.", "first": "Richard W.", "last": "Allen", "suffix": null },
  "filer_id": "ga.allen.richard",
  "state_district": { "raw": "GA12", "state": "GA", "district": 12 },
  "filing_type": { "code": "P", "label": "periodic_transaction_report" },
  "filing_date": "2024-01-08",
  "source_pdf": "raw/clerk/2024/ptr/20024277.pdf",
  "pdf_class": "efiled",
  "parse_status": "ok"
}
```

Nullability (all seen in real data): `state_district` may be `null` (empty
`StateDst`); `filing_date` may be `null`; `filer.prefix` / `filer.suffix` usually
null. `state_district.state` is any 2-letter postal code including `DC`, `PR`,
and territories; `district` is an int with `0` = at-large / n.a.

### filer_id (name-string key, NOT true identity)

The Clerk index has no member ID — only name strings that vary across years. The
`filer_id` is a normalized key that gets *close* to dedup without claiming
identity:

```
filer_id = lower(state) "." slug(Last) "." slug(first_token(First)) ["." slug(Suffix)]
```

`slug()` lowercases, strips punctuation/diacritics, collapses whitespace to `-`.
Only the **first token** of the first name participates; suffix is included when
present (distinguishes Jr/Sr). Empty state → segment `unk`. Because it is a
name-string key, `read --member` is substring matching, not identity resolution.
A filer that matches a House seat in the CC0 reference roster gets a stable
`bioguide:<id>` instead; an unmatched filer stays `name:`-keyed, and `parse`
records it in `identity_warnings` with a classified `reason` (`candidate`,
`no_district`, `unknown_seat`, `ambiguous_seat`, or `suspicious`). Only
`suspicious` — a seat that IS held by a known rep but whose holder's name didn't
match the filer — is surfaced per-name on stderr; the rest collapse into a
one-line per-year summary (`match_summary` in the manifest).

### Filing bodies

One JSON per filing under `parsed/clerk/<year>/ptr/<DocID>.json` (PTR transactions) or
`parsed/clerk/<year>/fd/<DocID>.json` (annual-FD schedules). Filings that did not parse
(scanned / missing / odd) are not bodies — they appear in
`unparsed-manifest.json` instead, never as a silent gap.

## FilingType code table (verified for 2024)

Parse preserves the **raw code** so an unrecognized letter never drops a filing.

| Code | Meaning (working) |
|---|---|
| `P` | Periodic Transaction Report (PTR) |
| `O` | Annual report ("original") |
| `C` | Candidate report |
| `A` | Amendment |
| `X` | Extension |
| `T` | Termination |
| `W` | likely Withdrawal (carries empty-field edge cases) |
| `D`, `H`, `B`, `G`, `E` | seen in the index; meaning not yet enumerated |

`read --type` and `read filings --type` accept either a code (`P`) or a label
(`ptr`).

## Manifests

- `pull-manifest.json` (per year, in `raw/clerk/<year>/`) — per DocID: URL, HTTP
  status, byte size, sha256, fetched-at.
- `parse-manifest.json` (per year, in `parsed/clerk/<year>/`) — counts by filing type,
  `efiled` vs `scanned` vs `missing`, ok vs error, `identity_warnings` (each with
  its `reason`), a `match_summary` (matched / unmatched-by-reason / `suspicious`
  filer_ids), and the **integer schema generation** it was produced at. If your
  installed openhouse reports a newer generation, re-run `clerk parse`.
- `unparsed-manifest.json` — every not-fully-parsed filing with `doc_id`,
  `filer_id`, and a `reason` (`scanned`, `missing`, `extract_failed`,
  `unknown_type`, `validation_error`) — the OCR / follow-up backlog.

## Query recipes

Examples omit `--data-dir`, so they use the default store `~/.openhouse`
(override with `--data-dir` or `$OPENHOUSE_DATA_DIR`). Pipe stdout to `jq`;
watch stderr for the residual line.

**Every PTR a member filed in a year:**
```
openhouse clerk read filings 2024 --member pelosi --type ptr
```

**One filing in full (metadata + body):**
```
openhouse clerk read filing 20024277
```

**Every trade in a symbol, soundly (no false positives — "at least these"):**
```
openhouse clerk read trades 2023-2024 --ticker NVDA
```

**Don't-miss-a-trade search (over-matches — "at most these"):**
```
openhouse clerk read trades 2024 --asset nvidia
```
Use `--asset` when the filer may have omitted the ticker; discard spurious hits.

**Purchases over $50k by a member, bounded by transaction date:**
```
openhouse clerk read trades 2023-2024 --member khanna --type P --min-amount 50000 --since 2023-01-01
```
Note: the range is the **filing** year; a transaction can predate its filing (a
Dec-2023 trade in a 2024 filing), so widen the range when bounding by
transaction date.

**Per-year roll-up:**
```
openhouse clerk read summary 2019-2024 --table
```

**Pull then parse then query, end to end:**
```
openhouse clerk pull 2024 --contact "Jane Doe <jane@example.com>"
openhouse clerk parse 2024
openhouse clerk read trades 2024 --ticker AAPL
```

## Trust guarantees, restated

- `trades --ticker` — **sound**: every hit is a real symbol match; results are a
  lower bound ("at least these"). Blind to filer-omitted symbols (reported in the
  residual).
- `trades --asset` — **complete-leaning**: substring over verbatim asset text;
  results are an upper bound ("at most these"), with spurious hits to discard.
- Every range query prints a residual to stderr — the count of in-range filings
  that did not parse. A count is only meaningful read together with its residual:
  "complete over K parsed; M did not parse."

## FEC lane (`fec` source)

The FEC lane normalizes FEC **bulk** files into PAC→member contribution records,
queryable per **two-year cycle**. It is Path-1-only: itemized line-11C receipts
from **connected separate-segregated-fund (SSF) committees** to a member's
principal campaign committee. (See README *Caveats* for the named non-goals.)

### year → cycle

A year argument resolves to the enclosing even-ending **cycle**: an even year is
its own cycle (`2024` → `2024`); an odd year rolls up to the next even year
(`2023` → `2024`). A range expands to every cycle it touches. The resolution is
always echoed on stderr.

### FEC record shapes

**Committee** — `parsed/fec/<cycle>/committees.json`, one per committee seen:

```json
{
  "committee_id": "C00002469",
  "name": "INTERNATIONAL ASSOCIATION OF MACHINISTS ... POLITICAL LEAGUE",
  "connected_organization_name": "INTERNATIONAL ASSOCIATION OF MACHINISTS AND AEROSPACE WORKERS",
  "organization_type": "labor",
  "organization_type_raw": "L",
  "committee_type": "Q",
  "affiliation": null,
  "provenance": "fec"
}
```

`organization_type` is the **normalized** sponsor class; `organization_type_raw`
keeps the verbatim single-letter code beside it. `affiliation` is **always
`null`** — bulk `cm` carries no affiliation column, so parent/subsidiary PACs are
never collapsed (a declared limitation, surfaced in the residual).

**Contribution** — `parsed/fec/<cycle>/contributions.json`, one per kept receipt
(an FEC Schedule A line-11C, committee→committee):

```json
{
  "recipient_committee_id": "C00546358",
  "contributor_committee_id": "C00002469",
  "amount": 5000.0,
  "date": "2024-06-01",
  "line": "F3-11C",
  "image_number": "...",
  "transaction_id": "...",
  "provenance": "fec"
}
```

`amount` is an **exact dollar figure** (FEC itemizes the real amount, not a
bucket — unlike Clerk PTR ranges). `image_number` + `transaction_id` are the
double-entry key. A literally repeated `transaction_id` is deduped.

**Member link** — `parsed/fec/<cycle>/member-links.json`, the offline
member→candidate→committee anchor (CC0 `id.fec[]` join, never fuzzy):

```json
{ "bioguide_id": "A000370", "candidate_id": "H4NC12100", "committee_id": "C00546358" }
```

### `organization_type` code table (§13.3)

| raw | normalized label | `--org-type` value |
|---|---|---|
| `C` | corporation | `corporation` |
| `T` | trade association | `trade` |
| `L` | labor organization | `labor` |
| `M` | membership organization | `membership` |
| `V` | cooperative | `cooperative` |
| `W` | corporation without capital stock | `corp_without_stock` |

Labor (`L`) **is included** as institutional PAC money and tagged so `read
--org-type labor` can slice it. A contributing committee with no SSF type, or
absent from `cm`, is **not** in the kept set — it lands in
`fec-unparsed-manifest.json` (`not_connected_ssf` / `unresolved_committee`),
never a silent gap.

### FEC manifests

- `fec-pull-manifest.json` (`raw/fec/<cycle>/`) — per bulk file: requested URL,
  final redirected URL, status, byte size, sha256, fetched-at.
- `fec-parse-manifest.json` (`parsed/fec/<cycle>/`) — `counts` (kept,
  filtered-by-reason, by-org-type, member-links resolved/unresolved,
  `pac_limit_breaches`), the `$10k`-per-cycle invariant breaches (flagged, never
  dropped), the `affiliation_limitation` note, and the integer schema version.
- `fec-unparsed-manifest.json` — every filtered contribution + unresolved member
  link, each with a reason — the residual the `read` line is computed from.

### FEC query recipes

**Who funded a member's campaign committee, by organization (sound — complete
over kept Path-1 receipts):**
```
openhouse fec read donors A000370 2024
```

**Just the labor PACs:**
```
openhouse fec read donors A000370 2024 --org-type labor --table
```

**The inverse — which members an org's PAC gave to:**
```
openhouse fec read pac MACHINISTS 2024
```

**Pull then parse then query, end to end:**
```
openhouse fec pull 2024 --contact "Jane Doe <jane@example.com>"
openhouse fec parse 2024
openhouse fec read donors A000370 2024
```

### FEC trust guarantee, restated

Every `fec read` answer is **complete over the N kept Path-1 itemized receipts**
for the cycle, with the stderr residual naming the filtered count by reason. It
is the **disclosed candidate-side slice, not total influence** — no dark money,
no super-PAC independent expenditures, no soft money — and parent/subsidiary PACs
are **not** affiliation-collapsed. Read the residual before trusting a roll-up.
