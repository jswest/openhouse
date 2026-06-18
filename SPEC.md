# openhouse — SPEC

A standalone tool that pulls, parses, and queries **U.S. House of Representatives
financial disclosure** filings from the Office of the Clerk.

The CLI is **source-scoped** (#174): a source noun (`clerk` / `fec`) sits above
the pipeline verbs, so today's House-Clerk pipeline lives under `clerk`. (`fec`
is scaffolded — verbs are stubs until #167.) The tool-level `ready` (and the
repo-local `release` skill) stay top-level, not under a source.

```
openhouse clerk pull  <year | year_start-year_end>   # acquire raw artifacts from the Clerk (network)
openhouse clerk parse <year | year_start-year_end>   # transform raw artifacts → normalized JSON (offline)
openhouse clerk read  <subcommand> [args]            # query the normalized JSON (offline, read-only)
openhouse clerk inspect <year> --sample <f>          # human accuracy review in a local web app (offline)
openhouse ready                                       # install the agent skill (offline, top-level)
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
  - **glyphs extract as U+0000 NULs** (dominant 2021 onward for annual FDs;
    PTRs cut over around **2022-04**), one NUL per lost
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
- The case-mangling hits **content tokens too** (GH-0070): owner columns render
  `Sp`/`Jt`/`Dc`, asset-type tags `[oT]`, and the `None`/`Undetermined`/`Over`
  value literals lowercase unpredictably. Matchers for those tokens must be
  case-insensitive; captured owner tokens normalize to upper-case (the
  `[TYPE]` tag value stays preserved raw, as ever).
- **Candidate/New-Filer reports use a different Schedule A table** (GH-0070):
  no "Tx. > $1,000?" checkbox column (no `gfedc` glyph anywhere, in any
  rendering) and **three** amount columns — value, income *current year to
  filing*, income *preceding year*. Anything keyed on the checkbox glyph as a
  row gate matches zero rows on these forms.
- **Row anchoring must be a disjunction of column signatures, never one
  gate** (GH-0070): the `[TYPE]` tag and the checkbox glyph routinely land on
  *different* physical lines (long names wrap either one off the row line); a
  subholding arrow `⇒` can land on a *continuation* line among wrapped high
  bounds (a bare arrow is NOT a row anchor — require the value/type column
  right after it); Schedule B's Date column can hold a periodicity word
  (`Semi-Annually`) instead of a date; D/F dates can be a **bare year**
  (`2019`). Verified across 2020–2024: the pre-GH-0070 anchors silently merged whole
  schedules on ~19% of parsed FDs and every post-2022-04 PTR.
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

## 3. Command: `openhouse clerk pull`

The CLI is **source-scoped** (#174): a source noun (`clerk` / `fec`) sits above
the pipeline verbs, so the House-Clerk acquisition path is `openhouse clerk pull`.
(`fec` is scaffolded but not implemented — see #167; the FEC raw lane reserves
`raw/fec/<cycle>/`.) The bare pre-namespace form (`openhouse pull …`) is removed.

```
openhouse clerk pull 2024
openhouse clerk pull 2019-2024
openhouse clerk pull 2024 --types ptr
openhouse clerk pull 2024 --index-only
```

**Behavior**, per year in range:

1. Download `<YEAR>FD.zip`; extract `<YEAR>FD.xml` (+ `.txt`) into `raw/clerk/<year>/`.
2. Parse the index just enough to enumerate `(DocID, FilingType, year)` targets.
3. Download each referenced PDF into `raw/clerk/<year>/<family>/<DocID>.pdf`
   (`family` = `ptr` if `FilingType == 'P'` else `fd` — the §2.2 routing rule),
   unless `--index-only`.
4. Write/update `raw/clerk/<year>/pull-manifest.json` (see §6.3).

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
  (default `~/.openhouse`), `--delay SECONDS` (default 2.5), `--concurrency N`
  (default 1 — exceeding the defaults is a deliberate, documented user choice),
  `--contact "NAME <EMAIL>"` (required unless `--user-agent`), `--user-agent
  STRING`, `--force` (re-download).

---

## 4. Command: `openhouse clerk parse`

```
openhouse clerk parse 2024
openhouse clerk parse 2019-2024
openhouse clerk parse 2024 --types fd
```

**Behavior**, per year in range — entirely offline, reading only `raw/clerk/<year>/`:

1. Parse `<YEAR>FD.xml` → one **filing metadata** record per entry (§6.1),
   including the computed `filer_id` (§6.2).
2. **Identity collision check** (§6.2): warn when one `filer_id` is shared by
   what look like two different people in the same year.
3. For each filing with a PDF on disk:
   - **Classify** the PDF: `efiled` (text extractable) / `scanned` (image-only) /
     `missing`. Extraction-yields-text is authoritative; the DocID prefix is a fast
     pre-filter only (§2.2).
   - `efiled` → extract structured fields per form family (§6.1 PTR / FD schedules).
   - `scanned` → record in `parsed/clerk/<year>/unparsed-manifest.json`, emit the
     metadata record with `pdf_class: "scanned"` and `body: null`. **No OCR in v1.**
4. Write normalized JSON to `parsed/clerk/<year>/` (§6.4 layout).
5. Write `parsed/clerk/<year>/parse-manifest.json` with counts, identity warnings, and a
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

## 5. Command: `openhouse clerk read`

The query surface. Offline, read-only, a pure function over `parsed/clerk/` —
never touches `raw/` or the network, never writes anything. **No database**: at this
scale (~2,250 filings and low-tens-of-thousands of transactions per year), scanning
the JSON in place is milliseconds, and skipping a load step means `read` can never
disagree with the last `parse`. (If cross-year analytics ever get heavy, DuckDB can
query the JSON files where they sit — still no load step.)

```
openhouse clerk read filings 2024 --type ptr --member adams       # filtered filing index
openhouse clerk read filing 20024277                              # one filing: metadata + body
openhouse clerk read trades 2019-2024 --ticker ALB --owner SP     # flattened transactions across years
openhouse clerk read summary 2024                                 # counts: types, efiled/scanned, errors, warnings
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

## 5.5 Command: `openhouse clerk inspect`

The accuracy-review surface. `pull`/`parse`/`read` move filings *through* the
pipeline; `inspect` asks whether the filings that came out are **right**. It
samples `parse_status: ok` filings, opens a small local web app, and shows each
filing beside its source PDF so a human can record a precision/recall verdict.
Offline and deterministic like `parse`/`read` — the only socket is the operator's
browser hitting `127.0.0.1`; no new Python deps (stdlib `http.server` +
`pdfplumber`).

```
openhouse clerk inspect 2022 --sample 0.05                 # review a stratified ~5% of 2022
openhouse clerk inspect 2022 --sample 0.1 --seed 7         # a different reproducible draw
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
  "source_pdf": "raw/clerk/2024/ptr/20024277.pdf",
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
("Alma Shealey Adams" vs "Alma S. Adams"). `filer_id` is a **two-tier identity
ladder** (#16); `parse` records which tier it used and exposes the matched
bioguide on a `bioguide_id` field:

1. **`bioguide:<id>`** — the filer's House seat (normalized last name + state +
   district) matched a single record in the CC0
   `@unitedstates/congress-legislators` bulk files. A stable identity across
   years and name spellings. The match is conservative: a seat that resolves to
   two legislators matches *nothing* (no false positive), and no bioguide is ever
   synthesized. The two bulk files are fetched once by `pull` into
   `raw/reference/` — the **single declared exception** to "`pull` is the only
   network step" (CC0, so outside the Clerk use restriction) — and joined
   **offline** by `parse`. `pull --no-reference` skips the fetch. **Candidate
   reports (FilingType `C`) are excluded from the seat join**: a candidate filing
   is made by someone *running* for the seat (incumbent or challenger), and the
   seat key alone can't tell a name-colliding challenger from the real member, so
   candidates fall back to the `name:` key rather than risk a false-positive
   bioguide (sound over complete; an incumbent stays bioguide-identified via their
   non-candidate filings).

   **Reference source URL — ✅ VERIFIED 2026-06-13 (#75).** The two CC0 JSON bulk
   files live at the project's gh-pages mirror (HTTP 200):

   - `https://unitedstates.github.io/congress-legislators/legislators-current.json`
   - `https://unitedstates.github.io/congress-legislators/legislators-historical.json`

   The former `raw.githubusercontent.com/unitedstates/congress-legislators/main/`
   path now **404s** and the legacy `theunitedstates.io` distribution is **410
   Gone**; the gh-pages mirror is the live successor. The files are still JSON
   (the existing parser is unchanged — **no YAML dependency**). The fetch is
   **non-fatal**: if it fails after the normal retry/backoff, `pull` warns to
   stderr and continues with **no** bioguide enrichment (identical to
   `--no-reference`), rather than aborting before any disclosure PDFs download —
   identity is optional enrichment, never a gate. If upstream moves again, update
   `LEGISLATORS_URL_TEMPLATE` in `pull.py` and this note together.
2. **`name:<name_key>`** — the last resort when no seat matched. The key is the
   old normalized slug:

```
name_key = lower(state) "." slug(Last) "." slug(first_token(First)) ["." slug(Suffix)]
```

- `slug()` lowercases, strips punctuation and diacritics, collapses whitespace to `-`.
- Only the **first token** of `First` participates (middle names/initials are the
  main source of cross-year variation).
- `Suffix` is included when present — it's exactly what distinguishes the
  father/son same-name case (Jr/Sr).
- Empty `StateDst` → state segment `unk`.

A `name:` key is a **bounded, unverified name-string claim**, not an identity:
two different people can share one. The middle `fec:` tier was considered and
**rejected** as scope creep — the ladder is bioguide-or-name, nothing between.

**Identity report (in `parse`, GH-0122):** the matcher fails for several
reasons, most of them *expected by design* — so a per-name warning for every
unmatched filer (the pre-0122 behavior) buried the one case that matters. `parse`
emits one `identity_warnings` entry per distinct `name:`-keyed `filer_id` (those
that matched **no** bioguide), carrying its distinct raw names, DocIDs, districts,
and a classified `reason`:

- `candidate` — a `FilingType C` report (a challenger must not be pinned to the
  incumbent — §6.2); demoted by design, expected.
- `no_district` — no `StateDst`, so no seat key was even possible.
- `unknown_seat` — a valid seat no rep in the reference roster ever held (a
  delegate/territory we don't index, a new district, or a data gap).
- `ambiguous_seat` — the exact seat key is on record but nulled (two bioguides
  share it; we declined to guess).
- `suspicious` — the seat **is** occupied by a known rep, but this filer's last
  name didn't match it: a likely name variant/typo or roster gap. The one
  actionable bucket. Its entry also carries the occupied `seats[]` and their
  roster `holders` (`{bioguide, last}`).

To stderr `parse` prints **two tiers**: one collapsed summary line per year
(`<year>: identity — N matched, M unmatched (… per reason)`) plus a per-name
`SUSPICIOUS` line for the `suspicious` filers **only**. A `bioguide:`-matched
filer is never listed (pinned to a real member, however many times it filed).

The classification needs no fuzzy matching — only an occupancy index
(`(state, district) → holders`) beside the exact seat join (§6.2), answering "is
anyone on record for this seat?". The exact join is unchanged (still no false
positives).

### 6.3 Bodies

**PTR body** — `transactions[]`:

```json
{
  "owner": "SP",                      // SP | DC | JT | self  (spouse/dependent/joint/self)
  "asset": "Albemarle Corporation",
  "ticker": "ALB",
  "asset_type": "ST",                 // bracketed [TYPE] tag, NORMALIZED (uppercased, trimmed)
  "asset_type_raw": "sT",             // the same tag VERBATIM (Clerk casing drifts: sT/Cs/gS) — #114
  "transaction_type": "P",            // P purchase | S sale | S(partial) | E exchange
  "transaction_date": "2023-12-21",  // null if outside 1990..entry_year+1; raw kept on date_raw — #113
  "notification_date": "2024-01-08",  // null if out of range; raw kept on notification_date_raw — #113
  "amount_range": { "low": 1001, "high": 15000, "label": "$1,001 - $15,000" },
  "cap_gains_over_200": false,       // true | false | null — null = UNKNOWN (see below)
  "description": null
}
```

`amount_range` is normally a `{low, high, label}` bucket. A minority of real rows
disclose a single **exact-dollar** value instead (e.g. `$894.97`); those serialize
as `{ "exact": 894.97, "label": "$894.97" }` — a sound *point*, never coerced into
a fake `low == high` range (#49). The two shapes are mutually exclusive; `read`'s
amount filters treat an exact value `X` as the closed point `[X, X]`. A row that is
neither a published bucket nor an exact value still fails loudly (`extract_failed`
in the unparsed manifest) rather than fabricating a range.

`cap_gains_over_200` is `null` when the state is **unrecoverable**: PTRs hit the
§2.2 glyphs-lost (NUL) rendering around **2022-04**, and in that rendering the
`gfedc`/`gfedcb` checkbox glyphs vanish from the text layer entirely — there is
nothing to read, so the field records *unknown*, never a fabricated boolean
(same treatment as the annual-FD Schedule B checkbox).

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
`raw_text` fallback so nothing extracted is lost to schema gaps. As of **v0.5.0**
all schedules **A–J carry structured columns** (#17 added per-schedule E–J column
extraction — E position/organization, F date, G source/value, H source/dates, I
source/activity/amount, J source/description), each line item still carrying the
verbatim `raw_text` so a low-confidence column split loses nothing. (Earlier the
plan allowed E–J to ship as `raw_text`-only; that is now done.)

Schedule A items carry `income_preceding` (GH-0070): the Candidate/New-Filer
form variant's third amount column ("income preceding year"); `null` on member
annual forms, which have no such column. A value column holding the literal
`None`/`Undetermined` yields `value_of_asset: null` with the amount buckets
shifted to the income columns — never a bucket misread as the asset value.

Schedules A and B are **guarded for completeness** (GH-0070): each row carries
one `[TYPE]` tag, so the segment's tag count approximates a row count
independent of the row anchors. The invariant drifts by a row or two on ~30%
of real documents (tag-less rows, brackets in filer text), so the guard fires
only on a **total collapse** (one anchored item where the tags say ≥3 rows) or
a **severe merge** (half or fewer of the ≥4 tag-counted rows anchored) — those become
`extract_failed` in the unparsed manifest rather than a plausible-but-wrong
body with `parse_status: "ok"`.

### 6.4 Directory layout

The layout is **source-scoped** (#174): each source owns a `<source>/` level
under both `raw/` and `parsed/`. The clerk pipeline writes under `clerk/`;
`raw/fec/<cycle>/` + `parsed/fec/<cycle>/` are **reserved** for the FEC lane
(cross-ref #167) but no FEC code path creates them yet. The CC0
congress-legislators reference set stays at the un-scoped `raw/reference/` — it
is shared bulk data, not a source's disclosures, so it is not relocated.

```
<data-dir>/
  raw/
    clerk/
      <year>/
        <year>FD.xml
        <year>FD.txt
        pull-manifest.json
        ptr/<DocID>.pdf
        fd/<DocID>.pdf
    fec/<cycle>/            # reserved for the FEC lane (#167) — not yet created
    reference/              # CC0 congress-legislators bulk files (un-scoped, shared)
  parsed/
    clerk/
      <year>/
        filings.json            # array of filing-metadata records (the index, normalized)
        ptr/<DocID>.json        # one file per PTR body
        fd/<DocID>.json         # one file per annual-FD body
        parse-manifest.json
        unparsed-manifest.json  # scanned/skipped + error filings, with reasons
    fec/<cycle>/           # reserved for the FEC lane (#167) — not yet created
```

> One file per filing body keeps `parse` incremental and diffs readable; `filings.json`
> is the single roll-up index for the year. ✅ Sized: ~2,250 filings in 2024 —
> thousands of small JSONs per year is fine.

The data root (`<data-dir>`) resolves with a uniform precedence across all
commands (#50): the explicit `--data-dir` flag → the `OPENHOUSE_DATA_DIR`
environment variable → the `~/.openhouse` default (#80, a single per-user
dotfolder in `$HOME`, expanded via `Path.home()`). Because the default is
home-relative rather than cwd-relative, an agent invoking openhouse from any
working directory lands in one stable store — no separate empty `./data` island
per cwd. When the default is in use and a non-empty `./data` exists in the cwd
(a leftover from before #80), openhouse prints a one-time stderr note that it is
being shadowed; it does not auto-migrate or read from `./data`.

**Migrating from the pre-namespace layout (#174).** A store created before the
source namespace has bare year dirs directly under `raw/` and `parsed/`. The
migration is a one-time **offline `mv`** — relocating bytes, not re-crawling:

```
mv ~/.openhouse/raw/<year>    ~/.openhouse/raw/clerk/<year>
mv ~/.openhouse/parsed/<year> ~/.openhouse/parsed/clerk/<year>
```

When openhouse detects a legacy `raw/<YYYY>/` it prints this `mv` once to stderr
as a **nudge** — it never relocates data itself (same spirit as the `./data`
shadow note above). After the `mv`, re-run `openhouse clerk parse <year>`: the
move relocates `filings.json` but not the `source_pdf` path each record embeds
(it still reads `raw/<year>/…`), and the schema-generation bump (9→10, §6.5)
makes `read`'s schema-drift warning surface the stale tree until the re-parse
refreshes those paths.

### 6.5 Manifests

- `pull-manifest.json` — per DocID: URL, HTTP status, byte size, sha256, fetched-at
  (timestamp injected by the tool, since scripts have no clock — see §9).
- `parse-manifest.json` — counts by filing type, `efiled` vs `scanned` vs `missing`,
  ok vs error, `identity_warnings` (§6.2 — the complete per-filer record, each with
  its classified `reason`), a `match_summary` (GH-0122: identity-level
  `matched` / `unmatched` / `by_reason` breakdown / `suspicious` filer_id list — the
  full seat detail stays on the `identity_warnings` entries, not duplicated here),
  and the schema version used (the integer parsed-schema generation — the minor of
  `v0.<gen>.<patch>`; see GH-0037). `match_summary` is manifest-only diagnostics
  (`read` doesn't consume it), so adding it did **not** bump the schema version —
  `filings.json` records are byte-identical.
- `unparsed-manifest.json` — every filing not fully parsed, with `doc_id`,
  `filer_id` (so a no-DocID row stays joinable), and a `reason`
  (`scanned`, `missing`, `extract_failed`, `unknown_type`, `validation_error`,
  `date_out_of_range`) for a clean OCR/follow-up backlog. (`missing` = the index
  lists the filing but no PDF is on disk; `validation_error` is reserved — no emit
  path in v0.2.0; `date_out_of_range` is the one reason that coexists with a fully
  written body and `parse_status: ok` — a row's date fell outside 1990..entry_year+1,
  so it was flagged in place rather than dropped (§6.3, #113).)

---

## 7. Deferred: OCR (post-v1 milestone)

Scanned/handwritten PDFs are **detected and catalogued** in v1, never parsed. The
deferred milestone adds an OCR path (likely: rasterize → OCR engine → the same
schedule/transaction extractors), consuming `unparsed-manifest.json` as its work list.
Keeping the classifier and manifest in v1 means OCR slots in without reshaping the
pipeline.

---

## 8. Agent skill + `openhouse ready` — ✅ DELIVERED v0.5.0 (#14)

openhouse ships a Claude Code skill, following the **bartleby pattern**
(`~/Code/spot/bartleby`, `bartleby/commands/ready.py`):

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
    legislators.py   # offline CC0 congress-legislators seat→bioguide join (§6.2)
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
3. Extent of cross-year name variation → how well `filer_id` dedups in practice.
   ✅ The bioguide identity join (the durable fix) shipped in **v0.5.0** (#16);
   `filer_id` is now `bioguide:`-keyed where a House seat matches, `name:` only as
   a last resort.
4. Older years (2008–2011) may diverge in PDF layout/URL details — verify when
   first pulled.

---

## 11. v1 acceptance (definition of done)

- `openhouse clerk pull 2024` downloads the index + all e-filed PTR and FD PDFs,
  resumably, with a complete `pull-manifest.json`.
- `openhouse clerk parse 2024` produces `filings.json` plus per-body JSON for every
  e-filed filing, with scanned filings catalogued (not parsed) in
  `unparsed-manifest.json`, and identity warnings surfaced per §6.2.
- `openhouse clerk read trades 2024 --ticker <X>` answers from parsed data alone — no
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
| M8 | Skill prose + `openhouse ready` (§8) — ✅ delivered v0.5.0 (#14) |
| M9+ | OCR backlog (§7, still deferred). ✅ delivered v0.5.0: bioguide identity join (#16), structured E–J schedules (#17), `OPENHOUSE_DATA_DIR` (#50), release drift guard (#43), PTR exact-dollar amounts (#49). Remaining: OCR, distribution (PyPI/plugin) if ever wanted. |
