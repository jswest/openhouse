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
from tqdm import tqdm

from . import __version__
from .index import IndexTarget, build_filing_records, enumerate_targets
from .legislators import LEGISLATORS_FILES, REFERENCE_SUBDIR

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

# The CC0 ``@unitedstates/congress-legislators`` bulk files (#16). Public domain
# (CC0) — no conflict with the Clerk FD use restriction, which governs disclosure
# data, not this reference set. Fetched once into ``raw/reference/`` and joined
# OFFLINE in ``parse`` to attach ``bioguide:<id>`` to member filings. This is the
# single declared exception to "``pull`` is the only network step": still inside
# ``pull``, just a different (public-domain) source. The on-disk layout
# (``REFERENCE_SUBDIR`` + the file names) is owned by ``legislators.py`` — the
# join's consumer — so producer and consumer can never drift.
#
# Source URL (verified 2026-06-13): the project's gh-pages mirror at
# ``unitedstates.github.io`` is the live home of these JSON files (HTTP 200). The
# former ``raw.githubusercontent.com/.../main/`` path is 404 and the legacy
# ``theunitedstates.io`` distribution is 410 Gone — see SPEC.md "verified facts".
LEGISLATORS_URL_TEMPLATE = (
    "https://unitedstates.github.io/congress-legislators/{name}"
)

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
# CC0 congress-legislators reference fetch (#16) — the one declared exception
# to "pull is the only network step": still in pull, a public-domain source.
# ---------------------------------------------------------------------------
def pull_legislators(
    client: httpx.Client,
    data_dir: Path,
    *,
    force: bool = False,
    delay: float = DEFAULT_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Fetch the two CC0 ``congress-legislators`` bulk files into ``raw/reference/``.

    ``legislators-current.json`` + ``legislators-historical.json`` are public
    domain (CC0) and carry a stable ``id.bioguide`` per legislator, which ``parse``
    joins **offline** to attach ``bioguide:<id>`` to member filings (#16). Same
    polite floor as every other fetch (paced, backoff, 403-is-fatal via
    :func:`polite_get`).

    Idempotent: a file already present is not re-fetched unless ``force`` — the
    reference set changes slowly, so a re-pull is a deliberate ``--force`` choice.
    Returns a small status dict for logging.
    """
    ref_dir = data_dir / REFERENCE_SUBDIR
    written: list[str] = []
    skipped: list[str] = []
    for i, name in enumerate(LEGISLATORS_FILES):
        dest = ref_dir / name
        if dest.exists() and not force:
            print(
                f"reference: {name} present ({dest}); skipping (re-fetch with "
                f"--force).",
                file=sys.stderr,
            )
            skipped.append(name)
            continue
        if i > 0:
            sleep(delay)  # pace before every fetch but the first (polite floor)
        url = LEGISLATORS_URL_TEMPLATE.format(name=name)
        print(f"reference: fetching {url}", file=sys.stderr)
        response = polite_get(client, url, sleep=sleep)
        ref_dir.mkdir(parents=True, exist_ok=True)
        # Atomic write (critic): an interrupted fetch must never leave a truncated
        # file behind — the skip-if-present check above would then permanently
        # serve the partial file, silently disabling the bioguide join. Write to a
        # .part sidecar and rename into place (rename is atomic on POSIX).
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(response.content)
        tmp.replace(dest)
        written.append(name)
    print(
        f"reference: {len(written)} fetched, {len(skipped)} present/skipped "
        f"(CC0 congress-legislators → {ref_dir}).",
        file=sys.stderr,
    )
    return {"fetched": written, "skipped": skipped}


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
    doc_ids: Optional[set[str]] = None,
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

    ``doc_ids`` (issue #78: targeted pull) narrows the download to just those
    DocIDs — the rest of the year's PDFs are NOT fetched. It is the only
    behavioural change of ``--doc-id`` / ``--member``: the index is still read in
    full (so routing/metadata is intact), the manifest still records every
    fetched/skipped target, and the polite floor is unchanged — targeted pull
    fetches FEWER bodies, never faster or less politely. ``None`` (the default)
    means the whole year, exactly as before.

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
        # Targeted pull (#78): a DocID outside the requested set is not fetched.
        # It is not "filtered" (that count is for --types exclusions, a different
        # axis) — it simply isn't in scope for this targeted run.
        if doc_ids is not None and target.doc_id not in doc_ids:
            continue
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
            # A tqdm bar per data type (ptr, then fd) with a *measured* ETA, so a
            # ~95-min crawl shows how long it has left (and a slow link is
            # reflected, unlike a fixed-pace estimate). On a resume the instant
            # skips race the bar ahead — exactly the "where are we" signal. tqdm
            # auto-disables off a TTY (``disable=None``), so piped/redirected runs
            # stay clean, matching the old behaviour.
            progress = tqdm(
                targets,
                desc=f"{year} {family}",
                unit="pdf",
                file=sys.stderr,
                disable=None,
                leave=True,
            )
            for target in progress:
                outcome = _process_pdf_target(
                    target, manifest, year_dir, client, fetched_at,
                    force=force, delay=delay, sleep=sleep,
                )
                counts[outcome] += 1
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


def doc_ids_for_member(xml_path: Path, year: int, member: str) -> set[str]:
    """The DocIDs in ``<year>FD.xml`` whose filer matches ``member`` (issue #78).

    Reuses the *same* name-matching the read surface uses (``read._member_matches``,
    a case-insensitive substring over ``filer_id`` + the raw name parts), fed from
    the same offline index parse ``parse`` uses (``build_filing_records``). No
    legislators index is supplied, so the match is on the raw name (and the
    last-resort ``name:`` key) regardless of whether the CC0 reference fetch ran —
    a ``--member`` pull is deterministic against the index alone. A row with no
    DocID yields no body to fetch and is skipped (it never matches a download).
    """
    # Imported here, not at module top, to avoid a pull↔read import cycle (read
    # imports nothing from pull, but keeping the dependency lazy is the smaller
    # footprint and matches the rest of the tool reusing read's matcher).
    from .read import _member_matches

    return {
        record.doc_id
        for record in build_filing_records(xml_path, year)
        if record.doc_id and _member_matches(record.model_dump(), member)
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
    # Atomic write: a Ctrl-C mid-write can only truncate the ``.part`` scratch
    # file; ``dest`` is swapped in by an atomic rename, so it is always either
    # absent or a complete PDF — never a half-written body that resume would
    # backfill as if whole (SPEC §3: safe to Ctrl-C).
    tmp = dest.with_name(f"{dest.name}.part")
    tmp.write_bytes(content)
    tmp.replace(dest)
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
    reference: bool = True,
    member: Optional[str] = None,
    doc_id: Optional[str] = None,
    newest_first: bool = False,
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

    Targeted pull (issue #78) narrows WHICH PDFs download, never how politely:

    - ``doc_id`` — fetch only that single filing's body. The year index is still
      fetched (it carries the filing's family/metadata), but no other PDF is
      downloaded. The CLI requires ``doc_id`` to be paired with exactly one year.
    - ``member`` — fetch only the bodies of filings whose filer matches the name,
      using the same matcher as ``read --member`` (see :func:`doc_ids_for_member`).
      The full year index is fetched; only matching DocIDs' PDFs download.
    - ``newest_first`` — process ``years`` in descending order instead of the
      default ascending. Purely an ordering choice; it changes nothing about
      what is fetched, only the sequence (and so which year a Ctrl-C lands in).

    ``doc_id`` and ``member`` are mutually exclusive (the CLI rejects both); a bare
    ``pull`` (neither) keeps the whole-year behaviour. All three preserve the
    polite floor, idempotence, and the per-year manifest exactly as before.

    ``fetched_at`` is the single entry-time timestamp threaded into every
    manifest entry (SPEC §9: no wall-clock in core logic). The caller (the CLI)
    captures it once; it defaults here to one ``datetime.now()`` read only so an
    ad-hoc call still works — production always passes it in.

    The ``client`` and ``sleep`` seams keep this fully offline-testable: tests
    pass an ``httpx.Client`` wired to ``httpx.MockTransport`` and a no-op sleep.
    """
    if fetched_at is None:
        fetched_at = datetime.now().isoformat()
    if doc_id and member:
        raise PullError(
            "--doc-id and --member are mutually exclusive: --doc-id fetches one "
            "filing by id, --member fetches a filer's filings. Pick one."
        )
    if doc_id and len(years) != 1:
        raise PullError(
            "--doc-id needs exactly one year (the per-document URL is "
            "ptr-pdfs/<year>/<doc_id>.pdf): pass a single YYYY, not a range."
        )
    # --newest-first: process the requested years descending instead of the
    # default ascending. Pure ordering — what is fetched is unchanged.
    if newest_first:
        years = sorted(years, reverse=True)
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
        # The CC0 congress-legislators reference fetch (#16) — once, up front, so
        # the offline join in `parse` has it. Disabled with --no-reference.
        #
        # Identity enrichment is OPTIONAL, never a gate (#75): if the reference
        # files can't be fetched (upstream moved/404/410, network error — even
        # after retry/backoff), warn and proceed WITHOUT bioguide data, exactly as
        # --no-reference does, rather than abort before any disclosure PDF
        # downloads. `parse` then falls back to the last-resort name: key for
        # every filer. A real disclosure-side failure below still aborts.
        if reference:
            try:
                pull_legislators(
                    client, data_dir, force=force, delay=delay, sleep=sleep
                )
            except PullError as exc:
                print(
                    f"warning: could not fetch the CC0 congress-legislators "
                    f"reference set ({exc}); continuing WITHOUT bioguide identity "
                    f"enrichment (as with --no-reference). Filers will be "
                    f"name-keyed only; re-run `openhouse pull` to retry the fetch.",
                    file=sys.stderr,
                )
        for i, year in enumerate(years):
            # Pace before every request except the very first (polite floor,
            # SPEC §3).
            if i > 0 or reference:
                sleep(delay)
            pull_index_year(client, year, data_dir, force=force, sleep=sleep)
            # --- issue #4 PDF-download path, gated on not index_only ---
            if not index_only:
                # Targeted pull (#78): narrow to the requested DocID(s) for this
                # year, computed AFTER the index is on disk (it is what we read).
                # ``None`` = the whole year (the default, unchanged).
                xml_path = data_dir / "raw" / str(year) / f"{year}FD.xml"
                selected_doc_ids: Optional[set[str]] = None
                if doc_id:
                    selected_doc_ids = {doc_id}
                    # A --doc-id absent from the index would otherwise fetch
                    # nothing with no signal; warn so it is never silent.
                    known = {t.doc_id for t in enumerate_targets(xml_path, year)}
                    if doc_id not in known:
                        print(
                            f"{year}: --doc-id {doc_id!r} is not in the index; "
                            f"nothing to fetch (check the id and year).",
                            file=sys.stderr,
                        )
                elif member:
                    selected_doc_ids = doc_ids_for_member(xml_path, year, member)
                    print(
                        f"{year}: --member {member!r} matched "
                        f"{len(selected_doc_ids)} filing(s).",
                        file=sys.stderr,
                    )
                pull_pdfs_year(
                    client,
                    year,
                    data_dir,
                    fetched_at,
                    types=types,
                    doc_ids=selected_doc_ids,
                    force=force,
                    delay=delay,
                    sleep=sleep,
                )
    finally:
        if owns_client:
            client.close()

    return 0
