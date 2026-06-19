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
openhouse clerk read holdings 2024 --asset nvda --owner SP        # Schedule A assets across annual FDs
openhouse clerk read summary 2024                                 # counts: types, efiled/scanned, errors, warnings
```

**Subcommands:**

| Subcommand | Input | Output |
|---|---|---|
| `filings <range>` | filters: `--type`, `--member`, `--state`, `--since/--until` | matching filing-metadata records |
| `filing <doc_id>` | a DocID | that filing's metadata + body (if parsed) |
| `trades <range>` | filters: `--ticker`, `--member`, `--owner`, `--type P\|S`, `--since/--until`, `--min-amount` | PTR transactions flattened across the range, each with its filer attached |
| `holdings <range>` | filters: `--asset`, `--member`, `--bioguide`, `--owner`, `--asset-type`, `--min-value` | Schedule A (assets & unearned income) from annual FDs, filer attached; `--asset` is COMPLETENESS-leaning (no `--ticker` — Schedule A has no parsed ticker field; see `docs/decisions/GH-0200-read-holdings-0001.md`) |
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

   **Committee membership — ✅ VERIFIED 2026-06-19 (#195).** The same CC0 project
   and the same gh-pages base serve the committee data, fetched by `pull` into the
   **same** `raw/reference/` lane and the **same** `--no-reference` gate:

   - `…/committees-current.json` — committee/subcommittee **definitions** (a list;
     each House committee has `type:"house"`, `thomas_id`, `name`, and a
     `subcommittees[]` array of `{thomas_id, name}`).
   - `…/committee-membership-current.json` — **membership** keyed by committee
     thomas code → a list of member rows `{bioguide, party, rank, title}`. A
     subcommittee's code is the parent's `thomas_id` **concatenated** with the
     subcommittee's `thomas_id` (parent `HSAG` + sub `03` → `HSAG03`). Senate
     (`SS…`) and joint codes also appear and are **excluded** from the House join.

   **Coverage limitation (verified, not assumed).** Membership is
   **CURRENT-CONGRESS-ONLY** — the 119th (2025–26). The membership rows carry **no
   congress field**, and there is **no historical membership file**:
   `committee-membership-historical.json` **404s** upstream (probed 2026-06-19).
   `committees-historical.json` exists but carries only *definitions* (each with a
   `congresses[]` list), never membership. So `reference --committees` can answer
   only the current congress; `--congress N` / `--year Y` outside it return
   nothing, declared in the stderr residual. The current congress is hard-coded as
   `CURRENT_MEMBERSHIP_CONGRESS` (`legislators.py`); bump it the cycle the upstream
   snapshot rolls forward. The join is offline/deterministic (`load_committee_index`
   in `legislators.py`), surfaced by `openhouse reference <member> --committees
   [--congress N | --year Y]`: opt-in (a bare `reference <str>` stays byte-stable),
   one row per committee/subcommittee seat `{congress, committee, subcommittee?,
   rank, title, party}`, **COMPLETE** over the cached snapshot with the
   current-congress-only residual on stderr. See README *reference* and
   `skill/reference.md`.
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

Schedule C items carry `amount_preceding` (GH-0166): the Candidate/New-Filer
form's second amount column ("amount preceding year"); `null` on member forms,
which report a single `amount`. Mirrors Schedule A's `income_preceding` split, so
a consumer reads two machine-parseable money values instead of one space-joined
string (`"$36,750.00 $40,078.00"`).

Schedule F's `parties`/`terms` and Schedule H's `source`/itinerary are a
**best-effort** structured split (GH-0166): the verbatim `raw_text` stays
authoritative where a column boundary is genuinely ambiguous — e.g. a Party that
is an organization whose name contains a terms keyword ("Pension") may split
imperfectly. The sound-or-complete guarantee rests on `raw_text`, never on these
structured fields alone.

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
`fec pull` (#170) now creates `raw/fec/<cycle>/` (the four bulk files +
`fec-pull-manifest.json`); `parsed/fec/<cycle>/` stays **reserved** for the FEC
normalization sub-issue. The CC0 congress-legislators reference set stays at the
un-scoped `raw/reference/` — it is shared bulk data, not a source's disclosures,
so it is not relocated.

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
    fec/<cycle>/            # FEC bulk lane (#170): cn.txt ccl.txt cm.txt itpas2.txt + fec-pull-manifest.json
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

---

## 13. The FEC lane — Path 1: connected-SSF PAC money

A **second data source** (#167), adjacent to the Clerk lane: the Clerk lane
answers *what a member owns and trades*; the FEC lane answers *who funds them
institutionally*, via the channel that is both legally real and cleanly
traceable — **connected-SSF PAC money** ("Path 1"). The source-namespace work
(§3/§4/§6.4, #174) and this scaffold (#168) lay the contract; acquisition,
normalization, and query are later #167 sub-issues. This section is the contract,
not the implementation.

### 13.1 The mechanic

Corporations and unions cannot give to federal candidates directly (Tillman Act
1907; FECA). The traceable institutional money is the **separate segregated
fund (SSF)**: a corporation, trade/membership group, cooperative, or labor
organization sponsors a PAC, which gives **hard money** to the member. FEC
discloses each such receipt on the **member's principal-committee Schedule A,
line 11C** (receipts from other political committees); the **sponsor link** lives
on the *contributing* committee's record (`connected_organization_name`,
`organization_type`). Path 1 is exactly the **connected SSFs** —
`organization_type ∈ {C, T, L, M, V, W}`. Labor (`L`) is included as
institutional PAC money and tagged by type so `read` can slice it.

### 13.2 Source & footing

- **Source:** the **OpenFEC** API plus FEC **bulk** files (structured JSON/CSV —
  normalization, not OCR; no extraction heroics).
- **Public domain.** FEC bulk/API data carries **no** commercial-use restriction
  (unlike Clerk FD, §1). The one statutory bar is **52 U.S.C. §30111(a)**:
  contributor information may not be **sold or used** to solicit contributions or
  for any commercial purpose. README and `--help` state this (full wording in
  #173); records carry a `provenance` tag (`"fec"` vs the Clerk lane's `"clerk"`)
  so the two legal footings stay distinguishable downstream.
- **Near-complete, low residual.** The sub-$200 itemization cliff that wrecks
  individual-donor completeness barely touches PACs (a PAC gift worth tracking is
  well over $200, so it is itemized) — so `read` can make a real
  soundness/completeness claim, as the repo demands.
- **The join already exists.** `congress-legislators` (`id.fec[]`, CC0, already
  fetched by `pull`/used for the bioguide ladder §6.2) anchors member → FEC
  candidate → committee **offline**, no fuzzy matching.

### 13.3 `organization_type` code table

| code | sponsor class |
|------|---------------|
| `C` | corporation |
| `T` | trade |
| `L` | labor |
| `M` | membership |
| `V` | cooperative |
| `W` | corporation without capital stock |

The raw single-letter code is preserved on the record beside the normalized
label (`organization_type_raw`), so an unmapped or blank type is never an error
(the FilingType pattern, §2.3: raw alongside normalized, never a dropped record).

### 13.4 Year → cycle convention

FEC reports on **2-year cycles**, labelled by their **even ending year**.
`openhouse fec <verb>` takes the **same `<year>` / `<year>-<year>`** argument as
the clerk lane — **no `--cycle` vocabulary**. Internally each named year is
**expanded to its enclosing cycle** (odd → next even: 2023 → 2024; even → itself:
2024 → 2024), emitting a one-line **stderr** note when expansion happens (the §5
`trades` filing-year-note pattern). So `fec pull 2023` and `fec pull 2024` both
resolve to the **2024 cycle**, and `2023-2024` is a single cycle. The expansion
lives in a small unit-testable helper (`cli.year_to_cycle` /
`expand_years_to_cycles`).

### 13.5 Data layout (cycle-keyed)

The FEC lane is **cycle-keyed on disk** (vs the clerk lane's per-coverage-year):
`raw/fec/<cycle>/` + `parsed/fec/<cycle>/` (§6.4), built by
`cli.fec_raw_dir` / `cli.fec_parsed_dir` with the same
`--data-dir` → `OPENHOUSE_DATA_DIR` → `~/.openhouse` precedence as everything
else.

### 13.5a Verified facts about the FEC bulk source (`fec pull`, #170)

From the by-hand polite probe of the 2024 cycle (carried into the trimmed
fixtures under `tests/fixtures/fec/`):

- **URLs & redirect.** `https://www.fec.gov/files/bulk-downloads/<cycle>/<file>`
  returns a **302** to an AWS GovCloud S3 host
  (`cg-…s3-us-gov-west-1.amazonaws.com/bulk-downloads/<cycle>/<file>`). The
  client follows redirects; the manifest records both the requested `www.fec.gov`
  URL and the final storage URL.
- **Zip name vs. inner member.** The *zip* carries the 2-digit cycle suffix
  (`cn24.zip`); the *inner member* does **not** — it is the bare stem: `cn.txt`,
  `ccl.txt`, `cm.txt`, and the irregular **`itpas2.txt`** for `pas2<yy>.zip`.
- **Files are pipe-delimited (`|`), LF-terminated, latin-1**, with the column
  orders from the FEC data-dictionary pages: `cn` 15 cols (CAND_ID … CAND_PCC at
  col 10 = principal committee), `ccl` 7 cols (CAND_ID, CAND_ELECTION_YR,
  FEC_ELECTION_YR, CMTE_ID, CMTE_TP, **CMTE_DSGN** = `P` for principal,
  LINKAGE_ID), `cm` 15 cols (CMTE_ID, CMTE_NM, … CMTE_TP col 10, **ORG_TP** col
  13, CONNECTED_ORG_NM col 14, CAND_ID col 15), `itpas2` 22 cols (CMTE_ID =
  contributor, IMAGE_NUM col 5, TRANSACTION_DT col 14, TRANSACTION_AMT col 15,
  **OTHER_ID col 16 = recipient committee**, CAND_ID col 17, TRAN_ID col 18).
- **Real 2024 sizes:** `cn24.zip` 356 KB, `ccl24.zip` 94 KB, `cm24.zip` 883 KB,
  `pas224.zip` ~24.7 MB (the largest — expected, well under the 150 MB park cap).
- **`cm` has no affiliated-committee column** — the public committee master file
  does not carry one, so `FecCommittee.affiliation` has no bulk source and is
  left `None` for now.

**Super-PAC independent expenditures (GH-0194 probe, 2024 cycle).** A *fifth*
bulk file, acquired alongside the four above but **structurally different**:

- **URL & redirect.** `https://www.fec.gov/files/bulk-downloads/<cycle>/independent_expenditure_<cycle>.csv`
  (e.g. `…/2024/independent_expenditure_2024.csv`) — 302s to the same AWS GovCloud
  S3 host as the four zips; the client follows redirects and records both URLs.
- **A plain headered CSV, NOT a zip.** Unlike the four pipe-delimited files, the
  IE file is a comma-delimited, **UTF-8**, header-row CSV served directly (no inner
  member to extract). `fec pull` writes the response body straight to
  `raw/fec/<cycle>/independent_expenditure_<cycle>.csv`.
- **23 columns**, header names (the ones the parse reads):
  `cand_id`, `cand_name`, `spe_id` (spender committee), `spe_nam`, `ele_type`,
  `can_office_state`, `can_office_dis`, **`can_office`** (`H`/`S`/`P`/blank — we
  keep only `H`), `cand_pty_aff`, **`exp_amo`** (amount), **`exp_date`**
  (`DD-MON-YY`, e.g. `28-OCT-24` — *unlike* itpas2's `MMDDYYYY`), `agg_amo`,
  **`sup_opp`** (`S` = support / `O` = oppose / blank), **`pur`** (purpose),
  `pay`, `file_num`, `amndt_ind`, **`tran_id`**, **`image_num`**, `receipt_dat`,
  `fec_election_yr`, `prev_file_num`, `dissem_dt`.
- **Real 2024 size:** ~19.5 MB, ~73,449 data rows (30,900 House; of those ~23,175
  support / ~7,631 oppose / ~94 blank; ~5,050 House rows carry a blank `cand_id`).
  Well under the 150 MB park cap.
- **`spe_id` / `spe_nam` carry leading/trailing whitespace** in the raw file — the
  parse strips them. A spender id may be a normal `C########` committee (joinable
  to `cm` for `connected_organization_name`) or a `C9#######` IE-only filer id
  (no `cm` row → connected org left `None`).
- **The IE file carries NO connected-organization column** — that is surfaced by
  joining `spe_id` to `cm` (raw; no industry classification, §13.7).

### 13.6 Records & schema version

The contract (`openhouse/schemas.py`): `FecCommittee` (committee id, name,
`connected_organization_name`, `organization_type` + `_raw`, `committee_type`,
`affiliation`); `FecPacContribution` (recipient/contributor committee ids,
`amount`, `date`, `line` `F3-11C`, and the `image_number` + `transaction_id`
**double-entry key** — the same receipt is disclosed on both committees, and the
pair lets a later pass de-duplicate the halves rather than double-count); and the
member↔candidate link `FecMemberCandidateLink` (`bioguide_id`, `candidate_id`,
`committee_id`).

Plus, since GH-0194, `FecIndependentExpenditure` — the separately-footed
super-PAC IE slice (`spender_committee_id`, `spender_name`,
`connected_organization_name`, targeted `candidate_id` + bridged `bioguide_id`,
`office` `H`, `support_oppose` + `_raw`, `amount`, `date`, `purpose`,
`image_number` + `transaction_id`, `provenance = "fec_ie"`). See §13.7 for why it
is never summed with `FecPacContribution`.

These are versioned by **`FEC_SCHEMA_VERSION`**, **independent of** the Clerk
lane's `SCHEMA_VERSION` — the same independence as `inspect`'s
`LABELS_SCHEMA_VERSION` (a reshape in one lane must not force a re-parse of the
other). It is **`2`** as of GH-0194 (bumped from `1` for the IE model — adding the
model also refreshed `schemas.fingerprint` in the same change) and is stamped into
the FEC lane's own parse-manifest
(a later sub-issue), the way `SCHEMA_VERSION` is stamped into the clerk
parse-manifest (§6.5). The release fingerprint guard (§GH-0043) auto-discovers
**all** models in `schemas.py`, so adding the FEC models refreshed
`schemas.fingerprint` (deliberately, in the same change) — that guard tracks
module structure, not lane membership.

### 13.7 Explicit non-goals (so the residual stays honest)

Out of scope for Path 1 (cross-ref #167): **Path 2** (employee-bundling by
free-text `employer` — lossy, and legally *not* institutional money); **industry
classification** (needs OpenSecrets CRP codes, which are non-commercial /
educational-only — importing them would re-import a license restriction we avoid;
we roll up to **organization**, never industry); **501(c)(4) / soft / "dark"
money** (undisclosed treasury money). `read` output states plainly that it shows
the *disclosed, candidate-side* slice, not total influence.

**Super-PAC independent expenditures — IN SCOPE as a separately-footed slice
(GH-0194).** Originally listed here as a non-goal on the grounds that IE treasury
money can't legally reach the member; that reasoning *stands*, and is exactly why
IEs are added as a **distinct, never-summed slice** rather than folded into the
Path-1 hard money:

- **Different legal footing.** A Schedule-E independent expenditure is
  *uncoordinated* outside spending FOR or AGAINST a candidate. The money does NOT
  go to the member and does NOT carry Path 1's "disclosed, candidate-side hard
  money" guarantee. It must **never** be summed with the connected-SSF set.
- **Different provenance.** IE records carry `provenance = "fec_ie"` (vs Path 1's
  `"fec"`), emitted to their **own** `independent-expenditures.json`, so the two
  footings can never blur downstream.
- **Both directions, House-only, completeness-first.** Support and oppose are both
  kept and tagged (`support_oppose` + `_raw`); every House-candidate IE (`office
  H`) is kept regardless of the spending committee's type; the spender's
  `connected_organization_name` is preserved **raw** (joined from `cm`), with **no
  industry classification** (the OpenSecrets non-goal above stays intact).
- **`pull` + `parse` only.** No `fec read` IE surface in GH-0194 (a deferred
  follow-up); the parsed JSON is the deliverable.

### 13.8 `fec parse` — normalization contract (#171)

Offline, deterministic normalization of the bulk files (§13.5a) into
`parsed/fec/<cycle>/`. Reads only `raw/fec/<cycle>/` (what `fec pull` extracted);
no network, no wall clock (the single entry-time `generated_at` is threaded in,
§9). A re-run from the same `raw/` is byte-identical (re-parse, not migrate).

**Outputs** (`parsed/fec/<cycle>/`):

- `committees.json` — the **contributing** connected-SSF committees behind the
  kept contributions (`FecCommittee`), in first-seen order. The full `cm` master
  is not emitted (it is tens of thousands of rows; we keep the tiny Path-1 slice).
- `contributions.json` — the kept Path-1 contributions (`FecPacContribution`),
  in first-appearance `itpas2` row order.
- `member-links.json` — the resolved `FecMemberCandidateLink`s, with
  `committee_id` filled from `ccl` (candidate→principal committee, designation
  `P`). An unresolved link is **not** written here (no sentinel committee leaks
  downstream); it lands in the residual instead.
- `independent-expenditures.json` — the kept House super-PAC IEs
  (`FecIndependentExpenditure`, `provenance = "fec_ie"`), in first-appearance CSV
  row order. A **separately-footed** slice (§13.7), written even when empty so a
  consumer can tell "no House IEs" from "IE file not pulled" (the latter logs to
  stderr; an old pre-GH-0194 cycle without the CSV is a clean skip of just this
  output). NEVER summed with `contributions.json`.
- `fec-parse-manifest.json` — `FEC_SCHEMA_VERSION`, counts (committees total /
  contributing, contributions kept / filtered, `by_org_type`, filtered-by-reason,
  member links resolved / unresolved, members without an FEC id, `pac_limit`
  breaches; plus the IE block `ie_kept` / `ie_filtered` / `ie_by_direction` /
  `ie_filtered_by_reason` and `source_rows.ie_data_rows`), the `pac_limit_breaches`
  detail, and the affiliation limitation note.
- `fec-unparsed-manifest.json` — every excluded contribution, every filtered/
  unattributed IE (`filtered_independent_expenditures`), every unresolved member
  link, every member with no FEC id, each with a `reason` — never a silent gap.

**IE filter (§13.7, GH-0194):** keep an IE iff `can_office == H`; a non-House row
is a `not_house_candidate` residual. Both directions kept. A House row with a blank
`cand_id` is `unattributed` — **kept anyway** (the `bioguide_id`/`candidate_id`
left `None`) AND recorded in the residual as `unresolved_candidate` (audit, not a
drop). A row shorter than the header is `malformed_short_row`. Reconciliation: kept
+ filtered = raw IE data rows, with the caveat that an unattributed House IE is
counted in **both** kept and the residual (it is kept, not dropped — CLAUDE.md).

**Path-1 filter:** keep a contribution iff its **contributing** committee (joined
via `cm`) has `organization_type ∈ {C, T, L, M, V, W}` (§13.3); retain the
normalized `organization_type` per kept record so `read` can slice corporate vs
labor. Residual reasons: `unresolved_committee` (contributor absent from `cm` — no
org type to test) and `not_connected_ssf` (in `cm` but not a connected SSF —
leadership/non-connected/ideological/super PAC).

**Org rollup key:** `connected_organization_name` if populated (it **is** in bulk
`cm` — the API-side "it's null" note was an artifact, #170), else the committee
`name`.

**Canonical source / dedup:** `itpas2` is the single committee→candidate file —
there is no recipient-side file to cross-check, so the API-era double-entry
cross-check does **not** apply. Rows are deduped by `transaction_id` (a literal
repeated `TRAN_ID`; first wins). The **$10k/PAC/cycle invariant** is a *sanity
flag*: a contributor→recipient cycle total over $10k is flagged in the manifest,
**never dropped** — it may legitimately trip across an un-collapsed affiliated
pair (below).

**Affiliation — DECLARED LIMITATION, not a gap.** Bulk `cm` has **no
affiliated-committee column** (§13.5a), so `FecCommittee.affiliation` has no bulk
source and is left `None`; the affiliated-PAC collapse **cannot** be done from
bulk data and is **not faked**. Consequence (stated in both manifests): a member's
org-level totals may count a parent and its subsidiary PAC as two separate orgs,
and the $10k invariant may legitimately trip across that un-collapsed pair.
Sourcing affiliation is a future enhancement.

### 13.9 `fec read` — query contract (#172)

Offline, read-only, deterministic query over `parsed/fec/<cycle>/` (what `fec
parse` wrote) — the FEC analogue of the clerk lane's `read` (§5). A **pure
function**: it never touches `raw/` or the network and never writes a byte. Two
subcommands, inverses over the same kept Path-1 contribution set; both take the
**same `<year>` / `<year>-<year>`** argument, expanded to the enclosing cycle(s)
(§13.4) with the one-line stderr expansion note.

- `openhouse fec read donors <member> <year|range>` — the connected-SSF PACs that
  gave to the member, **rolled up to organization** (key = `org_rollup_key`:
  `connected_organization_name` else committee `name`, §13.8), each
  `{org, organization_type, total, n_contributions}`, sorted by total desc (org
  name asc tie-break). `--member` matches case-insensitive substring over the
  member-link `bioguide_id` — the only identity the link carries (name-string
  matching, not true identity — §6.2). `--org-type` slices the tagged set to one
  connected-SSF class (the §13.3 labels, e.g. `labor`); an unknown class fails
  loudly (exit 2).
- `openhouse fec read pac <org> <year|range>` — the inverse: the members an org's
  PAC(s) supported, each `{bioguide_id, total, n_contributions}`, sorted by total
  desc. `<org>` matches case-insensitive substring over the org rollup key (a
  fuzzy name match, not verified identity). A receipt whose recipient committee
  has no member link is reported as an `unattributed` count on stderr, never
  dropped.

**Sound-or-complete (every response declares its guarantee, §CLAUDE.md).** The
answer is **complete over the Path-1 itemized connected-SSF receipts** `fec parse`
kept; the **residual** (stderr, reflected in `--table` runs too) names the
filtered contributions the parse counted (`not_connected_ssf` +
`unresolved_committee`), the affiliation-not-collapsed caveat (§13.8), and the
framing from §13.7: this is the **disclosed candidate-side** hard-money slice, not
total influence — no dark money, no super-PAC IE, no soft money. The residual
numbers are read straight from each cycle's `fec-parse-manifest.json` counts
(never recomputed). JSON to stdout, prose/guarantee/residual to stderr, exit 0
unless a query genuinely failed (an un-parsed cycle in a range is a clean skip
reported on stderr; a data dir with **no** parsed cycles for the range fails
loudly — exit 1 — rather than report a misleading empty match).
