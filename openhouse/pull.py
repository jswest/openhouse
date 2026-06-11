"""Acquisition: the polite httpx client + the ``--index-only`` path (SPEC §3).

This module owns the *only* network step in the product. Everything here is
built around the polite-crawling floor settled in the #1 thread and recorded in
SPEC §3:

- **sequential, concurrency 1** — one request at a time;
- **2.5 s between requests** — just above congress.gov's published
  ``Crawl-delay: 2`` (the House publishes no policy of its own, and there is no
  bulk PDF download to fall back on);
- **descriptive User-Agent** with a contact flow, logged to stderr at startup;
- **exponential backoff on 429/5xx** — the server is asking us to retry later;
- **403 is an immediate hard error** — never retried, no backoff: the server is
  refusing us, and hammering it again is the opposite of polite.

``--delay`` / ``--concurrency`` exist as deliberate, documented overrides; the
defaults are the politeness floor and must not be weakened to go faster.

Scope: for each year, download ``<YEAR>FD.zip`` and extract ``<YEAR>FD.xml`` +
``<YEAR>FD.txt`` into ``<data-dir>/raw/<year>/`` (issue #3), then enumerate
``(DocID, FilingType)`` and download each referenced PDF, routed by the §2.2
rule into ``raw/<year>/{ptr,fd}/``, recording a ``pull-manifest.json`` per year
(issue #4). No PDF *content* parsing — bytes only.

Testability: the network is reached only through an injected ``httpx.Client``
(tests pass one wired to ``httpx.MockTransport``, so nothing here touches the
live Clerk), and the politeness sleep is an injected callable (tests pass a
no-op so the suite never actually sleeps 2.5 s). No wall-clock lives in core
logic beyond the single ``fetched_at`` timestamp threaded in from the caller.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

import httpx

from . import __version__
from .index import IndexTarget, enumerate_targets

# SPEC §2.1: one ZIP per year, refreshed daily.
INDEX_URL_TEMPLATE = (
    "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
)

# SPEC §2.2: report bodies are PDFs addressed by DocID, routed by FilingType.
# ``P`` → ptr-pdfs; all other types → financial-pdfs.
PTR_PDF_URL_TEMPLATE = (
    "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
)
FD_PDF_URL_TEMPLATE = (
    "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}/{doc_id}.pdf"
)

# The two PDF families a --types filter can select (SPEC §3). Default is both.
PDF_FAMILIES = ("ptr", "fd")

# Polite-crawling floor (SPEC §3). Overridable via --delay / --concurrency, but
# these are the defaults and the floor.
DEFAULT_DELAY_SECONDS = 2.5
DEFAULT_CONCURRENCY = 1

# Backoff for 429/5xx (SPEC §3). A 403 never reaches this path.
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0

REPO_URL = "https://github.com/jswest/openhouse"

# A loose email matcher for the required contact — enough to insist a real
# address is present (not RFC-perfect validation, which would reject valid
# addresses and isn't the point: the Clerk just needs a reachable operator).
_CONTACT_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class PullError(Exception):
    """A pull failed in a way the user must see (printed to stderr, non-zero exit)."""


# ---------------------------------------------------------------------------
# User-Agent flow (SPEC §3)
# ---------------------------------------------------------------------------
def build_user_agent(
    contact: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> str:
    """Construct the User-Agent header per the SPEC §3 flow.

    - ``user_agent`` (``--user-agent``) overrides everything when given — the
      caller takes full responsibility for identifying themselves.
    - otherwise a **contact (name + email)** is **required** and the header is
      ``openhouse/<version> (+<repo>; contact: <Name> <email>)``.

    The contact is mandatory because the repo URL alone is the *same* for every
    operator: an anonymous shared User-Agent gives the Clerk no way to tell
    concurrent crawlers apart, so it may rate-limit or block all of them at once.
    A real name + email lets a server admin reach the actual operator instead.
    Raises :class:`PullError` (never crawls) if the contact is missing or lacks
    either a name or an email.
    """
    if user_agent:
        return user_agent
    if not contact or not contact.strip():
        raise PullError(
            "pull needs a contact so the Clerk can identify who is crawling. "
            "Without one, every openhouse user shares one anonymous User-Agent — "
            "the Clerk can't tell concurrent operators apart and may block all of "
            "them at once. Provide your name and email via "
            '--contact "Your Name <you@example.com>" or the OPENHOUSE_CONTACT '
            "environment variable. (Or pass --user-agent to set the whole header "
            "yourself.)"
        )
    contact = contact.strip()
    match = _CONTACT_EMAIL_RE.search(contact)
    if not match:
        raise PullError(
            f"--contact {contact!r} has no email address. The Clerk needs a real "
            'contact — pass a name and email, e.g. '
            '--contact "Jane Doe <jane@example.com>".'
        )
    name = contact.replace(match.group(0), "").strip(" \t<>")
    if not name:
        raise PullError(
            f"--contact {contact!r} has no name. The Clerk needs a person to "
            'reach, not just an inbox — e.g. --contact "Jane Doe '
            '<jane@example.com>".'
        )
    return f"openhouse/{__version__} (+{REPO_URL}; contact: {contact})"


# ---------------------------------------------------------------------------
# The polite GET: backoff on 429/5xx, hard error on 403.
# ---------------------------------------------------------------------------
def polite_get(
    client: httpx.Client,
    url: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    max_retries: int = MAX_RETRIES,
    backoff_base: float = BACKOFF_BASE_SECONDS,
    allow_not_found: bool = False,
) -> httpx.Response:
    """GET ``url`` with the SPEC §3 retry policy.

    - **403** → :class:`PullError` immediately, no retry, no backoff (the server
      is refusing us; the message explains likely causes).
    - **429 / 5xx** → exponential backoff (``backoff_base * 2**attempt``), up to
      ``max_retries`` times, then :class:`PullError`.
    - **2xx** → returned.
    - **404** → returned (not raised) when ``allow_not_found`` is set, so the PDF
      loop can record it as a non-fatal manifest gap (#4); otherwise (e.g. the
      index ZIP) it is a :class:`PullError`, no retry.
    - other 4xx → :class:`PullError`, no retry (a retry would not help).

    ``sleep`` is injected so tests don't actually wait.
    """
    last_status: Optional[int] = None
    for attempt in range(max_retries + 1):
        response = client.get(url)
        status = response.status_code

        if status == 403:
            raise PullError(
                f"403 Forbidden from {url}. The Clerk is refusing this client; "
                f"this is never retried. Likely causes: a missing or blocked "
                f"User-Agent, or pacing the server distrusts. Provide a contact "
                f"(OPENHOUSE_CONTACT / --contact), keep the polite defaults "
                f"(sequential, {DEFAULT_DELAY_SECONDS}s), and try later."
            )

        if status == 404 and allow_not_found:
            return response

        if 200 <= status < 300:
            return response

        last_status = status
        retriable = status == 429 or 500 <= status < 600
        if not retriable or attempt == max_retries:
            break

        wait = backoff_base * (2 ** attempt)
        print(
            f"warning: {status} from {url}; backing off {wait:.1f}s "
            f"(attempt {attempt + 1}/{max_retries})",
            file=sys.stderr,
        )
        sleep(wait)

    raise PullError(
        f"giving up on {url} after {max_retries} retries; last status "
        f"{last_status}."
    )


# ---------------------------------------------------------------------------
# Index download + extraction
# ---------------------------------------------------------------------------
def _extract_index_zip(zip_bytes: bytes, year: int, dest_dir: Path) -> list[str]:
    """Extract ``<year>FD.xml`` and ``<year>FD.txt`` from the index ZIP.

    Returns the list of written filenames. Raises :class:`PullError` if the XML
    member is absent (a structurally wrong ZIP — never silently a gap).
    """
    written: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise PullError(f"{year}FD.zip is not a valid ZIP: {exc}") from exc

    names = set(archive.namelist())
    xml_name = f"{year}FD.xml"
    if xml_name not in names:
        raise PullError(
            f"{year}FD.zip does not contain {xml_name} (members: "
            f"{sorted(names)})."
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    # The .xml is required; the .txt companion is written when present.
    for member in (xml_name, f"{year}FD.txt"):
        if member in names:
            (dest_dir / member).write_bytes(archive.read(member))
            written.append(member)
    return written


def pull_index_year(
    client: httpx.Client,
    year: int,
    data_dir: Path,
    *,
    force: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Download + extract one year's index ZIP into ``raw/<year>/``.

    Idempotent: if ``<year>FD.xml`` is already present it is *not* re-fetched
    unless ``force`` is set (the Clerk refreshes the ZIP daily, so a re-pull is
    a deliberate ``--force`` choice). Returns a small status dict for logging /
    manifests.
    """
    year_dir = data_dir / "raw" / str(year)
    xml_path = year_dir / f"{year}FD.xml"

    if xml_path.exists() and not force:
        print(
            f"{year}: index present ({xml_path}); skipping "
            f"(re-fetch with --force).",
            file=sys.stderr,
        )
        return {"year": year, "status": "skipped", "files": []}

    url = INDEX_URL_TEMPLATE.format(year=year)
    print(f"{year}: fetching {url}", file=sys.stderr)
    response = polite_get(client, url, sleep=sleep)
    written = _extract_index_zip(response.content, year, year_dir)
    print(
        f"{year}: extracted {', '.join(written)} into {year_dir}",
        file=sys.stderr,
    )
    return {"year": year, "status": "fetched", "files": written}


# ---------------------------------------------------------------------------
# PDF body download (issue #4)
# ---------------------------------------------------------------------------
def pdf_url_for(target: IndexTarget) -> str:
    """The §2.2 download URL for ``target``, routed by its raw FilingType.

    ``P`` → ``ptr-pdfs/<year>/<DocID>.pdf``; all other types →
    ``financial-pdfs/<year>/<DocID>.pdf``. Routing keys on the raw FilingType
    letter (via :attr:`IndexTarget.family`), never on the DocID.
    """
    template = PTR_PDF_URL_TEMPLATE if target.family == "ptr" else FD_PDF_URL_TEMPLATE
    return template.format(year=target.year, doc_id=target.doc_id)


def pull_pdfs_year(
    client: httpx.Client,
    year: int,
    data_dir: Path,
    fetched_at: str,
    *,
    types: Iterable[str] = PDF_FAMILIES,
    force: bool = False,
    delay: float = DEFAULT_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Download every referenced PDF for ``year`` into ``raw/<year>/{ptr,fd}/``.

    Enumerates ``(DocID, FilingType)`` from the on-disk ``<year>FD.xml`` (#3 has
    already written it), routes each by §2.2, and fetches the body unless it is
    already present and size-consistent. Writes ``raw/<year>/pull-manifest.json``
    (SPEC §6.5) with one entry per DocID: URL, HTTP status, byte size, sha256,
    and the single entry-time ``fetched_at`` threaded in from the caller.

    Resumability: a target whose file is already present and whose size matches
    the recorded manifest entry is **skipped** with no network request. A present
    file with *no* manifest entry (a prior run whose manifest write was lost) is
    backfilled into the manifest from the file itself rather than re-downloaded;
    a present file whose size *disagrees* with the manifest (a partial transfer)
    is re-downloaded. A previously recorded 404 is honored without re-requesting
    it. ``force`` re-downloads regardless. ``types`` filters which families to
    fetch.

    A **404 is non-fatal**: it is recorded in the manifest with its status (a
    recorded gap, never a silent one) and the run continues. A 403 / exhausted
    backoff still raises :class:`PullError` via :func:`polite_get`.
    """
    year_dir = data_dir / "raw" / str(year)
    xml_path = year_dir / f"{year}FD.xml"
    if not xml_path.exists():
        raise PullError(
            f"{year}: index {xml_path} is missing; cannot enumerate PDFs "
            f"(run pull without --index-only, or pull the index first)."
        )

    selected = set(types)
    manifest_path = year_dir / "pull-manifest.json"
    # Resume across interrupted runs: keep prior manifest entries (e.g. recorded
    # 404s) so a Ctrl-C mid-year never loses what was already learned.
    manifest = _load_manifest(manifest_path)

    # Group the selected targets by data type (family) so each gets its own
    # progress bar; everything outside the requested --types is `filtered`.
    by_family: dict[str, list[IndexTarget]] = {fam: [] for fam in PDF_FAMILIES}
    filtered = 0
    for target in enumerate_targets(xml_path, year):
        if target.family in selected and target.family in by_family:
            by_family[target.family].append(target)
        else:
            filtered += 1

    counts = {"fetched": 0, "backfilled": 0, "skipped": 0, "not_found": 0}
    # The manifest is written in a ``finally`` so an interrupted year (Ctrl-C, a
    # 403, exhausted backoff) never loses what was already fetched — on resume
    # the on-disk files are reconciled against it (SPEC §3: safe to Ctrl-C).
    try:
        for family in PDF_FAMILIES:
            targets = by_family[family]
            if family not in selected or not targets:
                continue
            total = len(targets)
            # Live per-family bar so the operator can see where the crawl is in
            # each data type; skips/backfills make it race ahead on a resume —
            # exactly the "where are we" signal wanted. (TTY only; see helper.)
            _render_progress(year, family, 0, total)
            for done, target in enumerate(targets, start=1):
                outcome = _process_pdf_target(
                    target, manifest, year_dir, client, fetched_at,
                    force=force, delay=delay, sleep=sleep,
                )
                counts[outcome] += 1
                _render_progress(year, family, done, total)
            _finish_progress(year, family, total)
    finally:
        _write_manifest(manifest_path, manifest, year, fetched_at)

    print(
        f"{year}: PDFs — {counts['fetched']} fetched, "
        f"{counts['backfilled']} backfilled, {counts['skipped']} present/skipped, "
        f"{counts['not_found']} not-found, {filtered} filtered "
        f"(manifest: {manifest_path}).",
        file=sys.stderr,
    )
    return {
        "year": year,
        "fetched": counts["fetched"],
        "backfilled": counts["backfilled"],
        "skipped": counts["skipped"],
        "not_found": counts["not_found"],
        "filtered": filtered,
    }


def _process_pdf_target(
    target: IndexTarget,
    manifest: dict,
    year_dir: Path,
    client: httpx.Client,
    fetched_at: str,
    *,
    force: bool,
    delay: float,
    sleep: Callable[[float], None],
) -> str:
    """Acquire one PDF target, mutating ``manifest``; return its outcome.

    Outcome is one of ``"fetched"`` / ``"backfilled"`` / ``"skipped"`` /
    ``"not_found"`` (the progress bar and year summary tally these). The
    resumability rules live here: a recorded 404 or a size-consistent present
    file is honored with no request; a present-but-unrecorded file is backfilled
    from disk; a size-mismatched file is re-downloaded. Raises :class:`PullError`
    on a 403 / exhausted backoff via :func:`polite_get`.
    """
    entry = manifest.get(target.doc_id)
    dest = year_dir / target.family / f"{target.doc_id}.pdf"

    if not force:
        # A previously recorded 404 has no file and no PDF to fetch; honor the
        # recorded gap without re-requesting a known-dead URL (SPEC §11).
        if entry is not None and entry.get("status") == 404:
            return "not_found"
        if dest.exists() and dest.stat().st_size > 0:
            size = dest.stat().st_size
            if entry is not None and entry.get("bytes") == size:
                return "skipped"  # present and size-consistent with the manifest
            if entry is None:
                # On disk but unrecorded — a prior run's manifest write was lost.
                # Backfill from the file itself (its real size + sha256) rather
                # than re-download it or leave an unrecorded gap.
                manifest[target.doc_id] = _entry_from_disk(
                    target, pdf_url_for(target), dest, fetched_at
                )
                return "backfilled"
            # Present but size disagrees with the manifest → a partial / corrupt
            # transfer; fall through and re-download it.

    url = pdf_url_for(target)
    # Pace before every *network* request (polite floor, SPEC §3). Skipped /
    # backfilled / recorded-404 targets cost no request, so they consume no
    # pacing delay; the across-year delay is held separately by pull().
    sleep(delay)
    response = polite_get(client, url, sleep=sleep, allow_not_found=True)
    if response.status_code == 404:
        # A 404 is the one non-fatal HTTP outcome — some index rows have no PDF.
        # Record it with its status (a gap, never silent).
        _record_404(manifest, target, url, fetched_at)
        return "not_found"

    content = response.content
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    manifest[target.doc_id] = {
        "doc_id": target.doc_id,
        "filing_type": target.filing_type,
        "family": target.family,
        "url": url,
        "status": response.status_code,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "fetched_at": fetched_at,
    }
    return "fetched"


_PROGRESS_WIDTH = 28


def _render_progress(year: int, family: str, done: int, total: int) -> None:
    """Draw a live per-family progress bar on stderr, in place (TTY only).

    No-op when stderr is not a TTY (piped / redirected) so logs aren't polluted
    with carriage returns — the per-family and per-year summary lines carry the
    information there instead.
    """
    if total == 0 or not sys.stderr.isatty():
        return
    filled = _PROGRESS_WIDTH * done // total
    bar = "#" * filled + "." * (_PROGRESS_WIDTH - filled)
    print(
        f"\r{year} {family}: [{bar}] {done}/{total} ({100 * done // total:3d}%)",
        end="",
        file=sys.stderr,
        flush=True,
    )


def _finish_progress(year: int, family: str, total: int) -> None:
    """Close a family's progress line with a completed summary (TTY or not).

    On a TTY the leading ``\\r`` overwrites the live bar with the final line; in
    a log it's a plain line, so non-interactive runs still get per-family marks.
    """
    prefix = "\r" if sys.stderr.isatty() else ""
    print(f"{prefix}{year} {family}: {total}/{total} done.", file=sys.stderr)


def _record_404(
    manifest: dict, target: IndexTarget, url: str, fetched_at: str
) -> None:
    """Record a non-fatal 404 in the manifest (a recorded gap, never silent).

    Deliberately quiet: a per-404 stderr line would shred the live progress bar,
    and the durable record is the manifest entry — the per-year summary reports
    the not-found count.
    """
    manifest[target.doc_id] = {
        "doc_id": target.doc_id,
        "filing_type": target.filing_type,
        "family": target.family,
        "url": url,
        "status": 404,
        "bytes": 0,
        "sha256": None,
        "fetched_at": fetched_at,
    }


def _entry_from_disk(
    target: IndexTarget, url: str, dest: Path, fetched_at: str
) -> dict:
    """Backfill a manifest entry from a PDF already on disk.

    Used on resume when a prior run downloaded the file but its manifest write
    was lost (e.g. Ctrl-C before the year finished). The file on disk is the
    truth, so record its real size + sha256 rather than leave it an unrecorded
    gap or pay a redundant re-download.
    """
    content = dest.read_bytes()
    return {
        "doc_id": target.doc_id,
        "filing_type": target.filing_type,
        "family": target.family,
        "url": url,
        "status": 200,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "fetched_at": fetched_at,
    }


def _load_manifest(manifest_path: Path) -> dict:
    """Load an existing pull-manifest's ``filings`` map, or an empty one."""
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return dict(data.get("filings", {}))


def _write_manifest(
    manifest_path: Path, filings: dict, year: int, fetched_at: str
) -> None:
    """Write ``raw/<year>/pull-manifest.json`` (SPEC §6.5)."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "year": year,
        "fetched_at": fetched_at,
        "count": len(filings),
        "filings": filings,
    }
    manifest_path.write_text(json.dumps(document, indent=2, sort_keys=True))


def pull(
    years: list[int],
    *,
    data_dir: Path,
    index_only: bool,
    delay: float = DEFAULT_DELAY_SECONDS,
    concurrency: int = DEFAULT_CONCURRENCY,
    contact: Optional[str] = None,
    user_agent: Optional[str] = None,
    force: bool = False,
    types: Iterable[str] = PDF_FAMILIES,
    fetched_at: Optional[str] = None,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run ``openhouse pull`` for ``years`` (SPEC §3). Returns a process exit code.

    Per year: fetch + extract the index ZIP, then (unless ``index_only``)
    enumerate ``(DocID, FilingType)`` from the on-disk XML and download each
    referenced PDF, routed by the §2.2 rule into ``raw/<year>/{ptr,fd}/``, and
    write ``raw/<year>/pull-manifest.json`` (SPEC §6.5). ``types`` filters which
    PDF families to fetch (default both).

    ``fetched_at`` is the single entry-time timestamp threaded into every
    manifest entry (SPEC §9: no wall-clock in core logic). The caller (the CLI)
    captures it once; it defaults here to one ``datetime.now()`` read only so an
    ad-hoc call still works — production always passes it in.

    The ``client`` and ``sleep`` seams keep this fully offline-testable: tests
    pass an ``httpx.Client`` wired to ``httpx.MockTransport`` and a no-op sleep.
    """
    if fetched_at is None:
        fetched_at = datetime.now().isoformat()
    ua = build_user_agent(contact=contact, user_agent=user_agent)
    print(f"pull: User-Agent: {ua}", file=sys.stderr)
    if delay != DEFAULT_DELAY_SECONDS:
        print(
            f"pull: delay overridden — {delay}s "
            f"(polite floor is {DEFAULT_DELAY_SECONDS}s).",
            file=sys.stderr,
        )
    if concurrency != DEFAULT_CONCURRENCY:
        # v0.1 is sequential-only; the flag is accepted (SPEC §3 reserves it) but
        # not yet wired, so say so plainly rather than imply parallel fetching.
        print(
            f"pull: --concurrency {concurrency} is not yet implemented; "
            f"running sequentially (concurrency {DEFAULT_CONCURRENCY}).",
            file=sys.stderr,
        )

    owns_client = client is None
    if client is None:
        client = httpx.Client(headers={"User-Agent": ua}, follow_redirects=True)

    try:
        for i, year in enumerate(years):
            # Pace before every request except the very first (polite floor,
            # SPEC §3).
            if i > 0:
                sleep(delay)
            pull_index_year(client, year, data_dir, force=force, sleep=sleep)
            # --- issue #4 PDF-download path, gated on not index_only ---
            if not index_only:
                pull_pdfs_year(
                    client,
                    year,
                    data_dir,
                    fetched_at,
                    types=types,
                    force=force,
                    delay=delay,
                    sleep=sleep,
                )
    finally:
        if owns_client:
            client.close()

    return 0
