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

from .schemas import (
    AmountRange,
    FdBody,
    PtrTransaction,
    RawLineItem,
    ScheduleAItem,
    ScheduleBItem,
    ScheduleCItem,
    ScheduleDItem,
)

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


class NotAnFdBody(Exception):
    """An e-filed fd-family PDF that carries no schedule headings.

    Extensions / cover sheets (DocID-prefix ``3``) route to the ``fd`` family but
    are *not* annual-FD schedule documents — they have no Schedule A–J headings.
    This is **not** an extraction failure (the PDF read cleanly); the ``parse``
    caller simply writes no FD body for it (the filing still lives in
    ``filings.json`` with ``pdf_class="efiled"``), so it is neither a silent drop
    nor a misleading empty body.
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
#
# The amount range's **high bound is optional here** (SPEC §2.2 column wrap): on a
# sizable minority of real e-filed PTRs the amount column wraps, leaving the
# header line ending ``$LOW - <glyph>`` with the ``$HIGH`` bound spilled onto the
# *following* line (``$50,000``). We capture the low bound and the (maybe-absent)
# high bound separately; the extractor folds the wrapped high bound back in from
# the next line. Without this, ~two-thirds of 2020 PTRs failed the completeness
# guard and were dropped wholesale — exactly the trades the spec says never to
# silently lose.
_PTR_ROW_RE = re.compile(
    r"^(?:(SP|DC|JT)\s+)?"  # owner column (blank → self)
    r"(.+?)\s+"  # asset blob (non-greedy up to the type)
    r"([Ss] \(partial\)|[PpSsEe])\s+"  # transaction type (only S can be partial)
    r"(\d{2}/\d{2}/\d{4})\s+"  # transaction date
    r"(\d{2}/\d{2}/\d{4})\s+"  # notification date
    r"(\$[\d,]+)\s+-\s+(\$[\d,]+)?\s*"  # amount range: low, then optional high
    r"(gfedcb?)\s*$"  # cap-gains glyph
)
# A wrapped high bound spilled to the next line. It is the lone ``$N`` money token
# on that continuation line, and may sit either at the **start** (``$50,000`` —
# the asset name did not also wrap) or **end** (``Shares (COLD) [ST] $50,000`` —
# the asset name wrapped onto the same line). Across 2020 every wrap-continuation
# line carries exactly one money token, so a single ``$N`` find is unambiguous;
# whatever else is on the line is asset-name wrap and folds back in normally.
_PTR_WRAPPED_HIGH_RE = re.compile(r"\$[\d,]+")

# Detail/section lines that END a row's asset-name wrap. Matched
# case-INSENSITIVELY: SPEC §2.2's small-cap glyphs land on different letters from
# one filing to the next (``FILINg STATUS:`` / ``FIlINg STATuS:`` /
# ``FIlINg STaTuS:`` — 10+ renderings seen across 2020 alone), so only the
# letter sequence is stable, never the case. Matching a fixed-case literal here
# silently dropped the detail boundary on the majority of real PTRs.
_PTR_DETAIL_RE = re.compile(
    r"^(FILING STATUS:|SUBHOLDING OF:|DESCRIPTION:)", re.IGNORECASE
)
# The per-row "FILING STATUS:" line, used to count blocks for the completeness
# guard — same case-insensitive shape as above (just the status line).
_PTR_STATUS_RE = re.compile(r"^FILING STATUS:", re.IGNORECASE)
# A row's DESCRIPTION: detail line (case-insensitive, same small-caps reason).
_PTR_DESCRIPTION_RE = re.compile(r"^DESCRIPTION:", re.IGNORECASE)

# Per-page table furniture pdfplumber repeats at the top of every page. When a
# row's asset name wraps across a page break, this furniture lands BETWEEN the
# header line and the wrapped continuation, so it must be skipped WITHOUT ending
# the wrap — otherwise the "(TICKER) [TYPE]" continuation is silently dropped
# (null ticker and asset_type, truncated asset, invisible to the §-residual).
_PTR_FURNITURE_RE = re.compile(r"^(ID Owner Asset|Type Date|Gains >|\$200\?)")

# A lone cap-gains glyph stranded on its own line. When a header line wraps across
# a *page break*, the per-page furniture (and sometimes a stray glyph remnant from
# the header's end) lands between the header and the wrapped ``$HIGH`` bound. The
# wrapped-high search skips both furniture and this glyph remnant so the page-break
# case recovers the high bound rather than dropping the whole row.
_PTR_GLYPH_ONLY_RE = re.compile(r"^gfedcb?\s*$")

_TICKER_RE = re.compile(r"\(([^()]+)\)")  # a parenthesized symbol in an asset name
# The ticker is the paren group immediately before the bracketed [TYPE] tag.
_TICKER_BEFORE_TYPE_RE = re.compile(r"\(([^()]+)\)\s*\[[^\[\]]+\]")
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

    The symbol is the paren group **immediately before the ``[TYPE]`` tag** (the
    Clerk's ticker slot): an asset can carry an earlier parenthetical that is not
    the symbol — ``Coca-Cola Company (The) (KO) [ST]`` → ``KO``, not ``THE`` (a
    fabricated symbol would break ``--ticker`` soundness). When the asset has no
    ``[TYPE]`` tag at all, fall back to the last paren group.
    """
    match = _TICKER_BEFORE_TYPE_RE.search(asset)
    if match:
        return match.group(1).strip().upper()
    parens = _TICKER_RE.findall(asset)
    return parens[-1].strip().upper() if parens else None


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

        owner, asset_head, txn_type, txn_date, notif_date, amount_low, amount_high, glyph = (
            match.groups()
        )
        # Small-caps can lower-case the type letter (``s``/``p``/``e``); normalize
        # to the schema's canonical form — ``S``/``P``/``E`` or ``S(partial)``.
        txn_type = (
            "S(partial)" if "(partial)" in txn_type.lower() else txn_type[0].upper()
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

        # Amount-column wrap (SPEC §2.2): when the header line carried only the
        # low bound (``$LOW - <glyph>``), the ``$HIGH`` bound spilled onto a
        # following line as the lone money token there. It is usually the very
        # next line, but when the header wraps across a *page break* the repeated
        # per-page furniture (and a stray glyph remnant) intervenes — so skip
        # furniture/glyph-only/blank lines first, then take the high bound wherever
        # it sits. Whatever else is on that line is asset-name wrap and folds in.
        if amount_high is None:
            k = j
            while k < n:
                nxt = lines[k].strip()
                if _PTR_ROW_RE.match(nxt) or _PTR_DETAIL_RE.match(nxt):
                    break  # reached the next row / this row's detail — high bound
                    # never materialized; leave amount_high None (row drops below).
                if not nxt or _PTR_FURNITURE_RE.match(nxt) or _PTR_GLYPH_ONLY_RE.match(nxt):
                    k += 1
                    continue  # page-break furniture / stray glyph — skip, keep looking
                high_m = _PTR_WRAPPED_HIGH_RE.search(nxt)
                if high_m:
                    amount_high = high_m.group(0)
                    remainder = (
                        nxt[: high_m.start()] + " " + nxt[high_m.end() :]
                    ).strip()
                    if remainder:
                        asset_parts.append(remainder)
                    j = k + 1
                break

        while j < n:
            nxt = lines[j].strip()
            if _PTR_ROW_RE.match(nxt):
                break  # next transaction row — end of this row
            desc_m = _PTR_DESCRIPTION_RE.match(nxt)
            if desc_m:
                description = nxt[desc_m.end() :].strip() or None
                seen_detail = True
            elif _PTR_DETAIL_RE.match(nxt):
                seen_detail = True
            elif _PTR_FURNITURE_RE.match(nxt):
                pass  # repeated per-page table header (page break) — skip, don't
                # end the wrap and don't fold it into the asset name
            elif nxt and not seen_detail:
                asset_parts.append(nxt)  # asset-name wrap
            j += 1

        # If the high bound never materialized (neither on the header line nor as
        # the next line's lead token), this row is not cleanly parsed: leave it
        # unmatched so the completeness guard below surfaces the mismatch as
        # ``extract_failed`` rather than fabricating a half-range.
        if amount_high is None:
            i += 1
            continue

        asset = " ".join(part for part in asset_parts if part)
        transactions.append(
            PtrTransaction(
                owner=owner or "self",
                asset=asset,
                ticker=_ticker_from_asset(asset),
                asset_type=_asset_type_from_asset(asset),
                transaction_type=txn_type,
                transaction_date=datetime.strptime(txn_date, "%m/%d/%Y").date(),
                notification_date=datetime.strptime(notif_date, "%m/%d/%Y").date(),
                amount_range=_parse_amount_range(f"{amount_low} - {amount_high}"),
                cap_gains_over_200=(glyph == "gfedcb"),
                description=description,
            )
        )
        i = j

    # Guard against a silently partial extraction (CLAUDE.md "never silently drop
    # a filing"): every transaction has exactly one "FILINg STATUS:" line, so a
    # count mismatch means a header row failed the one-line signature and was
    # skipped (or the body came back empty). Surface it loudly as extract_failed
    # rather than writing a too-short {"transactions": [...]} with status "ok".
    status_blocks = sum(1 for ln in lines if _PTR_STATUS_RE.match(ln.strip()))
    if status_blocks != len(transactions):
        raise PdfExtractError(
            f"PTR extraction incomplete for {pdf_path}: matched "
            f"{len(transactions)} transaction row(s) but found {status_blocks} "
            "'FILINg STATUS:' block(s) — the layout was not fully parsed"
        )

    return transactions


# ---------------------------------------------------------------------------
# E-filed annual-FD schedule body extraction (SPEC §6.3 / §6.4).
#
# An annual FD is a schedule-by-schedule document (A–J). The extraction segments
# the body by **schedule letter** — never by full heading text, since the form's
# small-cap glyphs are lost and render inconsistently (SPEC §2.2). Two renderings
# exist in the wild, depending on whether the small-caps font carried a
# /ToUnicode map when the Clerk generated the PDF:
#
# - **letters survive, case-mangled** (dominant through 2020): ``ScheDule`` /
#   ``ScHeDule`` / ``SCheDuLe`` — the letter sequence is intact, only case varies.
# - **glyphs lost to NULs** (dominant 2021 on): every small-cap glyph extracts as
#   U+0000, one NUL per glyph — ``S\x00{7} A: A\x00{5} …`` is ``Schedule A:
#   Assets …``. NUL is not ``\s``, not stripped by ``str.strip``, and invisible
#   in most viewers, so any matcher keyed on the letters alone goes blind.
#
# The stable structural invariant (verified across 2020–2022): **NULs appear
# only in the form's own small-caps furniture** — headings, section titles, the
# ``LOCATION:``/``DESCRIPTION:`` labels — never in filer-entered content, which
# is set in a regular font. So each matcher below accepts, alongside its
# letters-survive form, a NUL-run form that *cannot* collide with content.
# An empty schedule prints the literal ``None disclosed.`` → recorded as **absent**
# (its letter is simply omitted from the body). A–D are column-parsed; E–J ship as
# raw_text-only line items; every line item carries verbatim ``raw_text``.
# ---------------------------------------------------------------------------

# A schedule heading: ``S…edule <LETTER>:`` with the small-cap glyphs lost. The
# letter is captured (upper- or lower-case both occur) and the rest of the
# heading text is deliberately ignored. Anchored at line start. The ``\x00+``
# branch is the glyphs-lost rendering (``S\x00\x00\x00\x00\x00\x00\x00 A:``);
# requiring the NUL run keeps it collision-proof — content lines never carry
# NULs, and the small-caps appendix titles that do start ``S\x00`` (``Schedules
# A and B Asset Class Details``) carry no ``<LETTER>:`` so they never match.
_FD_HEADING_RE = re.compile(r"^S(?:c?h?e?d?ule|\x00+)\s+([A-Ja-j]):", re.IGNORECASE)

# The literal an empty schedule renders → schedule absent (SPEC §2.2).
_NONE_DISCLOSED_RE = re.compile(r"^None disclosed\.?\s*$", re.IGNORECASE)

# The trailing non-schedule sections that follow the last schedule (exclusions
# checkboxes + the certification/signature block). They end the last schedule's
# content. The ``[EC]\x00`` branch is the glyphs-lost rendering of the same two
# section titles (``E\x00…`` = "Exclusions of Spouse, Dependent, or Trust
# Information", ``C\x00…`` = "Certification and Signature"). The ``(?!:)``
# negative lookahead after the NUL run is load-bearing: the per-row ``COMMENTS:``
# detail label *inside* a schedule renders as ``C\x00{7}: <filer text>`` in NUL
# docs and would otherwise match this trailer branch, ending the body early and
# silently dropping every following content row. The two legitimate NUL trailers
# (Exclusions ``E\x00{9} …``, Certification ``C\x00{12} …``) are never followed by
# a colon; the comments label always is — so excluding a trailing colon keeps the
# real trailers matching while letting the comments line fold into the row's
# raw_text, exactly as the intact-glyph ``COMMENTS:`` label does. The quantifier
# is possessive (``\x00++``) so greedy backtracking can't shrink the NUL run to
# expose a non-colon char and defeat the lookahead.
_FD_TRAILER_RE = re.compile(
    r"^(exclusions of|certification and|digitally signed|[EC]\x00++(?!:))",
    re.IGNORECASE,
)

# An A/B row's trailing "tx. over $1,000?" checkbox glyph (gfedc unchecked /
# gfedcb checked) — the same glyph as the PTR cap-gains box, anchored at line end.
_FD_GLYPH_RE = re.compile(r"\bgfedcb?\s*$")

# The bracketed [TYPE] asset-class tag that marks an A/B asset line.
_FD_TYPE_TAG_RE = re.compile(r"\[([A-Za-z0-9]+)\]")

# An owner column token (SP/DC/JT) appearing right after the [TYPE] tag.
_FD_OWNER_AFTER_TYPE_RE = re.compile(r"\][\s]*(SP|DC|JT)\b")

# An amount-range bucket; FD ranges wrap across lines, so we match all occurrences
# in the assembled (de-wrapped) item text. ``Over $X`` / open-ended values do not
# match and correctly leave the structured field None (raw_text still carries it).
_FD_AMOUNT_RE = re.compile(r"\$[\d,]+\s*-\s*\$[\d,]+")

# Per-page repeated column-header furniture for each schedule (skipped, never
# folded into an item). Matched loosely on its leading words.
_FD_FURNITURE_RE = re.compile(
    r"^(asset owner|asset \[|owner creditor|Source type|Position name|"
    r"Date Parties|type\(s\)|gains >|\$1,000\?|\$200\?|filing|current Year|"
    r"to Preceding|liability|\* For the complete|\* Asset class|name of organization)",
    re.IGNORECASE,
)

# Detail lines that belong to (not start) an A item. The labels are small-caps
# form furniture, so the glyphs-lost rendering turns them into ``L\x00{7}:`` /
# ``D\x00{10}:`` — matched by the NUL branches (the *values* after the colon are
# filer content in a regular font and always survive intact).
_FD_LOCATION_LABEL = r"L(?:OCATION|\x00+):"
_FD_DESCRIPTION_LABEL = r"D(?:ESCRIPTION|\x00+):"
_FD_DETAIL_RE = re.compile(rf"^({_FD_LOCATION_LABEL}|{_FD_DESCRIPTION_LABEL})")


def _fd_amount_range(text: str) -> AmountRange | None:
    """First ``$lo - $hi`` bucket in ``text`` → AmountRange, else ``None``.

    Open-ended values (``Over $1,000,000``, ``Undetermined``, ``None``, ``N/A``)
    do not match and correctly yield ``None`` — the verbatim ``raw_text`` on the
    item still carries the original wording, so nothing is lost.
    """
    m = _FD_AMOUNT_RE.search(text)
    return _bucket(m.group(0)) if m else None


def _bucket(matched: str) -> AmountRange:
    """A matched ``$lo-$hi`` glob → AmountRange, normalizing the dash spacing."""
    return _parse_amount_range(re.sub(r"\s*-\s*", " - ", matched.strip()))


def _segment_schedules(lines: list[str]) -> dict[str, list[str]]:
    """Segment FD body ``lines`` into ``{LETTER: [content lines]}`` (SPEC §2.2).

    Splits on each ``S…edule <LETTER>:`` heading; a schedule whose only content is
    ``None disclosed.`` is **omitted** (absent, not empty). The trailing
    exclusions/certification block ends the final schedule. Furniture (repeated
    per-page column headers) is dropped here so the column parsers see only rows.
    """
    schedules: dict[str, list[str]] = {}
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is None:
            return
        content = [ln for ln in buf if ln.strip()]
        # An empty schedule (``None disclosed.``) is absent, never an empty list.
        meaningful = [
            ln
            for ln in content
            if not _NONE_DISCLOSED_RE.match(ln.strip())
            and not _FD_FURNITURE_RE.match(ln.strip())
        ]
        if meaningful:
            schedules[current] = meaningful

    for ln in lines:
        s = ln.strip()
        heading = _FD_HEADING_RE.match(s)
        if heading:
            flush()
            current = heading.group(1).upper()
            buf = []
            continue
        if current is not None and _FD_TRAILER_RE.match(s):
            flush()
            current = None
            buf = []
            continue
        if current is not None:
            buf.append(ln)
    flush()
    return schedules


def _scrub_raw_text(s: str) -> str:
    """Scrub the small-caps NUL furniture out of a line item's ``raw_text``.

    In the glyphs-lost rendering (SPEC §2.2) the form's small-caps furniture
    extracts as runs of ``U+0000`` folded into the row text — invisible in most
    viewers but literal NUL bytes in the JSON. Replace each NUL run with a single
    space, collapse the resulting runs of whitespace to one space, and strip the
    ends; every other character is left verbatim, so the asset names, amounts,
    dates and ``None disclosed.`` content survive intact (NULs only ever occur in
    the furniture, never in filer content).

    This is a strict no-op on any NUL-free string — collapsing already-single
    whitespace and stripping a ``_group_items``-joined row (its parts joined by a
    single space, ends already trimmed) leaves it byte-identical — so every
    intact-rendering body (all of 2020) extracts exactly as before. The trade is
    deliberately, mildly lossy: the exact furniture rendering is dropped.
    """
    return re.sub(r"\s+", " ", s.replace("\x00", " ")).strip()


def _scrub_field(s: str | None) -> str | None:
    """NUL-gated scrub for a *structured* string field sliced from the raw blob.

    The structured columns (``asset``/``location``/``description``/``income_type``/
    Schedule-C ``source``) are sliced out of the un-scrubbed ``raw`` blob, so in the
    glyphs-lost rendering they can carry literal ``\\x00`` furniture folded into the
    text. Run ``_scrub_raw_text`` only when the value actually contains a NUL — a
    blanket ``\\s+``-collapse is unsafe because legitimate filer values (notably
    ``income_type``) carry meaningful double spaces that must stay byte-identical.
    The gate makes this a strict no-op on every NUL-free value.
    """
    return _scrub_raw_text(s) if s and "\x00" in s else s


def _group_items(lines: list[str], *, starts_item) -> list[str]:
    """Fold ``lines`` into per-item verbatim ``raw_text`` blocks.

    A new item begins at each line for which ``starts_item(line)`` is true; every
    following line (wrapped column, ``LOCATION:``/``DESCRIPTION:`` detail, wrapped
    amount) folds into the current item until the next start. Returns one joined
    ``raw_text`` string per item, in document order.

    Any lines *before* the first item-start anchor are not dropped — a row whose
    anchor was lost (a Schedule D liability with a blank ``Date incurred``, an A/B
    row whose glyph rendered off, a signature-split row) would otherwise vanish
    with no ``raw_text`` and no manifest entry, violating CLAUDE.md's "never
    silently drop / verbatim raw_text on every line item". They are folded into a
    single leading raw item so their text survives verbatim.
    """
    items: list[str] = []
    cur: list[str] = []
    pre: list[str] = []  # lines seen before the first item-start anchor
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if starts_item(s):
            if cur:
                items.append(" ".join(cur))
            cur = [s]
        elif cur:
            cur.append(s)
        else:
            pre.append(s)  # pre-anchor line — preserved, never dropped
    if cur:
        items.append(" ".join(cur))
    # Emit the salvaged pre-anchor text as a leading raw item so nothing is lost.
    if pre:
        items.insert(0, " ".join(pre))
    return items


# A dangling value-range low bound (``$100,001 - Interest``) whose high bound did
# not follow on the same span — the income column (a word, e.g. ``Interest`` /
# ``Rent``) intruded right after the dash and the value's high bound wrapped to the
# line end (SPEC §2.2 column interleave). Requiring a *letter* after the dash (not
# merely "not a $") is what distinguishes a real interleave from an ordinary
# complete ``$lo - $hi`` bucket: the latter's dash is followed by whitespace then
# ``$``, never a word, so it must not be mistaken for a dangling low.
_FD_DANGLING_LOW_RE = re.compile(r"\$([\d,]+)\s+-\s+(?=[A-Za-z])")
# A standalone amount at the very end of the (de-wrapped) row — the wrapped high
# bound when the value range was split by the income column.
_FD_TRAILING_AMOUNT_RE = re.compile(r"\$([\d,]+)\s*$")


def _income_type_between(raw: str, start: int, end: int) -> str | None:
    """The income-category word(s) sitting between offsets ``start`` and ``end``.

    Schedule A's income *type* (``Rent`` / ``Dividends`` / ``Interest`` / …) prints
    between the value range and the income range. We take the text in that gap,
    strip the trailing ``$`` of a dangling low and any glyph remnant, and return the
    remaining words (or ``None`` if the gap is empty). Verbatim ``raw_text`` still
    carries the row regardless.
    """
    gap = raw[start:end]
    # Drop a leading dangling-low remnant ("- " after the value low) and any glyph.
    gap = re.sub(r"^\s*-\s*", "", gap)
    gap = _FD_GLYPH_RE.sub("", gap)
    gap = re.sub(r"\bgfedcb?\b", "", gap)
    cleaned = gap.strip(" -")
    return cleaned or None


def _schedule_a_amounts(
    raw: str,
) -> tuple[AmountRange | None, str | None, AmountRange | None]:
    """Untangle Schedule A's ``value_of_asset``, ``income_type``, ``income_amount``.

    Normal case: the two complete ``$lo - $hi`` buckets in order, with the income
    *type* word(s) (``Rent``/``Dividends``/…) sitting between them. Interleave case
    (SPEC §2.2): the value range's low bound is dangling (``$100,001 -`` with the
    income column right after) and its high bound wrapped to the row's end
    (``… gfedc $250,000``); we pair them, and the income type sits between the
    dangling ``-`` and the income bucket. A failure to untangle leaves the field
    ``None`` — the verbatim ``raw_text`` still carries the row in full.
    """
    dangling = _FD_DANGLING_LOW_RE.search(raw)
    if dangling:
        # Interleave case: the value low bound dangled. Its high bound is the
        # trailing wrapped amount if one materialized — otherwise the wrap is
        # unresolved and value stays None rather than mis-assigning the income
        # bucket to it (#12's "degrade to None rather than a wrong value"). The
        # income range and its type come from the first complete bucket after the
        # dangling ``-``, with the gap before that bucket holding the type word.
        trailing = _FD_TRAILING_AMOUNT_RE.search(raw)
        value = (
            _parse_amount_range(f"${dangling.group(1)} - ${trailing.group(1)}")
            if trailing
            else None
        )
        income_match = _FD_AMOUNT_RE.search(raw, dangling.end())
        if income_match:
            income = _bucket(income_match.group(0))
            income_type = _income_type_between(
                raw, dangling.end(), income_match.start()
            )
            return value, income_type, income
        if value is not None:
            return value, None, None
        # value None and no bucket after the dangling — fall through to the
        # generic two-bucket scan over the whole row (unchanged from #12).
    matches = list(_FD_AMOUNT_RE.finditer(raw))
    value = _bucket(matches[0].group(0)) if matches else None
    income = _bucket(matches[1].group(0)) if len(matches) > 1 else None
    income_type = (
        _income_type_between(raw, matches[0].end(), matches[1].start())
        if len(matches) > 1
        else None
    )
    return value, income_type, income


# A glyphs-lost A-row's value-column signature: right after the ``[TYPE]`` tag
# (and the optional owner token) the value column begins — a ``$lo -`` range
# start (the dash is load-bearing), the open-ended ``Over $X`` bucket, an exact
# dollar value (``$96,550.00`` — note the required ``.dd`` cents), or the literal
# ``None``/``Undetermined``.
# A wrapped subholding tail (``Cash [BA] $5,000,000`` — the row's wrapped value
# *high* bound) carries a bare amount with **no dash, no cents, no ``Over``**, so
# it does not match and stays a continuation, exactly as the glyph anchor would
# have treated it (verified collision-free against continuation lines).
_FD_A_ROW_AFTER_TYPE_RE = re.compile(
    r"\]\s*(?:SP|DC|JT)?\s*(?:\$[\d,]+\s*-|Over\b|\$[\d,]+\.\d{2}|None\b|Undetermined\b)"
)


def _parse_schedule_a(lines: list[str], *, glyphless: bool) -> list[ScheduleAItem]:
    """Schedule A (assets & "unearned" income) → structured items + raw_text.

    ``glyphless`` marks the glyphs-lost rendering (SPEC §2.2 NUL form), in which
    the trailing tx-over-$1,000 checkbox glyph (``gfedc``/``gfedcb``) is not in
    the text layer at all — so the intact-form row anchor (``[TYPE]`` + glyph)
    can never fire and every row would merge into one salvaged blob. For those
    documents only, a row anchors on its own column signature instead; intact
    documents keep the proven glyph anchor byte-for-byte.
    """

    def starts(s: str) -> bool:
        # An asset row carries a [TYPE] tag and ends in the tx-over-$1,000 glyph;
        # LOCATION:/DESCRIPTION: detail and bare amount-wrap lines do not.
        if _FD_DETAIL_RE.match(s):
            return False
        if _FD_TYPE_TAG_RE.search(s) and _FD_GLYPH_RE.search(s):
            return True
        if not glyphless:
            return False
        # Glyphs-lost rendering: anchor on the row's column signature — the
        # [TYPE] tag followed by the value column ($lo - / None / Undetermined),
        # or a subholding row (owner arrow ⇒) whose [TYPE]-tagged subholding
        # name wrapped onto the next line.
        if _FD_TYPE_TAG_RE.search(s):
            return bool(_FD_A_ROW_AFTER_TYPE_RE.search(s))
        return "⇒" in s

    items: list[ScheduleAItem] = []
    for raw in _group_items(lines, starts_item=starts):
        location = description = None
        loc_m = re.search(
            rf"{_FD_LOCATION_LABEL}\s*(.*?)(?:\s+{_FD_DESCRIPTION_LABEL}|$)", raw
        )
        if loc_m:
            location = _scrub_field(loc_m.group(1).strip()) or None
        desc_m = re.search(rf"{_FD_DESCRIPTION_LABEL}\s*(.*)$", raw)
        if desc_m:
            description = _scrub_field(desc_m.group(1).strip()) or None
        # The asset name is everything up to the [TYPE] tag.
        type_m = _FD_TYPE_TAG_RE.search(raw)
        asset_type = type_m.group(1) if type_m else None
        asset = _scrub_field(raw[: type_m.start()].strip() if type_m else raw.strip())
        owner_m = _FD_OWNER_AFTER_TYPE_RE.search(raw)
        owner = owner_m.group(1) if owner_m else None
        # The first amount bucket is the asset value; a second (if any) is income.
        # SPEC §2.2: a value range's high bound can wrap to the line *end*, with
        # the income column interleaved between low and high (``$100,001 -
        # Interest $201 - $1,000 gfedc $250,000``). _schedule_a_amounts untangles
        # that common interleave; otherwise the two complete buckets are taken in
        # order. raw_text always carries the verbatim row regardless.
        value_of_asset, income_type, income_amount = _schedule_a_amounts(raw)
        income_type = _scrub_field(income_type)
        items.append(
            ScheduleAItem(
                asset=asset,
                owner=owner,
                asset_type=asset_type,
                value_of_asset=value_of_asset,
                income_type=income_type,
                income_amount=income_amount,
                location=location,
                description=description,
                raw_text=_scrub_raw_text(raw),
            )
        )
    return items


def _parse_schedule_b(lines: list[str]) -> list[ScheduleBItem]:
    """Schedule B (transactions) → structured items + raw_text.

    A B row carries the asset's ``[TYPE]`` tag, an owner arrow ``⇒``, a date, a
    ``P``/``S`` type and an amount. The asset name often wraps onto the *following*
    line, so item-start anchors on the ``⇒``-bearing line and folds the wrap in.
    """

    def starts(s: str) -> bool:
        return "⇒" in s

    items: list[ScheduleBItem] = []
    for raw in _group_items(lines, starts_item=starts):
        owner_m = re.search(r"⇒\s*(SP|DC|JT)?\b", raw)
        owner = owner_m.group(1) if owner_m and owner_m.group(1) else None
        date_m = re.search(r"(\d{2}/\d{2}/\d{4})", raw)
        transaction_date = (
            datetime.strptime(date_m.group(1), "%m/%d/%Y").date() if date_m else None
        )
        # Transaction type: the P/S/E letter sitting after the date, with the
        # ``S (partial)`` marker preserved. The ``\b`` must sit on the *letter*
        # branch only — a trailing ``\b`` after ``S (partial)`` never matches (``)``
        # then space is not a word boundary), which silently collapsed every
        # partial sale to a bare ``S``. Mirror the PTR normalization instead.
        ttype = None
        if date_m:
            tt_m = re.match(r"(S \(partial\)|[PSE]\b)", raw[date_m.end() :].lstrip())
            if tt_m:
                token = tt_m.group(1)
                ttype = "S(partial)" if "(partial)" in token else token
        type_m = _FD_TYPE_TAG_RE.search(raw)
        asset_type = type_m.group(1) if type_m else None
        # Asset name = everything before the ⇒ arrow, plus any wrapped tail with
        # the [TYPE] tag; keep it simple — the text up to ⇒ is the primary name.
        asset = _scrub_field(raw.split("⇒", 1)[0].strip())
        glyph_m = re.search(r"\bgfedcb?\b", raw)
        cap_gains = (glyph_m.group(0) == "gfedcb") if glyph_m else None
        items.append(
            ScheduleBItem(
                asset=asset,
                owner=owner,
                asset_type=asset_type,
                transaction_date=transaction_date,
                transaction_type=ttype,
                amount_range=_fd_amount_range(raw),
                cap_gains_over_200=cap_gains,
                raw_text=_scrub_raw_text(raw),
            )
        )
    return items


def _parse_schedule_c(lines: list[str]) -> list[ScheduleCItem]:
    """Schedule C (earned income) → structured items + raw_text.

    Columns are ``Source | Type | Amount [| Preceding-year amount]``. Each row is
    one physical line; the trailing money/``N/A`` token(s) are the amount, the
    word before them the income type, the remainder the source.
    """
    items: list[ScheduleCItem] = []
    for raw in _group_items(lines, starts_item=lambda s: True):
        # Trailing amount(s): one or two money / N/A tokens at the line's end.
        amt_m = re.search(
            r"((?:\$[\d,]+(?:\.\d+)?|N/A)(?:\s+(?:\$[\d,]+(?:\.\d+)?|N/A))?)\s*$",
            raw,
        )
        amount = amt_m.group(1).strip() if amt_m else None
        head = raw[: amt_m.start()].strip() if amt_m else raw.strip()
        # The income type is the last whitespace-token of the head (Salary,
        # Pension, Annuity, …); the rest is the source.
        parts = head.rsplit(" ", 1)
        if len(parts) == 2 and parts[1]:
            source, income_type = parts[0].strip(), parts[1].strip()
        else:
            source, income_type = head, None
        items.append(
            ScheduleCItem(
                source=_scrub_field(source) or _scrub_field(raw),
                income_type=_scrub_field(income_type),
                amount=amount,
                raw_text=_scrub_raw_text(raw),
            )
        )
    return items


# A Schedule D ``Date incurred`` — ``Month YYYY``, ``MM/DD/YYYY``, or ``MM/YYYY``
# (longer date forms first so the most specific match wins). Used both to anchor a
# liability row's item-start and to extract the date, so the two always agree.
_FD_DATE_RE = re.compile(
    r"\b("
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}/\d{4})\b"
)


def _parse_schedule_d(lines: list[str]) -> list[ScheduleDItem]:
    """Schedule D (liabilities) → structured items + raw_text.

    Columns are ``Owner | Creditor | Date incurred | Type | Amount``. The amount
    range wraps onto the next line, so item-start anchors on the row bearing a
    month/year ``Date incurred`` and folds the wrapped amount in.
    """
    items: list[ScheduleDItem] = []
    for raw in _group_items(lines, starts_item=lambda s: bool(_FD_DATE_RE.search(s))):
        owner_m = re.match(r"(SP|DC|JT)\b", raw)
        owner = owner_m.group(1) if owner_m else None
        date_m = _FD_DATE_RE.search(raw)
        date_incurred = date_m.group(1) if date_m else None
        # Creditor = text between any owner token and the date.
        start = owner_m.end() if owner_m else 0
        end = date_m.start() if date_m else len(raw)
        creditor = _scrub_field(raw[start:end].strip())
        # Liability type = text between the date and the amount.
        amt_m = _FD_AMOUNT_RE.search(raw)
        ltype = None
        if date_m:
            type_end = amt_m.start() if amt_m else len(raw)
            ltype = _scrub_field(raw[date_m.end() : type_end].strip() or None)
        items.append(
            ScheduleDItem(
                creditor=creditor or _scrub_field(raw),
                owner=owner,
                date_incurred=date_incurred,
                liability_type=ltype,
                amount_range=_fd_amount_range(raw),
                raw_text=_scrub_raw_text(raw),
            )
        )
    return items


# Schedules E–J: each item is one row, raw_text-only (depth-ordering, SPEC §6.3).
def _parse_raw_schedule(lines: list[str]) -> list[RawLineItem]:
    """Schedules E–J → raw_text-only line items (one per physical row)."""
    return [
        RawLineItem(raw_text=_scrub_raw_text(ln)) for ln in lines if ln.strip()
    ]


# Schedule A dispatches separately in :func:`extract_fd_schedules` because it
# threads the document-level ``glyphless`` flag (the NUL-rendering row anchor).
_FD_STRUCTURED = {
    "B": _parse_schedule_b,
    "C": _parse_schedule_c,
    "D": _parse_schedule_d,
}


def extract_fd_schedules(pdf_path: Path) -> FdBody:
    """Extract an e-filed annual-FD's §6.3 schedule body from its PDF. Offline.

    Segments the body by schedule **letter** (SPEC §2.2 small-caps caveat),
    column-parses A–D, ships E–J as ``raw_text``-only items, and records a
    ``None disclosed.`` schedule as **absent**. Every line item carries verbatim
    ``raw_text``. Raises :class:`PdfExtractError` on a present-but-unreadable PDF;
    raises :class:`NotAnFdBody` when the PDF carries no schedule headings at all
    (an extension / cover sheet that is not an annual FD body) — the ``parse``
    caller writes no body for that case rather than a misleading empty one.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            lines: list[str] = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                lines.extend(text.splitlines())
    except Exception as exc:  # noqa: BLE001 — any pdfplumber/pdfminer failure
        raise PdfExtractError(
            f"could not extract FD body from {pdf_path}: {exc}"
        ) from exc

    if not any(_FD_HEADING_RE.match(ln.strip()) for ln in lines):
        raise NotAnFdBody(
            f"no FD schedule headings found in {pdf_path}: not an annual-FD body "
            "(likely an extension/cover sheet)"
        )

    # The glyphs-lost rendering (SPEC §2.2): small-caps furniture extracted as
    # NUL runs. NULs never occur in an intact-rendering body, so this flag is a
    # precise document-level marker — intact documents take exactly the same
    # code paths as before.
    glyphless = any("\x00" in ln for ln in lines)

    segments = _segment_schedules(lines)
    schedules: dict[str, list] = {}
    for letter in sorted(segments):
        content = segments[letter]
        if letter == "A":
            items = _parse_schedule_a(content, glyphless=glyphless)
        elif parser := _FD_STRUCTURED.get(letter):
            items = parser(content)
        else:
            items = _parse_raw_schedule(content)
        # An item-less segment (parser found no rows it could anchor) still carries
        # its lines as raw_text rather than vanishing — never silently drop.
        if not items:
            items = _parse_raw_schedule(content)
        schedules[letter] = [it.model_dump(mode="json") for it in items]

    return FdBody(schedules=schedules)
