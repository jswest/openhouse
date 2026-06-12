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

import re
from datetime import datetime
from pathlib import Path

import pdfplumber

from .schemas import AmountRange, PtrTransaction

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


# ---------------------------------------------------------------------------
# E-filed PTR body extraction (SPEC §6.3 transactions[]).
#
# An e-filed PTR's TRANSACTIONS table is positional, not line-clean: pdfplumber
# joins each row into a header line — ``[owner] <asset…> <type> <txn-date>
# <notif-date> <amount range> <cap-gains glyph>`` — but a long asset name wraps
# onto the *following* line(s) before the row's ``FILINg STATUS:`` /
# ``SUBHOLDINg OF:`` / ``DESCRIPTION:`` detail lines. We anchor on the header
# line (the date pair + amount + glyph are an unambiguous signature), then fold
# any wrapped asset-name continuation back in. SPEC §2.2's small-caps caveat is
# why we never match heading text and why the ticker is uppercased below.
# ---------------------------------------------------------------------------

# The transaction header line: optional owner letters, an asset blob, the
# transaction type, the date pair, the amount range, and the cap-gains glyph
# (``gfedc`` unchecked / ``gfedcb`` checked) anchored at end of line.
_PTR_ROW_RE = re.compile(
    r"^(?:(SP|DC|JT)\s+)?"  # owner column (blank → self)
    r"(.+?)\s+"  # asset blob (non-greedy up to the type)
    r"(S \(partial\)|[PSE])\s+"  # transaction type
    r"(\d{2}/\d{2}/\d{4})\s+"  # transaction date
    r"(\d{2}/\d{2}/\d{4})\s+"  # notification date
    r"(\$[\d,]+ - \$[\d,]+)\s+"  # amount range label
    r"(gfedcb?)\s*$"  # cap-gains glyph
)

# Lines that end a row's asset-name wrap (the row's detail/section lines). An
# asset continuation is any line that is none of these and not a new header.
_PTR_DETAIL_RE = re.compile(
    r"^(FILINg STATUS:|SUBHOLDINg OF:|DESCRIPTION:|ID Owner Asset|Type Date|\$200\?)",
)

_TICKER_RE = re.compile(r"\(([^()]+)\)")  # the parenthesized symbol in an asset name
_ASSET_TYPE_RE = re.compile(r"\[([^\[\]]+)\]")  # the bracketed [ST]-style tag


def _parse_amount_range(label: str) -> AmountRange:
    """Parse a ``"$1,001 - $15,000"`` bucket label into ``{low, high, label}``."""
    low_s, high_s = (part.strip() for part in label.split(" - ", 1))
    low = int(low_s.lstrip("$").replace(",", ""))
    high = int(high_s.lstrip("$").replace(",", ""))
    return AmountRange(low=low, high=high, label=label)


def _ticker_from_asset(asset: str) -> str | None:
    """Strict symbol-only ticker: the parenthesized ``(SYMBOL)``, uppercased.

    Returns ``None`` when the asset carries no parenthesized symbol — corp bonds
    ``[CS]`` and other non-ticker classes legitimately have none, and that is a
    correct ``None``, never a sentinel. The ticker is **never** inferred from the
    company name. Uppercasing defeats pdfplumber's small-caps glyph artifact
    (``AAPl`` → ``AAPL``, ``bRK.b`` → ``BRK.B``); precision is 1 by design.
    """
    match = _TICKER_RE.search(asset)
    return match.group(1).strip().upper() if match else None


def _asset_type_from_asset(asset: str) -> str | None:
    """The bracketed ``[ST]``-style tag, preserved raw (without brackets)."""
    match = _ASSET_TYPE_RE.search(asset)
    return match.group(1).strip() if match else None


def extract_ptr_transactions(pdf_path: Path) -> list[PtrTransaction]:
    """Extract an e-filed PTR's §6.3 ``transactions[]`` from its PDF. Offline.

    Layout-aware (SPEC §2.2): anchors on each row's header line and folds any
    wrapped asset-name continuation back in. Raises :class:`PdfExtractError` on a
    present-but-unreadable PDF so the ``parse`` caller can record an
    ``extract_failed`` outcome rather than crash the run.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            lines: list[str] = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                lines.extend(text.splitlines())
    except Exception as exc:  # noqa: BLE001 — any pdfplumber/pdfminer failure
        raise PdfExtractError(
            f"could not extract PTR body from {pdf_path}: {exc}"
        ) from exc

    transactions: list[PtrTransaction] = []
    i = 0
    n = len(lines)
    while i < n:
        match = _PTR_ROW_RE.match(lines[i].strip())
        if not match:
            i += 1
            continue

        owner, asset_head, txn_type, txn_date, notif_date, amount, glyph = (
            match.groups()
        )
        asset_parts = [asset_head.strip()]
        description: str | None = None

        # Fold the row's following lines, in two phases. Phase 1 (before any
        # detail line): a bare line is an asset-name wrap → join it into the
        # asset. Phase 2 (from the first detail line on): only a DESCRIPTION:
        # line matters; everything else is skipped. The row ends at the next
        # header line, which the outer loop then picks up.
        seen_detail = False
        j = i + 1
        while j < n:
            nxt = lines[j].strip()
            if _PTR_ROW_RE.match(nxt):
                break  # next transaction row — end of this row
            if nxt.startswith("DESCRIPTION:"):
                description = nxt[len("DESCRIPTION:") :].strip() or None
                seen_detail = True
            elif _PTR_DETAIL_RE.match(nxt):
                seen_detail = True
            elif nxt and not seen_detail:
                asset_parts.append(nxt)  # asset-name wrap
            j += 1

        asset = " ".join(part for part in asset_parts if part)
        transactions.append(
            PtrTransaction(
                owner=owner or "self",
                asset=asset,
                ticker=_ticker_from_asset(asset),
                asset_type=_asset_type_from_asset(asset),
                transaction_type="S(partial)" if txn_type == "S (partial)" else txn_type,
                transaction_date=datetime.strptime(txn_date, "%m/%d/%Y").date(),
                notification_date=datetime.strptime(notif_date, "%m/%d/%Y").date(),
                amount_range=_parse_amount_range(amount),
                cap_gains_over_200=(glyph == "gfedcb"),
                description=description,
            )
        )
        i = j

    return transactions
