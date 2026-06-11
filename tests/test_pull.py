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

from openhouse import pull as pull_mod
from openhouse.pull import (
    PullError,
    build_user_agent,
    polite_get,
    pull,
    pull_index_year,
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


def test_pull_without_index_only_fetches_index_then_notes_pdf_todo(tmp_path, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=make_index_zip(2024))

    client = make_client(handler)
    rc = pull(
        [2024],
        data_dir=tmp_path,
        index_only=False,
        client=client,
        sleep=no_sleep,
    )
    assert rc == 0
    assert (tmp_path / "raw" / "2024" / "2024FD.xml").exists()
    err = capsys.readouterr().err
    assert "not yet implemented" in err and "#4" in err


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
