"""Offline tests for the FEC bulk-data acquisition (``openhouse fec pull``, #170).

Every test runs offline: the network is reached only through an ``httpx.Client``
wired to ``httpx.MockTransport`` (no real FEC call ever happens), and the
politeness sleep is patched to a no-op so the suite never waits 10 s. The four
served zips are built in-memory from the **trimmed real fixtures** under
``tests/fixtures/fec/`` (a handful of real pipe-delimited rows of
cn/ccl/cm/itpas2, including the labor/trade/corporate committees that exercise
#171's Path-1 filter).

The mock transport reproduces the live host's two real behaviours captured by
the by-hand probe (GH-0170): the bulk URL **302-redirects** to an AWS GovCloud
storage host, and the inner zip member is named by the bare stem
(``cn.txt``/``ccl.txt``/``cm.txt``/``itpas2.txt``), not the cycle-suffixed zip.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from openhouse import fec_pull as fec
from openhouse.fec_pull import FEC_BULK_FILES, fec_pull, pull_fec_cycle
from openhouse.pull import PullError

FIXTURES = Path(__file__).parent / "fixtures" / "fec"
CONTACT = "Jane Doe <jane@example.com>"

# The final storage host the bulk URLs 302 to (the real prefix, GH-0170 probe).
STORAGE_HOST = (
    "https://cg-519a459a-0ea3-42c2-b7bc-fa1143481f74.s3-us-gov-west-1."
    "amazonaws.com"
)


def _zip_for(stem: str, inner_name: str) -> bytes:
    """An in-memory bulk zip whose ``inner_name`` member is the trimmed fixture."""
    rows = (FIXTURES / inner_name).read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(inner_name, rows)
    return buf.getvalue()


# The four served zips, keyed by their 2024 cycle-suffixed bulk file name.
def _served_zips(cycle: int = 2024) -> dict[str, bytes]:
    suffix = f"{cycle % 100:02d}"
    return {
        f"{stem}{suffix}.zip": _zip_for(stem, inner)
        for stem, inner in FEC_BULK_FILES
    }


def make_handler(cycle: int = 2024, *, redirect: bool = True):
    """A MockTransport handler that 302s the bulk URL to the storage host, then
    serves the matching in-memory zip (mirroring the live host, GH-0170)."""
    zips = _served_zips(cycle)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        path = request.url.path  # /files/bulk-downloads/<cycle>/<name> or /bulk-downloads/...
        name = path.rsplit("/", 1)[-1]
        if name not in zips:
            return httpx.Response(404, text="not found")
        # First hit on the fec.gov host → 302 to the storage host (the real flow).
        if redirect and request.url.host == "www.fec.gov":
            target = f"{STORAGE_HOST}/bulk-downloads/{cycle}/{name}"
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, content=zips[name])

    handler.calls = calls  # type: ignore[attr-defined]
    # Origin requests only (the polite fec.gov hit, not the 302's storage GET):
    # one per file fetched, the meaningful "did we hit the network" count.
    handler.origin_calls = lambda: [c for c in calls if "www.fec.gov" in c]  # type: ignore[attr-defined]
    return handler


def make_client(handler) -> httpx.Client:
    """An httpx.Client wired to the mock handler, following redirects (offline)."""
    return httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )


def no_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# Happy path: four files, redirect followed, manifest complete.
# ---------------------------------------------------------------------------
def test_pull_cycle_fetches_four_files_and_extracts(tmp_path):
    handler = make_handler()
    client = make_client(handler)
    result = pull_fec_cycle(
        client, 2024, tmp_path, "2026-01-01T00:00:00", sleep=no_sleep
    )
    assert result == {"cycle": 2024, "fetched": 4, "skipped": 0}

    cycle_dir = tmp_path / "raw" / "fec" / "2024"
    for inner in ("cn.txt", "ccl.txt", "cm.txt", "itpas2.txt"):
        assert (cycle_dir / inner).exists(), inner
    # Adams' principal committee resolves in the ccl fixture (the #169 seam fill).
    assert "C00546358" in (cycle_dir / "ccl.txt").read_text()


def test_manifest_records_requested_and_final_url(tmp_path):
    client = make_client(make_handler())
    pull_fec_cycle(client, 2024, tmp_path, "2026-01-01T00:00:00", sleep=no_sleep)

    manifest = json.loads(
        (tmp_path / "raw" / "fec" / "2024" / "fec-pull-manifest.json").read_text()
    )
    assert manifest["cycle"] == 2024
    assert manifest["fetched_at"] == "2026-01-01T00:00:00"
    assert manifest["count"] == 4
    entry = manifest["files"]["cn24.zip"]
    # requested URL is the polite fec.gov bulk URL; final URL is the storage host.
    assert entry["requested_url"].startswith("https://www.fec.gov/files/bulk-downloads/2024/")
    assert entry["final_url"].startswith(STORAGE_HOST)
    assert entry["status"] == 200
    assert entry["inner"] == "cn.txt"
    assert entry["bytes"] > 0
    assert len(entry["sha256"]) == 64


def test_pas2_inner_member_is_itpas2(tmp_path):
    # The contributions zip's inner member is the irregular ``itpas2.txt``, not
    # ``pas224.txt`` — the line-11C PAC money lands there (GH-0170 probe).
    client = make_client(make_handler())
    pull_fec_cycle(client, 2024, tmp_path, "2026-01-01T00:00:00", sleep=no_sleep)
    itpas2 = (tmp_path / "raw" / "fec" / "2024" / "itpas2.txt").read_text()
    # the three connected-SSF contributors (L/T/C) all gave to Adams' committee.
    for contributor in ("C00002469", "C00130773", "C00792127"):
        assert contributor in itpas2


# ---------------------------------------------------------------------------
# Idempotent / resumable: a present, size-consistent file is skipped.
# ---------------------------------------------------------------------------
def test_resume_skips_present_size_consistent_files(tmp_path):
    handler1 = make_handler()
    pull_fec_cycle(
        make_client(handler1), 2024, tmp_path, "2026-01-01T00:00:00", sleep=no_sleep
    )
    assert len(handler1.origin_calls()) == 4  # all four fetched on the first run

    # Second run against a fresh handler: everything present + size-consistent →
    # no network request at all.
    handler2 = make_handler()
    result = pull_fec_cycle(
        make_client(handler2), 2024, tmp_path, "2026-02-02T00:00:00", sleep=no_sleep
    )
    assert result == {"cycle": 2024, "fetched": 0, "skipped": 4}
    assert handler2.origin_calls() == []  # nothing re-requested


def test_force_redownloads_present_files(tmp_path):
    pull_fec_cycle(
        make_client(make_handler()), 2024, tmp_path, "2026-01-01T00:00:00",
        sleep=no_sleep,
    )
    handler = make_handler()
    result = pull_fec_cycle(
        make_client(handler), 2024, tmp_path, "2026-02-02T00:00:00",
        force=True, sleep=no_sleep,
    )
    assert result == {"cycle": 2024, "fetched": 4, "skipped": 0}
    assert len(handler.origin_calls()) == 4


def test_partial_file_is_redownloaded(tmp_path):
    # A size-mismatched on-disk file (a partial transfer) is re-downloaded, not
    # served stale — the manifest's recorded size is the resumability check.
    pull_fec_cycle(
        make_client(make_handler()), 2024, tmp_path, "2026-01-01T00:00:00",
        sleep=no_sleep,
    )
    cn = tmp_path / "raw" / "fec" / "2024" / "cn.txt"
    cn.write_text("truncated")  # corrupt one file's size
    handler = make_handler()
    result = pull_fec_cycle(
        make_client(handler), 2024, tmp_path, "2026-02-02T00:00:00", sleep=no_sleep
    )
    assert result["fetched"] == 1 and result["skipped"] == 3
    assert cn.read_text() != "truncated"  # restored from the re-download


# ---------------------------------------------------------------------------
# Politeness floor: 10 s between fetches, first not delayed.
# ---------------------------------------------------------------------------
def test_paces_between_files_not_before_first(tmp_path):
    sleeps: list[float] = []
    pull_fec_cycle(
        make_client(make_handler()), 2024, tmp_path, "2026-01-01T00:00:00",
        sleep=sleeps.append,
    )
    # four files, first not paced → three 10 s sleeps between them.
    assert sleeps == [fec.FEC_DEFAULT_DELAY_SECONDS] * 3


# ---------------------------------------------------------------------------
# Anomalies → STOP / PARK (PullError), never push through.
# ---------------------------------------------------------------------------
def test_403_is_hard_stop(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(PullError) as exc:
        pull_fec_cycle(
            make_client(handler), 2024, tmp_path, "2026-01-01T00:00:00",
            sleep=no_sleep,
        )
    assert "403" in str(exc.value)


def test_non_zip_response_is_parked(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.fec.gov":
            return httpx.Response(
                302,
                headers={"location": f"{STORAGE_HOST}{request.url.path}"},
            )
        return httpx.Response(200, content=b"<html>not a zip</html>")

    with pytest.raises(PullError) as exc:
        pull_fec_cycle(
            make_client(handler), 2024, tmp_path, "2026-01-01T00:00:00",
            sleep=no_sleep,
        )
    assert "not a valid ZIP" in str(exc.value)


def test_missing_inner_member_is_parked(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.fec.gov":
            return httpx.Response(
                302,
                headers={"location": f"{STORAGE_HOST}{request.url.path}"},
            )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("WRONG.txt", b"x")
        return httpx.Response(200, content=buf.getvalue())

    with pytest.raises(PullError) as exc:
        pull_fec_cycle(
            make_client(handler), 2024, tmp_path, "2026-01-01T00:00:00",
            sleep=no_sleep,
        )
    assert "does not contain" in str(exc.value)


def test_oversize_file_is_parked(tmp_path, monkeypatch):
    # A file larger than the gentleness cap STOPs rather than pushing through.
    monkeypatch.setattr(fec, "MAX_FILE_BYTES", 10)
    with pytest.raises(PullError) as exc:
        pull_fec_cycle(
            make_client(make_handler()), 2024, tmp_path, "2026-01-01T00:00:00",
            sleep=no_sleep,
        )
    assert "cap" in str(exc.value)


# ---------------------------------------------------------------------------
# The fec_pull entry point: multi-cycle, contact required.
# ---------------------------------------------------------------------------
def test_fec_pull_requires_a_contact(tmp_path):
    with pytest.raises(PullError) as exc:
        fec_pull(
            [2024], data_dir=tmp_path, fetched_at="2026-01-01T00:00:00",
            client=make_client(make_handler()), sleep=no_sleep,
        )
    assert "contact" in str(exc.value).lower()


def test_fec_pull_runs_with_contact(tmp_path):
    code = fec_pull(
        [2024], data_dir=tmp_path, contact=CONTACT,
        fetched_at="2026-01-01T00:00:00",
        client=make_client(make_handler()), sleep=no_sleep,
    )
    assert code == 0
    assert (tmp_path / "raw" / "fec" / "2024" / "fec-pull-manifest.json").exists()
