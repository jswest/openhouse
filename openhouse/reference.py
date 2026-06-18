"""Top-level ``reference`` command: look up legislators by name or bioguide-id (#184).

Read-only offline search over CC0 ``congress-legislators`` bulk files cached
at ``raw/reference/`` by ``clerk pull``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .legislators import LEGISLATORS_FILES, REFERENCE_SUBDIR, _norm_name


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _load_records(data_dir: Path) -> tuple[list[dict], int]:
    """Load the union of current + historical legislators from disk.

    Missing files are silently skipped; malformed files warn to stderr.
    Raises :class:`ReferenceDataError` if neither file is present.
    """
    ref_dir = data_dir / REFERENCE_SUBDIR
    records: list[dict] = []
    found_any = False
    for fname in LEGISLATORS_FILES:
        path = ref_dir / fname
        if not path.exists():
            continue
        found_any = True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                records.extend(data)
        except Exception as exc:  # noqa: BLE001
            print(
                f"warning: could not read {path}: {exc}",
                file=sys.stderr,
            )
    if not found_any:
        raise ReferenceDataError(
            f"no reference data under {ref_dir}; "
            f"run 'openhouse clerk pull <year>' to fetch it"
        )
    return records, len(records)


class ReferenceDataError(RuntimeError):
    """Raised when the on-disk reference data is absent or unreadable."""


def _matches(record: dict, needle: str) -> bool:
    """Case-insensitive substring match on bioguide id or any name field.

    Names are diacritic-normalised first (``"gonzalez"`` matches ``"González-Colón"``).
    """
    bioguide = (record.get("id") or {}).get("bioguide", "")
    if needle.lower() in bioguide.lower():
        return True
    name = record.get("name") or {}
    norm_needle = _norm_name(needle)
    for field in ("first", "last", "official_full"):
        value = name.get(field) or ""
        if norm_needle in _norm_name(value):
            return True
    return False


def _to_row(record: dict) -> dict:
    """Flatten a raw legislator record into an output row dict."""
    bioguide = (record.get("id") or {}).get("bioguide", "")
    name_obj = record.get("name") or {}
    display_name = name_obj.get("official_full") or (
        f"{name_obj.get('first', '')} {name_obj.get('last', '')}".strip()
    )
    terms = record.get("terms") or []
    last_term = terms[-1] if terms else {}
    chamber = last_term.get("type", "")
    state = last_term.get("state", "")
    return {
        "name": display_name,
        "bioguide_id": bioguide,
        "chamber": chamber,
        "state": state,
    }


def search(needle: str, data_dir: Path) -> tuple[list[dict], int]:
    """Return ``(rows, total_searched)`` for all legislators matching ``needle``.

    Raises :class:`ReferenceDataError` if neither reference file is on disk.
    """
    records, total = _load_records(data_dir)
    rows = [_to_row(r) for r in records if _matches(r, needle)]
    rows.sort(key=lambda r: (r["name"], r["bioguide_id"]))
    return rows, total


# ---------------------------------------------------------------------------
# CLI entry point (called from cli.main via pre-argparse intercept)
# ---------------------------------------------------------------------------


def run(argv: list[str]) -> int:
    """Run the ``reference`` command.  Returns an exit code."""
    # Lazy import to break the cli ↔ reference circular dependency at module load.
    from openhouse.cli import _emit, resolve_data_dir

    parser = argparse.ArgumentParser(
        prog="openhouse reference",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Look up legislators by name or bioguide-id substring.\n\n"
            "Searches the union of current and historical legislators cached\n"
            "in raw/reference/ (fetched by 'openhouse clerk pull'). Matching\n"
            "is case-insensitive and diacritic-insensitive for names\n"
            "(so 'gonzalez' matches 'González-Colón') and plain\n"
            "case-insensitive for bioguide IDs.\n\n"
            "GUARANTEE: COMPLETE over the cached congress-legislators set\n"
            "(current ∪ historical) — every record whose bioguide or name\n"
            "contains the search string is returned, none dropped. The only\n"
            "residual is members absent from the on-disk cache (e.g. sworn in\n"
            "after the last 'openhouse clerk pull'); re-pull to refresh."
        ),
        epilog=(
            "examples:\n"
            "  openhouse reference Adams --table\n"
            "  openhouse reference A000370\n"
            "  openhouse reference gonzalez"
        ),
    )
    parser.add_argument(
        "needle",
        metavar="<str>",
        help="name or bioguide-id substring to search for (case-insensitive)",
    )
    parser.add_argument(
        "--table",
        action="store_true",
        help="human-aligned table to stdout instead of JSON",
    )
    parser.add_argument(
        "--data-dir",
        metavar="DIR",
        default=None,
        help="data directory (default: $OPENHOUSE_DATA_DIR or ~/.openhouse)",
    )

    args = parser.parse_args(argv)
    resolved_dir = resolve_data_dir(args.data_dir)

    try:
        rows, total = search(args.needle, resolved_dir)
    except ReferenceDataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _emit(
        rows,
        table=args.table,
        table_fn=_table_fn,
    )

    ref_dir = resolved_dir / REFERENCE_SUBDIR
    print(
        f"note: searched {total} records in {ref_dir}; "
        f"members absent from the cache (e.g. sworn in after the last pull) "
        f"are not included — re-run 'openhouse clerk pull <year>' to refresh.",
        file=sys.stderr,
    )
    return 0


def _table_fn(rows: list[dict]) -> tuple[list[str], list[list[str]]]:
    """Build ``(headers, rows)`` for :func:`cli._emit` table rendering."""
    headers = ["name", "bioguide_id", "chamber", "state"]
    return headers, [[r[h] for h in headers] for r in rows]
