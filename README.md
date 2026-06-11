# openhouse

Pull, parse, and query **U.S. House of Representatives financial disclosure
filings** — annual Financial Disclosure statements and STOCK Act Periodic
Transaction Reports — from the Office of the Clerk, into normalized JSON you
can actually ask questions of.

> **Status: pre-v1.** The design is fully specified and verified against live
> Clerk data ([SPEC.md](./SPEC.md)); implementation is underway. Nothing below
> works yet — this README previews where it's going.

## What it will do

Three commands, one data directory:

```sh
openhouse pull 2024          # fetch the year's filing index + every PDF (resumable, polite)
openhouse parse 2024         # offline: PDFs + index → normalized JSON, nothing dropped
openhouse read trades 2024 --ticker NVDA --table
                             # offline: ask the parsed data a question
```

- **`pull`** is the only network step. Idempotent and Ctrl-C-safe — re-runs
  fetch only what's missing. Multi-year ranges (`openhouse pull 2019-2024`)
  throughout.
- **`parse`** never touches the network and never silently drops a filing:
  machine-readable (e-filed) PDFs become structured records; scanned/paper
  filings are detected and catalogued for a future OCR pass; anything odd lands
  in a manifest with a reason.
- **`read`** answers questions from the parsed JSON — filings by member or
  type, a single filing's full contents, stock transactions flattened across
  years, per-year summaries. JSON to stdout for machines and `jq`; `--table`
  for humans.

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

## What's coming, roughly in order

1. The three commands above, end to end, for any year range since 2008.
2. Near-dedup of filers via a normalized identity key, with explicit warnings
   when two people may share a name.
3. A Claude Code agent skill, so an AI agent can drive `pull`/`parse`/`read`
   directly.
4. OCR for the scanned/handwritten backlog (already detected and catalogued by
   `parse`).

## Use restriction

Clerk financial-disclosure data may **not** be used for commercial purposes
(news/media dissemination excepted), for solicitation, or to establish credit
ratings — this is statutory ([5 U.S.C. §
13107](https://www.law.cornell.edu/uscode/text/5/13107), Ethics in Government
Act as amended), not a request. `openhouse` is a research and transparency
tool; don't build a commercial product on its output.

## Development

Python 3.12+, managed with [`uv`](https://docs.astral.sh/uv/):

```sh
uv run pytest        # tests
```

Design contract: [SPEC.md](./SPEC.md). License: [MIT](./LICENSE.txt).
