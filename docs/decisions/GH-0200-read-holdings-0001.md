# GH-0200 `read holdings` — no `--ticker` filter; use `--asset` for symbol lookup

**Issue:** #200 — `openhouse clerk read holdings <range>`  
**Date:** 2026-06-19  
**Status:** accepted

## Context

`read trades` (PTR bodies) has two asset-text filters:

- `--ticker` — **SOUND** (no false positives): exact case-insensitive match on the
  parsed `ticker` field, which the PTR extractor populates from the embedded
  `(SYMBOL)` in each transaction row.
- `--asset` — **COMPLETENESS-leaning** (substring over verbatim `asset` text).

`ScheduleAItem` (annual FD Schedule A) was checked before implementing `holdings`.
Result: **Schedule A items have no `ticker` field.** The `ScheduleAItem` schema
(`openhouse/schemas.py` line 349) defines:

```
asset          str         (verbatim, may embed "(TICKER)" as in "Apple Inc. (AAPL)")
owner          Optional[str]
asset_type     Optional[str]
asset_type_raw Optional[str]
value_of_asset Optional[AmountRange]
income_type    Optional[str]
income_amount  Optional[AmountRange]
income_preceding Optional[AmountRange]
location       Optional[str]
description    Optional[str]
raw_text       str
```

The real fixture (`tests/fixtures/parsed/clerk/2021/fd/10100003.json`) confirms
this: `"asset": "Apple Inc. (AAPL)"` — the symbol is embedded but no separate
`ticker` key exists.

## Decision

**Do not add a `--ticker` filter to `read holdings`.**

Rationale:
1. There is no parsed `ticker` field to match against — any ticker extraction
   would require regex over the `asset` string, which is COMPLETENESS-leaning,
   not SOUND. Calling such a filter `--ticker` and claiming it is SOUND would be
   misleading.
2. The only honest asset-text filter for Schedule A is COMPLETENESS-leaning
   (substring). This is already `--asset`, which correctly covers symbol lookup:
   `--asset AAPL` matches `"Apple Inc. (AAPL)"` and any other asset whose text
   contains "AAPL".
3. Adding a second flag with different semantics and the same name would confuse
   users who expect parity with `trades --ticker` (SOUND, no false positives).

## Consequence

`read holdings` has one asset-text filter: `--asset` (COMPLETENESS-leaning,
substring). Users searching by symbol use `--asset <TICKER>`. The guarantee line
on stderr declares AT MOST semantics. Documentation notes the absence of a
`--ticker` filter and explains why.

If the FD parser is ever extended to extract a `ticker` field from Schedule A
items (analogously to the PTR extractor), a `--ticker` SOUND filter can be added
then. That would require a schema bump and re-parse.
