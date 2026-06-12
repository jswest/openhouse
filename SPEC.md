# openhouse — SPEC

A standalone tool that pulls, parses, and queries **U.S. House of Representatives
financial disclosure** filings from the Office of the Clerk.

Three commands, one data directory:

```
openhouse pull  <year | year_start-year_end>   # acquire raw artifacts from the Clerk (network)
openhouse parse <year | year_start-year_end>   # transform raw artifacts → normalized JSON (offline)
openhouse read  <subcommand> [args]            # query the normalized JSON (offline, read-only)
```

`pull` is the network step; `parse` is purely local; `read` is a pure function over
`parse`'s output. The pull/parse split exists so you can re-parse (new schema, bug
fix, new field) **without re-downloading**, and so a failed parse never costs a
re-crawl. `read` exists because parsed JSON on disk isn't an answer — "who traded
NVDA in 2023?" should be one command, and it's also the surface a future agent
skill will drive (§8). All three commands take the identical year-range argument
where applicable.

> Status: **specification, verified against live Clerk data on 2026-06-11.**
> The former ⚠ VERIFY items are resolved — marked **✅ VERIFIED** inline, with
> findings consolidated in §10. No implementation yet.

---

## 1. Background: what we're ingesting

Under the Ethics in Government Act (as amended by the STOCK Act), the following file
financial disclosures with the **Clerk of the House** (Legislative Resource Center);
the House Committee on Ethics oversees the program:

- **All Members** of the House and **House candidates** (anyone raising/spending >$5,000).
- **Officers and senior staff** — employees paid ≥120% of the GS-15 base (~$135–145K)
  for ≥60 days in the year. Every Member office must have ≥1 filer; absent a
  threshold-paid aide, the Member designates a **principal assistant**.
- **New-entrant, terminating, and nominee** filers.

Two document families, both in scope for v1:

| Family | Form | What it contains | Cadence |
|---|---|---|---|
| **Annual FD** | Financial Disclosure Statement | Full picture: assets, income, liabilities, positions, agreements, gifts, travel, transactions (Schedules A–J) | Annual, due **May 15** (prior calendar year) |
| **PTR** | Periodic Transaction Report | Securities trades >$1,000 (STOCK Act) | Within **30–45 days** of the trade |

Plus the index also enumerates **candidate**, **extension**, **amendment**, and
**termination** filings, which `parse` records as metadata even when the body adds little.

### Legal use restriction (design constraint)

Clerk FD data carries a statutory restriction: it may **not** be used for any
*commercial* purpose (except news/media dissemination), for soliciting, or to
establish anyone's credit rating. `openhouse` is a research/transparency tool;
the README and `--help` should state this, and the project should not ship a
commercial-facing product on top of the raw data.

---

## 2. The source: Office of the Clerk bulk data

### 2.1 Annual index (fully structured — the easy layer) — ✅ VERIFIED

One ZIP per year, refreshed daily:

```
https://disclosures-clerk.house.gov/public_disc/financial-pdfs/<YEAR>FD.zip
```

Contains `<YEAR>FD.xml` (and a `<YEAR>FD.txt` companion). **Confirmed structure**
(live 2024 file): root `<FinancialDisclosure>`, one `<Member>` element per filing,
with exactly these child tags:

| Field | Example | Notes |
|---|---|---|
| `Prefix` | `Hon.` | often empty |
| `Last` / `First` / `Suffix` | `Allen` / `Richard W.` | `First` may include middle names; stray punctuation occurs (`Maryam.`) |
| `FilingType` | `P` | single-letter code — see §2.3 |
| `StateDst` | `GA12` | state + 2-digit district; **can be empty** (seen on type `W`) |
| `Year` | `2024` | the **coverage** year, not the filing year |
| `FilingDate` | `5/15/2024` | `M/D/YYYY`; **can be empty** (seen on type `W`) |
| `DocID` | `20024277` | maps to the PDF filename — treat as an **opaque string** |

**Edge cases observed in real 2024 data — the schema must handle all of these:**

- `StateDst` and `FilingDate` **empty** on some type-`W` records → both nullable.
- `StateDst` includes non-states: `DC00`, `PR00` (DC, territories; district `00`
  also covers at-large seats) → do not validate against the 50 states.
- A `Year=2024` annual report with `FilingDate` 4/29/**2025** → never derive or
  cross-validate one from the other.
- 4-digit DocIDs exist (e.g. `7940`, type `W`) alongside 7- and 8-digit → opaque string.

**Coverage: 2008 → present** for the bulk index. PTR records appear **2012 →
present** (STOCK Act). `pull` should reject years <2008 with a clear error and warn
that PTRs are absent before 2012.

### 2.2 Report bodies (PDFs — the hard layer) — ✅ VERIFIED

The Clerk publishes report **contents only as PDFs**, addressed by `DocID`.
**Confirmed routing rule — route by `FilingType`, not by DocID:**

```
FilingType == 'P':  https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/<YEAR>/<DocID>.pdf
all other types:    https://disclosures-clerk.house.gov/public_disc/financial-pdfs/<YEAR>/<DocID>.pdf
```

Verified by live probe (2024): e-filed PTR `20024277` → 200 in `ptr-pdfs`; paper
PTR `8220682` → 200 in `ptr-pdfs`, 404 in `financial-pdfs`; e-filed FD `10066961`,
paper FD `8220122`, extension `30022163`, and 4-digit `7940` → all 200 in
`financial-pdfs`.

Each PDF is one of two populations:

- **E-filed** (FDonline / IntelliWorxIT) — generated from form data, **text-based,
  consistent layout → text-extractable** without OCR. This is the v1 target.
- **Scanned / paper** — image-only, sometimes **handwritten → requires OCR**.
  v1 **detects, flags, and skips** these (see §7).

**✅ VERIFIED classifier:** text extraction is the authoritative test and it is
unambiguous — an e-filed page yields ~1,000 chars of real text; a scanned page
yields **0**. The DocID heuristic holds as a fast pre-filter: 8-digit DocIDs
prefixed `1`/`2`/`3`/`4` are e-filed (FD / PTR / extension / staff-FD
respectively, tentatively); 7-digit prefixed `8`/`9` are paper. Use the prefix to
*predict*, use extraction to *decide*.

**Extraction caveats (from real e-filed PDFs):**

- Headings lose their small-cap glyphs — in **two distinct renderings**
  (verified across all 2020–2022 e-filed annual FDs; every document is fully
  one or the other, never mixed):
  - **letters survive, case-mangled** (dominant through 2020): `ScheDule A:` /
    `ScHeDule A:` / `SCheDuLe A:` — sequence intact, case unstable.
  - **glyphs extract as U+0000 NULs** (dominant 2021 onward), one NUL per lost
    glyph: `Schedule A:` extracts as `S\x00{7} A:`. NUL is not regex `\s`, not
    removed by `str.strip()`, and invisible in viewers. The NUL form also hits
    the other small-caps furniture: section titles (`E\x00…` = "Exclusions
    of …"), the `LOCATION:`/`DESCRIPTION:` labels (`L\x00{7}:` / `D\x00{10}:`),
    and the `gfedc`/`gfedcb` checkbox glyphs **vanish from the text layer
    entirely**. Filer-entered content is a regular font and always extracts
    intact — **NULs appear only in form furniture**, which makes a NUL run a
    collision-proof anchor.

  **Match schedule letters by pattern (`S … <LETTER>:`), never by full heading
  text — and accept the NUL-run form (`S\x00+ <LETTER>:`) alongside it.**
- Naive text mode runs columns together (`12/21/202301/08/2024$1,001 - …`) and
  wraps amount ranges across lines. **Row parsing must be layout-aware**
  (pdfplumber positional words/tables), not line-splitting.
- Empty schedules render as the literal line `None disclosed.`

### 2.3 FilingType code table — ✅ VERIFIED for 2024; full enumeration pending

Empirical counts from the live 2024 index — **12 distinct codes**:

| Code | 2024 count | Meaning (working) |
|---|---|---|
| `C` | 660 | Candidate report |
| `X` | 454 | Extension |
| `P` | 451 | Periodic Transaction Report (PTR) — confirmed by PDF inspection |
| `O` | 372 | Annual report ("original") — confirmed by PDF inspection |
| `A` | 97 | Amendment |
| `D` | 70 | unknown — enumerate |
| `W` | 66 | likely withdrawal; carries the empty-field edge cases |
| `H` | 66 | unknown — enumerate |
| `T` | 7 | Termination |
| `B` | 4 | unknown — enumerate |
| `G` | 3 | unknown — enumerate |
| `E` | 1 | unknown — enumerate |

Remaining task (cheap, fold into M2): pull every index 2008→present (~80KB each)
and enumerate the full cross-year code set.

Implementation: parse the raw code, map via a single source-of-truth dict, and
**preserve the raw code** in output so an unrecognized letter never silently drops a
filing.

---

## 3. Command: `openhouse pull`

```
openhouse pull 2024
openhouse pull 2019-2024
openhouse pull 2024 --types ptr
openhouse pull 2024 --index-only
```

**Behavior**, per year in range:

1. Download `<YEAR>FD.zip`; extract `<YEAR>FD.xml` (+ `.txt`) into `raw/<year>/`.
2. Parse the index just enough to enumerate `(DocID, FilingType, year)` targets.
3. Download each referenced PDF into `raw/<year>/<family>/<DocID>.pdf`
   (`family` = `ptr` if `FilingType == 'P'` else `fd` — the §2.2 routing rule),
   unless `--index-only`.
4. Write/update `raw/<year>/pull-manifest.json` (see §6.3).

**Requirements:**

- **Idempotent / resumable.** Skip files already present and size-consistent; a
  re-run only fetches what's missing or changed. Safe to Ctrl-C and resume.
- **Polite crawling.** **Sequential (concurrency 1) with 2.5 s between requests,
  by default.** Grounding: the House publishes no crawl policy (no robots.txt on
  either Clerk domain — verified), there is **no bulk PDF download** (the yearly
  ZIP is index-only), and the nearest published legislative-branch standard is
  congress.gov's `Crawl-delay: 2`; we default just above it. Cost at default:
  ~95 min for 2024 (2,251 filings), ~26 h for the full 2008→present corpus —
  acceptable because `pull` is resumable, chunkable by year, and one-time.
  Plus: exponential backoff on `429`/`5xx` (the server is asking us to retry
  later); a `403` is an **immediate hard error — never retried, no backoff**
  (the server is refusing us; hammering it again is the opposite of polite).
  The error message explains likely causes (UA, pacing) and exits non-zero.
  (✅ verified: a descriptive UA gets clean 200s; keep the polite defaults
  anyway.)
- **User-Agent flow.** A contact is **required**: the operator's **name and
  email**, via `--contact "Jane Doe <jane@example.com>"` or the
  `OPENHOUSE_CONTACT` env var, producing
  `openhouse/<version> (+https://github.com/jswest/openhouse; contact: <Name>
  <email>)`. The contact is mandatory because the repo URL alone is identical for
  every operator — an anonymous shared User-Agent gives the Clerk no way to tell
  concurrent crawlers apart, so it may rate-limit or block all of them at once; a
  real name + email lets an admin reach the actual operator. `pull` refuses to
  crawl (non-zero exit, explanatory message) when the contact is missing or lacks
  a name or email. `--user-agent` overrides the whole header (the caller then owns
  identifying themselves). `pull` logs the UA in use to stderr at startup.
- **No parsing of PDF bodies here.** `pull` only acquires bytes + the index.
- **Progress.** The PDF-download phase shows a per-data-type `tqdm` progress bar
  on stderr (`2024 ptr: …`, then `fd`) with a **measured ETA and rate**, so a
  multi-hour crawl shows how long it has left (and a slow link is reflected); per-
  file chatter is folded into the bar plus an end-of-year summary. Auto-suppressed
  when stderr is not a TTY (piped/logged).
- **Flags:** `--types ptr,fd` (default both), `--index-only`, `--data-dir PATH`
  (default `./data`), `--delay SECONDS` (default 2.5), `--concurrency N`
  (default 1 — exceeding the defaults is a deliberate, documented user choice),
  `--contact "NAME <EMAIL>"` (required unless `--user-agent`), `--user-agent
  STRING`, `--force` (re-download).

---

## 4. Command: `openhouse parse`

```
openhouse parse 2024
openhouse parse 2019-2024
openhouse parse 2024 --types fd
```

**Behavior**, per year in range — entirely offline, reading only `raw/<year>/`:

1. Parse `<YEAR>FD.xml` → one **filing metadata** record per entry (§6.1),
   including the computed `filer_id` (§6.2).
2. **Identity collision check** (§6.2): warn when one `filer_id` is shared by
   what look like two different people in the same year.
3. For each filing with a PDF on disk:
   - **Classify** the PDF: `efiled` (text extractable) / `scanned` (image-only) /
     `missing`. Extraction-yields-text is authoritative; the DocID prefix is a fast
     pre-filter only (§2.2).
   - `efiled` → extract structured fields per form family (§6.1 PTR / FD schedules).
   - `scanned` → record in `parsed/<year>/unparsed-manifest.json`, emit the metadata
     record with `pdf_class: "scanned"` and `body: null`. **No OCR in v1.**
4. Write normalized JSON to `parsed/<year>/` (§6.4 layout).
5. Write `parsed/<year>/parse-manifest.json` with counts, identity warnings, and a
   parse-quality summary.

**Requirements:**

- **Re-runnable from raw** with no network. `parse` never touches the Clerk.
- **Never silently drop a filing.** An unrecognized filing type, an unreadable PDF, or
  an extraction failure produces a record with explicit `pdf_class` / `parse_status`
  and lands in a manifest — not a gap.
- **Validation.** Records validated against the schemas in §6 before write; a
  validation failure marks `parse_status: "error"` with the reason, never a crash that
  loses the year.
- **Progress.** The per-PDF classification phase — the slow part, thousands of PDFs
  in one loop — shows a `tqdm` progress bar on stderr per year (`2020 FD/PTR`) with
  count / total / rate / ETA, so a long-but-healthy run is distinguishable from a
  hang. Purely cosmetic: never enters a manifest or stdout, and auto-suppressed when
  stderr is not a TTY (piped/logged).
- **Flags:** `--types`, `--data-dir`, `--strict` (exit non-zero if any filing errors).

---

## 5. Command: `openhouse read`

The query surface. Offline, read-only, a pure function over `parsed/` — never
touches `raw/` or the network, never writes anything. **No database**: at this
scale (~2,250 filings and low-tens-of-thousands of transactions per year), scanning
the JSON in place is milliseconds, and skipping a load step means `read` can never
disagree with the last `parse`. (If cross-year analytics ever get heavy, DuckDB can
query the JSON files where they sit — still no load step.)

```
openhouse read filings 2024 --type ptr --member adams       # filtered filing index
openhouse read filing 20024277                              # one filing: metadata + body
openhouse read trades 2019-2024 --ticker ALB --owner SP     # flattened transactions across years
openhouse read summary 2024                                 # counts: types, efiled/scanned, errors, warnings
```

**Subcommands:**

| Subcommand | Input | Output |
|---|---|---|
| `filings <range>` | filters: `--type`, `--member`, `--state`, `--since/--until` | matching filing-metadata records |
| `filing <doc_id>` | a DocID | that filing's metadata + body (if parsed) |
| `trades <range>` | filters: `--ticker`, `--member`, `--owner`, `--type P\|S`, `--since/--until`, `--min-amount` | PTR transactions flattened across the range, each with its filer attached |
| `summary <range>` | — | per-year roll-up from the manifests |

**Requirements:**

- **Output:** JSON to stdout by default (the machine/agent contract, `jq`-composable);
  `--table` renders a human-readable aligned table instead. Prose/progress to stderr,
  as everywhere.
- **`--member` matching:** case-insensitive substring match against both `filer_id`
  and the raw name fields. Document plainly that this is name-string matching, not
  true identity (§6.2).
- **Missing years degrade gracefully:** a range where some years aren't parsed yet
  reports which years were skipped (stderr) and answers from the rest.
- **`trades <range>` range = filing year:** the range selects **filing** years,
  not transaction years; transactions routinely predate the filing (a Dec-2020
  trade in a 2021 filing). Widen the range when bounding by transaction date.
- **Flags:** `--data-dir`, `--table`.

---

## 5.5 Command: `openhouse inspect`

The accuracy-review surface. `pull`/`parse`/`read` move filings *through* the
pipeline; `inspect` asks whether the filings that came out are **right**. It
samples `parse_status: ok` filings, opens a small local web app, and shows each
filing beside its source PDF so a human can record a precision/recall verdict.
Offline and deterministic like `parse`/`read` — the only socket is the operator's
browser hitting `127.0.0.1`; no new Python deps (stdlib `http.server` +
`pdfplumber`).

```
openhouse inspect 2022 --sample 0.05                 # review a stratified ~5% of 2022
openhouse inspect 2022 --sample 0.1 --seed 7         # a different reproducible draw
```

**Why it exists:** a `parse_status: ok` filing can still be wrong — most visibly
the scanned PTRs that extract **zero** trades while the PDF plainly has them.
Those are silent recall failures (the "never silently drop a filing" hazard) and
nothing else surfaces them.

**Sampling** (in `inspect/core.py`, pure):

- `--sample` is a fraction `(0, 1]` of the year's **reviewable** filings
  (`parse_status: ok`; error/unparsed are already manifest residual).
- A seeded `hash(seed, doc_id)` rank makes selection **reproducible** (same
  args → same set; no wall-clock) and **monotonic** (`0.2 ⊃ 0.1`, so a sample can
  be widened later without re-reviewing).
- **Stratified** by `pdf_class` × is-PTR: within each stratum the smallest
  `ceil(sample · n)` by rank are taken, so every non-empty cell (notably scanned
  PTRs) is represented for any `sample > 0`.

**Verdict** (filing mode — the only mode this milestone; trade mode is deferred):
a 2×2 over the sound/complete duality, applied to **entries** (PTR trades / FD
line items) and **metadata** (the index-derived scalars), plus optional entry
magnitudes and a note, snapshot-pinned:

| Field | Meaning |
|---|---|
| `is_fully_precise` | entries sound — nothing hallucinated/wrong |
| `is_fully_recalled` | entries complete — nothing in the PDF missed |
| `n_incorrect_entries` / `n_missing_entries` | optional magnitude; `null` = untallied |
| `is_metadata_accurate` / `is_metadata_fully_complete` | the same pair for metadata |
| `snapshot` | `sha256:…` of the parsed record at review time |
| `note` | ground truth for scanned cases + correction-agent handoff hint |

Invariant (enforced server-side): a tallied count agrees with its boolean —
`count > 0 ⟺ boolean false`; `null` always allowed ("wrong, didn't tally").
Entries carry counts (unbounded list — *how many* missed is signal); metadata
does not (a fixed handful of scalars).

**Output & storage:**

- Verdicts persist to a gitignored, `doc_id`-keyed
  `data/inspect/<year>/labels.json`, **resumable** across restarts. Its
  `LABELS_SCHEMA_VERSION` is independent of the parsed-data `SCHEMA_VERSION`: a
  verdict-schema change must not force a re-parse. The `snapshot` survives a
  re-parse — a filing whose parse later changes has its label flagged **stale**
  (caught on reload, never silently blessing changed output).
- The **scorecard** is emitted as JSON to **stdout** when the operator stops the
  server (Ctrl-C): per-stratum **doc-level** precision/recall rates *and* an
  **entry-level** rollup (`Σ n_missing` / `Σ n_incorrect`) — the number that says
  *where* the parser leaks. The reviewable/not-reviewable residual goes to stderr.

**The web surface:** a stdlib `http.server` serves the committed static bundle, a
small JSON API (`GET /api/queue`, `GET /api/filing/<doc_id>`,
`POST /api/verdict/<doc_id>`), and sandboxed PDF bytes (`GET /api/pdf/<doc_id>`,
derived from the record's `source_pdf`, confirmed inside `data/`). The frontend is
**Svelte compiled to a static SPA via Vite (not SvelteKit)**; its built bundle is
committed under `openhouse/inspect/static/`, so `inspect` runs with **zero Node**.
`web/inspect/` holds the contributor-only source (`npm run build` regenerates the
bundle).

**Flags:** `--sample` (required), `--mode` (`filing`), `--seed` (default `0`),
`--data-dir`.

---

## 6. Data model & on-disk layout

### 6.1 Record shapes (normalized JSON)

**Filing metadata** (always present, from the index):

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

Nullability (all observed in real data): `state_district` may be `null` (empty
`StateDst`); `filing_date` may be `null`; `filer.prefix`/`filer.suffix` usually
null. `state_district.state` is any 2-letter postal code including `DC`, `PR`,
and other territories; `district` is an int with `0` = at-large/n.a.

### 6.2 Member identity: `filer_id`

The Clerk index has **no member ID** — only name strings, which vary across years
("Alma Shealey Adams" vs "Alma S. Adams"). v1 mitigates with a normalized key that
gets *close* to dedup without pretending to be identity:

```
filer_id = lower(state) "." slug(Last) "." slug(first_token(First)) ["." slug(Suffix)]
```

- `slug()` lowercases, strips punctuation and diacritics, collapses whitespace to `-`.
- Only the **first token** of `First` participates (middle names/initials are the
  main source of cross-year variation).
- `Suffix` is included when present — it's exactly what distinguishes the
  father/son same-name case (Jr/Sr).
- Empty `StateDst` → state segment `unk`.

**Collision warning (in `parse`):** the same person filing many times per year is
normal (PTRs + annual + extension all share a `filer_id` — that's the point). The
signals that one `filer_id` may cover **two different people** are:

- the same `filer_id` appears with **different districts** within one year, or
- raw names at the same `filer_id` differ by **suffix** or **last name** after
  normalization (i.e. the slug collided rather than matched).

`parse` emits each such case as a warning on stderr and records it under
`identity_warnings` in `parse-manifest.json` (with the colliding raw names and
DocIDs), so `read --member` users know when a name is ambiguous. True identity
resolution (bioguide ID join) is a post-v1 enrichment.

### 6.3 Bodies

**PTR body** — `transactions[]`:

```json
{
  "owner": "SP",                      // SP | DC | JT | self  (spouse/dependent/joint/self)
  "asset": "Albemarle Corporation",
  "ticker": "ALB",
  "asset_type": "ST",                 // the bracketed tag, e.g. [ST] stock — preserve raw
  "transaction_type": "P",            // P purchase | S sale | S(partial) | E exchange
  "transaction_date": "2023-12-21",
  "notification_date": "2024-01-08",
  "amount_range": { "low": 1001, "high": 15000, "label": "$1,001 - $15,000" },
  "cap_gains_over_200": false,
  "description": null
}
```

**Annual FD body** — schedules (line-item arrays; keys present only when the schedule
has data). ✅ VERIFIED: live e-filed FDs carry these labels (A–F observed directly;
empty schedules render `None disclosed.`):

| Schedule | Contents |
|---|---|
| A | Assets & "unearned" income |
| B | Transactions |
| C | Earned income |
| D | Liabilities |
| E | Positions |
| F | Agreements |
| G | Gifts |
| H | Travel payments/reimbursements |
| I | Payments in lieu of honoraria |
| J | Compensation in excess of $5,000 (new filers) |

Each line item is a flat object with the schedule's columns plus a verbatim
`raw_text` fallback so nothing extracted is lost to schema gaps. v1 depth-orders
the work: **A–D fully structured first; E–J may ship as `raw_text`-only line items**
without violating acceptance (§11).

### 6.4 Directory layout

```
<data-dir>/
  raw/
    <year>/
      <year>FD.xml
      <year>FD.txt
      pull-manifest.json
      ptr/<DocID>.pdf
      fd/<DocID>.pdf
  parsed/
    <year>/
      filings.json            # array of filing-metadata records (the index, normalized)
      ptr/<DocID>.json        # one file per PTR body
      fd/<DocID>.json         # one file per annual-FD body
      parse-manifest.json
      unparsed-manifest.json  # scanned/skipped + error filings, with reasons
```

> One file per filing body keeps `parse` incremental and diffs readable; `filings.json`
> is the single roll-up index for the year. ✅ Sized: ~2,250 filings in 2024 —
> thousands of small JSONs per year is fine.

### 6.5 Manifests

- `pull-manifest.json` — per DocID: URL, HTTP status, byte size, sha256, fetched-at
  (timestamp injected by the tool, since scripts have no clock — see §9).
- `parse-manifest.json` — counts by filing type, `efiled` vs `scanned` vs `missing`,
  ok vs error, `identity_warnings` (§6.2), and the schema version used (the integer
  parsed-schema generation — the minor of `v0.<gen>.<patch>`; see GH-0037).
- `unparsed-manifest.json` — every filing not fully parsed, with `doc_id`,
  `filer_id` (so a no-DocID row stays joinable), and a `reason`
  (`scanned`, `missing`, `extract_failed`, `unknown_type`, `validation_error`) for a
  clean OCR/follow-up backlog. (`missing` = the index lists the filing but no PDF is
  on disk; `validation_error` is reserved — no emit path in v0.2.0.)

---

## 7. Deferred: OCR (post-v1 milestone)

Scanned/handwritten PDFs are **detected and catalogued** in v1, never parsed. The
deferred milestone adds an OCR path (likely: rasterize → OCR engine → the same
schedule/transaction extractors), consuming `unparsed-manifest.json` as its work list.
Keeping the classifier and manifest in v1 means OCR slots in without reshaping the
pipeline.

---

## 8. Deferred: agent skill + `openhouse ready` (post-v1 milestone)

After v1 acceptance, openhouse gets a Claude Code skill, following the **bartleby
pattern** (`~/Code/spot/bartleby`, `bartleby/commands/ready.py`):

- **Skill prose ships as package data** — `openhouse/skill/SKILL.md` (short: how to
  invoke the three commands, where the JSON lands, the §1 legal restriction) plus
  `openhouse/skill/reference.md` (record schemas, FilingType table, query recipes).
  **No code ever lives in the skill directory.**
- **`openhouse ready`** stamps the skill into `~/.claude/skills/openhouse`, cribbed
  near-verbatim from bartleby's: wipe-and-copy install, a hidden marker recording
  the producing version plus a **content hash** over the skill files, and `--check`
  to report up-to-date / stale / hand-edited drift. Releasing a change = run the
  tests, run `openhouse ready`.
- The skill's verbs are just §5's `read` subcommands (plus `pull`/`parse`) — the
  CLI is the entire interface, which is what keeps SKILL.md thin.
- Not cribbed: bartleby's `skill_runner`/dispatch layer. Three CLI verbs don't
  warrant it.

Rationale: the repo stays the only source of truth forever; the skill folder is
build output. Stamped (rather than pointing at the checkout) so parallel agent
sessions run the last *blessed* version, not whatever mid-refactor state HEAD is in.

---

## 9. Stack & conventions

- **Python 3.12+, managed with `uv`** (`uv run python`, `uv run pytest`) — consistent
  with the author's other projects.
- Suggested libs (not binding): `httpx` (download), stdlib `xml.etree` or `lxml`
  (index), `pdfplumber` (layout-aware extraction — required by the §2.2 caveats;
  `pypdf` acceptable for classification), `pydantic` (schemas/validation), `typer`
  or stdlib `argparse` (CLI). `--table` rendering: stdlib formatting is fine.
- **JSON to stdout for machine output; prose/progress to stderr.** Non-zero exit on
  error.
- **No wall-clock in core logic** beyond an explicit timestamp captured once at command
  entry and threaded into manifests (keeps `parse` deterministic and testable).
- **Year-range parsing** is shared by all commands: `YYYY` or `YYYY-YYYY`, validated
  against `[2008, current_year]`, inclusive.

### Proposed project layout

```
openhouse/
  SPEC.md            (this file)
  pyproject.toml
  openhouse/
    __init__.py
    cli.py           # arg parsing, range parsing, dispatch
    pull.py          # acquisition
    parse.py         # transformation
    read.py          # query surface (§5)
    index.py         # XML index → filing-metadata records (incl. filer_id)
    pdf.py           # classify (efiled/scanned) + text extraction
    schemas.py       # pydantic models + filing-type code table
    skill/           # post-v1: SKILL.md + reference.md as package data (§8)
  tests/
    fixtures/        # checked-in sample PDFs + trimmed index XML
  data/              # gitignored; default --data-dir
```

---

## 10. Verification log & remaining questions

All §8-era open questions were probed against live Clerk data on **2026-06-11**
(2024 index + sample PDFs `20024277` e-filed PTR, `8220682` paper PTR, `10066961`
e-filed FD, `8220122` paper FD, `30022163` extension, `7940` type-W):

| # | Question | Status |
|---|---|---|
| 1 | FilingType code table | ✅ 12 codes enumerated for 2024 (§2.3); cross-year enumeration folded into M2 |
| 2 | PDF URL patterns | ✅ Resolved: route by FilingType (§2.2) |
| 3 | E-filed vs scanned classifier | ✅ Text-extraction test authoritative and unambiguous; DocID prefix = pre-filter |
| 4 | Schedule A–J labels | ✅ Confirmed on live e-filed FD; small-caps caveat noted (§2.2) |
| 5 | 403 handling | ✅ Non-issue with descriptive User-Agent; polite defaults retained |
| 6 | Per-year file volume | ✅ ~2,250 filings/2024; one-JSON-per-body stands |

**Still open (none blocking):**

1. Full FilingType enumeration across 2008→present (cheap; during M2).
2. Meanings of codes `D`, `W`, `H`, `B`, `G`, `E` — enumerate, spot-check PDFs.
3. Extent of cross-year name variation → how well `filer_id` dedups in practice
   (measure during M7; bioguide join is the post-v1 fix).
4. Older years (2008–2011) may diverge in PDF layout/URL details — verify when
   first pulled.

---

## 11. v1 acceptance (definition of done)

- `openhouse pull 2024` downloads the index + all e-filed PTR and FD PDFs, resumably,
  with a complete `pull-manifest.json`.
- `openhouse parse 2024` produces `filings.json` plus per-body JSON for every e-filed
  filing, with scanned filings catalogued (not parsed) in `unparsed-manifest.json`,
  and identity warnings surfaced per §6.2.
- `openhouse read trades 2024 --ticker <X>` answers from parsed data alone — no
  network, no opening PDFs by hand — in both JSON and `--table` form.
- A multi-year range (`2019-2024`) works for all three commands.
- Re-running any command is idempotent; `parse` and `read` need no network.
- Test suite covers: range parsing, index→metadata mapping (including the §2.1 edge
  cases), `filer_id` + collision warnings, PTR extraction, FD-schedule extraction,
  efiled/scanned classification, and `read` filters — against checked-in PDF fixtures.

---

## 12. Build plan

**Phase 1 — make it work (v1):**

| M | Scope |
|---|---|
| M1 | Scaffold: pyproject/uv, package layout, year-range parser, schemas with §2.1 edge cases |
| M2 | `pull` index-only + full FilingType enumeration 2008→present |
| M3 | `pull` PDFs: routing, politeness, resumability, manifest |
| M4 | `parse`: metadata + `filer_id` + classifier + manifests |
| M5 | PTR body extraction (+ `read filings/filing/summary` light up) |
| M6 | FD schedules: A–D structured, E–J raw_text (+ `read trades` complete) |
| M7 | Acceptance pass (§11); promote samples to `tests/fixtures/` |

**Phase 2 — make it shippable, then grow:**

| M | Scope |
|---|---|
| M8 | Skill prose + `openhouse ready` (§8) |
| M9+ | OCR backlog (§7), bioguide identity join, deeper E–J structure, distribution (PyPI/plugin) if ever wanted |
