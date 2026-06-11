"""Transformation: ``<year>FD.xml`` → normalized JSON (SPEC §4) — offline.

This module owns the ``parse`` command. It is the **offline, deterministic**
counterpart to ``pull``: it reads only ``raw/<year>/`` and writes only
``parsed/<year>/``, never touching the Clerk or the wall clock (the single
entry-time ``fetched_at`` is threaded in, SPEC §9).

Scope here (#6) is **metadata only** — every ``<Member>`` becomes a
schema-validated :class:`~openhouse.schemas.FilingMetadata` record with a
computed ``filer_id`` (SPEC §6.2), and identity collisions are surfaced. The
per-PDF classification pass (efiled / scanned / missing) + body extraction is #7
(v0.3.0); the CLI flags ``--types`` / ``--strict`` are accepted now (so #7 needs
not touch ``cli.py``) but do not yet change behavior. The manifest is shaped so
#7 can ADD a ``pdf_class`` breakdown and ``parse_status`` tally without reshaping
what is written here.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .index import build_filing_records
from .schemas import SCHEMA_VERSION, FilingMetadata


class ParseError(Exception):
    """A parse failed in a way the user must see (printed to stderr, non-zero exit)."""


def _detect_identity_warnings(records: list[FilingMetadata]) -> list[dict]:
    """Find ``filer_id`` collisions that look like *two different people* (§6.2).

    The same person filing many times per year is normal — PTRs, the annual
    report, and an extension all share one ``filer_id`` by design, and that is
    NOT a warning. The signals that one ``filer_id`` may cover two people are:

    - the same ``filer_id`` appears with **different districts** in the year, or
    - the raw **last name** or **suffix** differs among records sharing a
      ``filer_id`` (the slug collided rather than matched).

    Returns one dict per colliding ``filer_id`` — its distinct raw names, the
    involved ``doc_ids``, and the distinct districts — in first-appearance order
    (deterministic).
    """
    by_filer: dict[str, list[FilingMetadata]] = defaultdict(list)
    order: list[str] = []
    for rec in records:
        if rec.filer_id not in by_filer:
            order.append(rec.filer_id)
        by_filer[rec.filer_id].append(rec)

    warnings: list[dict] = []
    for filer_id in order:
        group = by_filer[filer_id]
        if len(group) < 2:
            continue

        # Distinct districts (None — no StateDst — is its own bucket). A single
        # person never spans two districts in one year; two districts → two people.
        districts = {
            r.state_district.district if r.state_district else None for r in group
        }
        # The slug can collide two different raw names: differing last name or
        # suffix at the same filer_id means the key matched names it shouldn't.
        last_names = {r.filer.last for r in group}
        suffixes = {(r.filer.suffix or "") for r in group}

        collides = len(districts) > 1 or len(last_names) > 1 or len(suffixes) > 1
        if not collides:
            continue

        # Distinct raw names, first-appearance order (a set would be nondeterministic).
        seen_names: list[str] = []
        for r in group:
            name = " ".join(
                part for part in (r.filer.first, r.filer.last, r.filer.suffix) if part
            )
            if name not in seen_names:
                seen_names.append(name)

        warnings.append(
            {
                "filer_id": filer_id,
                "raw_names": seen_names,
                "doc_ids": [r.doc_id for r in group],
                "districts": sorted(d for d in districts if d is not None),
            }
        )
    return warnings


def parse_year(
    year: int, *, data_dir: Path, fetched_at: str
) -> Optional[dict]:
    """Parse one year's index into ``parsed/<year>/`` (SPEC §4). Offline.

    Reads ``<data_dir>/raw/<year>/<year>FD.xml`` (written by ``pull``). If the
    XML is absent this is a clean skip (clear stderr message, returns ``None``) —
    not a crash, so a multi-year range survives a missing year. Otherwise builds
    every ``<Member>`` into a record, detects identity collisions, and writes
    ``filings.json`` + ``parse-manifest.json``. Returns a compact summary dict.
    """
    raw_dir = data_dir / "raw" / str(year)
    xml_path = raw_dir / f"{year}FD.xml"
    if not xml_path.exists():
        print(
            f"{year}: index {xml_path} is missing; skipping "
            f"(run `openhouse pull {year}` first).",
            file=sys.stderr,
        )
        return None

    records = build_filing_records(xml_path, year)

    by_filing_type: dict[str, int] = defaultdict(int)
    for rec in records:
        by_filing_type[rec.filing_type.code] += 1
    filing_type_counts = dict(sorted(by_filing_type.items()))

    identity_warnings = _detect_identity_warnings(records)
    for warning in identity_warnings:
        print(
            f"{year}: identity warning — filer_id {warning['filer_id']!r} covers "
            f"{len(warning['raw_names'])} distinct name(s) "
            f"{warning['raw_names']} across docs {warning['doc_ids']} "
            f"(districts {warning['districts']}); `read --member` on this name is "
            f"ambiguous.",
            file=sys.stderr,
        )

    parsed_dir = data_dir / "parsed" / str(year)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    # JSON-mode dump so dates serialize as ISO strings; sort_keys + trailing
    # newline so two runs from the same raw produce byte-identical files.
    filings = [rec.model_dump(mode="json") for rec in records]
    filings_path = parsed_dir / "filings.json"
    filings_path.write_text(
        json.dumps(filings, indent=2, sort_keys=True) + "\n"
    )

    # Shaped so #7 ADDS to ``counts`` (a ``by_pdf_class`` /
    # ``by_parse_status`` breakdown) without reshaping these keys.
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": fetched_at,
        "year": year,
        "counts": {
            "total": len(records),
            "by_filing_type": filing_type_counts,
        },
        "identity_warnings": identity_warnings,
    }
    manifest_path = parsed_dir / "parse-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    print(
        f"{year}: parsed {len(records)} filings → {filings_path} "
        f"({len(identity_warnings)} identity warning(s); manifest: "
        f"{manifest_path}).",
        file=sys.stderr,
    )
    return {
        "year": year,
        "total": len(records),
        "by_filing_type": filing_type_counts,
        "identity_warnings": len(identity_warnings),
    }


def parse(
    years: list[int],
    *,
    data_dir: Path,
    types: list[str],
    strict: bool,
    fetched_at: str,
) -> int:
    """Run ``openhouse parse`` for ``years`` (SPEC §4). Returns a process exit code.

    Entirely offline and deterministic: each year reads only ``raw/<year>/`` and
    writes ``parsed/<year>/``, and a re-run from the same ``raw/`` produces
    byte-identical output. A year whose index XML is absent is a clean skip, not
    a crash, so a range survives a not-yet-pulled year.

    ``types`` and ``strict`` are accepted for the #7 per-PDF pass and do not yet
    change behavior in #6 (metadata only — no PDFs touched). ``fetched_at`` is the
    single entry-time timestamp threaded into the manifest (SPEC §9: no wall-clock
    in core logic).

    Emits one compact JSON summary object (the per-year results) to **stdout**
    (machine-composable, CLAUDE.md "JSON to stdout"); progress / warnings go to
    stderr. Non-zero exit only on a real error.
    """
    summaries: list[dict] = []
    skipped: list[int] = []
    for year in years:
        summary = parse_year(year, data_dir=data_dir, fetched_at=fetched_at)
        if summary is None:
            skipped.append(year)
        else:
            summaries.append(summary)

    combined = {
        "command": "parse",
        "generated_at": fetched_at,
        "years": summaries,
        "skipped_years": skipped,
    }
    print(json.dumps(combined, indent=2, sort_keys=True))
    return 0
