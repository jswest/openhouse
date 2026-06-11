"""Offline tests for the polite client + the ``pull --index-only`` path (#3).

Every test runs offline: the network is reached only through an ``httpx.Client``
wired to ``httpx.MockTransport`` (no real Clerk call ever happens), and the
politeness sleep is patched to a no-op so the suite never waits 2.5 s.

The "fabricated trimmed-index fixture" is built in :func:`make_index_zip`: an
in-memory ZIP (stdlib ``zipfile``) whose ``2024FD.xml`` / ``2024FD.txt`` members
carry the SPEC §2.1 edge cases — a type-``W`` row with empty ``StateDst`` and
empty ``FilingDate``, ``DC00`` and ``PR00`` rows, a 4-digit ``DocID``, plus a
normal e-filed PTR (``P``) and annual (``O``). The bytes come from the
checked-in fixtures under ``tests/fixtures/``.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

import hashlib
import json

from openhouse import pull as pull_mod
from openhouse.pull import (
    PullError,
    build_user_agent,
    polite_get,
    pull,
    pull_index_year,
    pull_pdfs_year,
)

FIXTURES = Path(__file__).parent / "fixtures"


def make_index_zip(year: int = 2024) -> bytes:
    """Build an in-memory ``<year>FD.zip`` from the trimmed fixtures."""
    xml = (FIXTURES / "2024FD-trimmed.xml").read_bytes()
    txt = (FIXTURES / "2024FD-trimmed.txt").read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{year}FD.xml", xml)
        zf.writestr(f"{year}FD.txt", txt)
    return buf.getvalue()


def make_client(handler) -> httpx.Client:
    """An httpx.Client whose transport is the given mock handler (offline)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def no_sleep(_seconds: float) -> None:
    """A patched sleep that records nothing and never waits."""
    return None


# ---------------------------------------------------------------------------
# User-Agent construction (SPEC §3)
# ---------------------------------------------------------------------------
def test_ua_default():
    ua = build_user_agent()
    assert ua.startswith("openhouse/")
    assert "+https://github.com/jswest/openhouse" in ua
    assert "contact:" not in ua


def test_ua_contact_appends():
    ua = build_user_agent(contact="john@example.com")
    assert ua.startswith("openhouse/")
    assert ua.endswith("; contact: john@example.com")


def test_ua_user_agent_overrides_entirely():
    ua = build_user_agent(contact="john@example.com", user_agent="custom/9.9")
    assert ua == "custom/9.9"


# ---------------------------------------------------------------------------
# polite_get: 403 hard error, 429/5xx backoff, success
# ---------------------------------------------------------------------------
def test_403_is_immediate_hard_error_no_retry():
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="forbidden")

    client = make_client(handler)
    with pytest.raises(PullError) as exc:
        polite_get(client, "https://example/x", sleep=sleeps.append)

    assert calls["n"] == 1  # exactly one request: never retried
    assert sleeps == []  # no backoff happened
    assert "403" in str(exc.value)


def test_429_backs_off_then_succeeds():
    statuses = [429, 503, 200]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 200:
            return httpx.Response(200, content=b"ok")
        return httpx.Response(status)

    client = make_client(handler)
    response = polite_get(
        client, "https://example/x", sleep=sleeps.append, backoff_base=1.0
    )
    assert response.status_code == 200
    # Two retriable failures → two backoff sleeps, exponential.
    assert sleeps == [1.0, 2.0]


def test_5xx_exhausts_retries_then_raises():
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = make_client(handler)
    with pytest.raises(PullError) as exc:
        polite_get(
            client, "https://example/x", sleep=sleeps.append, max_retries=3
        )
    assert "500" in str(exc.value)
    assert len(sleeps) == 3  # one sleep per retry, no sleep on the final give-up


def test_404_not_retried():
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = make_client(handler)
    with pytest.raises(PullError):
        polite_get(client, "https://example/x", sleep=sleeps.append)
    assert sleeps == []  # a 404 is not retriable


# ---------------------------------------------------------------------------
# Index fetch + extraction into raw/<year>/
# ---------------------------------------------------------------------------
def test_pull_index_year_extracts_xml_and_txt(tmp_path):
    zip_bytes = make_index_zip(2024)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("2024FD.zip")
        return httpx.Response(200, content=zip_bytes)

    client = make_client(handler)
    result = pull_index_year(
        client, 2024, tmp_path, sleep=no_sleep
    )

    year_dir = tmp_path / "raw" / "2024"
    assert (year_dir / "2024FD.xml").exists()
    assert (year_dir / "2024FD.txt").exists()
    assert result["status"] == "fetched"
    # Sanity: the extracted XML carries the edge-case rows.
    xml_text = (year_dir / "2024FD.xml").read_text()
    assert "DC00" in xml_text and "PR00" in xml_text
    assert "<DocID>7940</DocID>" in xml_text


def test_pull_index_year_idempotent_skip(tmp_path):
    calls = {"n": 0}
    zip_bytes = make_index_zip(2024)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=zip_bytes)

    client = make_client(handler)
    pull_index_year(client, 2024, tmp_path, sleep=no_sleep)
    assert calls["n"] == 1

    # Second run with the index already present: no fetch.
    result = pull_index_year(client, 2024, tmp_path, sleep=no_sleep)
    assert calls["n"] == 1
    assert result["status"] == "skipped"


def test_pull_index_year_force_refetches(tmp_path):
    calls = {"n": 0}
    zip_bytes = make_index_zip(2024)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=zip_bytes)

    client = make_client(handler)
    pull_index_year(client, 2024, tmp_path, sleep=no_sleep)
    pull_index_year(client, 2024, tmp_path, force=True, sleep=no_sleep)
    assert calls["n"] == 2


def test_zip_missing_xml_is_error(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("wrong.xml", b"<x/>")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=buf.getvalue())

    client = make_client(handler)
    with pytest.raises(PullError):
        pull_index_year(client, 2024, tmp_path, sleep=no_sleep)


# ---------------------------------------------------------------------------
# The pull() orchestrator
# ---------------------------------------------------------------------------
def test_pull_index_only_multiyear_paces_between_years(tmp_path):
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # path is .../<year>FD.zip — serve a matching ZIP.
        year = int(request.url.path.split("/")[-1][:4])
        return httpx.Response(200, content=make_index_zip(year))

    client = make_client(handler)
    rc = pull(
        [2023, 2024],
        data_dir=tmp_path,
        index_only=True,
        client=client,
        sleep=sleeps.append,
        delay=2.5,
    )
    assert rc == 0
    assert (tmp_path / "raw" / "2023" / "2023FD.xml").exists()
    assert (tmp_path / "raw" / "2024" / "2024FD.xml").exists()
    # One pacing sleep between the two years; none before the first.
    assert sleeps == [2.5]


def test_pull_without_index_only_fetches_index_then_pdfs(tmp_path, capsys):
    """Without --index-only, the index is fetched and then the PDFs (issue #4)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("2024FD.zip"):
            return httpx.Response(200, content=make_index_zip(2024))
        # Every enumerated PDF: tiny fabricated bytes.
        return httpx.Response(200, content=b"%PDF-fake")

    client = make_client(handler)
    rc = pull(
        [2024],
        data_dir=tmp_path,
        index_only=False,
        fetched_at="2026-06-11T00:00:00",
        client=client,
        sleep=no_sleep,
    )
    assert rc == 0
    year_dir = tmp_path / "raw" / "2024"
    assert (year_dir / "2024FD.xml").exists()
    # The P row (DocID 20024277) routes to ptr/, a non-P row to fd/.
    assert (year_dir / "ptr" / "20024277.pdf").exists()
    assert (year_dir / "fd" / "10066961.pdf").exists()
    assert (year_dir / "pull-manifest.json").exists()


def test_pull_logs_user_agent(tmp_path, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=make_index_zip(2024))

    client = make_client(handler)
    pull(
        [2024],
        data_dir=tmp_path,
        index_only=True,
        contact="john@example.com",
        client=client,
        sleep=no_sleep,
    )
    err = capsys.readouterr().err
    assert "User-Agent:" in err
    assert "contact: john@example.com" in err


def test_pull_403_propagates_as_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    client = make_client(handler)
    with pytest.raises(PullError):
        pull(
            [2024],
            data_dir=tmp_path,
            index_only=True,
            client=client,
            sleep=no_sleep,
        )


# ---------------------------------------------------------------------------
# Bad year range is rejected (reusing the #2 parser, via the CLI)
# ---------------------------------------------------------------------------
def test_cli_rejects_bad_year_range():
    from openhouse.cli import main

    rc = main(["pull", "2007", "--index-only"])
    assert rc == 2  # before MIN_YEAR → YearRangeError → exit 2


def test_cli_rejects_reversed_range():
    from openhouse.cli import main

    rc = main(["pull", "2024-2019", "--index-only"])
    assert rc == 2


# ---------------------------------------------------------------------------
# PDF body download (issue #4): routing, resumability, --types, 404, manifest
# ---------------------------------------------------------------------------
FETCHED_AT = "2026-06-11T12:00:00"

# The trimmed fixture's rows, by DocID → FilingType (see 2024FD-trimmed.xml).
PTR_DOC = "20024277"  # FilingType P → ptr-pdfs
FD_DOC = "10066961"  # FilingType O → financial-pdfs


def write_index(tmp_path: Path, year: int = 2024) -> Path:
    """Lay down ``raw/<year>/<year>FD.xml`` from the trimmed fixture (no network)."""
    year_dir = tmp_path / "raw" / str(year)
    year_dir.mkdir(parents=True)
    (year_dir / f"{year}FD.xml").write_bytes(
        (FIXTURES / "2024FD-trimmed.xml").read_bytes()
    )
    return year_dir


def pdf_handler(fake_bytes: bytes = b"%PDF-fake", not_found: set | None = None):
    """A routing-aware mock: 200 with ``fake_bytes`` except DocIDs in ``not_found``.

    Records every requested URL on ``handler.urls`` so tests can assert routing
    and count fetches.
    """
    not_found = not_found or set()
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        doc_id = request.url.path.split("/")[-1].removesuffix(".pdf")
        if doc_id in not_found:
            return httpx.Response(404)
        return httpx.Response(200, content=fake_bytes)

    handler.urls = urls
    return handler


def test_pdf_routing_p_to_ptr_else_fd(tmp_path):
    write_index(tmp_path)
    handler = pdf_handler()
    client = make_client(handler)

    pull_pdfs_year(client, 2024, tmp_path, FETCHED_AT, sleep=no_sleep)

    ptr_pdf = tmp_path / "raw" / "2024" / "ptr" / f"{PTR_DOC}.pdf"
    fd_pdf = tmp_path / "raw" / "2024" / "fd" / f"{FD_DOC}.pdf"
    assert ptr_pdf.exists()
    assert fd_pdf.exists()
    # The P DocID hit the ptr-pdfs URL; the O DocID hit financial-pdfs.
    assert any(f"ptr-pdfs/2024/{PTR_DOC}.pdf" in u for u in handler.urls)
    assert any(f"financial-pdfs/2024/{FD_DOC}.pdf" in u for u in handler.urls)


def test_pdf_resumable_skips_present_file(tmp_path):
    write_index(tmp_path)
    handler = pdf_handler()
    client = make_client(handler)

    pull_pdfs_year(client, 2024, tmp_path, FETCHED_AT, sleep=no_sleep)
    first_count = len(handler.urls)
    assert first_count > 0

    # Second run: every file is present and non-empty → no second fetch.
    pull_pdfs_year(client, 2024, tmp_path, FETCHED_AT, sleep=no_sleep)
    assert len(handler.urls) == first_count


def test_pdf_force_refetches(tmp_path):
    write_index(tmp_path)
    handler = pdf_handler()
    client = make_client(handler)

    pull_pdfs_year(client, 2024, tmp_path, FETCHED_AT, sleep=no_sleep)
    first_count = len(handler.urls)

    pull_pdfs_year(client, 2024, tmp_path, FETCHED_AT, force=True, sleep=no_sleep)
    assert len(handler.urls) == 2 * first_count


def test_pdf_types_filter_fetches_only_ptr(tmp_path):
    write_index(tmp_path)
    handler = pdf_handler()
    client = make_client(handler)

    pull_pdfs_year(
        client, 2024, tmp_path, FETCHED_AT, types=["ptr"], sleep=no_sleep
    )
    year_dir = tmp_path / "raw" / "2024"
    # Only the PTR family landed on disk; no fd/ directory was created.
    assert (year_dir / "ptr" / f"{PTR_DOC}.pdf").exists()
    assert not (year_dir / "fd").exists()
    # Every requested URL is a ptr-pdfs URL.
    assert handler.urls
    assert all("ptr-pdfs/" in u for u in handler.urls)


def test_pdf_404_recorded_non_fatally(tmp_path):
    write_index(tmp_path)
    # The PTR DocID 404s; the run must continue and still fetch the FD.
    handler = pdf_handler(not_found={PTR_DOC})
    client = make_client(handler)

    result = pull_pdfs_year(client, 2024, tmp_path, FETCHED_AT, sleep=no_sleep)

    year_dir = tmp_path / "raw" / "2024"
    # The 404'd PDF is absent on disk but the FD was still fetched (non-fatal).
    assert not (year_dir / "ptr" / f"{PTR_DOC}.pdf").exists()
    assert (year_dir / "fd" / f"{FD_DOC}.pdf").exists()
    assert result["not_found"] == 1

    manifest = json.loads((year_dir / "pull-manifest.json").read_text())
    entry = manifest["filings"][PTR_DOC]
    assert entry["status"] == 404
    assert entry["sha256"] is None
    assert entry["bytes"] == 0
    assert entry["fetched_at"] == FETCHED_AT


def test_pull_manifest_content_and_injected_fetched_at(tmp_path):
    write_index(tmp_path)
    fake = b"%PDF-tiny-body"
    handler = pdf_handler(fake_bytes=fake)
    client = make_client(handler)

    pull_pdfs_year(client, 2024, tmp_path, FETCHED_AT, sleep=no_sleep)

    manifest = json.loads(
        (tmp_path / "raw" / "2024" / "pull-manifest.json").read_text()
    )
    assert manifest["fetched_at"] == FETCHED_AT
    fd_entry = manifest["filings"][FD_DOC]
    assert fd_entry["url"].endswith(f"financial-pdfs/2024/{FD_DOC}.pdf")
    assert fd_entry["status"] == 200
    assert fd_entry["bytes"] == len(fake)
    assert fd_entry["sha256"] == hashlib.sha256(fake).hexdigest()
    # The fetched-at is the single injected timestamp — no wall-clock per file.
    assert all(
        f["fetched_at"] == FETCHED_AT for f in manifest["filings"].values()
    )


def test_pull_pdfs_missing_index_is_error(tmp_path):
    # No index on disk → cannot enumerate → PullError (never a silent gap).
    (tmp_path / "raw" / "2024").mkdir(parents=True)
    client = make_client(pdf_handler())
    with pytest.raises(PullError):
        pull_pdfs_year(client, 2024, tmp_path, FETCHED_AT, sleep=no_sleep)


def test_pdf_paces_between_requests(tmp_path):
    write_index(tmp_path)
    sleeps: list[float] = []
    client = make_client(pdf_handler())

    # 5 enumerated PDFs (1 PTR + 4 FD rows), each preceded by a pacing sleep at
    # the polite-floor delay (the across-year delay is held by pull() separately).
    pull_pdfs_year(
        client, 2024, tmp_path, FETCHED_AT, delay=2.5, sleep=sleeps.append
    )
    assert sleeps == [2.5, 2.5, 2.5, 2.5, 2.5]
