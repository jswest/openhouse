"""FEC acquisition: the polite bulk-data download for the FEC lane (SPEC §13, #170).

This is the FEC lane's analogue of :mod:`openhouse.pull` — the *only* network
step for ``openhouse fec``. It is **bulk-data-only**: the four FEC bulk files
per cycle from ``https://www.fec.gov/files/bulk-downloads/<cycle>/``. The
OpenFEC ``/v1`` API is **never** used here (it is robots-disallowed for crawling
and rate-limited); bulk data is the sanctioned path for volume (#170).

The polite floor is grounded in ``fec.gov/robots.txt`` → ``Crawl-delay: 10`` for
``*`` (which does NOT disallow ``/files/``):

- **sequential, one file at a time** — concurrency 1;
- **10 s between file fetches** — the host's own published policy (more
  conservative than the Clerk's 2.5 s, grounded the same way);
- **descriptive User-Agent** with the required ``OPENHOUSE_CONTACT`` contact
  flow — reused verbatim from :func:`openhouse.pull.build_user_agent`;
- **exponential backoff on 429/5xx**, **hard-stop on 403** — reused verbatim
  from :func:`openhouse.pull.polite_get`;
- **follow redirects** — the bulk URLs 302 to a storage host; the *final* URL is
  recorded in the manifest beside the requested one.

The four files per cycle (``<yy>`` = 2-digit cycle, 2024 → ``24``):

- ``cn<yy>.zip``   — candidate master (candidate_id ↔ name/state/district)
- ``ccl<yy>.zip``  — candidate-committee linkage (incl. principal designation)
- ``cm<yy>.zip``   — committee master (id ↔ name, org_type, committee_type)
- ``pas2<yy>.zip`` — committee→candidate contributions (line-11C PAC money),
  whose inner member is ``itpas2.txt``

Plus, since GH-0194, the super-PAC **independent-expenditure** file —
``independent_expenditure_<cycle>.csv`` (a *plain headered CSV, not a zip*; the
Schedule-E outside-spending slice, separately footed from Path 1, SPEC §13.7).

Each zip is extracted into ``raw/fec/<cycle>/`` and a ``fec-pull-manifest.json``
is written (per file: requested URL, final redirected URL, status, byte size,
sha256, fetched-at injected once at command entry — no wall-clock in logic).

Idempotent/resumable: a file already extracted whose on-disk size matches the
recorded manifest entry is **skipped** with no network request.

Testability mirrors the Clerk lane: the network is reached only through an
injected ``httpx.Client`` (tests pass one wired to ``httpx.MockTransport``) and
the politeness sleep is an injected callable (tests pass a no-op), so the suite
never touches the live FEC site and never actually waits 10 s.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

import httpx
from tqdm import tqdm

from .cli import fec_raw_dir
from .pull import PullError, build_user_agent, polite_get

# SPEC §13 / #170: FEC bulk downloads, one directory per 2-year cycle. The bulk
# host 302s to a storage backend; the client follows redirects and records the
# final URL.
FEC_BULK_URL_TEMPLATE = (
    "https://www.fec.gov/files/bulk-downloads/{cycle}/{name}"
)

# The polite floor for the FEC lane: 10 s between file fetches, grounded in
# fec.gov/robots.txt's Crawl-delay: 10 (more conservative than the Clerk's 2.5).
# Overridable via --delay, but this is the floor and must not be weakened.
FEC_DEFAULT_DELAY_SECONDS = 10.0

# Path-1's four bulk files, in fetch order. Each entry: the bulk file stem (the
# ``<yy>`` cycle suffix is appended to the *zip* name at fetch time) and the
# ``.txt`` member that zip actually contains. Verified by the by-hand probe
# (GH-0170): the FEC names the inner member by the bare stem, NOT the cycle-suffixed
# zip name — ``cn<yy>.zip`` → ``cn.txt``, ``ccl<yy>.zip`` → ``ccl.txt``,
# ``cm<yy>.zip`` → ``cm.txt``, ``pas2<yy>.zip`` → ``itpas2.txt`` (the contributions
# file's inner member is the irregular ``itpas2.txt``, hence the explicit map).
FEC_BULK_FILES: tuple[tuple[str, str], ...] = (
    ("cn", "cn.txt"),
    ("ccl", "ccl.txt"),
    ("cm", "cm.txt"),
    ("pas2", "itpas2.txt"),
)

# Gentleness cap (operator instruction / #170): pas2 is the largest file (tens of
# MB) — that is expected, not weirdness. But a file larger than this after
# following redirects is surprising enough to STOP rather than push through.
MAX_FILE_BYTES = 150 * 1024 * 1024

# The super-PAC independent-expenditure bulk file (GH-0194, SPEC §13.5a). Unlike
# the four Path-1 files this is a **plain headered CSV, not a zip** —
# ``independent_expenditure_<cycle>.csv`` at the same cycle path (verified by the
# by-hand probe; the 2024 file is ~19.5 MB). Acquired alongside the four files,
# but down a separate code path because there is no inner zip member to extract.
FEC_IE_FILE_TEMPLATE = "independent_expenditure_{cycle}.csv"


def cycle_suffix(cycle: int) -> str:
    """The FEC 2-digit cycle suffix for a 4-digit cycle (2024 → ``"24"``).

    FEC names its bulk files with the cycle's last two digits. Pure and
    wall-clock-free — the caller passes an already-expanded even cycle.
    """
    return f"{cycle % 100:02d}"


def fec_bulk_name(stem: str, cycle: int) -> str:
    """The bulk zip file name for a stem + cycle (``cn``, 2024 → ``cn24.zip``)."""
    return f"{stem}{cycle_suffix(cycle)}.zip"


def _extract_bulk_zip(
    zip_bytes: bytes, inner_name: str, bulk_name: str, dest_dir: Path
) -> int:
    """Extract ``inner_name`` from a bulk zip into ``dest_dir``; return its size.

    Raises :class:`PullError` if the bytes are not a valid ZIP or the expected
    inner member is absent (a structurally wrong download — STOP, never silently
    a gap; the operator instruction is to park on anything surprising).
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise PullError(
            f"{bulk_name} is not a valid ZIP: {exc}. Stopping — a non-zip "
            f"response from the FEC bulk host is unexpected; do not retry blindly."
        ) from exc

    names = set(archive.namelist())
    if inner_name not in names:
        raise PullError(
            f"{bulk_name} does not contain the expected member {inner_name!r} "
            f"(members: {sorted(names)}). Stopping — the FEC layout may have "
            f"changed; re-check the data dictionary before pushing ahead."
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    content = archive.read(inner_name)
    # Atomic write (mirrors pull.py): an interrupted run must never leave a
    # truncated .txt behind, since the skip-if-present check would then serve the
    # partial file forever. Write to a .part sidecar and rename into place.
    dest = dest_dir / inner_name
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(content)
    tmp.replace(dest)
    return len(content)


def _pull_fec_bulk(
    client: httpx.Client,
    cycle: int,
    *,
    file_name: str,
    inner_name: Optional[str],
    data_dir: Path,
    fetched_at: str,
    prior: Optional[dict],
    force: bool,
    delay: float,
    sleep: Callable[[float], None],
    paced: bool,
    finalize: Callable[[bytes, Path], tuple[int, dict]],
) -> dict:
    """Shared polite/resumable/manifest core for one FEC bulk fetch (§13.5).

    The load-bearing crawl contract lives here once — size-consistent skip, the
    polite pacing floor, the :data:`MAX_FILE_BYTES` cap, redirect-final-url capture,
    and the manifest entry shape (requested+final URL, status, bytes, sha256,
    fetched-at). Only how the validated body lands on disk varies, and that is the
    ``finalize(content, cycle_dir) -> (bytes_written, extra_fields)`` callback: the
    zip lane extracts an inner ``.txt``; the IE lane writes the raw CSV.

    ``inner_name`` is the on-disk file the skip-check watches (the extracted member
    for a zip, ``None`` when the saved file *is* ``file_name``); when set it is also
    recorded as the manifest ``inner`` field. ``paced`` (default True) sleeps
    ``delay`` before the *network* request — False for the first fetch of a run so
    the very first request is not delayed. ``prior`` is this file's prior manifest
    entry (the resumability check: a present file whose on-disk size matches
    ``prior["bytes"]`` is skipped with no request).

    Raises :class:`PullError` on 403 / exhausted backoff (via :func:`polite_get`),
    a file larger than :data:`MAX_FILE_BYTES`, or whatever ``finalize`` rejects (a
    non-zip response, a missing inner member) — STOP on anything surprising.
    """
    cycle_dir = fec_raw_dir(data_dir, cycle)
    url = FEC_BULK_URL_TEMPLATE.format(cycle=cycle, name=file_name)
    dest_name = inner_name if inner_name is not None else file_name
    dest = cycle_dir / dest_name

    base = {"file": file_name, "requested_url": url}
    if inner_name is not None:
        base["inner"] = inner_name

    # Resume: a present file whose size matches the prior manifest entry is skipped
    # with no request. We do not re-fetch just to re-verify; the size match is the
    # resumability check, mirroring pull.py's PDF skip.
    if not force and dest.exists() and dest.stat().st_size > 0:
        if prior is not None and prior.get("bytes") == dest.stat().st_size:
            print(
                f"fec {cycle}: {dest_name} present and size-consistent; "
                f"skipping (re-fetch with --force).",
                file=sys.stderr,
            )
            entry = dict(prior)
            entry["status"] = "skipped"
            return entry

    if paced:
        sleep(delay)  # polite floor (10 s) before every network request

    print(f"fec {cycle}: fetching {url}", file=sys.stderr)
    response = polite_get(client, url, sleep=sleep)

    content = response.content
    if len(content) > MAX_FILE_BYTES:
        raise PullError(
            f"{file_name} is {len(content):,} bytes (> {MAX_FILE_BYTES:,} cap). "
            f"FEC bulk files are expected to be tens of MB, but this is far larger "
            f"than expected — stopping rather than pushing through (operator: park "
            f"on anything surprising)."
        )

    # ``response.url`` is the FINAL url after redirects (the client follows them);
    # record it beside the requested one (#170: 302 to a storage host).
    final_url = str(response.url)
    size, extra = finalize(content, cycle_dir)

    return {
        **base,
        "final_url": final_url,
        "status": response.status_code,
        "bytes": size,
        "sha256": hashlib.sha256(content).hexdigest(),
        "fetched_at": fetched_at,
        **extra,
    }


def pull_fec_file(
    client: httpx.Client,
    cycle: int,
    stem: str,
    inner_name: str,
    data_dir: Path,
    fetched_at: str,
    *,
    prior: Optional[dict] = None,
    force: bool = False,
    delay: float = FEC_DEFAULT_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    paced: bool = True,
) -> dict:
    """Acquire one zipped FEC bulk file for ``cycle`` into ``raw/fec/<cycle>/``.

    Downloads ``<stem><yy>.zip``, validates the ZIP, and extracts the inner
    ``.txt``; the manifest entry carries an extra ``zip_bytes`` (the compressed
    download size) beside ``bytes`` (the extracted member). All the polite/resumable
    contract is in :func:`_pull_fec_bulk`.
    """
    bulk_name = fec_bulk_name(stem, cycle)

    def finalize(content: bytes, cycle_dir: Path) -> tuple[int, dict]:
        size = _extract_bulk_zip(content, inner_name, bulk_name, cycle_dir)
        print(
            f"fec {cycle}: extracted {inner_name} ({size:,} bytes) into {cycle_dir}",
            file=sys.stderr,
        )
        return size, {"zip_bytes": len(content)}

    return _pull_fec_bulk(
        client, cycle, file_name=bulk_name, inner_name=inner_name,
        data_dir=data_dir, fetched_at=fetched_at, prior=prior, force=force,
        delay=delay, sleep=sleep, paced=paced, finalize=finalize,
    )


def fec_ie_name(cycle: int) -> str:
    """The IE bulk file name for a cycle (2024 → ``independent_expenditure_2024.csv``)."""
    return FEC_IE_FILE_TEMPLATE.format(cycle=cycle)


def pull_fec_ie_file(
    client: httpx.Client,
    cycle: int,
    data_dir: Path,
    fetched_at: str,
    *,
    prior: Optional[dict] = None,
    force: bool = False,
    delay: float = FEC_DEFAULT_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    paced: bool = True,
) -> dict:
    """Acquire the super-PAC IE bulk CSV for ``cycle`` (GH-0194, SPEC §13.5a).

    The IE file is a **plain headered CSV, not a zip** (the by-hand probe), so the
    response body is written straight to ``raw/fec/<cycle>/`` with no inner member —
    the only divergence from the zip lane, isolated in ``finalize`` below. The
    polite/resumable/manifest contract is shared via :func:`_pull_fec_bulk`.
    """
    ie_name = fec_ie_name(cycle)

    def finalize(content: bytes, cycle_dir: Path) -> tuple[int, dict]:
        cycle_dir.mkdir(parents=True, exist_ok=True)
        # Atomic write (mirrors _extract_bulk_zip): an interrupted run must never
        # leave a truncated CSV behind, since the skip-if-present check would then
        # serve the partial file forever.
        dest = cycle_dir / ie_name
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(content)
        tmp.replace(dest)
        print(
            f"fec {cycle}: saved {ie_name} ({len(content):,} bytes) into {cycle_dir}",
            file=sys.stderr,
        )
        return len(content), {}

    return _pull_fec_bulk(
        client, cycle, file_name=ie_name, inner_name=None,
        data_dir=data_dir, fetched_at=fetched_at, prior=prior, force=force,
        delay=delay, sleep=sleep, paced=paced, finalize=finalize,
    )


def _write_manifest(
    cycle_dir: Path, cycle: int, files: dict, fetched_at: str
) -> None:
    """Write ``raw/fec/<cycle>/fec-pull-manifest.json`` (SPEC §13.5 / #170)."""
    cycle_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "cycle": cycle,
        "fetched_at": fetched_at,
        "count": len(files),
        "files": files,
    }
    (cycle_dir / "fec-pull-manifest.json").write_text(
        json.dumps(document, indent=2, sort_keys=True)
    )


def pull_fec_cycle(
    client: httpx.Client,
    cycle: int,
    data_dir: Path,
    fetched_at: str,
    *,
    force: bool = False,
    delay: float = FEC_DEFAULT_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    pace_first: bool = False,
) -> dict:
    """Acquire all four Path-1 bulk files for one cycle (SPEC §13 / #170).

    Fetches cn/ccl/cm/pas2 sequentially, 10 s apart, into ``raw/fec/<cycle>/``,
    and writes ``fec-pull-manifest.json``. Returns a small status dict. The
    manifest is written in a ``finally`` so an interrupted cycle (Ctrl-C, a 403,
    exhausted backoff) never loses what was already fetched — on resume the
    on-disk files are reconciled against it.

    ``pace_first`` paces (10 s) before even the first file of this cycle — the
    caller sets it True for every cycle after the first so cross-cycle fetches
    stay paced too; the very first file of the whole run is not delayed.
    """
    cycle_dir = fec_raw_dir(data_dir, cycle)
    # Seed from the prior manifest so an interrupted run's skips/entries survive.
    files: dict = {}
    prior_path = cycle_dir / "fec-pull-manifest.json"
    if prior_path.exists():
        try:
            files = dict(json.loads(prior_path.read_text()).get("files", {}))
        except (json.JSONDecodeError, OSError):
            files = {}

    counts = {"fetched": 0, "skipped": 0}
    try:
        progress = tqdm(
            FEC_BULK_FILES,
            desc=f"fec {cycle}",
            unit="file",
            file=sys.stderr,
            disable=None,
            leave=True,
        )
        for i, (stem, inner_name) in enumerate(progress):
            # Pace before every network request but the first of the run. Within a
            # cycle, every file after the first paces; the first file paces only if
            # the caller asked (a later cycle).
            paced = pace_first or i > 0
            entry = pull_fec_file(
                client, cycle, stem, inner_name, data_dir, fetched_at,
                prior=files.get(fec_bulk_name(stem, cycle)),
                force=force, delay=delay, sleep=sleep, paced=paced,
            )
            files[entry["file"]] = entry
            counts["skipped" if entry.get("status") == "skipped" else "fetched"] += 1

        # The super-PAC IE CSV (GH-0194), acquired alongside the four Path-1 files.
        # It always paces (it follows the four files, never the run's first fetch).
        ie_name = fec_ie_name(cycle)
        ie_entry = pull_fec_ie_file(
            client, cycle, data_dir, fetched_at,
            prior=files.get(ie_name),
            force=force, delay=delay, sleep=sleep, paced=True,
        )
        files[ie_entry["file"]] = ie_entry
        counts["skipped" if ie_entry.get("status") == "skipped" else "fetched"] += 1
    finally:
        _write_manifest(cycle_dir, cycle, files, fetched_at)

    print(
        f"fec {cycle}: {counts['fetched']} fetched, {counts['skipped']} "
        f"present/skipped (manifest: {cycle_dir / 'fec-pull-manifest.json'}).",
        file=sys.stderr,
    )
    return {"cycle": cycle, **counts}


def fec_pull(
    cycles: list[int],
    *,
    data_dir: Path,
    delay: float = FEC_DEFAULT_DELAY_SECONDS,
    contact: Optional[str] = None,
    user_agent: Optional[str] = None,
    force: bool = False,
    fetched_at: str,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run ``openhouse fec pull`` for ``cycles`` (SPEC §13 / #170). Returns an exit code.

    Per cycle: fetch the four Path-1 bulk files (cn/ccl/cm/pas2) into
    ``raw/fec/<cycle>/``, extract each inner ``.txt``, and write
    ``fec-pull-manifest.json``. The polite floor is 10 s between every file
    fetch (grounded in fec.gov's published ``Crawl-delay: 10``). ``contact`` /
    ``user_agent`` build the User-Agent exactly as the Clerk lane does (the
    contact is required unless ``user_agent`` overrides it).

    ``fetched_at`` is the single command-entry timestamp threaded into every
    manifest entry (no wall-clock in core logic). The ``client`` / ``sleep``
    seams keep this fully offline-testable (a ``MockTransport`` client + a no-op
    sleep), exactly as :func:`openhouse.pull.pull` is.
    """
    ua = build_user_agent(contact=contact, user_agent=user_agent)
    print(f"fec pull: User-Agent: {ua}", file=sys.stderr)
    if delay != FEC_DEFAULT_DELAY_SECONDS:
        print(
            f"fec pull: delay overridden — {delay}s "
            f"(polite floor is {FEC_DEFAULT_DELAY_SECONDS}s, grounded in "
            f"fec.gov/robots.txt Crawl-delay: 10).",
            file=sys.stderr,
        )
    owns_client = client is None
    if client is None:
        # follow_redirects=True: the bulk URLs 302 to a storage host (#170).
        client = httpx.Client(headers={"User-Agent": ua}, follow_redirects=True)

    try:
        for i, cycle in enumerate(cycles):
            pull_fec_cycle(
                client, cycle, data_dir, fetched_at,
                force=force, delay=delay, sleep=sleep,
                pace_first=i > 0,
            )
    finally:
        if owns_client:
            client.close()

    return 0
