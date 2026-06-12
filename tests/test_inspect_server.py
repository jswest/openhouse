"""Offline tests for the ``inspect`` web surface (#56).

Two layers, both hermetic: :class:`ReviewSession` is driven directly (no HTTP) on
a fixture data tree built under ``tmp_path``, and a handful of smoke tests start
the real stdlib server on ``127.0.0.1:0`` (a local socket, never the network) to
prove routing + the verdict round-trip end to end.

``_raw_text`` is exercised with a non-PDF stand-in: pdfplumber fails to parse it
and the session returns ``None`` rather than crashing — the same graceful path a
scanned image PDF takes.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from openhouse.inspect.server import ReviewSession, _InspectHTTPServer

STARTED_AT = "2026-06-12T00:00:00"


def _filing(doc_id, *, code="P", status="ok", pdf_class="efiled", sub="ptr"):
    return {
        "doc_id": doc_id,
        "year": 2022,
        "filer": {"first": "Ada", "last": "Lovelace"},
        "filing_type": {"code": code, "label": "x"},
        "pdf_class": pdf_class,
        "parse_status": status,
        "source_pdf": f"raw/2022/{sub}/{doc_id}.pdf",
    }


def _build_tree(tmp_path: Path, filings, bodies=None) -> Path:
    """Lay down a minimal ``data_dir``: filings.json + bodies + dummy PDFs."""
    data = tmp_path / "data"
    ydir = data / "parsed" / "2022"
    (ydir).mkdir(parents=True)
    (ydir / "filings.json").write_text(json.dumps(filings))
    for f in filings:
        sub = "ptr" if f["filing_type"]["code"] == "P" else "fd"
        body = (bodies or {}).get(f["doc_id"])
        if body is not None:
            bdir = ydir / sub
            bdir.mkdir(exist_ok=True)
            (bdir / f"{f['doc_id']}.json").write_text(json.dumps(body))
        pdf = data / f["source_pdf"]
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4 not-a-real-pdf")
    return data


def _session(tmp_path, filings, bodies=None, *, sample=1.0, seed=0):
    data = _build_tree(tmp_path, filings, bodies)
    return ReviewSession(data, 2022, sample, seed, STARTED_AT)


# ---------------------------------------------------------------------------
# ReviewSession
# ---------------------------------------------------------------------------
def test_session_samples_only_reviewable(tmp_path):
    s = _session(tmp_path, [_filing("a"), _filing("b", status="error"), _filing("c")])
    assert set(s.queue_order) == {"a", "c"}  # the errored filing is residual, not sampled


def test_unparsed_year_raises(tmp_path):
    from openhouse.inspect.server import InspectError

    with pytest.raises(InspectError):
        ReviewSession(tmp_path / "data", 2022, 1.0, 0, STARTED_AT)


def test_filing_payload_shape(tmp_path):
    body = {"transactions": [{"asset": "AAPL"}]}
    s = _session(tmp_path, [_filing("a")], {"a": body})
    payload = s.filing("a")
    assert payload["body"] == body
    assert payload["snapshot"].startswith("sha256:")
    assert payload["raw_text"] is None  # dummy bytes → pdfplumber fails → None
    assert payload["verdict"] is None
    assert payload["pdf_url"] == "/api/pdf/a"


def test_filing_outside_sample_is_none(tmp_path):
    s = _session(tmp_path, [_filing("a"), _filing("b")], sample=0.5, seed=1)
    missing = [d for d in ("a", "b") if d not in s.queue_order]
    assert s.filing(missing[0]) is None


def test_record_verdict_persists_and_stamps_snapshot(tmp_path):
    s = _session(tmp_path, [_filing("a")])
    saved = s.record_verdict(
        "a",
        {
            "is_fully_precise": False,
            "is_fully_recalled": False,
            "n_missing_entries": 12,
            "is_metadata_accurate": True,
            "is_metadata_fully_complete": True,
            "note": "scanned PTR; 0 extracted",
        },
    )
    assert saved["snapshot"] == s.filing("a")["snapshot"]  # server-stamped, authoritative
    # Resumable: a fresh session reads it back.
    s2 = ReviewSession(s.data_dir, 2022, 1.0, 0, STARTED_AT)
    assert s2.labels["a"]["n_missing_entries"] == 12


def test_record_verdict_rejects_invalid(tmp_path):
    s = _session(tmp_path, [_filing("a")])
    with pytest.raises(ValueError):  # precise=True but a positive incorrect count
        s.record_verdict(
            "a",
            {
                "is_fully_precise": True,
                "is_fully_recalled": True,
                "n_incorrect_entries": 3,
                "is_metadata_accurate": True,
                "is_metadata_fully_complete": True,
            },
        )


def test_record_verdict_unknown_doc(tmp_path):
    s = _session(tmp_path, [_filing("a")])
    with pytest.raises(KeyError):
        s.record_verdict("nope", {})


def test_stale_flag_when_parse_changes(tmp_path):
    s = _session(tmp_path, [_filing("a")], {"a": {"transactions": []}})
    s.record_verdict(
        "a",
        {
            "is_fully_precise": True,
            "is_fully_recalled": True,
            "is_metadata_accurate": True,
            "is_metadata_fully_complete": True,
        },
    )
    assert s.filing("a")["stale"] is False
    # Simulate a re-parse that changed the body, then reload.
    (s.data_dir / "parsed/2022/ptr/a.json").write_text(json.dumps({"transactions": [{"x": 1}]}))
    s2 = ReviewSession(s.data_dir, 2022, 1.0, 0, STARTED_AT)
    assert s2.filing("a")["stale"] is True


def test_pdf_path_sandbox(tmp_path):
    escape = _filing("a")
    escape["source_pdf"] = "../../../etc/passwd"
    s = _session(tmp_path, [escape, _filing("b")])
    assert s.pdf_path("a") is None  # path escapes data_dir → refused
    assert s.pdf_path("b") is not None


def test_scorecard_over_labelled(tmp_path):
    s = _session(tmp_path, [_filing("a", pdf_class="scanned"), _filing("b")])
    s.record_verdict(
        "a",
        {
            "is_fully_precise": True,
            "is_fully_recalled": False,
            "n_missing_entries": 7,
            "is_metadata_accurate": True,
            "is_metadata_fully_complete": True,
        },
    )
    card = s.scorecard()
    assert card["n_in_sample"] == 2
    assert card["overall"]["n_reviewed"] == 1  # only the labelled one
    assert card["by_stratum"]["scanned/ptr"]["entry_level"]["sum_missing"] == 7


def test_residual_line(tmp_path):
    s = _session(
        tmp_path,
        [_filing("a"), _filing("b", status="error"), _filing("c", status="error", pdf_class="scanned")],
    )
    line = s.residual_line()
    assert "1 reviewable" in line and "error 2" in line


# ---------------------------------------------------------------------------
# HTTP smoke tests (local socket, not the network)
# ---------------------------------------------------------------------------
@pytest.fixture
def http(tmp_path):
    s = _session(tmp_path, [_filing("a"), _filing("b")])
    httpd = _InspectHTTPServer(("127.0.0.1", 0), s)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    base = f"http://{host}:{port}"
    yield base, s
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urlopen(url) as resp:
        return resp.status, json.loads(resp.read())


def test_http_queue(http):
    base, _ = http
    status, payload = _get(f"{base}/api/queue")
    assert status == 200
    assert payload["count"] == 2
    assert {it["doc_id"] for it in payload["items"]} == {"a", "b"}


def test_http_pdf_bytes(http):
    base, _ = http
    with urlopen(f"{base}/api/pdf/a") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "application/pdf"
        assert resp.read().startswith(b"%PDF")


def test_http_verdict_round_trip(http):
    base, session = http
    body = json.dumps(
        {
            "is_fully_precise": True,
            "is_fully_recalled": True,
            "is_metadata_accurate": True,
            "is_metadata_fully_complete": True,
        }
    ).encode()
    req = Request(f"{base}/api/verdict/a", data=body, method="POST")
    with urlopen(req) as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["ok"] is True
    assert "a" in session.labels


def test_http_verdict_invalid_is_400(http):
    base, _ = http
    body = json.dumps(
        {
            "is_fully_precise": True,
            "n_incorrect_entries": 5,  # contradicts precise=True
            "is_fully_recalled": True,
            "is_metadata_accurate": True,
            "is_metadata_fully_complete": True,
        }
    ).encode()
    req = Request(f"{base}/api/verdict/a", data=body, method="POST")
    with pytest.raises(Exception) as exc:
        urlopen(req)
    assert "400" in str(exc.value)
