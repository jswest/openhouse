"""Transformation: ``<year>FD.xml`` → normalized JSON (SPEC §4) — offline.

This module owns the ``parse`` command. It is the **offline, deterministic**
counterpart to ``pull``: it reads only ``raw/<year>/`` and writes only
``parsed/<year>/``, never touching the Clerk or the wall clock (the single
entry-time ``fetched_at`` is threaded in, SPEC §9).

Two passes run here, both offline. First every ``<Member>`` becomes a
schema-validated :class:`~openhouse.schemas.FilingMetadata` record with a
computed ``filer_id`` (SPEC §6.2) and identity collisions are surfaced; then each
on-disk PDF is classified ``efiled`` / ``scanned`` / ``missing`` by authoritative
text extraction (SPEC §2.2). For an ``efiled`` **PTR** (filing type ``P``) the
§6.3 ``transactions[]`` body is then extracted and written to
``parsed/<year>/ptr/<DocID>.json``; for an ``efiled`` **annual FD** the §6.3
schedule body (A–D structured, E–J raw_text-only) is extracted and written to
``parsed/<year>/fd/<DocID>.json``. ``--types`` restricts
which families are classified (out-of-scope filings stay unclassified yet still
count toward the total); ``--strict`` exits non-zero if any filing errored.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

from tqdm import tqdm

from .index import build_filing_records
from .legislators import load_legislator_index
from .pdf import (
    NotAnFdBody,
    PdfExtractError,
    classify,
    extract_fd_schedules,
    extract_ptr_transactions,
)
from .schemas import SCHEMA_VERSION, UNKNOWN_FILING_LABEL, FilingMetadata

# Exit code returned when ``--strict`` is set and any filing errored (SPEC §4:
# "exit non-zero if any filing errors"). Distinct from the argument-validation
# exit (2, in cli.py) so the two failure modes are tellable apart.
STRICT_ERROR_EXIT = 1

# Exit code when *no* year in the range produced output (every year's index was
# absent). A bare ``openhouse parse 2024`` that silently exits 0 having written
# nothing is a footgun for `parse … && next-step`; non-zero says "nothing parsed".
# A range where *some* years parsed and some were skipped still exits 0 (graceful).
NOTHING_PARSED_EXIT = 1


# FilingType codes whose e-filed PDF is a genuine **annual-FD body** carrying the
# §6.3 schedules (SPEC §2.3): the original annual report (``O``) and its amendment
# (``A``), both of which render the full Schedule A–J document. For these, a
# :class:`~openhouse.pdf.NotAnFdBody` (no schedule headings found) is NOT benign —
# it means the body's headings were lost and a real disclosure would be silently
# dropped, so we record it as ``extract_failed`` (explicit manifest entry) rather
# than a no-body ``ok``. Every *other* fd-family code (``X`` extension and the
# candidate/cover-sheet/etc. types) legitimately has no schedule body, so
# ``NotAnFdBody`` there is the benign "no body" path. #12 introduced
# ``NotAnFdBody`` specifically for the e-filed extension cover sheet (``X``).
_ANNUAL_FD_CODES = frozenset({"O", "A"})


class ParseError(Exception):
    """A parse failed in a way the user must see (printed to stderr, non-zero exit)."""


def _detect_identity_warnings(records: list[FilingMetadata]) -> list[dict]:
    """Surface filers that matched **no** congress-legislators bioguide (#16).

    The actionable identity signal since #16 is *unmatched* identity: a filer
    whose ``bioguide_id`` is ``None`` is keyed only by name (``name:<slug>``) — a
    bounded, unverified claim. The old "two-people share a slug" collision check is
    retired in favor of this honest "we could not pin this filer to a real member
    record" signal, which is what a ``read --member`` user actually needs to know.

    One warning per *distinct* unmatched ``filer_id`` (the ``name:`` key), in
    first-appearance order (deterministic), carrying its distinct raw names, the
    involved ``doc_ids``, and the distinct districts seen. A bioguide-matched
    filer is never warned — it is pinned to a stable identity, however many times
    it filed.
    """
    by_filer: dict[str, list[FilingMetadata]] = defaultdict(list)
    order: list[str] = []
    for rec in records:
        if rec.bioguide_id is not None:
            continue  # pinned to a real member — not an identity warning.
        if rec.filer_id not in by_filer:
            order.append(rec.filer_id)
        by_filer[rec.filer_id].append(rec)

    warnings: list[dict] = []
    for filer_id in order:
        group = by_filer[filer_id]

        districts = {
            r.state_district.district if r.state_district else None for r in group
        }
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
                "reason": "unmatched_no_bioguide",
                "raw_names": seen_names,
                "doc_ids": [r.doc_id for r in group],
                "districts": sorted(d for d in districts if d is not None),
            }
        )
    return warnings


def _unparsed_entry(rec: FilingMetadata, reason: str) -> dict:
    """One unparsed-manifest entry (SPEC §6.5).

    Carries ``filer_id`` alongside ``doc_id`` so a no-DocID row (``doc_id == ""``)
    — of which a year can have several — is still joinable back to its
    ``filings.json`` record, which a bare empty ``doc_id`` could not do.
    """
    return {"doc_id": rec.doc_id, "filer_id": rec.filer_id, "reason": reason}


def _write_ptr_body(parsed_dir: Path, doc_id: str, transactions: list) -> None:
    """Write one e-filed PTR body to ``parsed/<year>/ptr/<DocID>.json`` (§6.4).

    Exact contract shape (a sibling sub-issue's reader joins on it): an object
    with a single ``"transactions"`` key holding the §6.3 transaction array.
    Filing metadata is *not* duplicated here — ``filings.json`` is the single
    source of truth (joined by DocID). Byte-stable (``indent=2``, ``sort_keys``,
    trailing newline) so re-parse is deterministic, matching parse.py's
    convention.
    """
    ptr_dir = parsed_dir / "ptr"
    ptr_dir.mkdir(parents=True, exist_ok=True)
    body = {"transactions": [t.model_dump(mode="json") for t in transactions]}
    (ptr_dir / f"{doc_id}.json").write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n"
    )


def _write_fd_body(parsed_dir: Path, doc_id: str, fd_body) -> None:
    """Write one e-filed annual-FD body to ``parsed/<year>/fd/<DocID>.json`` (§6.4).

    Contract shape (mirrors the PTR body's single-key convention): an object with
    a single ``"schedules"`` key holding the §6.3 schedule map (only the letters
    that have data; a ``None disclosed.`` schedule is absent). Filing metadata is
    *not* duplicated — ``filings.json`` is the single source of truth (joined by
    DocID). Byte-stable (``indent=2``, ``sort_keys``, trailing newline) so re-parse
    is deterministic, matching parse.py's convention.
    """
    fd_dir = parsed_dir / "fd"
    fd_dir.mkdir(parents=True, exist_ok=True)
    body = {"schedules": fd_body.schedules}
    (fd_dir / f"{doc_id}.json").write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n"
    )


def _classify_records(
    records: list[FilingMetadata],
    *,
    data_dir: Path,
    types: list[str],
    year: int,
    parsed_dir: Path,
) -> list[dict]:
    """Classify each record's on-disk PDF, mutating ``pdf_class``/``parse_status``.

    Authoritative test is text extraction (``pdf.classify``, SPEC §2.2). Per
    record, the outcome is one of:

    - ``efiled`` / ``scanned`` / ``missing`` → ``parse_status="ok"``. Scanned and
      missing keep their metadata record with ``body: null`` and land in the
      unparsed manifest (SPEC §6.5).
    - ``extract_failed`` (a present-but-corrupt PDF, or an efiled PTR whose §6.3
      body extraction failed) → ``parse_status="error"``, ``pdf_class`` stays
      ``None``, unparsed reason ``extract_failed``.

    An ``efiled`` PTR (filing type ``P``) additionally has its §6.3
    ``transactions[]`` extracted and written to ``parsed/<year>/ptr/<DocID>.json``
    (``parsed_dir``) during this pass. An ``efiled`` **annual FD** (fd-family,
    schedule-bearing) likewise has its §6.3 schedule body extracted and written to
    ``parsed/<year>/fd/<DocID>.json``; an efiled fd-family PDF with no schedule
    headings (an extension/cover sheet) is left ``efiled``/``ok`` with no body
    (it still lives in ``filings.json``), neither dropped nor a misleading empty
    body.

    ``--types`` partial runs: a filing whose family is **not** in ``types`` is not
    classified this run — ``pdf_class`` stays ``None`` and ``parse_status="ok"``,
    and it is *not* added to the unparsed manifest (it was simply out of scope, not
    unparsed). Such rows are tallied separately so counts still reconcile to the
    total (SPEC §4: never silently drop a filing).

    Independently of the PDF outcome, an **unknown** FilingType label (a letter not
    in the §2.3 table) is recorded in the unparsed manifest with reason
    ``unknown_type`` — the raw code is preserved on the record, never dropped.

    Returns the unparsed-manifest entries (``doc_id`` + ``reason``) in record
    order (deterministic).
    """
    unparsed: list[dict] = []
    # Progress bar on stderr (tqdm's default); cosmetic only — never enters a
    # manifest or stdout. ``disable=None`` auto-suppresses when stderr is not a
    # TTY, so redirects/logs don't fill with carriage-return spam (SPEC: JSON to
    # stdout, progress to stderr).
    for rec in tqdm(records, desc=f"{year} FD/PTR", unit="pdf", disable=None):
        family = "ptr" if rec.filing_type.code == "P" else "fd"
        in_scope = family in types

        if not in_scope:
            # Out of scope for this --types run: leave unclassified, status ok.
            rec.pdf_class = None
            rec.parse_status = "ok"
        elif rec.source_pdf is None:
            # No DocID → no body was ever fetchable; treat as missing (SPEC §6.5).
            rec.pdf_class = "missing"
            rec.parse_status = "ok"
            unparsed.append(_unparsed_entry(rec, "missing"))
        else:
            pdf_path = data_dir / rec.source_pdf
            transactions = None
            fd_body = None
            try:
                pdf_class = classify(pdf_path)
                # E-filed bodies are extracted now, per family. PTR (filing type
                # P) → §6.3 transactions[]; fd-family → §6.3 schedules (annual FD).
                # An extraction failure here is the one e-filed path that lands in
                # the unparsed manifest (reason ``extract_failed``) — never a
                # crash, never a silent gap. A fd-family PDF with no schedule
                # headings (an extension cover sheet) is *not* a failure: it
                # stays efiled/ok with no body.
                if pdf_class == "efiled" and family == "ptr":
                    transactions = extract_ptr_transactions(pdf_path)
                elif pdf_class == "efiled" and family == "fd":
                    try:
                        fd_body = extract_fd_schedules(pdf_path)
                    except NotAnFdBody:
                        # No schedule headings. Benign for a cover-sheet/extension
                        # type (legitimately no body); but on an annual-report type
                        # (O/A) it means a real FD body's headings were lost — an
                        # invisible gap — so escalate to extract_failed.
                        if rec.filing_type.code in _ANNUAL_FD_CODES:
                            raise PdfExtractError(
                                f"annual-FD body for {pdf_path} (FilingType "
                                f"{rec.filing_type.code!r}) has no schedule headings "
                                "— extraction failed, not a cover sheet"
                            )
                        fd_body = None  # extension/cover sheet — no body, not error
            except PdfExtractError:
                rec.pdf_class = None
                rec.parse_status = "error"
                unparsed.append(_unparsed_entry(rec, "extract_failed"))
            else:
                rec.pdf_class = pdf_class
                rec.parse_status = "ok"
                if transactions is not None:
                    _write_ptr_body(parsed_dir, rec.doc_id, transactions)
                if fd_body is not None:
                    _write_fd_body(parsed_dir, rec.doc_id, fd_body)
                if pdf_class in ("scanned", "missing"):
                    unparsed.append(_unparsed_entry(rec, pdf_class))

        # Unknown FilingType is its own unparsed reason, independent of the PDF
        # outcome (the raw code is preserved on the record, never dropped).
        if in_scope and rec.filing_type.label == UNKNOWN_FILING_LABEL:
            unparsed.append(_unparsed_entry(rec, "unknown_type"))

    return unparsed


def parse_year(
    year: int, *, data_dir: Path, types: Optional[list[str]] = None, fetched_at: str
) -> Optional[dict]:
    """Parse one year's index into ``parsed/<year>/`` (SPEC §4). Offline.

    Reads ``<data_dir>/raw/<year>/<year>FD.xml`` (written by ``pull``). If the
    XML is absent this is a clean skip (clear stderr message, returns ``None``) —
    not a crash, so a multi-year range survives a missing year. Otherwise builds
    every ``<Member>`` into a record, detects identity collisions, **classifies
    each on-disk PDF** (efiled / scanned / missing, via authoritative text
    extraction — SPEC §2.2), and writes ``filings.json`` + ``parse-manifest.json``
    + ``unparsed-manifest.json``. Returns a compact summary dict.

    ``types`` restricts which families (``ptr`` / ``fd``) are classified this run;
    a filing outside it keeps ``pdf_class=None`` and is not deemed unparsed (it was
    out of scope), while counts still reconcile to the total. Defaults to both.

    The summary's ``has_error`` flag lets ``--strict`` exit non-zero when any
    filing errored (``parse_status="error"``).
    """
    if types is None:
        types = ["ptr", "fd"]
    raw_dir = data_dir / "raw" / str(year)
    xml_path = raw_dir / f"{year}FD.xml"
    if not xml_path.exists():
        print(
            f"{year}: index {xml_path} is missing; skipping "
            f"(run `openhouse pull {year}` first).",
            file=sys.stderr,
        )
        return None

    # Offline CC0 congress-legislators join (#16): attach bioguide where the
    # House seat matches; a missing reference cache simply matches nothing.
    legislators = load_legislator_index(data_dir)
    records = build_filing_records(xml_path, year, legislators)

    by_filing_type: dict[str, int] = defaultdict(int)
    for rec in records:
        by_filing_type[rec.filing_type.code] += 1
    filing_type_counts = dict(sorted(by_filing_type.items()))

    parsed_dir = data_dir / "parsed" / str(year)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    # Authoritative per-PDF classification (SPEC §2.2), mutating each record's
    # pdf_class / parse_status and collecting the unparsed-manifest entries.
    # E-filed PTR bodies are extracted and written here (parsed/<year>/ptr/).
    unparsed = _classify_records(
        records, data_dir=data_dir, types=types, year=year, parsed_dir=parsed_dir
    )

    by_pdf_class: dict[str, int] = defaultdict(int)
    not_classified = 0
    for rec in records:
        if rec.pdf_class is None:
            not_classified += 1
        else:
            by_pdf_class[rec.pdf_class] += 1
    # Stable key order (efiled/scanned/missing) so the manifest is byte-stable.
    pdf_class_counts = {
        k: by_pdf_class.get(k, 0) for k in ("efiled", "scanned", "missing")
    }

    by_parse_status: dict[str, int] = defaultdict(int)
    for rec in records:
        by_parse_status[rec.parse_status or "ok"] += 1
    parse_status_counts = {k: by_parse_status.get(k, 0) for k in ("ok", "error")}
    has_error = parse_status_counts["error"] > 0

    identity_warnings = _detect_identity_warnings(records)
    for warning in identity_warnings:
        print(
            f"{year}: identity warning — filer_id {warning['filer_id']!r} matched "
            f"no congress-legislators bioguide; it is name-keyed only "
            f"(names {warning['raw_names']}, docs {warning['doc_ids']}, "
            f"districts {warning['districts']}) — `read --member` on it is an "
            f"unverified name-string claim.",
            file=sys.stderr,
        )

    # JSON-mode dump so dates serialize as ISO strings; sort_keys + trailing
    # newline so two runs from the same raw produce byte-identical files.
    filings = [rec.model_dump(mode="json") for rec in records]
    filings_path = parsed_dir / "filings.json"
    filings_path.write_text(
        json.dumps(filings, indent=2, sort_keys=True) + "\n"
    )

    # Counts reconcile: efiled + scanned + missing + not_classified == total.
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": fetched_at,
        "year": year,
        "counts": {
            "total": len(records),
            "by_filing_type": filing_type_counts,
            "by_pdf_class": pdf_class_counts,
            "not_classified": not_classified,
            "by_parse_status": parse_status_counts,
        },
        "identity_warnings": identity_warnings,
    }
    manifest_path = parsed_dir / "parse-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    # Every filing not fully usable as an e-filed body, each with a reason (SPEC
    # §6.5). The one e-filed path that can appear here is an efiled PTR whose body
    # extraction failed (``extract_failed``); otherwise e-filed filings are not
    # listed. Order is deterministic (record order).
    unparsed_manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": fetched_at,
        "year": year,
        "unparsed": unparsed,
    }
    unparsed_path = parsed_dir / "unparsed-manifest.json"
    unparsed_path.write_text(
        json.dumps(unparsed_manifest, indent=2, sort_keys=True) + "\n"
    )

    print(
        f"{year}: parsed {len(records)} filings → {filings_path} "
        f"(efiled {pdf_class_counts['efiled']}, scanned "
        f"{pdf_class_counts['scanned']}, missing {pdf_class_counts['missing']}, "
        f"error {parse_status_counts['error']}; {len(identity_warnings)} identity "
        f"warning(s); manifests: {manifest_path}, {unparsed_path}).",
        file=sys.stderr,
    )
    return {
        "year": year,
        "total": len(records),
        "by_filing_type": filing_type_counts,
        "by_pdf_class": pdf_class_counts,
        "not_classified": not_classified,
        "by_parse_status": parse_status_counts,
        "identity_warnings": len(identity_warnings),
        "has_error": has_error,
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

    ``types`` restricts which PDF families are classified (out-of-scope filings
    stay ``pdf_class=None`` but still count toward the total). ``fetched_at`` is
    the single entry-time timestamp threaded into the manifest (SPEC §9: no
    wall-clock in core logic).

    Emits one compact JSON summary object (the per-year results) to **stdout**
    (machine-composable, CLAUDE.md "JSON to stdout"); progress / warnings go to
    stderr. With ``strict``, returns :data:`STRICT_ERROR_EXIT` if any filing in
    any year errored (``parse_status="error"``); otherwise returns ``0``.
    """
    # A --types subset leaves the excluded family unclassified; `parse` rewrites
    # filings.json wholesale ("re-parse, not migrate"), so a later partial run
    # downgrades the other family's prior classification. Say so once, up front.
    excluded = [f for f in ("ptr", "fd") if f not in types]
    if excluded:
        print(
            f"note: --types excludes {', '.join(excluded)}; those filings are left "
            f"unclassified (pdf_class=null) and a re-run without --types is needed "
            f"for full classification.",
            file=sys.stderr,
        )

    summaries: list[dict] = []
    skipped: list[int] = []
    for year in years:
        summary = parse_year(
            year, data_dir=data_dir, types=types, fetched_at=fetched_at
        )
        if summary is None:
            skipped.append(year)
        else:
            summaries.append(summary)

    any_error = any(s["has_error"] for s in summaries)

    combined = {
        "command": "parse",
        "generated_at": fetched_at,
        "years": summaries,
        "skipped_years": skipped,
    }
    print(json.dumps(combined, indent=2, sort_keys=True))

    if not summaries:
        # Nothing was parsed at all — every requested year's index was missing.
        print(
            f"error: no years parsed; indices absent for {skipped} "
            f"(run `openhouse pull` first).",
            file=sys.stderr,
        )
        return NOTHING_PARSED_EXIT

    if strict and any_error:
        print(
            "error: --strict and one or more filings errored "
            "(parse_status='error'); see unparsed-manifest.json.",
            file=sys.stderr,
        )
        return STRICT_ERROR_EXIT
    return 0
