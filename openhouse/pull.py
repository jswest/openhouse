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

Scope here is the **index** pull only (issue #3): for each year, download
``<YEAR>FD.zip`` and extract ``<YEAR>FD.xml`` + ``<YEAR>FD.txt`` into
``<data-dir>/raw/<year>/``. PDF-body downloading is issue #4; see
:func:`pull` for the seam it slots into.

Testability: the network is reached only through an injected ``httpx.Client``
(tests pass one wired to ``httpx.MockTransport``, so nothing here touches the
live Clerk), and the politeness sleep is an injected callable (tests pass a
no-op so the suite never actually sleeps 2.5 s). No wall-clock lives in core
logic beyond the single ``fetched_at`` timestamp threaded in from the caller.
"""

from __future__ import annotations

import io
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

import httpx

from . import __version__

# SPEC §2.1: one ZIP per year, refreshed daily.
INDEX_URL_TEMPLATE = (
    "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
)

# Polite-crawling floor (SPEC §3). Overridable via --delay / --concurrency, but
# these are the defaults and the floor.
DEFAULT_DELAY_SECONDS = 2.5
DEFAULT_CONCURRENCY = 1

# Backoff for 429/5xx (SPEC §3). A 403 never reaches this path.
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0

REPO_URL = "https://github.com/jswest/openhouse"


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

    - ``user_agent`` (``--user-agent``) overrides everything when given.
    - otherwise the default is ``openhouse/<version> (+<repo>)``;
    - ``contact`` (``--contact`` / ``OPENHOUSE_CONTACT``) appends
      ``; contact: <email>``.
    """
    if user_agent:
        return user_agent
    ua = f"openhouse/{__version__} (+{REPO_URL})"
    if contact:
        ua = f"{ua}; contact: {contact}"
    return ua


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
) -> httpx.Response:
    """GET ``url`` with the SPEC §3 retry policy.

    - **403** → :class:`PullError` immediately, no retry, no backoff (the server
      is refusing us; the message explains likely causes).
    - **429 / 5xx** → exponential backoff (``backoff_base * 2**attempt``), up to
      ``max_retries`` times, then :class:`PullError`.
    - **2xx** → returned.
    - other 4xx (e.g. 404) → :class:`PullError`, no retry (a retry would not
      help).

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
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run ``openhouse pull`` for ``years`` (SPEC §3). Returns a process exit code.

    Only the **index** path is implemented in issue #3. PDF-body downloading is
    issue #4: when ``index_only`` is False this fetches the index (so #4's PDF
    enumeration always has the XML on disk) and then prints a clear notice that
    PDF download is not yet implemented. #4 slots its per-DocID download loop in
    right after the per-year index pull below, gated on ``not index_only``.

    The ``client`` and ``sleep`` seams keep this fully offline-testable: tests
    pass an ``httpx.Client`` wired to ``httpx.MockTransport`` and a no-op sleep.
    """
    ua = build_user_agent(contact=contact, user_agent=user_agent)
    print(f"pull: User-Agent: {ua}", file=sys.stderr)
    if concurrency != DEFAULT_CONCURRENCY or delay != DEFAULT_DELAY_SECONDS:
        print(
            f"pull: politeness overridden — concurrency={concurrency}, "
            f"delay={delay}s (default is {DEFAULT_CONCURRENCY} / "
            f"{DEFAULT_DELAY_SECONDS}s).",
            file=sys.stderr,
        )

    owns_client = client is None
    if client is None:
        client = httpx.Client(headers={"User-Agent": ua}, follow_redirects=True)

    try:
        for i, year in enumerate(years):
            # Pace before every request except the first (polite floor, SPEC §3).
            if i > 0:
                sleep(delay)
            pull_index_year(client, year, data_dir, force=force, sleep=sleep)
        # --- issue #4 PDF-download path slots in here, gated on not index_only ---
        if not index_only:
            print(
                "pull: PDF body download is not yet implemented (issue #4); "
                "the index has been pulled. Re-run with --index-only to "
                "suppress this notice.",
                file=sys.stderr,
            )
    finally:
        if owns_client:
            client.close()

    return 0
