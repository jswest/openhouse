"""PDF classification: ``efiled`` / ``scanned`` / ``missing`` — offline (SPEC §2.2).

The ``parse`` command must decide, for each filing's on-disk PDF, whether it is
an **e-filed** (text-based, FDonline/IntelliWorxIT) body — the v1 extraction
target — or a **scanned** image-only/paper body that v1 catalogues but does not
OCR. SPEC §2.2 settles the test: **text extraction is authoritative**. A live
e-filed page yields ~1,000 chars of real text; a scanned page yields literally
**0**, so extraction alone decides — the DocID-prefix heuristic noted in SPEC §2.2
is not consulted.

Everything here is offline and deterministic: it opens only files already on disk
(the committed fixtures or a prior ``pull``), never the Clerk.
"""

from __future__ import annotations

from pathlib import Path

import pdfplumber

# Non-whitespace characters at or above which a PDF is classified ``efiled``.
#
# SPEC §2.2 verified the populations are unambiguous and far apart: an e-filed
# page extracts ~1,000 chars, a scanned page extracts exactly 0. Measured on the
# committed fixtures (tests/fixtures/pdf/README.md): the *smallest* e-filed body
# yields ~960 non-whitespace chars; both scanned bodies yield 0. A threshold of
# 20 sits in the wide empty gap between 0 and ~960 — generous enough to ignore a
# stray watermark/scanner-artifact glyph on an otherwise image-only page, yet far
# below any real e-filed body. We count non-whitespace chars (not raw length) so
# layout whitespace never inflates a near-empty page toward the threshold.
EFILED_MIN_NONWS_CHARS = 20


class PdfExtractError(Exception):
    """A present PDF could not be opened/extracted (corrupt, truncated, not a PDF).

    Distinct from ``missing`` (no file on disk): the bytes exist but pdfplumber
    could not read them. The ``parse`` caller turns this into
    ``parse_status="error"`` + unparsed reason ``"extract_failed"`` (never a
    crash that loses the year — CLAUDE.md "never silently drop a filing").
    """


def _nonws_char_count(pdf_path: Path) -> int:
    """Sum non-whitespace extracted-text chars across all pages of ``pdf_path``.

    Raises :class:`PdfExtractError` if the file is present but unreadable.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = 0
            for page in pdf.pages:
                text = page.extract_text() or ""
                total += len("".join(text.split()))
            return total
    except Exception as exc:  # noqa: BLE001 — any pdfplumber/pdfminer failure
        # A present-but-corrupt PDF must surface as an error outcome, never crash
        # the whole parse run.
        raise PdfExtractError(f"could not extract text from {pdf_path}: {exc}") from exc


def classify(pdf_path: Path) -> str:
    """Classify a PDF as ``"efiled"`` / ``"scanned"`` / ``"missing"`` (SPEC §2.2).

    - File not on disk → ``"missing"``.
    - Present and extracts >= :data:`EFILED_MIN_NONWS_CHARS` non-whitespace chars
      → ``"efiled"``.
    - Present but extracts fewer (a scanned/image-only body yields 0) → ``"scanned"``.

    Text extraction is the **authoritative** test (SPEC §2.2). A present-but-corrupt
    PDF raises :class:`PdfExtractError` rather than being classified — the caller
    maps that to an ``extract_failed`` error outcome.
    """
    if not pdf_path.exists():
        return "missing"
    if _nonws_char_count(pdf_path) >= EFILED_MIN_NONWS_CHARS:
        return "efiled"
    return "scanned"
