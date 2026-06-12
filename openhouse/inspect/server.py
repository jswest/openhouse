"""The ``inspect`` web surface: a small local review app over stdlib ``http.server``.

This is the only non-pure piece of the package — it does the disk I/O the pure
core (:mod:`openhouse.inspect.core`) deliberately avoids, and it binds a local
socket. It stays inside the repo's rails: **no network** (the only fetch is the
operator's own browser hitting ``127.0.0.1``), **no new Python deps** (stdlib
server + ``pdfplumber``, already a dependency), JSON as the machine contract
(scorecard to **stdout** on shutdown), prose/progress to **stderr**.

Two layers:

- :class:`ReviewSession` — all the state and logic (sample selection, body/PDF
  loading, verdict validation+persistence, scorecard). Pure-ish and unit-testable
  without ever starting an HTTP server.
- :class:`_Handler` / :func:`run` — a thin HTTP shell translating requests into
  ``ReviewSession`` calls and serving the committed static bundle.

The scorecard is emitted to stdout when the operator stops the server (Ctrl-C):
the review *is* the computation, so its result lands at the end, ``jq``-composable
like every other openhouse machine output.
"""

from __future__ import annotations

import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import pdfplumber

from . import core
from .labels import read_labels, write_labels
from .verdict import Verdict

# The committed Svelte bundle ships inside the package, so ``inspect`` runs with
# zero Node at runtime (the bundle is built by a contributor via ``npm run build``).
STATIC_DIR = Path(__file__).parent / "static"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class InspectError(Exception):
    """A fatal setup problem (e.g. the year isn't parsed). Caught in :func:`run`."""


def _contained(root: Path, target: Path) -> bool:
    """True iff resolved ``target`` sits inside resolved ``root`` (path-escape guard).

    The one place the package decides whether a path is safe to serve; both the
    PDF and static-file handlers route their containment check through here so the
    security-critical logic is defined once. Existence/file-type checks stay at the
    call sites (a PDF must exist; a static asset must be a regular file).
    """
    return root.resolve() in target.resolve().parents


# ---------------------------------------------------------------------------
# The review session — all logic, no HTTP.
# ---------------------------------------------------------------------------
class ReviewSession:
    """Holds the sampled queue, labels, and the verdict/scorecard logic.

    Constructed once per ``inspect`` run; the sample is selected deterministically
    at construction (same year/sample/seed → same queue) and the labels file is
    loaded so a session **resumes** where the last one left off.
    """

    def __init__(
        self, data_dir: Path, year: int, sample: float, seed: int, started_at: str
    ):
        self.data_dir = data_dir
        self.year = year
        self.sample = sample
        self.seed = seed
        self.started_at = started_at

        filings = self._load_filings()
        if filings is None:
            raise InspectError(
                f"{year} is not parsed (no parsed/{year}/filings.json); "
                f"run `openhouse parse {year}` first."
            )
        self._all = filings
        reviewable = core.reviewable(filings)
        selected = core.select(reviewable, sample, seed)
        self.queue_order = [f["doc_id"] for f in selected]
        self._by_id = {f["doc_id"]: f for f in selected}
        self.labels = read_labels(data_dir, year)

    # -- loading -----------------------------------------------------------
    def _year_dir(self) -> Path:
        return self.data_dir / "parsed" / str(self.year)

    def _load_filings(self) -> Optional[list[dict]]:
        path = self._year_dir() / "filings.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def _load_body(self, filing: dict) -> Optional[dict]:
        """The PTR/FD body for a filing, or ``None`` if it has no body on disk."""
        sub = "ptr" if (filing.get("filing_type") or {}).get("code") == "P" else "fd"
        path = self._year_dir() / sub / f"{filing['doc_id']}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def pdf_path(self, doc_id: str) -> Optional[Path]:
        """The on-disk source PDF for a *sampled* filing, sandboxed under ``data_dir``.

        The path is derived from the record's ``source_pdf`` (never from client
        input) and confirmed to resolve inside ``data_dir`` before it is handed to
        the byte server — a doc_id outside the sample, or a record whose path
        escapes the data root, yields ``None``.
        """
        filing = self._by_id.get(doc_id)
        if filing is None or not filing.get("source_pdf"):
            return None
        path = (self.data_dir / filing["source_pdf"]).resolve()
        if not _contained(self.data_dir, path) or not path.exists():
            return None
        return path

    def _raw_text(self, pdf_path: Optional[Path]) -> Optional[str]:
        """Full extracted text of the PDF, or ``None`` if it can't be read.

        For a scanned PDF this is near-empty — which is exactly the signal the
        reviewer needs (no text layer → the recall failure is an OCR gap, not a
        parser bug). Never raises into the request: an unreadable PDF returns
        ``None`` rather than crashing the review.
        """
        if pdf_path is None:
            return None
        try:
            with pdfplumber.open(pdf_path) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception:  # noqa: BLE001 — pdfplumber/pdfminer failure is non-fatal
            return None

    # -- API -------------------------------------------------------------
    def queue(self) -> dict:
        """The review queue: ordered doc_ids with per-item progress + the residual."""
        items = []
        for doc_id in self.queue_order:
            filing = self._by_id[doc_id]
            verdict = self.labels.get(doc_id)
            items.append(
                {
                    "doc_id": doc_id,
                    "filer": filing.get("filer"),
                    "filing_type": filing.get("filing_type"),
                    "pdf_class": filing.get("pdf_class"),
                    "stratum": "/".join(core.stratum_key(filing)),
                    "labelled": verdict is not None,
                    "stale": self._is_stale(filing, verdict),
                }
            )
        return {
            "year": self.year,
            "sample": self.sample,
            "seed": self.seed,
            "count": len(items),
            "residual": self._residual(),
            "items": items,
        }

    def filing(self, doc_id: str) -> Optional[dict]:
        """The full review payload for one filing, or ``None`` if not in the sample."""
        filing = self._by_id.get(doc_id)
        if filing is None:
            return None
        body = self._load_body(filing)
        verdict = self.labels.get(doc_id)
        return {
            "doc_id": doc_id,
            "filing": filing,
            "body": body,
            "raw_text": self._raw_text(self.pdf_path(doc_id)),
            "snapshot": core.snapshot_hash(filing, body),
            "verdict": verdict,
            "stale": self._is_stale(filing, verdict),
            "pdf_url": f"/api/pdf/{doc_id}",
        }

    def record_verdict(self, doc_id: str, payload: dict) -> dict:
        """Validate + persist a verdict for a sampled filing.

        The snapshot is stamped server-side from the *current* parsed record, so a
        verdict is always pinned to what the reviewer actually saw — the client
        cannot forge it. Persists the whole labels file atomically (resumable).
        Raises :class:`KeyError` for an unknown doc_id and ``ValueError`` (pydantic)
        for an invalid verdict.
        """
        filing = self._by_id.get(doc_id)
        if filing is None:
            raise KeyError(doc_id)
        body = self._load_body(filing)
        data = {**payload, "snapshot": core.snapshot_hash(filing, body)}
        verdict = Verdict(**data)
        self.labels[doc_id] = verdict.model_dump()
        write_labels(self.data_dir, self.year, self.labels, started_at=self.started_at)
        return self.labels[doc_id]

    def scorecard(self) -> dict:
        """The accuracy scorecard over every sampled filing that has a verdict."""
        reviewed = []
        for doc_id in self.queue_order:
            verdict = self.labels.get(doc_id)
            if verdict is None:
                continue
            filing = self._by_id[doc_id]
            reviewed.append(
                {
                    "filing": filing,
                    "verdict": verdict,
                    "stale": self._is_stale(filing, verdict),
                }
            )
        card = core.scorecard(reviewed)
        card["year"] = self.year
        card["n_in_sample"] = len(self.queue_order)
        return card

    # -- helpers ---------------------------------------------------------
    def _is_stale(self, filing: dict, verdict: Optional[dict]) -> bool:
        if verdict is None or "snapshot" not in verdict:
            return False
        return core.is_stale(verdict["snapshot"], filing, self._load_body(filing))

    def _residual(self) -> dict:
        """Counts for the "complete over K reviewable; M not reviewable" line."""
        reviewable = sum(1 for f in self._all if f.get("parse_status") == "ok")
        error = sum(1 for f in self._all if f.get("parse_status") == "error")
        scanned = sum(
            1
            for f in self._all
            if f.get("pdf_class") == "scanned" and f.get("parse_status") != "ok"
        )
        return {
            "reviewable": reviewable,
            "not_reviewable": len(self._all) - reviewable,
            "scanned_unparsed": scanned,
            "error": error,
        }

    def residual_line(self) -> str:
        r = self._residual()
        return (
            f"residual: complete over {r['reviewable']} reviewable; "
            f"{r['not_reviewable']} not reviewable "
            f"(scanned-unparsed {r['scanned_unparsed']} / error {r['error']})"
        )


# ---------------------------------------------------------------------------
# The HTTP shell — thin translation over ReviewSession.
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "openhouse-inspect"

    @property
    def session(self) -> ReviewSession:
        return self.server.session  # type: ignore[attr-defined]

    def log_message(self, *args) -> None:  # silence per-request stderr noise
        pass

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/pdf/"):
            self._serve_pdf(unquote(path[len("/api/pdf/"):]))
        elif path.startswith("/api/filing/"):
            payload = self.session.filing(unquote(path[len("/api/filing/"):]))
            self._json(payload, 404 if payload is None else 200)
        elif path == "/api/queue":
            self._json(self.session.queue())
        else:
            self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/verdict/"):
            self._json({"error": "not found"}, 404)
            return
        doc_id = unquote(path[len("/api/verdict/"):])
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            payload = json.loads(body or b"{}")
            saved = self.session.record_verdict(doc_id, payload)
        except KeyError:
            self._json({"error": f"unknown doc_id {doc_id!r}"}, 404)
        except (ValueError, TypeError) as exc:
            self._json({"error": f"invalid verdict: {exc}"}, 400)
        else:
            self._json({"ok": True, "verdict": saved})

    # -- responders ------------------------------------------------------
    def _send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        """The shared byte-writing tail behind every response (JSON, PDF, static)."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload, status: int = 200) -> None:
        self._send_bytes(json.dumps(payload).encode(), "application/json", status)

    def _serve_pdf(self, doc_id: str) -> None:
        path = self.session.pdf_path(doc_id)
        if path is None:
            self._json({"error": "no such PDF in sample"}, 404)
            return
        self._send_bytes(path.read_bytes(), "application/pdf")

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not _contained(STATIC_DIR, target) or not target.is_file():
            # SPA fallback: unknown non-API path → index.html (if the bundle exists).
            target = (STATIC_DIR / "index.html").resolve()
        if not target.is_file():
            self._json(
                {"error": "UI bundle not built; run `npm run build` in web/inspect"},
                404,
            )
            return
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send_bytes(target.read_bytes(), ctype)


class _InspectHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, session: ReviewSession):
        super().__init__(address, _Handler)
        self.session = session


def run(year: int, *, data_dir: Path, sample: float, seed: int, started_at: str) -> int:
    """Launch the review app, serve until Ctrl-C, then emit the scorecard to stdout.

    Returns the process exit code: ``0`` on a clean stop, ``1`` on a setup error
    (e.g. the year isn't parsed). The single ``started_at`` timestamp is threaded
    in from the CLI entry — no wall-clock is read here.
    """
    try:
        session = ReviewSession(data_dir, year, sample, seed, started_at)
    except InspectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not session.queue_order:
        print(
            f"note: no reviewable filings selected for {year} at --sample {sample}.",
            file=sys.stderr,
        )

    httpd = _InspectHTTPServer(("127.0.0.1", 0), session)
    host, port = httpd.server_address
    url = f"http://{host}:{port}/"
    n = len(session.queue_order)
    print(f"inspect: reviewing {n} filings — {url}", file=sys.stderr)
    print(session.residual_line(), file=sys.stderr)
    print("inspect: Ctrl-C when done — scorecard goes to stdout.", file=sys.stderr)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 — a headless box just won't pop a browser
        pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\ninspect: stopping; computing scorecard…", file=sys.stderr)
    finally:
        httpd.shutdown()
        httpd.server_close()

    json.dump(session.scorecard(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0
