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
from .legislators import LegislatorIndex, load_legislator_index
from .pdf import (
    FALLBACK_MAX_YEAR,
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


# Reason buckets for an unmatched filer (GH-0122). The matcher fails for several
# reasons, and most of them are *expected* — only ``suspicious`` (a seat that IS
# held by a known rep, but whose holder's name didn't match the filer) is an
# actionable likely-missed-link. The two orderings differ on purpose:
#   * priority — when a single filer's filings classify differently, surface the
#     most actionable; ``suspicious`` always wins so it can never hide behind a
#     candidate filing by the same name key.
#   * display — fixed left-to-right order for the summary line, expected-first so
#     the eye lands on the ``suspicious`` punchline at the end.
_REASON_PRIORITY = (
    "suspicious",
    "ambiguous_seat",
    "unknown_seat",
    "candidate",
    "no_district",
)
_REASON_ORDER = (
    "candidate",
    "no_district",
    "unknown_seat",
    "ambiguous_seat",
    "suspicious",
)


def _classify_unmatched(
    rec: FilingMetadata, legislators: Optional[LegislatorIndex]
) -> str:
    """Why did *this* filing fail to match a bioguide? One of ``_REASON_ORDER``.

    Candidate reports (``FilingType C``) are demoted by design (a challenger must
    not be pinned to the incumbent — see ``index.build_filing_records``), so they
    are expected non-matches regardless of seat. Everything else defers to the seat
    classification; with no reference index loaded no seat is "occupied", so a
    real seat reads as ``unknown_seat`` (honest — we know of no holder).
    """
    if rec.filing_type.code == "C":
        return "candidate"
    sd = rec.state_district
    if sd is None:
        return "no_district"
    if legislators is None:
        return "unknown_seat"
    return legislators.classify_seat(last=rec.filer.last, state=sd.state, district=sd.district)


def _suspicious_seats(
    group: list[FilingMetadata],
    reasons: list[str],
    legislators: Optional[LegislatorIndex],
) -> list[dict]:
    """The occupied seats that make a filer suspicious, with their roster holders.

    One entry per distinct ``(state, district)`` whose filing classified
    ``suspicious``, carrying the rep(s) on record for that seat so an operator can
    eyeball the likely variant/typo. Deterministic (first-appearance seat order).
    """
    seats: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for rec, reason in zip(group, reasons):
        if reason != "suspicious" or rec.state_district is None:
            continue
        sd = rec.state_district
        key = (sd.state, sd.district)
        if key in seen:
            continue
        seen.add(key)
        holders = legislators.seat_holders(sd.state, sd.district) if legislators else ()
        seats.append(
            {
                "state": sd.state,
                "district": sd.district,
                "holders": [{"bioguide": b, "last": last} for last, b in holders],
            }
        )
    return seats


def _detect_identity_warnings(
    records: list[FilingMetadata],
    legislators: Optional[LegislatorIndex] = None,
) -> list[dict]:
    """Surface filers that matched **no** congress-legislators bioguide (#16).

    The actionable identity signal since #16 is *unmatched* identity: a filer
    whose ``bioguide_id`` is ``None`` is keyed only by name (``name:<slug>``) — a
    bounded, unverified claim. GH-0122 sharpens it: each unmatched filer carries a
    classified ``reason`` (``_REASON_ORDER``) so the *expected* non-matches (a
    candidate, a seatless filer, a seat no rep ever held) can be collapsed to a
    count and only the ``suspicious`` ones — seat occupied, name didn't match —
    are surfaced per-name. A ``suspicious`` entry also carries the occupied
    ``seats`` and their roster holders.

    One entry per *distinct* unmatched ``filer_id`` (the ``name:`` key), in
    first-appearance order (deterministic), carrying its reason, distinct raw
    names, the involved ``doc_ids``, and the distinct districts seen. A
    bioguide-matched filer is never listed — it is pinned to a stable identity,
    however many times it filed.
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
        reasons = [_classify_unmatched(r, legislators) for r in group]
        # A filer's filings can classify differently; surface the most actionable.
        reason = next(r for r in _REASON_PRIORITY if r in reasons)

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

        entry = {
            "filer_id": filer_id,
            "reason": reason,
            "raw_names": seen_names,
            "doc_ids": [r.doc_id for r in group],
            "districts": sorted(d for d in districts if d is not None),
        }
        if reason == "suspicious":
            entry["seats"] = _suspicious_seats(group, reasons, legislators)
        warnings.append(entry)
    return warnings


def _match_summary(records: list[FilingMetadata], warnings: list[dict]) -> dict:
    """Roll the per-filer warnings up into a manifest summary (GH-0122).

    Identity-level (not per-filing): ``matched`` is the count of distinct
    bioguides pinned, ``unmatched`` the count of distinct ``name:`` keys (==
    ``len(warnings)``), ``by_reason`` the breakdown over ``_REASON_ORDER``, and
    ``suspicious`` the list of the suspicious filer_ids (their full seat detail
    lives on the matching ``identity_warnings`` entry — not duplicated here).
    """
    matched = len({r.bioguide_id for r in records if r.bioguide_id is not None})
    by_reason = {reason: 0 for reason in _REASON_ORDER}
    for w in warnings:
        by_reason[w["reason"]] += 1
    return {
        "matched": matched,
        "unmatched": len(warnings),
        "by_reason": by_reason,
        "suspicious": [w["filer_id"] for w in warnings if w["reason"] == "suspicious"],
    }


def _format_suspicious_seat(seat: dict) -> str:
    """``WA-07 held by Smith (S001234)`` — holders capped so the line stays short."""
    loc = f"{seat['state']}-{seat['district']:02d}"
    holders = seat["holders"]
    shown = ", ".join(f"{h['last']} ({h['bioguide']})" for h in holders[:4])
    if len(holders) > 4:
        shown += f", +{len(holders) - 4} more"
    return f"{loc} held by {shown}" if shown else f"{loc} (no roster holder)"


def _print_identity_report(
    year: int, summary: dict, warnings: list[dict]
) -> None:
    """The two-tier identity report to stderr (GH-0122).

    One summary line collapses the expected non-matches to a per-reason count;
    then a per-name line for *only* the ``suspicious`` filers — the ones a human
    should actually look at. The full per-filer detail is always in the manifest.
    """
    counts = summary["by_reason"]
    breakdown = ", ".join(f"{counts[r]} {r}" for r in _REASON_ORDER)
    print(
        f"{year}: identity — {summary['matched']} matched, "
        f"{summary['unmatched']} unmatched ({breakdown}).",
        file=sys.stderr,
    )
    for w in warnings:
        if w["reason"] != "suspicious":
            continue
        seats = "; ".join(_format_suspicious_seat(s) for s in w.get("seats", []))
        print(
            f"{year}: SUSPICIOUS identity — filer {w['filer_id']!r} "
            f"(names {w['raw_names']}) is unmatched, but its seat is on record: "
            f"{seats}. Likely a name variant/typo or roster gap — `read --member` "
            f"on it is an unverified name-string claim.",
            file=sys.stderr,
        )


def _unparsed_entry(rec: FilingMetadata, reason: str) -> dict:
    """One unparsed-manifest entry (SPEC §6.5).

    Carries ``filer_id`` alongside ``doc_id`` so a no-DocID row (``doc_id == ""``)
    — of which a year can have several — is still joinable back to its
    ``filings.json`` record, which a bare empty ``doc_id`` could not do.
    """
    return {"doc_id": rec.doc_id, "filer_id": rec.filer_id, "reason": reason}


def _remove_stale_body(parsed_dir: Path, doc_id: str, family: str) -> None:
    """Delete a body file left by an earlier parse generation, if present.

    Called whenever the current run produces **no** body for a filing (an
    extraction failure, a scanned/missing PDF, a bodyless cover sheet): a
    leftover ``parsed/<year>/{ptr,fd}/<DocID>.json`` from a previous run would
    otherwise masquerade as current data beside a manifest that says otherwise
    (GH-0070 — observed with bodies the completeness guard now rejects).
    """
    stale = parsed_dir / family / f"{doc_id}.json"
    stale.unlink(missing_ok=True)


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


def _date_anomaly(body_dicts: list[dict]) -> bool:
    """True if any extracted row carries a preserved out-of-range date string.

    The per-row anomaly flag (GH-0113) is a set ``*_raw`` date field: the
    structured date was rejected by the sanity range and the verbatim string kept
    in its place. One such row makes the whole filing carry a ``date_out_of_range``
    residual entry — the filing is never dropped, just flagged.
    """
    keys = ("date_raw", "notification_date_raw", "transaction_date_raw")
    return any(row.get(k) for row in body_dicts for k in keys)


def _classify_records(
    records: list[FilingMetadata],
    *,
    data_dir: Path,
    types: list[str],
    year: int,
    parsed_dir: Path,
    max_year: int = FALLBACK_MAX_YEAR,
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
            _remove_stale_body(parsed_dir, rec.doc_id, family)
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
                    transactions = extract_ptr_transactions(
                        pdf_path, max_year=max_year
                    )
                elif pdf_class == "efiled" and family == "fd":
                    try:
                        fd_body = extract_fd_schedules(pdf_path, max_year=max_year)
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
                # No body this run — remove any stale one (see _remove_stale_body).
                _remove_stale_body(parsed_dir, rec.doc_id, family)
            else:
                rec.pdf_class = pdf_class
                rec.parse_status = "ok"
                if transactions is not None:
                    _write_ptr_body(parsed_dir, rec.doc_id, transactions)
                    # An out-of-range date in any row is flagged in place (the row
                    # keeps its raw string, the structured date is None) — the
                    # filing is sound otherwise, so it stays ok with a residual
                    # entry rather than being dropped (GH-0113 / CLAUDE.md).
                    if _date_anomaly(
                        [t.model_dump(mode="json") for t in transactions]
                    ):
                        unparsed.append(_unparsed_entry(rec, "date_out_of_range"))
                elif fd_body is not None:
                    _write_fd_body(parsed_dir, rec.doc_id, fd_body)
                    b_items = fd_body.schedules.get("B", [])
                    if _date_anomaly(b_items):
                        unparsed.append(_unparsed_entry(rec, "date_out_of_range"))
                else:
                    # No body this run for either family (scanned/missing PDF,
                    # bodyless cover sheet, empty outcome) — drop any stale one
                    # (see _remove_stale_body).
                    _remove_stale_body(parsed_dir, rec.doc_id, family)
                if pdf_class in ("scanned", "missing"):
                    unparsed.append(_unparsed_entry(rec, pdf_class))

        # Unknown FilingType is its own unparsed reason, independent of the PDF
        # outcome (the raw code is preserved on the record, never dropped).
        if in_scope and rec.filing_type.label == UNKNOWN_FILING_LABEL:
            unparsed.append(_unparsed_entry(rec, "unknown_type"))

    return unparsed


def parse_year(
    year: int,
    *,
    data_dir: Path,
    types: Optional[list[str]] = None,
    fetched_at: str,
    entry_year: int = FALLBACK_MAX_YEAR - 1,
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

    # Date sanity-range upper bound (GH-0113): the command-entry year + 1, NOT a
    # wall-clock read (SPEC §9 / CLAUDE.md — one timestamp captured at entry,
    # threaded down). A disclosure date whose year exceeds it (or falls below
    # 1990) is an extraction artifact, flagged in place rather than emitted valid.
    max_year = entry_year + 1

    # Offline CC0 congress-legislators join (#16): attach bioguide where the
    # House seat matches; a missing reference cache simply matches nothing.
    legislators = load_legislator_index(data_dir)
    records = build_filing_records(xml_path, year, legislators, max_year=max_year)

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
        records,
        data_dir=data_dir,
        types=types,
        year=year,
        parsed_dir=parsed_dir,
        max_year=max_year,
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

    identity_warnings = _detect_identity_warnings(records, legislators)
    match_summary = _match_summary(records, identity_warnings)
    _print_identity_report(year, match_summary, identity_warnings)

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
        "match_summary": match_summary,
        "identity_warnings": identity_warnings,
    }
    manifest_path = parsed_dir / "parse-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    # Every filing not fully usable as an e-filed body, each with a reason (SPEC
    # §6.5). Two e-filed paths can appear here: an efiled PTR whose body extraction
    # failed (``extract_failed``), and a filing with a written body but an
    # out-of-range date flagged in place (``date_out_of_range``). The latter keeps
    # ``parse_status: ok`` and a body, so it is the one reason that coexists with a
    # fully parsed filing (GH-0113); otherwise e-filed filings are not listed.
    # Order is deterministic (record order).
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
        f"error {parse_status_counts['error']}; {match_summary['unmatched']} "
        f"unmatched identity, {len(match_summary['suspicious'])} suspicious; "
        f"manifests: {manifest_path}, {unparsed_path}).",
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
        "suspicious": len(match_summary["suspicious"]),
        "has_error": has_error,
    }


def parse(
    years: list[int],
    *,
    data_dir: Path,
    types: list[str],
    strict: bool,
    fetched_at: str,
    entry_year: int = FALLBACK_MAX_YEAR - 1,
) -> int:
    """Run ``openhouse parse`` for ``years`` (SPEC §4). Returns a process exit code.

    Entirely offline and deterministic: each year reads only ``raw/<year>/`` and
    writes ``parsed/<year>/``, and a re-run from the same ``raw/`` produces
    byte-identical output. A year whose index XML is absent is a clean skip, not
    a crash, so a range survives a not-yet-pulled year.

    ``types`` restricts which PDF families are classified (out-of-scope filings
    stay ``pdf_class=None`` but still count toward the total). ``fetched_at`` is
    the single entry-time timestamp threaded into the manifest (SPEC §9: no
    wall-clock in core logic); ``entry_year`` is that same timestamp's year,
    threaded down to bound the date sanity range (GH-0113) — ``entry_year + 1``
    is the upper bound, again never a fresh wall-clock read.

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
            year,
            data_dir=data_dir,
            types=types,
            fetched_at=fetched_at,
            entry_year=entry_year,
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
