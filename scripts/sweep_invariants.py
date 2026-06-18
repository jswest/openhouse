#!/usr/bin/env python3
"""Tier-2 parse-validation: deterministic, offline invariant checks over the
*whole* parsed corpus.

The complete-from-above complement to the visual (Tier-1) parse-validation
sweep (``docs/parse-validation-sweep.md``). Where the visual sweep is *sound*
— sampled, bounding real bugs from below — this pass is *complete*: it walks
every parsed record and checks a small set of internal invariants that need no
PDF, because they relate fields already on the record to each other. The
sweeper runs this first, then fans out the visual pass over the sample.

Invariants (SPEC §6.3 / ``openhouse.schemas``):

  - ``asset_type == _normalize_asset_type(asset_type_raw)``     [PTR, FD A/B]
  - ``ticker     == _ticker_from_asset(asset)``                 [PTR]
  - a structured date and its ``*_raw`` anomaly flag are never both set
    (the #113 contract: ``*_raw`` is the rejected-date flag)   [PTR, FD B]
  - every ``AmountRange`` is exactly one shape on the wire:
    ``{low, high, label}`` xor ``{exact, label}`` (#49)        [PTR, FD A/B/D]

The normalize / ticker checks reuse the parser's own helpers, so a violation
means the emitted JSON disagrees with the logic that produced it — drift, a
hand-edit, or a stale schema — never a reimplementation mismatch.

Contract: violations as JSONL on stdout (jq-composable); progress + a summary on
stderr; exit 1 if any violation, 0 if clean, 2 on an operational error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from openhouse.cli import resolve_data_dir
from openhouse.pdf import _normalize_asset_type, _ticker_from_asset
from openhouse.schemas import SCHEMA_VERSION

# Which AmountRange-typed fields live on each FD schedule (others carry verbatim
# string amounts or none, so they are not shape-checked here).
_FD_AMOUNT_FIELDS = {"A": ("value_of_asset", "income_amount", "income_preceding"),
                     "B": ("amount_range",),
                     "D": ("amount_range",)}


def check_amount_range(obj) -> str | None:
    """Return a one-line reason an ``AmountRange`` object is malformed, else None.

    The wire form is sparse (``openhouse.schemas.AmountRange._serialize``): a
    range emits ``low``/``high`` and no ``exact``; an exact value emits
    ``exact`` and no ``low``/``high``. Exactly one shape, always a ``label``.
    """
    if not isinstance(obj, dict):
        return f"expected object, got {type(obj).__name__}"
    if not isinstance(obj.get("label"), str):
        return "missing string 'label'"
    has_exact = "exact" in obj
    has_range = "low" in obj or "high" in obj
    if has_exact and has_range:
        return "both 'exact' and 'low'/'high' present (shapes not exclusive)"
    if not has_exact and not has_range:
        return "neither 'exact' nor 'low'/'high' present (label only)"
    if has_range and not ("low" in obj and "high" in obj):
        return "range shape missing 'low' or 'high'"
    if has_exact and obj.get("exact") is None:
        return "'exact' present but null"
    return None


def _asset_type_violation(item):
    """asset_type must be the normalized form of asset_type_raw (when raw present)."""
    if "asset_type_raw" not in item:
        return None
    expected = _normalize_asset_type(item.get("asset_type_raw"))
    if item.get("asset_type") != expected:
        return {"invariant": "asset_type_normalized", "field": "asset_type",
                "parsed": item.get("asset_type"), "expected": expected,
                "detail": "asset_type != normalize(asset_type_raw)"}
    return None


def _date_exclusive_violation(item, date_field, raw_field):
    """A structured date and its *_raw rejected-date flag are never both set."""
    if item.get(date_field) is not None and item.get(raw_field) is not None:
        return {"invariant": "date_raw_exclusive", "field": date_field,
                "parsed": item.get(date_field), "expected": None,
                "detail": f"{date_field} and {raw_field} both set"}
    return None


def _amount_violation(item, field):
    val = item.get(field)
    if val is None:
        return None
    detail = check_amount_range(val)
    if detail:
        return {"invariant": "amount_range_shape", "field": field,
                "parsed": val, "expected": "exactly one shape", "detail": detail}
    return None


def ptr_violations(txn):
    """All invariant violations on one PTR ``transactions[]`` element."""
    checks = [_asset_type_violation(txn),
              _amount_violation(txn, "amount_range"),
              _date_exclusive_violation(txn, "transaction_date", "date_raw"),
              _date_exclusive_violation(txn, "notification_date",
                                        "notification_date_raw")]
    if "asset" in txn:
        expected = _ticker_from_asset(txn["asset"])
        if txn.get("ticker") != expected:
            checks.append({"invariant": "ticker_from_asset", "field": "ticker",
                           "parsed": txn.get("ticker"), "expected": expected,
                           "detail": "ticker != _ticker_from_asset(asset)"})
    return [c for c in checks if c]


def fd_item_violations(letter, item):
    """All invariant violations on one FD ``schedules.<letter>[]`` element."""
    checks = []
    if letter in ("A", "B"):
        checks.append(_asset_type_violation(item))
    if letter == "B":
        checks.append(_date_exclusive_violation(item, "transaction_date",
                                                "transaction_date_raw"))
    for field in _FD_AMOUNT_FIELDS.get(letter, ()):
        checks.append(_amount_violation(item, field))
    return [c for c in checks if c]


def iter_violations(data_dir: Path):
    """Yield an enriched violation dict for every invariant breach under data_dir."""
    parsed = data_dir / "parsed" / "clerk"
    if not parsed.is_dir():
        raise FileNotFoundError(f"no parsed/clerk/ directory under {data_dir}")
    for year_dir in sorted(p for p in parsed.iterdir() if p.is_dir()):
        year = year_dir.name
        _warn_schema(year_dir)
        for body_file in sorted((year_dir / "ptr").glob("*.json")):
            txns = json.loads(body_file.read_text()).get("transactions", [])
            for i, txn in enumerate(txns):
                for v in ptr_violations(txn):
                    yield {"year": year, "doc_id": body_file.stem,
                           "body_type": "ptr", "location": f"transactions[{i}]", **v}
        for body_file in sorted((year_dir / "fd").glob("*.json")):
            schedules = json.loads(body_file.read_text()).get("schedules", {})
            for letter, items in schedules.items():
                for i, item in enumerate(items):
                    for v in fd_item_violations(letter, item):
                        yield {"year": year, "doc_id": body_file.stem,
                               "body_type": "fd",
                               "location": f"schedules.{letter}[{i}]", **v}


def _warn_schema(year_dir: Path) -> None:
    """Note on stderr when a year's parse-manifest is not the current schema."""
    manifest = year_dir / "parse-manifest.json"
    if not manifest.exists():
        return
    seen = json.loads(manifest.read_text()).get("schema_version")
    if str(seen) != str(SCHEMA_VERSION):
        print(f"warning: {year_dir.name} parsed at schema_version {seen!r}, "
              f"expected {SCHEMA_VERSION!r} — re-parse before trusting results",
              file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Tier-2 parse-validation: complete, offline invariant checks "
                    "over the whole parsed corpus. Violations as JSONL on stdout.")
    parser.add_argument("--data-dir", default=None,
                        help="data root (default: $OPENHOUSE_DATA_DIR or ~/.openhouse)")
    args = parser.parse_args(argv)

    try:
        data_dir = resolve_data_dir(args.data_dir)
        violations = list(iter_violations(data_dir))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for v in violations:
        print(json.dumps(v, ensure_ascii=False, sort_keys=True))

    if violations:
        by_invariant = Counter(v["invariant"] for v in violations)
        breakdown = ", ".join(f"{k}={n}" for k, n in sorted(by_invariant.items()))
        print(f"{len(violations)} invariant violation(s): {breakdown}", file=sys.stderr)
    else:
        print("no invariant violations", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
