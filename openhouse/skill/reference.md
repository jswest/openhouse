# openhouse reference

Record schemas, the FilingType code table, and query recipes. Accurate to the
current CLI; the on-disk JSON shape is the contract.

## Record shapes

### Filing metadata (always present, from the index)

Every record in `parsed/<year>/filings.json`:

```json
{
  "doc_id": "20024277",
  "year": 2024,
  "filer": { "prefix": "Hon.", "first": "Richard W.", "last": "Allen", "suffix": null },
  "filer_id": "ga.allen.richard",
  "state_district": { "raw": "GA12", "state": "GA", "district": 12 },
  "filing_type": { "code": "P", "label": "periodic_transaction_report" },
  "filing_date": "2024-01-08",
  "source_pdf": "raw/2024/ptr/20024277.pdf",
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
name-string key, `read --member` is substring matching, not identity resolution;
`parse` emits `identity_warnings` when one `filer_id` may cover two people
(different districts in a year, or a slug collision by suffix / last name).

### Filing bodies

One JSON per filing under `parsed/<year>/ptr/<DocID>.json` (PTR transactions) or
`parsed/<year>/fd/<DocID>.json` (annual-FD schedules). Filings that did not parse
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

- `pull-manifest.json` (per year, in `raw/<year>/`) — per DocID: URL, HTTP
  status, byte size, sha256, fetched-at.
- `parse-manifest.json` (per year, in `parsed/<year>/`) — counts by filing type,
  `efiled` vs `scanned` vs `missing`, ok vs error, `identity_warnings`, and the
  **integer schema generation** it was produced at. If your installed openhouse
  reports a newer generation, re-run `parse`.
- `unparsed-manifest.json` — every not-fully-parsed filing with `doc_id`,
  `filer_id`, and a `reason` (`scanned`, `missing`, `extract_failed`,
  `unknown_type`, `validation_error`) — the OCR / follow-up backlog.

## Query recipes

All examples assume `--data-dir ./data`. Pipe stdout to `jq`; watch stderr for
the residual line.

**Every PTR a member filed in a year:**
```
openhouse read filings 2024 --member pelosi --type ptr
```

**One filing in full (metadata + body):**
```
openhouse read filing 20024277
```

**Every trade in a symbol, soundly (no false positives — "at least these"):**
```
openhouse read trades 2023-2024 --ticker NVDA
```

**Don't-miss-a-trade search (over-matches — "at most these"):**
```
openhouse read trades 2024 --asset nvidia
```
Use `--asset` when the filer may have omitted the ticker; discard spurious hits.

**Purchases over $50k by a member, bounded by transaction date:**
```
openhouse read trades 2023-2024 --member khanna --type P --min-amount 50000 --since 2023-01-01
```
Note: the range is the **filing** year; a transaction can predate its filing (a
Dec-2023 trade in a 2024 filing), so widen the range when bounding by
transaction date.

**Per-year roll-up:**
```
openhouse read summary 2019-2024 --table
```

**Pull then parse then query, end to end:**
```
openhouse pull 2024 --contact "Jane Doe <jane@example.com>"
openhouse parse 2024
openhouse read trades 2024 --ticker AAPL
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
