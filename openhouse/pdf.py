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
_PTR_ROW_RE = re.compile(
    r"^(?:(SP|DC|JT)\s+)?"  # owner column (blank → self)
    r"(.+?)\s+"  # asset blob (non-greedy up to the type)
    r"(S \(partial\)|[PSE])\s+"  # transaction type
    r"(\d{2}/\d{2}/\d{4})\s+"  # transaction date
    r"(\d{2}/\d{2}/\d{4})\s+"  # notification date
    r"(\$[\d,]+ - \$[\d,]+)\s+"  # amount range label
    r"(gfedcb?)\s*$"  # cap-gains glyph
)

# Detail/section lines that END a row's asset-name wrap.
_PTR_DETAIL_RE = re.compile(r"^(FILINg STATUS:|SUBHOLDINg OF:|DESCRIPTION:)")

# Per-page table furniture pdfplumber repeats at the top of every page. When a
# row's asset name wraps across a page break, this furniture lands BETWEEN the
# header line and the wrapped continuation, so it must be skipped WITHOUT ending
# the wrap — otherwise the "(TICKER) [TYPE]" continuation is silently dropped
# (null ticker and asset_type, truncated asset, invisible to the §-residual).
_PTR_FURNITURE_RE = re.compile(r"^(ID Owner Asset|Type Date|Gains >|\$200\?)")

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
            elif _PTR_FURNITURE_RE.match(nxt):
                pass  # repeated per-page table header (page break) — skip, don't
                # end the wrap and don't fold it into the asset name
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

    # Guard against a silently partial extraction (CLAUDE.md "never silently drop
    # a filing"): every transaction has exactly one "FILINg STATUS:" line, so a
    # count mismatch means a header row failed the one-line signature and was
    # skipped (or the body came back empty). Surface it loudly as extract_failed
    # rather than writing a too-short {"transactions": [...]} with status "ok".
    status_blocks = sum(
        1 for ln in lines if ln.strip().startswith("FILINg STATUS:")
    )
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
# small-cap glyphs are lost and render inconsistently (``ScHeDule`` / ``SCheDule``
# / ``ScheDule``); only the ``S…edule <LETTER>:`` shape is stable (SPEC §2.2).
# An empty schedule prints the literal ``None disclosed.`` → recorded as **absent**
# (its letter is simply omitted from the body). A–D are column-parsed; E–J ship as
# raw_text-only line items; every line item carries verbatim ``raw_text``.
# ---------------------------------------------------------------------------

# A schedule heading: ``S…edule <LETTER>:`` with the small-cap glyphs lost. The
# letter is captured (upper- or lower-case both occur) and the rest of the
# heading text is deliberately ignored. Anchored at line start.
_FD_HEADING_RE = re.compile(r"^Sc?h?e?d?ule\s+([A-Ja-j]):", re.IGNORECASE)

# The literal an empty schedule renders → schedule absent (SPEC §2.2).
_NONE_DISCLOSED_RE = re.compile(r"^None disclosed\.?\s*$", re.IGNORECASE)

# The trailing non-schedule sections that follow Schedule J (exclusions checkboxes
# + the certification/signature block). They end the last schedule's content.
_FD_TRAILER_RE = re.compile(
    r"^(exclusions of|certification and|digitally signed)", re.IGNORECASE
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

# Detail lines that belong to (not start) an A item.
_FD_DETAIL_RE = re.compile(r"^(LOCATION:|DESCRIPTION:)")


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


def _group_items(lines: list[str], *, starts_item) -> list[str]:
    """Fold ``lines`` into per-item verbatim ``raw_text`` blocks.

    A new item begins at each line for which ``starts_item(line)`` is true; every
    following line (wrapped column, ``LOCATION:``/``DESCRIPTION:`` detail, wrapped
    amount) folds into the current item until the next start. Returns one joined
    ``raw_text`` string per item, in document order.
    """
    items: list[str] = []
    cur: list[str] = []
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
        # A line before any item-start (stray header remnant) is dropped.
    if cur:
        items.append(" ".join(cur))
    return items


# A dangling value-range low bound (``$100,001 -``) whose high bound did not
# follow on the same span — the income column intruded and the high bound wrapped
# to the line end (SPEC §2.2 column interleave).
_FD_DANGLING_LOW_RE = re.compile(r"\$([\d,]+)\s*-\s*(?!\$)")
# A standalone amount at the very end of the (de-wrapped) row — the wrapped high
# bound when the value range was split by the income column.
_FD_TRAILING_AMOUNT_RE = re.compile(r"\$([\d,]+)\s*$")


def _schedule_a_amounts(raw: str) -> tuple[AmountRange | None, AmountRange | None]:
    """Untangle Schedule A's ``value_of_asset`` and ``income_amount`` from ``raw``.

    Normal case: the two complete ``$lo - $hi`` buckets in order. Interleave case
    (SPEC §2.2): the value range's low bound is dangling (``$100,001 -`` with the
    income column right after) and its high bound wrapped to the row's end
    (``… gfedc $250,000``); we pair them. A failure to untangle leaves the field
    ``None`` — the verbatim ``raw_text`` still carries the row in full.
    """
    dangling = _FD_DANGLING_LOW_RE.search(raw)
    trailing = _FD_TRAILING_AMOUNT_RE.search(raw)
    if dangling and trailing:
        value = _parse_amount_range(f"${dangling.group(1)} - ${trailing.group(1)}")
        # Income is the first *complete* bucket sitting between the two halves.
        income_match = _FD_AMOUNT_RE.search(raw, dangling.end())
        income = _bucket(income_match.group(0)) if income_match else None
        return value, income
    amounts = [_bucket(m.group(0)) for m in _FD_AMOUNT_RE.finditer(raw)]
    value = amounts[0] if amounts else None
    income = amounts[1] if len(amounts) > 1 else None
    return value, income


def _parse_schedule_a(lines: list[str]) -> list[ScheduleAItem]:
    """Schedule A (assets & "unearned" income) → structured items + raw_text."""

    def starts(s: str) -> bool:
        # An asset row carries a [TYPE] tag and ends in the tx-over-$1,000 glyph;
        # LOCATION:/DESCRIPTION: detail and bare amount-wrap lines do not.
        return bool(
            _FD_TYPE_TAG_RE.search(s)
            and _FD_GLYPH_RE.search(s)
            and not _FD_DETAIL_RE.match(s)
        )

    items: list[ScheduleAItem] = []
    for raw in _group_items(lines, starts_item=starts):
        location = description = None
        loc_m = re.search(r"LOCATION:\s*(.*?)(?:\s+DESCRIPTION:|$)", raw)
        if loc_m:
            location = loc_m.group(1).strip() or None
        desc_m = re.search(r"DESCRIPTION:\s*(.*)$", raw)
        if desc_m:
            description = desc_m.group(1).strip() or None
        # The asset name is everything up to the [TYPE] tag.
        type_m = _FD_TYPE_TAG_RE.search(raw)
        asset_type = type_m.group(1) if type_m else None
        asset = raw[: type_m.start()].strip() if type_m else raw.strip()
        owner_m = _FD_OWNER_AFTER_TYPE_RE.search(raw)
        owner = owner_m.group(1) if owner_m else None
        # The first amount bucket is the asset value; a second (if any) is income.
        # SPEC §2.2: a value range's high bound can wrap to the line *end*, with
        # the income column interleaved between low and high (``$100,001 -
        # Interest $201 - $1,000 gfedc $250,000``). _schedule_a_amounts untangles
        # that common interleave; otherwise the two complete buckets are taken in
        # order. raw_text always carries the verbatim row regardless.
        value_of_asset, income_amount = _schedule_a_amounts(raw)
        items.append(
            ScheduleAItem(
                asset=asset,
                owner=owner,
                asset_type=asset_type,
                value_of_asset=value_of_asset,
                income_amount=income_amount,
                location=location,
                description=description,
                raw_text=raw,
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
        # Transaction type: the P/S/E letter sitting after the date.
        ttype = None
        if date_m:
            tt_m = re.match(r"(S \(partial\)|[PSE])\b", raw[date_m.end() :].lstrip())
            if tt_m:
                ttype = "S(partial)" if tt_m.group(1) == "S (partial)" else tt_m.group(1)
        type_m = _FD_TYPE_TAG_RE.search(raw)
        asset_type = type_m.group(1) if type_m else None
        # Asset name = everything before the ⇒ arrow, plus any wrapped tail with
        # the [TYPE] tag; keep it simple — the text up to ⇒ is the primary name.
        asset = raw.split("⇒", 1)[0].strip()
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
                raw_text=raw,
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
                source=source or raw,
                income_type=income_type,
                amount=amount,
                raw_text=raw,
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
        creditor = raw[start:end].strip()
        # Liability type = text between the date and the amount.
        amt_m = _FD_AMOUNT_RE.search(raw)
        ltype = None
        if date_m:
            type_end = amt_m.start() if amt_m else len(raw)
            ltype = raw[date_m.end() : type_end].strip() or None
        items.append(
            ScheduleDItem(
                creditor=creditor or raw,
                owner=owner,
                date_incurred=date_incurred,
                liability_type=ltype,
                amount_range=_fd_amount_range(raw),
                raw_text=raw,
            )
        )
    return items


# Schedules E–J: each item is one row, raw_text-only (depth-ordering, SPEC §6.3).
def _parse_raw_schedule(lines: list[str]) -> list[RawLineItem]:
    """Schedules E–J → raw_text-only line items (one per physical row)."""
    return [
        RawLineItem(raw_text=ln.strip()) for ln in lines if ln.strip()
    ]


_FD_STRUCTURED = {
    "A": _parse_schedule_a,
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

    segments = _segment_schedules(lines)
    schedules: dict[str, list] = {}
    for letter in sorted(segments):
        content = segments[letter]
        parser = _FD_STRUCTURED.get(letter)
        items = parser(content) if parser else _parse_raw_schedule(content)
        # An item-less segment (parser found no rows it could anchor) still carries
        # its lines as raw_text rather than vanishing — never silently drop.
        if not items:
            items = _parse_raw_schedule(content)
        schedules[letter] = [it.model_dump(mode="json") for it in items]

    return FdBody(schedules=schedules)
