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

import itertools
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pdfplumber

from .schemas import (
    AmountRange,
    FdBody,
    PtrTransaction,
    ScheduleAItem,
    ScheduleBItem,
    ScheduleCItem,
    ScheduleDItem,
    ScheduleEItem,
    ScheduleFItem,
    ScheduleGItem,
    ScheduleHItem,
    ScheduleIItem,
    ScheduleJItem,
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

# Lower bound of the parse-time date sanity range (GH-0113). The STOCK Act
# (which created the PTR) and the modern e-filed FD predate nothing earlier than
# this; a disclosure date with a year below 1990 is an extraction artifact, not a
# real trade. The *upper* bound is data, not wall-clock — ``entry_year + 1``,
# derived from the single command-entry timestamp threaded into ``parse`` (SPEC
# §9 / CLAUDE.md: no ``date.today()`` in core logic).
MIN_DISCLOSURE_YEAR = 1990

# Static fallback upper bound, used ONLY when a caller does not thread the real
# command-entry year down (direct API/test use, and the date-agnostic
# ``pull.doc_ids_for_member`` path). It is a fixed constant, never a wall-clock
# read — the production ``parse`` path always passes the real ``entry_year + 1``
# (SPEC §9 / CLAUDE.md: no ``date.today()`` in core logic). Generous so it never
# rejects a real near-future date, while still catching transposed-digit years.
FALLBACK_MAX_YEAR = 2100


def parse_disclosure_date(raw: str, *, max_year: int) -> Optional[date]:
    """Parse a ``M/D/YYYY`` disclosure date, or ``None`` if implausible (GH-0113).

    Returns the :class:`date` only when it both parses and its year falls in the
    sanity range ``MIN_DISCLOSURE_YEAR ≤ year ≤ max_year`` (``max_year`` is the
    entry year + 1, threaded down from the command-entry timestamp — never the
    wall clock). A transposed-digit year (``3031``, ``2202``) parses as readily as
    ``2024`` via ``strptime`` but is rejected here as the extraction artifact it
    is. ``None`` on either an unparseable string or an out-of-range year; the
    caller preserves the raw string and flags the anomaly (never a silent valid
    date — CLAUDE.md).
    """
    try:
        parsed = datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError:
        return None
    if MIN_DISCLOSURE_YEAR <= parsed.year <= max_year:
        return parsed
    return None


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
# The five leading groups every row form shares: owner, asset blob, type, and
# the date pair. ``_match_ptr_header`` unpacks all four row regexes positionally,
# so the group count and order MUST stay in lockstep across them — composing
# from this one fragment (the ``_PTR_*_LABEL`` idiom below) makes that
# structural, not a convention.
_PTR_ROW_PREFIX = (
    r"^(?:(SP|DC|JT)\s+)?"  # owner column (blank → self)
    r"(.+?)\s+"  # asset blob (non-greedy up to the type)
    r"([Ss] \(partial\)|[PpSsEe])\s+"  # transaction type (only S can be partial)
    r"(\d{2}/\d{2}/\d{4})\s+"  # transaction date
    r"(\d{2}/\d{2}/\d{4})\s+"  # notification date
)
# The exact-dollar amount token shared by the two exact-row forms (GH-0049):
# one bare ``$N[,NNN][.NN]`` money value, no dash. Whole-dollar values are
# accepted; cents are not required.
_PTR_EXACT_AMOUNT = r"(\$[\d,]+(?:\.\d{2})?)"
_PTR_ROW_RE = re.compile(
    _PTR_ROW_PREFIX
    + r"(\$[\d,]+)\s+-\s+(\$[\d,]+)?\s*"  # amount range: low, then optional high
    + r"(gfedcb?)\s*$"  # cap-gains glyph
)
# Exact-dollar amount variant (GH-0049). A minority of real PTR rows disclose a
# single **exact** dollar value (e.g. ``$894.97``) in the amount column instead of
# a ``$LOW - $HIGH`` bucket. Same row signature as ``_PTR_ROW_RE`` up to the
# amount, but the amount column is one bare ``$N[,NNN][.NN]`` money token with no
# dash and no leading word. Keeping the leading ``$`` and trailing-glyph anchors
# (and forbidding a dash) is what keeps this SOUND — it will not match a one-sided
# ``Over $1,000,000`` (no ``$`` immediately before the glyph after a word) or a
# half-range, both of which must stay ``extract_failed`` (CLAUDE.md: never
# fabricate a range). Whole-dollar exact values (``$500``) are accepted too;
# cents are not required.
_PTR_EXACT_ROW_RE = re.compile(
    _PTR_ROW_PREFIX
    + _PTR_EXACT_AMOUNT
    + r"\s+(gfedcb?)\s*$"  # exact value, then the cap-gains glyph
)
# Glyphs-lost rendering row variants (SPEC §2.2 NUL form; PTRs cut over around
# 2022-04). In those documents the cap-gains checkbox glyph (``gfedc``/
# ``gfedcb``) is not in the text layer AT ALL, so the trailing-glyph anchor
# above can never fire and every row would be silently skipped. The remaining
# signature — optional owner, type letter, date pair, amount column anchored at
# end of line — is still unambiguous (the ``\s+`` adjacency between the
# notification date and the leading ``$`` keeps a one-sided ``Over $1,000,000``
# from matching, exactly as the glyph anchor did). The checkbox *state* is
# unrecoverable from the text layer: those rows carry ``cap_gains_over_200:
# None`` ("unknown"), never a fabricated boolean. These variants are consulted
# only for documents detected as glyphless (NULs in the furniture), so intact
# documents take exactly the same code paths as before. The high bound is
# optional for the same column-wrap reason as ``_PTR_ROW_RE`` — a wrapped
# header now ends ``$LOW -`` bare (no glyph after the dash).
_PTR_ROW_GLYPHLESS_RE = re.compile(
    _PTR_ROW_PREFIX
    + r"(\$[\d,]+)\s+-(?:\s+(\$[\d,]+))?\s*$"  # amount range: low, optional high
)
_PTR_EXACT_ROW_GLYPHLESS_RE = re.compile(_PTR_ROW_PREFIX + _PTR_EXACT_AMOUNT + r"\s*$")
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
#
# Each label also carries a NUL branch (the ``\x00+`` alternations) for the
# glyphs-lost rendering (SPEC §2.2 NUL form, same as ``_FD_DETAIL_RE``): the
# small-caps labels extract as NUL runs, one per lost glyph — ``FILING STATUS:``
# becomes ``F\x00{5} S\x00{5}:``, ``SUBHOLDING OF:`` becomes ``S\x00{9} O\x00:``,
# ``DESCRIPTION:`` becomes ``D\x00{10}:``. NULs never occur in filer-entered
# content (the verified §2.2 invariant), so the NUL branches are collision-proof.
_PTR_STATUS_LABEL = r"F(?:ILING|\x00+) S(?:TATUS|\x00+):"
_PTR_SUBHOLDING_LABEL = r"S(?:UBHOLDING|\x00+) O(?:F|\x00+):"
_PTR_DESCRIPTION_LABEL = r"D(?:ESCRIPTION|\x00+):"
_PTR_DETAIL_RE = re.compile(
    rf"^({_PTR_STATUS_LABEL}|{_PTR_SUBHOLDING_LABEL}|{_PTR_DESCRIPTION_LABEL})",
    re.IGNORECASE,
)
# The per-row "FILING STATUS:" line, used to count blocks for the completeness
# guard — same case-insensitive + NUL-branch shape as above (just the status
# line). Keeping this NUL-aware is what makes the guard LOUD on a glyphless
# document: a zero-extraction body still counts its real status blocks, so
# 0 != N raises extract_failed instead of returning a silent empty list.
_PTR_STATUS_RE = re.compile(rf"^{_PTR_STATUS_LABEL}", re.IGNORECASE)
# A row's DESCRIPTION: detail line (case-insensitive + NUL branch, as above).
_PTR_DESCRIPTION_RE = re.compile(rf"^{_PTR_DESCRIPTION_LABEL}", re.IGNORECASE)

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

# The TRANSACTIONS table's footnote, printed exactly once, after the last row
# (regular font — identical in both renderings). Everything beyond it is
# document trailer: the ASSET CLASS DETAILS appendix (whose per-asset
# ``LOCATION:``/``DESCRIPTION:`` detail lines would otherwise bleed into the
# *last row's* description), the IPO section, and the certification block.
# Extraction truncates the line list here; every per-row ``FILING STATUS:``
# block counted by the completeness guard lives above the footnote.
_PTR_TABLE_END_RE = re.compile(
    r"^\* For the complete list of asset type abbreviations", re.IGNORECASE
)

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


def _parse_exact_amount(token: str) -> AmountRange:
    """Parse a single exact dollar value (``"$894.97"``) into an exact AmountRange.

    The value lands in ``exact`` (a float — exact figures carry cents); ``low``
    and ``high`` stay ``None``. The verbatim ``$``-prefixed token is preserved as
    ``label``. This is a *point*, deliberately NOT coerced into a ``{low, high}``
    bucket (GH-0049): the on-wire shape stays honest about being an exact value.
    """
    value = float(token.lstrip("$").replace(",", ""))
    return AmountRange(exact=value, label=token)


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


def _asset_type_raw_from_asset(asset: str) -> str | None:
    """The bracketed ``[ST]``-style tag, verbatim (without brackets).

    Casing is **not** touched here — the Clerk's PDFs render the tag with
    inconsistent casing (``ST``/``sT``/``Cs``/``gS`` all occur), and this is the
    raw value preserved beside the normalized one (CLAUDE.md: raw alongside
    normalized). Use :func:`_normalize_asset_type` for the clean, comparable
    value.
    """
    match = _ASSET_TYPE_RE.search(asset)
    return match.group(1).strip() if match else None


def _normalize_asset_type(raw: str | None) -> str | None:
    """Uppercased, trimmed asset-type tag — the convenient default value.

    ``None`` in, ``None`` out (no tag on the row). Defeats the small-caps glyph
    artifact and per-form casing drift so consumers need not defensively
    ``upper()`` the field (GH-0114).
    """
    if raw is None:
        return None
    normalized = raw.strip().upper()
    return normalized or None


def _match_ptr_header(
    line: str, *, glyphless: bool
) -> tuple[bool, tuple[str | None, ...], str | None] | None:
    """Match a PTR row header line → ``(is_exact, fields, glyph)``, else ``None``.

    ``fields`` is ``(owner, asset_head, txn_type, txn_date, notif_date, …amount)``
    — two amount fields (low, optional high) for a range row, one (the exact
    token) for an exact-dollar row. ``glyph`` is the trailing cap-gains glyph, or
    ``None`` in the glyphs-lost rendering where the checkbox is not in the text
    layer at all. The glyph-anchored forms are always tried first (they cannot
    match a glyphless line, nor vice versa — the end-of-line anchors are mutually
    exclusive); the glyphless variants are consulted only when the document was
    detected as glyphless, so intact documents behave exactly as before.
    """
    m = _PTR_ROW_RE.match(line)
    if m:
        *fields, glyph = m.groups()
        return False, tuple(fields), glyph
    m = _PTR_EXACT_ROW_RE.match(line)
    if m:
        *fields, glyph = m.groups()
        return True, tuple(fields), glyph
    if glyphless:
        m = _PTR_ROW_GLYPHLESS_RE.match(line)
        if m:
            return False, m.groups(), None
        m = _PTR_EXACT_ROW_GLYPHLESS_RE.match(line)
        if m:
            return True, m.groups(), None
    return None


def _is_ptr_header(line: str, *, glyphless: bool = False) -> bool:
    """True if ``line`` opens a PTR transaction row — range OR exact-dollar form.

    Used as the row boundary throughout extraction so an exact-dollar row (#49)
    both starts a new row and ends the previous one, exactly like a range row.
    """
    return _match_ptr_header(line, glyphless=glyphless) is not None


def _is_ptr_skippable(stripped: str) -> bool:
    """True for a line that carries no row content — skip without ending a wrap.

    Blank lines, repeated per-page table furniture, a stray glyph remnant, and
    NUL-bearing non-detail lines (the glyphs-lost rendering's form furniture —
    section titles etc.; SPEC §2.2: NULs never occur in filer content). The
    wrap-recovery loop and the GH-0049 exact-row guard MUST share one view of
    "skippable" — the guard exists precisely to mirror the recovery loop's walk,
    so a divergence here silently breaks its soundness claim.
    """
    return bool(
        not stripped
        or _PTR_FURNITURE_RE.match(stripped)
        or _PTR_GLYPH_ONLY_RE.match(stripped)
        or "\x00" in stripped
    )


def _wrapped_range_tail_follows(
    lines: list[str], start: int, n: int, *, glyphless: bool = False
) -> bool:
    """True if the next content line begins with ``-`` (a spilled ``- $HIGH`` tail).

    GH-0049 soundness guard (critic): an exact-dollar match (``$N`` + glyph on the
    header) is only *really* exact if the amount was a lone value. A range whose
    ``- $HIGH`` tail wrapped off the header — leaving ``… $LOW <glyph>`` — looks
    identical to an exact row, and would fabricate a point. Peeking the next
    content line distinguishes them: a leading dash means a wrapped range, not an
    exact value, so the row must fall through to ``extract_failed`` (never
    fabricate). Skips blank / per-page furniture / glyph-only lines like the wrap
    recovery; stops (``False``) at the next header or a detail line. This shape is
    unobserved in 2020–2021 real data (the dash stays on the header there); the
    guard upholds the binding invariant for any future filing that wraps it.
    """
    k = start
    while k < n:
        nxt = lines[k].strip()
        if _is_ptr_header(nxt, glyphless=glyphless) or _PTR_DETAIL_RE.match(nxt):
            return False
        if _is_ptr_skippable(nxt):
            k += 1
            continue
        return nxt.startswith("-")
    return False


def extract_ptr_transactions(
    pdf_path: Path, *, max_year: int = FALLBACK_MAX_YEAR
) -> list[PtrTransaction]:
    """Extract an e-filed PTR's §6.3 ``transactions[]`` from its PDF. Offline.

    Layout-aware (SPEC §2.2): anchors on each row's header line and folds any
    wrapped asset-name continuation back in. Raises :class:`PdfExtractError` on a
    present-but-unreadable PDF so the ``parse`` caller can record an
    ``extract_failed`` outcome rather than crash the run.

    ``max_year`` (the command-entry year + 1) bounds the date sanity range
    (GH-0113): a transaction/notification date with an implausible year is set
    ``None`` with its raw string kept on ``date_raw`` / ``notification_date_raw``,
    never emitted as a valid date.
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

    # The TRANSACTIONS table ends at its footnote; drop the trailer (appendix /
    # IPO / certification) so its detail lines never fold into the last row.
    for idx, ln in enumerate(lines):
        if _PTR_TABLE_END_RE.match(ln.strip()):
            lines = lines[:idx]
            break

    # The glyphs-lost rendering (SPEC §2.2): small-caps furniture extracted as
    # NUL runs and the checkbox glyphs gone from the text layer. NULs never
    # occur in an intact-rendering body, so this flag is a precise document-
    # level marker — intact documents take exactly the same code paths as
    # before (same detection as the FD extractor).
    glyphless = any("\x00" in ln for ln in lines)

    transactions: list[PtrTransaction] = []
    i = 0
    n = len(lines)
    while i < n:
        header = _match_ptr_header(lines[i].strip(), glyphless=glyphless)
        if header is None:
            i += 1
            continue
        is_exact, fields, glyph = header

        if not is_exact:
            owner, asset_head, txn_type, txn_date, notif_date, amount_low, amount_high = (
                fields
            )
            amount: AmountRange | None = None  # filled once the high bound is known
        else:
            # Exact-dollar form (#49): the amount column is a single ``$N`` value,
            # not a ``$LOW - $HIGH`` bucket. No amount-column wrap to recover — the
            # value is complete on the header line — so ``amount`` is set now and
            # ``amount_high`` is sentinel-non-None to skip the wrap-recovery block.
            owner, asset_head, txn_type, txn_date, notif_date, exact_token = fields
            # Soundness guard (critic): refuse the exact reading if a wrapped
            # range tail ("- $HIGH") follows — that's a range that lost its dash to
            # a line wrap, not a lone exact value. Fall through to extract_failed
            # rather than fabricate a point.
            if _wrapped_range_tail_follows(lines, i + 1, n, glyphless=glyphless):
                i += 1
                continue
            amount = _parse_exact_amount(exact_token)
            amount_low = amount_high = exact_token  # not None → skip wrap recovery
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
                if _is_ptr_header(nxt, glyphless=glyphless) or _PTR_DETAIL_RE.match(nxt):
                    break  # reached the next row / this row's detail — high bound
                    # never materialized; leave amount_high None (row drops below).
                if _is_ptr_skippable(nxt):
                    k += 1
                    continue  # page-break furniture / stray glyph / NUL-run form
                    # furniture (never filer content — SPEC §2.2) — skip, keep looking
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
            if _is_ptr_header(nxt, glyphless=glyphless):
                break  # next transaction row — end of this row
            desc_m = _PTR_DESCRIPTION_RE.match(nxt)
            if desc_m:
                description = nxt[desc_m.end() :].strip() or None
                seen_detail = True
            elif _PTR_DETAIL_RE.match(nxt):
                seen_detail = True
            elif _PTR_FURNITURE_RE.match(nxt) or "\x00" in nxt:
                pass  # repeated per-page table header (page break) or NUL-run
                # form furniture (glyphs-lost rendering — never filer content,
                # SPEC §2.2) — skip, don't end the wrap and don't fold it into
                # the asset name
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

        # A range row builds its AmountRange now (once the high bound is known); an
        # exact-dollar row already set ``amount`` at the top of the iteration.
        if amount is None:
            amount = _parse_amount_range(f"{amount_low} - {amount_high}")
        asset = _scrub_field(" ".join(part for part in asset_parts if part))
        asset_type_raw = _asset_type_raw_from_asset(asset)
        # Sanity-range the two dates (GH-0113); a rejected year keeps its raw
        # string on the sibling ``*_raw`` field as the per-row anomaly flag and
        # the structured date stays None — never a fabricated valid date.
        txn_d = parse_disclosure_date(txn_date, max_year=max_year)
        notif_d = parse_disclosure_date(notif_date, max_year=max_year)
        transactions.append(
            PtrTransaction(
                owner=owner or "self",
                asset=asset,
                ticker=_ticker_from_asset(asset),
                asset_type=_normalize_asset_type(asset_type_raw),
                asset_type_raw=asset_type_raw,
                transaction_type=txn_type,
                transaction_date=txn_d,
                date_raw=txn_date if txn_d is None else None,
                notification_date=notif_d,
                notification_date_raw=notif_date if notif_d is None else None,
                amount_range=amount,
                # In the glyphs-lost rendering the checkbox glyph is absent from
                # the text layer entirely, so its state is unrecoverable: None
                # means "unknown", never a fabricated boolean (SPEC §6.3).
                cap_gains_over_200=(glyph == "gfedcb") if glyph is not None else None,
                description=_scrub_field(description),
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

# The shape of a real Clerk asset-type code (``ST``/``MF``/``BA``…, rendered in
# inconsistent case — ``sT``/``Ba``; plus the digit-led ``5P``/``5F``/``5``
# forms). A 3+ char bracket is a ticker the filer wrote into the name (``[VOO]``,
# ``[ARKK]``) and ``[1]``/``[2]`` are footnote refs — neither is a type tag. Used
# to tell a real second type tag (a merged row — GH-0100) from such noise.
_FD_ASSET_TYPE_CODE_RE = re.compile(r"[A-Za-z]{2}|5[A-Za-z0-9]?")

# An owner column token (SP/DC/JT) appearing right after the [TYPE] tag.
# Case-insensitive: the case-mangled rendering (SPEC §2.2) lowercases the
# small-caps owner tokens unpredictably (``Sp`` / ``Jt``); extractors normalize
# the captured token with .upper().
_FD_OWNER_AFTER_TYPE_RE = re.compile(r"\][\s]*(SP|DC|JT)\b", re.IGNORECASE)

# The same owner token after the subholding arrow ``⇒`` — where it prints on a
# wrapped row whose ``[TYPE]`` tag landed off the anchor line (GH-0099). The
# arrow itself is the column boundary, so the token right after it is the owner.
_FD_OWNER_AFTER_ARROW_RE = re.compile(r"⇒[\s]*(SP|DC|JT)\b", re.IGNORECASE)

# The owner token sitting immediately before the value column's low bound —
# where it lands on a wrapped-``[TYPE]`` row whose tag wrapped off the value line
# entirely, so neither the after-tag nor the after-arrow form is on the line
# (GH-0100; ``…Lease JT $1,001 - $15,000`` / ``[GS]``). Anchored to the value low
# so it cannot match an ``SP``/``JT`` that happens to fall inside an asset name.
# A normal row matches the after-tag form first; this is only the fallback.
_FD_OWNER_BEFORE_VALUE_RE = re.compile(
    r"\b(SP|DC|JT)\b\s+(?=\$[\d,]+\s*-)", re.IGNORECASE
)

# The checkbox glyph matched anywhere (not just line-end) — used to lift the
# glyph token out of the asset slice on wrapped rows where it renders mid-name.
_FD_GLYPH_TOKEN_RE = re.compile(r"\bgfedcb?\b")

# An amount-range bucket; FD ranges wrap across lines, so we match all occurrences
# in the assembled (de-wrapped) item text. ``Over $X`` / open-ended values do not
# match and correctly leave the structured field None (raw_text still carries it).
_FD_AMOUNT_RE = re.compile(r"\$[\d,]+\s*-\s*\$[\d,]+")

# Per-page repeated column-header furniture for each schedule (skipped, never
# folded into an item). Matched loosely on its leading words.
_FD_FURNITURE_RE = re.compile(
    r"^(asset owner|asset \[|owner creditor|Source type|Position name|"
    r"Date Parties|type\(s\)|type gains >|gains >|\$1,000\?|\$200\?|filing|current Year|"
    r"year to year|"
    r"to Preceding|liability|\* For the complete|\* Asset class|name of organization|"
    # E–J column-header furniture (#17): each schedule's header row, on its own
    # line, leads with these tokens; the values below are filer content.
    r"Source Description|Source Date|Source Activity|Source Brief|"
    # The modern intact-letter H/J header bands (#146). The live form's Schedule H
    # header wraps across physical lines as ``Trip Details Inclusions`` /
    # ``Source Start [Date] End Date Itinerary Days at Own Lodging? Food? Family?``
    # / a ``[Date ]Exp.`` tail; Schedule J's header is
    # ``Source (Name and Address) Brief Description of Duties``. None leads with a
    # bare ``Source <column>`` token, so the branches above miss them and the
    # banner leaks as phantom pre-anchor rows. A real H row opens with the trip's
    # funding source then a date (never ``Source Start`` / ``Trip Details``), and a
    # real J row's source is the org's name+parenthesized address (not the literal
    # ``(Name and Address)`` placeholder), so none collides.
    r"Trip Details|Source Start|(?:Date )?Exp\.$|"
    r"Source \(Name and Address\)|"
    # Schedule H/J column header in the glyphs-lost (NUL) rendering (#133):
    # the small-caps header words extract as NUL runs, so the intact-letter
    # ``Source Date``/``Source Description`` branches above miss it and the
    # banner leaks as a phantom raw_text row. Both H (``Source Dates Location
    # Items``) and J (``Source Description of Duties``) lead with the ``Source``
    # column (``S\x00+``) followed by a ``D``-initial second column
    # (``Dates``/``Description`` → ``D\x00+``). Anchoring on that ``S\x00+ D\x00+``
    # pair is collision-proof: the ``S\x00+ A:`` schedule heading is consumed
    # before this check, and the ``S\x00+ A \x00+ B`` appendix title has an
    # ``A``-initial second token — so neither matches. NULs appear only in
    # furniture (SPEC §2.2), so a real H/J row never trips this.
    r"S\x00+\s+D\x00+)",
    re.IGNORECASE,
)

# Detail lines that belong to (not start) an A item. The labels are small-caps
# form furniture, so the glyphs-lost rendering turns them into ``L\x00{7}:`` /
# ``D\x00{10}:`` — matched by the NUL branches (the *values* after the colon are
# filer content in a regular font and always survive intact).
_FD_LOCATION_LABEL = r"L(?:OCATION|\x00+):"
_FD_DESCRIPTION_LABEL = r"D(?:ESCRIPTION|\x00+):"
_FD_DETAIL_RE = re.compile(rf"^({_FD_LOCATION_LABEL}|{_FD_DESCRIPTION_LABEL})")
# The same two labels matched mid-string — where a row's detail lines have
# already been folded into its assembled raw blob and mark the end of the
# row's *columns* (everything after them is detail text, not column data).
_FD_DETAIL_ANYWHERE_RE = re.compile(rf"{_FD_LOCATION_LABEL}|{_FD_DESCRIPTION_LABEL}")


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

    def meaningful_rows() -> list[str]:
        # The non-furniture, non-``None disclosed.`` lines of the current buffer.
        # An empty schedule (``None disclosed.``) yields ``[]`` and is recorded as
        # **absent**, never an empty list.
        return [
            ln
            for ln in (b for b in buf if b.strip())
            if not _NONE_DISCLOSED_RE.match(ln.strip())
            and not _FD_FURNITURE_RE.match(ln.strip())
        ]

    def is_none_disclosed() -> bool:
        # True once the filer's ``None disclosed.`` marker is in the buffer — the
        # schedule was explicitly left blank. This distinguishes an empty *blank*
        # schedule (whose only line is the marker) from a populated schedule that
        # merely hasn't seen its first content row yet (buffer holds furniture).
        return any(_NONE_DISCLOSED_RE.match(b.strip()) for b in buf if b.strip())

    def flush() -> None:
        if current is None:
            return
        if rows := meaningful_rows():
            schedules[current] = rows

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
        # A post-table appendix (asset-class legends, investment-vehicle keys, …)
        # follows the last schedule and is never disclosed rows. When the filer
        # marked that trailing schedule ``None disclosed.``, ANY content line that
        # follows is appendix material, not schedule rows — terminate here so it is
        # not salvaged into fabricated rows for a schedule the filer left blank
        # (#130, generalizing #97 which whitelisted only "Asset Class Details").
        # The test is heading-agnostic: an explicitly-blank schedule cannot grow
        # real rows, so the first such line ends it regardless of its title. A
        # populated schedule (real ``meaningful_rows``) never trips this and folds
        # any appendix into its content as on an intact document — never dropped.
        if current is not None and is_none_disclosed() and not meaningful_rows():
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


def _strip_spans(raw: str, end: int, spans: list[tuple[int, int]]) -> str:
    """``raw[:end]`` with the character ranges in ``spans`` removed.

    Used to lift Schedule A's column text (value / income / owner / glyph) out of
    the asset-name slice (GH-0099). Only spans that fall within ``[0, end)`` are
    removed; a span reaching past ``end`` (a wrapped high bound that landed after
    the ``[TYPE]`` tag) is clipped to ``end`` so it cannot leave a fragment. The
    gaps left behind are collapsed to single spaces, then trimmed — the verbatim
    ``raw_text`` keeps every character regardless.
    """
    keep = []
    for s, e in spans:
        e = min(e, end)
        if s < e:  # non-empty after clipping to ``end``
            keep.append((s, e))
    keep.sort()
    out: list[str] = []
    cursor = 0
    for s, e in keep:
        if s > cursor:
            out.append(raw[cursor:s])
        cursor = max(cursor, e)
    out.append(raw[cursor:end])
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _group_items(lines: list[str], *, starts_item, starts_before=None) -> list[str]:
    """Fold ``lines`` into per-item verbatim ``raw_text`` blocks.

    A new item begins at each line for which ``starts_item(line)`` is true; every
    following line (wrapped column, ``LOCATION:``/``DESCRIPTION:`` detail, wrapped
    amount) folds into the current item until the next start. Returns one joined
    ``raw_text`` string per item, in document order.

    ``starts_before(line, next_line)`` (optional) is a one-line lookahead: a line
    that ``starts_item`` did not anchor still opens a row when the *next*
    meaningful line proves it did — used by Schedule A for the row whose ``[TYPE]``
    tag (and only the tag) wrapped onto the following line, so the body line above
    carries no on-line tag/glyph/value-low to anchor on (GH-0100).

    Any lines *before* the first item-start anchor are not dropped — a row whose
    anchor was lost (a Schedule D liability with a blank ``Date incurred``, an A/B
    row whose glyph rendered off, a signature-split row) would otherwise vanish
    with no ``raw_text`` and no manifest entry, violating CLAUDE.md's "never
    silently drop / verbatim raw_text on every line item". They are folded into a
    single leading raw item so their text survives verbatim.
    """
    # Drop blank, whitespace-, or NUL-only furniture (NUL isn't stripped by
    # str.strip but scrubs to empty): it carries no filer content, so it must not
    # seed an empty-raw_text item — matches _salvage_raw, which also skips such
    # lines. Never drops content (there is none). Precomputed so the lookahead can
    # see the next *meaningful* line, not an intervening blank.
    rows = [s for ln in lines if (s := ln.strip()) and _scrub_raw_text(s)]
    items: list[str] = []
    cur: list[str] = []
    pre: list[str] = []  # lines seen before the first item-start anchor
    for i, s in enumerate(rows):
        start = starts_item(s)
        if not start and starts_before is not None and i + 1 < len(rows):
            start = starts_before(s, rows[i + 1])
        if start:
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


# The wrapped second line of a stacked ``Income Type(s)`` cell (GH-0166). The
# Clerk's Schedule A income-type column holds a small closed set of categories;
# two stacked ones (``Capital Gains,`` then ``Dividends``) wrap, and the second
# line lands in the de-wrapped row tail. This matches the continuation category
# there. Listed so the longest phrase wins; ``Capital Gains`` precedes its own
# trailing-comma first line, which is already captured between the columns.
_FD_A_INCOME_TYPE_CONT_RE = re.compile(
    r"Capital Gains|Dividends|Interest|Rent|Royalties|Partnership Income|"
    r"Tax-Deferred|Excepted Investment Fund|Excepted Trust",
    re.IGNORECASE,
)


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


# An open-ended ``Over $X`` value — a real column entry that cannot be
# represented as a ``{low, high}`` bucket. It occupies its column *slot* (so the
# buckets after it are not mis-assigned one column left) but parses to ``None``;
# the verbatim ``raw_text`` keeps the wording.
_FD_A_OVER_RE = re.compile(r"Over\s+\$[\d,]+", re.IGNORECASE)
# Any bare money token — the candidate pool for wrapped high bounds, filtered in
# ``_fd_amount_entries`` down to tokens claimed by no bucket/Over/dangling span.
_FD_BARE_AMOUNT_RE = re.compile(r"\$[\d,]+")
# An amount-column *opener* — a ``$lo -`` range start, low figure captured. Each
# is one amount column; ``_fd_amount_entries`` (GH-0166) pairs each opener with
# its high (the adjacent inline ``$hi`` or a wrapped bare token in the tail).
_FD_AMOUNT_OPENER_RE = re.compile(r"\$([\d,]+)\s*-")
# A single exact-dollar column value (``$4,425.09``) — cents required so it cannot
# be confused with a bare whole-dollar wrapped high (GH-0166 / GH-0160). Serialized
# as an ``{exact, label}`` point, the same shape PTR exact amounts use (#49).
_FD_EXACT_AMOUNT_RE = re.compile(r"\$[\d,]+\.\d{2}")


def _fd_columns(raw: str) -> str:
    """The row's *column* data: everything before the first folded detail label.

    A row's ``LOCATION:``/``DESCRIPTION:`` detail lines fold into its assembled
    raw blob after the columns, so amounts must never be read past the first
    label — yet a wrapped high bound can sit immediately before one.
    """
    m = _FD_DETAIL_ANYWHERE_RE.search(raw)
    return raw[: m.start()] if m else raw


def _fd_amount_entries(
    cols: str, start: int = 0
) -> tuple[list[tuple[int, int, AmountRange | None]], list[tuple[int, int]]]:
    """The row's column-amount *entries*, in column order: ``(start, end, range)``,
    plus the character spans consumed as **wrapped high bounds** (the bare ``$N``
    tokens paired to dangling lows — GH-0099).

    The entries carry the column slots the caller maps positionally to value /
    income / preceding. A dangling low's slot ends at the dash; its wrapped high
    bound lands later in the de-wrapped text as a bare token. That bare span is
    column data too — physically displaced from its slot — so it is returned
    alongside the entries for callers (the asset-name slicer) that must strip
    *all* column text out of a row's name field, not just the contiguous slots.

    An entry is anything that occupies an amount column in the de-wrapped row
    text (SPEC §2.2 wraps put every wrapped piece back on one line, in column
    order, because pdfplumber emits left-to-right):

    - a complete ``$lo - $hi`` bucket → its parsed range;
    - an open-ended ``Over $X`` → ``None`` (unrepresentable as a bucket, but it
      holds its column slot);
    - a **dangling low** (``$lo -`` with a word right after — the high bound
      wrapped off the column) → resolved by pairing, in order, with the row's
      **bare** money tokens (a ``$N`` that is part of no bucket/dangling/``Over``
      and is not a cents-bearing exact value): each wrapped high lands as a bare
      token after its dangling low, in the same column order. The pairing is
      attempted only when the counts match exactly — any surplus or deficit
      (say, a dollar figure inside an asset name) makes the wrap ambiguous, and
      every dangling resolves to ``None`` instead (degrade, never fabricate —
      #12). An unresolved dangling still holds its column slot.

    **Cross-column contamination** (GH-0098 / GH-0166): a column's high bound
    wraps onto a following physical line, so pdfplumber's left-to-right re-flow
    drops the bare ``$hi`` token at the de-wrapped *tail*, in column order. When
    only one column wraps the bare lands cleanly after the row; when *several*
    wrap, an earlier column's wrapped high can land right in front of a later
    column's ``$lo -`` start, so the two glue into a textually-adjacent but
    spurious bucket (``$15,001 - $1,000,000`` where ``$1,000,000`` is the *value*
    column's wrapped high, not the *preceding* column's — Pou ``10068928``). Read
    naively that crosses the columns and leaves the row's real first low dangling
    against the wrong high (often inverting it).

    The general resolver (GH-0166) treats every ``$lo -`` opener as a column and
    searches for the **smallest set of textually-adjacent buckets to split** —
    releasing each split bucket's ``$hi`` back into the ordered tail pool of bare
    wrapped highs — such that every column pairs to a high with no inverted range
    and no leftover dangling. A clean row (no wraps, or one wrap) needs no split
    and the greedy reading already wins; a multi-wrap row recovers exactly when a
    consistent column-ordered assignment exists. The split set is tiny (one per
    amount column, ≤ 3–4), so the search is bounded. When no consistent
    assignment exists the row degrades (danglings → ``None``, slot held,
    ``raw_text`` intact): an impossible bucket is never emitted (degrade, never
    fabricate — #12).
    """
    overs = list(_FD_A_OVER_RE.finditer(cols, start))
    over_spans = [m.span() for m in overs]

    def in_over(pos: int) -> bool:
        return any(a <= pos < b for a, b in over_spans)

    def inverted(r: AmountRange | None) -> bool:
        return bool(r and r.low is not None and r.high is not None and r.low > r.high)

    # Every ``$lo -`` column opener (position order), skipping any inside an
    # ``Over $X`` slot. Each opener is one amount column; its high is either the
    # adjacent inline ``$hi`` or a wrapped bare token in the tail.
    openers = [
        (m.start(), m.end(), m.group(1))
        for m in _FD_AMOUNT_OPENER_RE.finditer(cols, start)
        if not in_over(m.start())
    ]

    def adjacent_high(opener_end: int) -> re.Match | None:
        """The ``$hi`` immediately following an opener's dash (inline bucket)."""
        return re.match(r"\s*(\$[\d,]+)", cols[opener_end:])

    def build(
        split: frozenset[int],
    ) -> tuple[list[tuple[int, int, AmountRange | None]], list[tuple[int, int]], bool]:
        """Resolve with the openers in ``split`` forced dangling (their inline
        high, if any, released into the tail pool). Returns the entries, the
        wrapped-high spans, and whether the assignment was *consistent* (every
        dangling paired, no inversion)."""
        consumed: list[tuple[int, int]] = []  # adjacent highs kept by their opener
        dangling: list[int] = []  # opener indices needing a tail high
        kept: dict[int, tuple[int, int, str]] = {}  # idx → (lo, hi, span_end)
        for i, (s, e, lo) in enumerate(openers):
            hm = adjacent_high(e)
            if hm and i not in split:
                hi = hm.group(1).lstrip("$")
                kept[i] = (lo, hi, e + hm.end())
                consumed.append((e + hm.start(1), e + hm.end()))
            else:
                dangling.append(i)
        # Tail pool: bare ``$N`` tokens that are not an opener's own low figure,
        # not an opener's kept adjacent high, not a cents-bearing exact value,
        # and not inside an ``Over`` slot. In document (column) order.
        low_spans = [(s, s + len("$" + lo)) for s, e, lo in openers]
        claimed = low_spans + consumed
        bares = [
            m
            for m in _FD_BARE_AMOUNT_RE.finditer(cols, start)
            if not any(a <= m.start() < b for a, b in claimed)
            and not in_over(m.start())
            and not cols[m.end() :].lstrip().startswith("-")  # a range's own low
            and not cols[m.end() :].startswith(".")  # an exact value's dollars
        ]
        consistent = len(dangling) == len(bares)
        wrapped: list[tuple[int, int]] = []
        pairs: dict[int, tuple[str, str]] = {i: (lo, hi) for i, (lo, hi, _) in kept.items()}
        if consistent:
            for idx, b in zip(dangling, bares):
                pairs[idx] = (openers[idx][2], b.group(0).lstrip("$"))
                wrapped.append(b.span())

        entries: list[tuple[int, int, AmountRange | None]] = []
        for i, (s, e, lo) in enumerate(openers):
            if i in pairs:
                plo, phi = pairs[i]
                rng = _parse_amount_range(f"${plo} - ${phi}")
                if inverted(rng):
                    rng, consistent = None, False  # inverted → not a valid assignment
                # A kept inline bucket spans low..adjacent-high; a wrapped pairing
                # holds only the opener slot (its high lives in ``wrapped``).
                span_end = kept[i][2] if i in kept else e
                entries.append((s, span_end, rng))
            else:  # unresolved dangling — slot held, no range
                entries.append((s, e, None))
        entries += [(m.start(), m.end(), None) for m in overs]
        entries.sort()
        return entries, wrapped, consistent

    adj_idxs = [i for i, (s, e, _) in enumerate(openers) if adjacent_high(e)]
    best: tuple[list, list[tuple[int, int]]] | None = None
    degrade: tuple[list, list[tuple[int, int]]] | None = None
    for k in range(len(adj_idxs) + 1):
        for combo in itertools.combinations(adj_idxs, k):
            entries, wrapped, consistent = build(frozenset(combo))
            if k == 0:
                degrade = (entries, wrapped)  # the no-split reading — fallback
            if consistent:
                best = (entries, wrapped)
                break
        if best:
            break
    entries, wrapped = best if best is not None else degrade
    # Exact-dollar column values (GH-0166 / GH-0160): a Schedule A income column
    # can hold a single exact figure (``$4,425.09``) rather than a ``$lo - $hi``
    # bucket — the same point-not-bucket shape PTR rows use (#49). Such a token is
    # not a range opener, not a wrapped high already claimed by a dangling, and not
    # inside an ``Over`` slot — so any remaining cents-bearing ``$N.dd`` token (the
    # unambiguous exact marker) becomes its own column entry. We require the cents
    # so a bare whole-dollar wrapped high that the consistency search left unpaired
    # never masquerades as an exact value (degrade, never fabricate — #12).
    claimed_spans = [(s, e) for s, e, _ in entries] + wrapped
    for m in _FD_EXACT_AMOUNT_RE.finditer(cols, start):
        if in_over(m.start()) or any(
            a <= m.start() < b for a, b in claimed_spans
        ):
            continue
        entries.append((m.start(), m.end(), _parse_exact_amount(m.group(0))))
    entries.sort()
    return entries, wrapped


def _schedule_a_amounts(
    raw: str,
) -> tuple[
    AmountRange | None,
    str | None,
    AmountRange | None,
    AmountRange | None,
    list[tuple[int, int]],
]:
    """Untangle Schedule A's ``value_of_asset``, ``income_type``, ``income_amount``,
    and (Candidate/New-Filer forms only) ``income_preceding`` — plus the
    character spans (into ``raw``) of all the column text consumed, so the
    caller can lift those spans out of the asset-name slice (GH-0099).

    The row's amount columns print in a fixed order — value, income current
    year, and (C/H forms only — GH-0070) income preceding year — so the parse is
    positional over the row's :func:`_fd_amount_entries`: first entry → value,
    second → income, third → preceding. The member form never prints three
    amount columns, so a third entry only ever means a C/H row. Two twists:

    - **None-value rows**: a value column opening with the literal ``None``/
      ``Undetermined`` (``_FD_A_NONE_VALUE_RE``) holds the value slot, shifting
      every amount entry one column right — assigning the first bucket to value
      would fabricate one.
    - **Column wraps** (SPEC §2.2): a wrapped high bound leaves a dangling low;
      :func:`_fd_amount_entries` re-pairs them (or degrades that entry to
      ``None``) while preserving the column order.

    The income *type* word(s) (``Rent``/``Dividends``/…) sit between the value
    and income columns. Only the text before the first folded ``LOCATION:``/
    ``DESCRIPTION:`` label is column data — a wrapped high bound can sit right
    before a detail label, and detail text must never be read as columns. A
    field that cannot be untangled is ``None`` — the verbatim ``raw_text``
    still carries the row in full.
    """
    cols = _fd_columns(raw)

    none_value = _FD_A_NONE_VALUE_RE.search(cols)
    # Wrapped-name fallback (GH-0166 / GH-0160): when the asset name wraps so far
    # that its ``[TYPE]`` tag lands *after* the value literal, the ``]``/``⇒``
    # anchor ``_FD_A_NONE_VALUE_RE`` needs is gone (Smith ``10066320``: ``…Health
    # Undetermined Royalties $4,425.09 Communications, Inc. [IP]``). A ``None``/
    # ``Undetermined`` immediately followed by an income-type category is still
    # unambiguously the value slot — a value literal is the only place that
    # signature occurs (an income ``None`` sits *after* the type, never before it).
    if none_value is None:
        cand = _FD_A_NONE_BEFORE_TYPE_RE.search(cols)
        # Only the *value* slot's literal qualifies: it must precede every amount
        # opener. A ``None`` sitting after a ``$lo -`` is an income-current-year
        # cell on the three-column candidate form (Hackett ``10035478``: ``…100%
        # $250,001 - None Interest [OL] $500,000``), never the value — taking it
        # as the value would shift the real value range out of its slot.
        if cand:
            first_opener = _FD_AMOUNT_OPENER_RE.search(cols)
            if first_opener is None or cand.start() < first_opener.start():
                none_value = cand
    entries: list[tuple[int, int, AmountRange | None]]
    wrapped: list[tuple[int, int]]
    if none_value:
        # The literal None occupies the value slot; amounts shift right. Anchor
        # the slot at the literal word itself — not the ⇒/] before it (so stripping
        # this column span (GH-0099) leaves the subholding arrow in the name) and
        # not the income-type word after it (the wrapped-name fallback's match
        # runs through the type, but the value slot ends at the literal so the
        # income-type stays readable between the columns).
        lit = re.search(r"(?:None|Undetermined)\b", none_value.group(0))
        lit_start = none_value.start() + lit.start()
        lit_end = none_value.start() + lit.end()
        entries = [(lit_start, lit_end, None)]
        rest, wrapped = _fd_amount_entries(cols, lit_end)
        entries += rest
    else:
        entries, wrapped = _fd_amount_entries(cols)

    value = entries[0][2] if entries else None
    income = entries[1][2] if len(entries) > 1 else None
    preceding = entries[2][2] if len(entries) > 2 else None
    income_type = (
        _income_type_between(cols, entries[0][1], entries[1][0])
        if len(entries) > 1
        else None
    )
    # Wrapped income-type second line (GH-0166 / GH-0160): the ``Income Type(s)``
    # cell can hold two stacked categories (``Capital Gains, Dividends``). The
    # first line (``Capital Gains,`` — note the trailing comma) prints between the
    # value and income columns where ``_income_type_between`` reads it; the second
    # line wraps to the de-wrapped *tail*, after the income amount, where it would
    # otherwise be left dangling (a bare ``Dividends`` folded into the asset name
    # and a corrupt ``Capital Gains,`` type). A trailing comma on the captured
    # type is the tell the category continues; complete it from the row tail.
    type_tail_span: tuple[int, int] | None = None
    if income_type and income_type.rstrip().endswith(","):
        cont = _FD_A_INCOME_TYPE_CONT_RE.search(cols, entries[1][1])
        if cont:
            income_type = f"{income_type.rstrip(', ')}, {cont.group(0)}"
            type_tail_span = cont.span()
    # Two-income (Candidate/New-Filer) form, current-year column = literal None
    # (#132): the form prints Type | Current-Year | Preceding-Year. When the
    # current-year cell is ``None`` it carries no ``$`` so it never becomes an
    # amount entry — the lone trailing ``$lo - $hi`` is the *preceding* column,
    # and the ``None`` sentinel lands in the value→income gap, contaminating
    # ``income_type`` ("Capital Gains None") and sliding the preceding range into
    # the income (current-year) slot. Recognize a ``None``/``Undetermined``
    # trailing the type words as the empty current-year cell: strip it from the
    # type, null the current-year income, and reassign the range to preceding.
    if income_type and preceding is None:
        type_m = _FD_A_NONE_INCOME_RE.search(income_type)
        if type_m:
            income, preceding = None, income
            income_type = income_type[: type_m.start()].strip() or None
    # Column spans to lift out of the asset name: each entry's slot, the whole
    # value→income gap (which carries the income-type word(s) — they print
    # *between* the name fragments on wrapped/subholding rows), and the wrapped
    # high bounds displaced out of their slots. The trailing checkbox glyph is
    # handled separately by the slicer (it is row furniture, not an amount).
    spans = [(s, e) for s, e, _ in entries]
    spans += wrapped
    if len(entries) > 1:
        spans.append((entries[0][1], entries[1][0]))
    if type_tail_span is not None:
        spans.append(type_tail_span)
    # ``None``/``Undetermined`` value/income literals carry no ``$`` so they are
    # not amount entries, yet on a ⇒/]-anchored wrapped row they sit in the
    # column zone (``⇒ None None gfedc Name``) and bleed into the name. The
    # checkbox glyph marks the column/name boundary on these rows, so any such
    # literal between the anchor and the glyph is column data — strip it
    # (GH-0099). Member rows print the glyph after the name, so this only fires
    # on wrapped rows; literals already inside an amount entry are left alone.
    glyph = _FD_GLYPH_TOKEN_RE.search(cols)
    if glyph:
        anchors = [m.end() for m in re.finditer(r"[\]⇒]", cols) if m.end() <= glyph.start()]
        if anchors:
            zone_start = anchors[-1]
            spans += [
                m.span()
                for m in re.finditer(r"\b(?:None|Undetermined)\b", cols)
                if zone_start <= m.start() < glyph.start()
                and not any(s <= m.start() < e for s, e, _ in entries)
            ]
    return value, income_type, income, preceding, spans


# An A-row's value-column signature: right after the ``[TYPE]`` tag (and the
# optional owner token) the value column begins — a ``$lo -`` range start (the
# dash is load-bearing), the open-ended ``Over $X`` bucket, an exact dollar
# value (``$96,550.00`` — note the required ``.dd`` cents), or the literal
# ``None``/``Undetermined``.
# A wrapped subholding tail (``Cash [BA] $5,000,000`` — the row's wrapped value
# *high* bound) carries a bare amount with **no dash, no cents, no ``Over``**, so
# it does not match and stays a continuation, exactly as the glyph anchor would
# have treated it (verified collision-free against continuation lines).
#
# Until GH-0070 this signature anchored only glyphs-lost (NUL) documents, with
# intact documents gated on the trailing checkbox glyph. That gate was the bug:
# Candidate/New-Filer report forms have **no checkbox column at all** (no glyph
# in any rendering), and even on the member form a row whose ``[TYPE]``-tagged
# name wraps off the glyph-bearing line never carries tag+glyph together. The
# signature anchors in **every** rendering now; the glyph remains an additional
# anchor signal, never a gate.
# The literal words match case-insensitively via (?i:…) — the case-mangled
# rendering lowercases them (``none`` / ``over``) like every other small-caps
# victim; the owner tokens in the two row signatures below get the same
# treatment. The money forms are case-free.
_FD_VALUE_START = r"(?:\$[\d,]+\s*-|(?i:Over)\b|\$[\d,]+\.\d{2}|(?i:None)\b|(?i:Undetermined)\b)"
_FD_A_ROW_AFTER_TYPE_RE = re.compile(rf"\]\s*(?i:SP|DC|JT)?\s*{_FD_VALUE_START}")
# The same value-column signature after the subholding arrow. A bare ``⇒`` is
# NOT a row anchor (GH-0070): a long parent name can wrap with the arrow
# landing on the *continuation* line among the wrapped high bounds
# (``LISA BLUNT ROCHESTER TR ⇒ $500,000 Dividends $2,500``) — only an arrow
# followed by the value column's start opens a row.
_FD_A_ROW_AFTER_ARROW_RE = re.compile(rf"⇒\s*(?i:SP|DC|JT)?\s*{_FD_VALUE_START}")

# A tag-less Schedule A row-start signal (GH-0100): the value column's low-bound
# start — a ``$lo -`` standing on a line that carries no [TYPE] tag, no glyph,
# and no anchoring arrow. The asset name (and, on a ⇒ subholding, the umbrella
# account name + arrow + the subholding's own name) can wrap so far that the
# value column prints on the name's *first* physical line while the [TYPE] tag
# lands a line (or several) below it. That left the row anchorless on every other
# signal, so it — and its value bucket — folded silently into the row above (the
# #70 regression; ``Account [BA]`` after ``…Jet Checking $1,001 - $15,000``, or a
# whole buried ⇒ subholding cluster ``…Deferred $15,001 - $50,000 Tax-Deferred`` /
# ``Compensation Plan ⇒`` / ``BNY Mellon … (STSVX)`` / ``[MF]``). Anchoring on the
# value low recovers the row: the wrapped tag/name fold in as continuations and
# the column parser untangles them. Only a column *low* matches — a wrapped HIGH
# lands as a bare ``$N`` with no trailing dash, so a continuation line never trips
# this. It fires on any ``$lo -`` opener — a dangling low *or* a complete ``$lo -
# $hi`` value bucket — since either marks a wrapped row whose value column prints
# above its [TYPE] tag. (``_fd_amount_entries`` reuses the same opener shape via
# ``_FD_AMOUNT_OPENER_RE`` to enumerate amount columns — GH-0166.)
_FD_A_VALUE_LOW_RE = re.compile(r"\$[\d,]+\s*-")

# A value column that *opens* with the literal ``None``/``Undetermined`` —
# immediately after the ``[TYPE]`` tag or the subholding arrow (plus the
# optional owner token). On such a row every ``$lo - $hi`` bucket belongs to
# the **income** columns, not the value column; assigning the first bucket to
# ``value_of_asset`` (the positional default) would fabricate a value the
# filer explicitly declared None. The ``]``/``⇒`` anchor is what keeps this
# sound: a ``None`` in the *income* columns sits after the value bucket, never
# in this position.
_FD_A_NONE_VALUE_RE = re.compile(
    r"[\]⇒]\s*(?:SP|DC|JT)?\s*(?:None|Undetermined)\b", re.IGNORECASE
)

# A value literal whose ``]``/``⇒`` anchor wrapped off the line (GH-0166): a
# ``None``/``Undetermined`` immediately followed by an income-type category. That
# adjacency is unique to the *value* slot — the value→type→income column order
# means a value literal is the only ``None``/``Undetermined`` that precedes a
# type word; an income ``None`` always follows it. Used only as a fallback when
# the bracket-anchored form misses (the asset name wrapped past its ``[TYPE]``).
_FD_A_NONE_BEFORE_TYPE_RE = re.compile(
    r"\b(?:None|Undetermined)\s+(?:"
    + _FD_A_INCOME_TYPE_CONT_RE.pattern
    + r")\b",
    re.IGNORECASE,
)

# A literal ``None``/``Undetermined`` current-year income cell trailing the
# income-type word(s) on the two-income (Candidate/New-Filer) form (#132). It
# carries no ``$``, so it is never an amount entry — it folds into the
# value→income gap and contaminates ``income_type``. Anchored to end-of-string
# so only a *trailing* sentinel matches (the type word(s) come first).
_FD_A_NONE_INCOME_RE = re.compile(r"\s+(?:None|Undetermined)\s*$", re.IGNORECASE)

# Any value-column signature anywhere on a line (the same alternation as
# ``_FD_VALUE_START``, un-anchored). A row whose value is ``None``/``Over`` rather
# than a ``$lo -`` range carries no value low for ``_FD_A_VALUE_LOW_RE`` to catch,
# so the wrapped-tag lookahead (``_is_wrapped_tag_tail`` below) uses this to tell
# a value-bearing row body from a ⇒ subholding's pure-name continuation line.
_FD_A_HAS_VALUE_RE = re.compile(_FD_VALUE_START)


def _is_wrapped_tag_tail(s: str) -> bool:
    """``s`` is only a wrapped ``[TYPE]`` tag (plus maybe the tail of the asset
    name): ``[MF]`` / ``Account [BA]`` / ``(PENN) [ST]`` / ``REFUNDING BOND [GS]``.

    It carries a real asset-type code but no value column, no glyph, no arrow, and
    is not a detail line — so it can only be the ``[TYPE]`` tag that wrapped off
    the row body on the line above (GH-0100), never a row of its own. A line that
    anchors itself (tag + value, or a value low) or carries a ticker/footnote
    bracket rather than a real code is not a tail.
    """
    if _FD_DETAIL_RE.match(s) or _FD_GLYPH_RE.search(s) or "⇒" in s:
        return False
    if _FD_A_VALUE_LOW_RE.search(s) or _FD_A_ROW_AFTER_TYPE_RE.search(s):
        return False
    m = _FD_TYPE_TAG_RE.search(s)
    return bool(m and _FD_ASSET_TYPE_CODE_RE.fullmatch(m.group(1)))


def _parse_schedule_a(lines: list[str]) -> list[ScheduleAItem]:
    """Schedule A (assets & "unearned" income) → structured items + raw_text.

    A row anchors on any of three rendering-independent signals (GH-0070): the
    ``[TYPE]`` tag + trailing tx-over-$1,000 checkbox glyph (member forms, intact
    rendering), the ``[TYPE]`` tag + value-column signature (all renderings —
    the only anchor available on Candidate/New-Filer forms, which have no
    checkbox column, and on glyphs-lost NUL documents, which lose the glyph),
    or the subholding owner arrow ``⇒`` (the row's ``[TYPE]``-tagged name often
    wraps onto the next line, leaving only the arrow on the anchor line). A
    fourth signal — a tag-less line opening the value column with a ``$lo -`` low
    bound — catches the rows whose ``[TYPE]`` tag (and, on a ⇒ cluster, the
    subholding name and arrow) wrapped off the value line entirely (GH-0100):
    the wrapped-tag member/Candidate row and the buried ⇒-subholding cluster
    whose value prints a line above its tag. Without it those rows folded
    silently into the row above — the #70 Schedule A regression.
    """

    def starts(s: str) -> bool:
        # LOCATION:/DESCRIPTION: detail and bare amount-wrap lines never start
        # a row; everything else is judged by the column signatures above.
        if _FD_DETAIL_RE.match(s):
            return False
        if _FD_GLYPH_RE.search(s):
            # The checkbox is the row's LAST column, so a glyph-terminated line
            # IS a row line — even with no [TYPE] on it (a long name can push
            # the tag onto the wrap with the glyph still on the row line:
            # ``Public Employees' Retirement System of Mississippi Undetermined
            # Tax-Deferred gfedc`` / ``[DB]``). The one exception is a stranded
            # glyph alone on its line (a wrapped checkbox remnant, same
            # artifact as the PTR page-break case): that belongs to the row
            # above and must fold in, not anchor an empty one.
            return not _PTR_GLYPH_ONLY_RE.match(s)
        if _FD_TYPE_TAG_RE.search(s):
            return bool(_FD_A_ROW_AFTER_TYPE_RE.search(s))
        if "⇒" in s:
            # Only an arrow followed by the value column opens a row — a bare
            # arrow can sit on a wrapped continuation (see the regex comment).
            return bool(_FD_A_ROW_AFTER_ARROW_RE.search(s))
        # Tag-less line opening the value column with a ``$lo -`` low bound — the
        # asset (or ⇒ subholding) name ran long enough to push the [TYPE] tag onto
        # a later line, so the value column's range start is the only anchor signal
        # left (GH-0100; see _FD_A_VALUE_LOW_RE). Covers the dangling-low C/H case
        # (``…100% $250,001 - None``), the wrapped-[TYPE] member row, and the
        # buried ⇒-subholding cluster. A wrapped HIGH bound is a bare ``$N`` with no
        # trailing dash, so a continuation line cannot split a row here.
        return bool(_FD_A_VALUE_LOW_RE.search(s))

    def starts_before(s: str, nxt: str) -> bool:
        # One-line lookahead (GH-0100): ``s`` opens a row when its ``[TYPE]`` tag
        # wrapped onto ``nxt`` and its value is None/Over (no ``$lo -`` for the
        # tag-less signal above to see) — e.g. ``403b … International Opportunities
        # None Tax-Deferred`` / ``[MF]``. Requiring a value signature on ``s`` is
        # what keeps this from splitting a ⇒ subholding: that cluster's pure-name
        # continuation line (``BNY Mellon … (STSVX)``) carries no value and must
        # fold into the subholding's value line above, not anchor on its own tag.
        return (
            not _FD_DETAIL_RE.match(s)
            and bool(_FD_A_HAS_VALUE_RE.search(s))
            and _is_wrapped_tag_tail(nxt)
        )

    items: list[ScheduleAItem] = []
    for raw in _group_items(lines, starts_item=starts, starts_before=starts_before):
        location = description = None
        loc_m = re.search(
            rf"{_FD_LOCATION_LABEL}\s*(.*?)(?:\s+{_FD_DESCRIPTION_LABEL}|$)", raw
        )
        if loc_m:
            location = _scrub_field(loc_m.group(1).strip()) or None
        desc_m = re.search(rf"{_FD_DESCRIPTION_LABEL}\s*(.*)$", raw)
        if desc_m:
            description = _scrub_field(desc_m.group(1).strip()) or None
        # Column amounts are positional — see _schedule_a_amounts for the
        # None-value shift and the SPEC §2.2 wrap repair. ``col_spans`` are the
        # character ranges those columns occupy in ``raw`` (GH-0099).
        type_m = _FD_TYPE_TAG_RE.search(raw)
        asset_type_raw = type_m.group(1) if type_m else None
        value_of_asset, income_type, income_amount, income_preceding, col_spans = (
            _schedule_a_amounts(raw)
        )
        # The owner column (SP/DC/JT) prints right after the [TYPE] tag on a
        # single-line row, after the subholding arrow ⇒ when the name wraps, or —
        # when the tag itself wrapped off the value line (GH-0100) — right before
        # the value low. Each lands *inside* the asset slice; match all three.
        owner_m = (
            _FD_OWNER_AFTER_TYPE_RE.search(raw)
            or _FD_OWNER_AFTER_ARROW_RE.search(raw)
            or _FD_OWNER_BEFORE_VALUE_RE.search(raw)
        )
        owner = owner_m.group(1).upper() if owner_m else None
        # The asset name is everything up to the [TYPE] tag — but on wrapped /
        # subholding (⇒) rows the value / income-type / income column text
        # renders physically *between* the name fragments, so it falls inside
        # that slice (GH-0099). Lift the already-parsed column spans (plus the
        # owner token and trailing checkbox glyph) back out, then collapse the
        # gaps they leave, so the structured ``asset`` is the clean name only.
        slice_end = type_m.start() if type_m else len(raw)
        # _strip_spans clips/drops anything past slice_end, so the owner and
        # glyph spans can be appended unconditionally alongside the column spans.
        strip = list(col_spans)
        if owner_m:
            strip.append(owner_m.span())
        strip += [g.span() for g in _FD_GLYPH_TOKEN_RE.finditer(raw, 0, slice_end)]
        asset = _scrub_field(_strip_spans(raw, slice_end, strip))
        income_type = _scrub_field(income_type)
        items.append(
            ScheduleAItem(
                asset=asset,
                owner=owner,
                asset_type=_normalize_asset_type(asset_type_raw),
                asset_type_raw=asset_type_raw,
                value_of_asset=value_of_asset,
                income_type=income_type,
                income_amount=income_amount,
                income_preceding=income_preceding,
                location=location,
                description=description,
                raw_text=_scrub_raw_text(raw),
            )
        )
    return items


# A B transaction row's column signature: the Date column (the Clerk prints
# single-digit days/months unpadded — ``05/5/2020``, ``04/8/2021``), the tx-type
# letter (small-caps can lower-case it, as on PTR rows), then the amount
# column's start (``$lo`` or ``Over``). Requiring the amount start is what keeps
# this from false-anchoring a wrapped asset *name* that happens to contain a
# date (a bond's maturity, say) — a name never continues ``<date> P $…``. The
# date and type are captured so field extraction can use the SAME match that
# anchored the row (never a stray date earlier in the asset name).
_FD_B_ROW_RE = re.compile(
    r"\b(\d{1,2}/\d{1,2}/\d{4})\s+([Ss] \(partial\)|[PpSsEe])\s+(?:\$[\d,]|Over\b)"
)
# A subholding B row: the arrow plus the type+amount columns identify it. The
# Date column is deliberately NOT required — filers put periodicity words there
# (``457 Sooner Savings ⇒ Semi-Annually S $15,001 - …``), and such a row still
# has the type letter and amount. A bare ``⇒`` is NOT an anchor — a wrapped
# parent name can put the arrow on a continuation line among wrapped high
# bounds (same artifact as Schedule A's ``_FD_A_ROW_AFTER_ARROW_RE``).
_FD_B_ARROW_ROW_RE = re.compile(
    r"⇒.*?\b([Ss] \(partial\)|[PpSsEe])\s+(?:\$[\d,]|Over\b)"
)
# A B row's Date column value (same unpadded forms; strptime's %m/%d accepts
# unpadded components) — used on ⇒-anchored rows, whose asset side never
# carries the column signature.
_FD_B_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")


def _parse_schedule_b(
    lines: list[str], *, max_year: int = FALLBACK_MAX_YEAR
) -> list[ScheduleBItem]:
    """Schedule B (transactions) → structured items + raw_text.

    A B row carries the asset's ``[TYPE]`` tag, a date, a ``P``/``S`` type and
    an amount; a subholding row leads with the owner arrow ``⇒``. The asset name
    often wraps onto the *following* line(s), so item-start anchors on the
    ``⇒``-bearing line **or** the date+type+amount column signature
    (``_FD_B_ROW_RE``) — a directly-held asset's row has no arrow at all
    (GH-0070), and anchoring on the arrow alone merged every such transaction
    into the preceding item.
    """

    def starts(s: str) -> bool:
        if _FD_DETAIL_RE.match(s):
            return False
        if _FD_GLYPH_RE.search(s):
            # The cap-gains checkbox is the row's last column — a
            # glyph-terminated line IS a row line, except a stranded glyph
            # alone (a wrapped checkbox remnant folds into its row).
            return not _PTR_GLYPH_ONLY_RE.match(s)
        return bool(_FD_B_ROW_RE.search(s) or _FD_B_ARROW_ROW_RE.search(s))

    items: list[ScheduleBItem] = []
    for raw in _group_items(lines, starts_item=starts):
        # Transaction type tokens normalize as on PTR rows: small-caps can
        # lower-case the letter; ``S (partial)`` keeps its marker. The ``\b``
        # must sit on the *letter* branch only — a trailing ``\b`` after
        # ``S (partial)`` never matches (``)`` then space is not a word
        # boundary), which silently collapsed every partial sale to a bare S.
        token = None
        if "⇒" in raw:
            owner_m = re.search(r"⇒\s*(SP|DC|JT)?\b", raw, re.IGNORECASE)
            owner = (
                owner_m.group(1).upper() if owner_m and owner_m.group(1) else None
            )
            # The Date column is the first date after the asset name; the type
            # letter follows it. (A subholding row's asset side never carries
            # the column signature, so the first date IS the Date column.)
            # Filers can put a periodicity word there instead of a date
            # (``⇒ Semi-Annually S $15,001 - …``): then the date degrades to
            # None and the type comes from the type+amount signature.
            date_m = _FD_B_DATE_RE.search(raw)
            asset = _scrub_field(raw.split("⇒", 1)[0].strip())
            if date_m:
                tt_m = re.match(
                    r"[Ss] \(partial\)|[PpSsEe]\b", raw[date_m.end() :].lstrip()
                )
                token = tt_m.group(0) if tt_m else None
            else:
                tt_m = _FD_B_ARROW_ROW_RE.search(raw)
                token = tt_m.group(1) if tt_m else None
        else:
            # Directly-held row (GH-0070): the owner column (if any) sits right
            # after the [TYPE] tag, as on Schedule A — and the date/type come
            # from the SAME column-signature match that anchored the row, so a
            # date embedded in the asset name (a bond's maturity) can never be
            # mistaken for the Date column. A salvaged pre-anchor item has no
            # signature match and correctly degrades to None.
            after_type = _FD_OWNER_AFTER_TYPE_RE.search(raw)
            owner = after_type.group(1).upper() if after_type else None
            date_m = _FD_B_ROW_RE.search(raw)
            asset = _scrub_field(raw[: date_m.start()].strip() if date_m else raw)
            token = date_m.group(2) if date_m else None
        # Sanity-range the Date column (GH-0113): a date with an implausible year
        # (a transposed-digit extraction artifact) is set None with its raw string
        # kept on ``transaction_date_raw`` — the per-row anomaly flag — rather than
        # emitted as a valid date. A row with no date column stays None/None.
        date_raw_str = date_m.group(1) if date_m else None
        transaction_date = (
            parse_disclosure_date(date_raw_str, max_year=max_year)
            if date_raw_str
            else None
        )
        transaction_date_raw = (
            date_raw_str
            if date_raw_str and transaction_date is None
            else None
        )
        ttype = None
        if token:
            ttype = "S(partial)" if "(partial)" in token else token[0].upper()
            # The "(partial)" marker can wrap onto the asset-name continuation
            # line, detached from its S; only sales carry it, so an S row whose
            # raw text holds the marker anywhere is a partial sale.
            if ttype == "S" and "(partial)" in raw:
                ttype = "S(partial)"
        type_m = _FD_TYPE_TAG_RE.search(raw)
        asset_type_raw = type_m.group(1) if type_m else None
        glyph_m = re.search(r"\bgfedcb?\b", raw)
        cap_gains = (glyph_m.group(0) == "gfedcb") if glyph_m else None
        # The amount column, wrap-aware: the first column-amount entry over the
        # pre-detail text (a wrapped high bound can sit past the checkbox glyph
        # — ``… S $1,000,001 - gfedcb $5,000,000``; B has one amount column, so
        # the first entry is it).
        entries, _ = _fd_amount_entries(_fd_columns(raw))
        items.append(
            ScheduleBItem(
                asset=asset,
                owner=owner,
                asset_type=_normalize_asset_type(asset_type_raw),
                asset_type_raw=asset_type_raw,
                transaction_date=transaction_date,
                transaction_date_raw=transaction_date_raw,
                transaction_type=ttype,
                amount_range=entries[0][2] if entries else None,
                cap_gains_over_200=cap_gains,
                raw_text=_scrub_raw_text(raw),
            )
        )
    return items


# Schedule C income-Type vocabulary (GH-0101). The Type column is a small,
# closed set of phrases on the live form; these are the values attested on the
# committed fixtures (Thompson: ``Retirement Plan``, ``Pension``, ``Annuity
# Plan``; Hackett candidate: ``Salary``) and in the issue's cited filings
# (``Spouse Salary``, ``Professional Services``, ``Spouse Pension``; ``Pension
# Plan`` attested on GH-0131's 10068086 / 10057260). Longest phrases must precede
# their prefixes so the alternation is greedy-correct (``Retirement Plan`` before
# ``Pension``/``Salary``; ``Pension Plan`` before ``Pension``). An UNKNOWN Type is *not*
# in this list — the caller then falls back to the single-token split so the row
# still divides (and ``raw_text`` keeps it whole regardless): never dropped.
_FD_C_TYPE_PHRASES = (
    "Retirement Plan",
    "Annuity Plan",
    # ``Pension Distribution`` / ``Pension Plan`` must precede bare ``Pension`` so
    # the longest attested Type wins — Raskin ``10068086`` C[2] is ``Barbara
    # Raskin Pension Plan | Pension Distribution`` (a source that itself ends
    # ``Pension Plan``), so the trailing-anchored match must claim ``Pension
    # Distribution`` and leave the source intact (GH-0166 / GH-0162).
    "Pension Distribution",
    "Pension Plan",
    "Professional Services",
    # ``Speech/panel fee`` / ``Speech fee`` — Moore ``10062886`` C activity-fee
    # Types (GH-0166 / GH-0162). Listed before bare ``Fees`` so the multi-word
    # phrase wins; the ``/`` form precedes the plain one (longest-first).
    "Speech/panel fee",
    "Speech fee",
    "Salary",
    "Pension",
    "Annuity",
    "Bonus",
    "Commission",
    "Director Fees",
    "Director's Fees",
    "Fees",
    "Honoraria",
    "Partnership Income",
    "Severance",
    # Tax-form Type labels filers enter verbatim (Raskin/Moore — GH-0162). The
    # form numbers are a closed set; matched case-insensitively like the rest.
    "1099-NEC",
    "1099-MISC",
    "Wages",
    "Distribution",
)
# An optional owner token (the owner column renders folded in front of the Type:
# ``Member Retirement Plan`` / ``Spouse Pension`` — issue example Davis is
# ``Spouse Salary``) then one vocabulary phrase, anchored to the head's end so it
# only claims the trailing Type column, never a phrase buried in the source name.
# The owner token renders in either case on the live form (``Spouse`` and the
# all-caps ``SPOUSE``); GH-0101 only listed ``Spouse``, so an all-caps owner bled
# its token into ``source`` (GH-0131). Both casings are owner tokens, not source.
_FD_C_OWNER_TOKENS = ("Member", "Spouse", "SPOUSE", "SP", "DC", "JT")
_FD_C_TYPE_RE = re.compile(
    r"\s((?:(?:" + "|".join(_FD_C_OWNER_TOKENS) + r")\s+)?(?:"
    + "|".join(re.escape(p) for p in _FD_C_TYPE_PHRASES)
    + r"))\s*$"
)


def _parse_schedule_c(lines: list[str]) -> list[ScheduleCItem]:
    """Schedule C (earned income) → structured items + raw_text.

    Columns are ``Source | Type | Amount [| Preceding-year amount]``. Each row is
    one physical line; the trailing money/``N/A`` token(s) are the amount, the
    ``Type`` phrase before them the income type, the remainder the source. The
    member form has one Amount column → ``amount``; the Candidate/New-Filer form
    has two (current-year-to-filing, then preceding-year) → ``amount`` +
    ``amount_preceding`` (GH-0161), kept apart rather than space-joined.

    The Type column is *multi-word* on real forms (``Retirement Plan``, ``Annuity
    Plan``, ``Professional Services``) and may carry the owner column folded in
    front of it (``Spouse Pension``, ``Member Retirement Plan``). A naive
    last-whitespace split bleeds the Type's leading word(s) into ``source`` and
    truncates ``income_type`` to its final word (GH-0101). We match the Type from
    a small vocabulary of phrases attested on the committed fixtures / the issue,
    longest-first, optionally prefixed by an owner token — and fall back to the
    single-token split for an *unknown* Type so it still divides somewhere rather
    than being dropped (the verbatim ``raw_text`` carries the whole row either way).
    """
    items: list[ScheduleCItem] = []
    for raw in _group_items(lines, starts_item=lambda s: True):
        # Trailing amount(s): one money / N/A token (member form, one Amount
        # column) or two (Candidate/New-Filer form: "Amount Current Year to
        # Filing" then "Amount Preceding Year" — GH-0161). Captured as two groups
        # so the candidate form's columns are kept apart instead of space-joined
        # into one unparseable string; ``amount`` is the current/only column,
        # ``amount_preceding`` the candidate-only second column (None on the
        # member form).
        amt_m = re.search(
            r"(\$[\d,]+(?:\.\d+)?|N/A)(?:\s+(\$[\d,]+(?:\.\d+)?|N/A))?\s*$",
            raw,
        )
        amount = amt_m.group(1).strip() if amt_m else None
        amount_preceding = (
            amt_m.group(2).strip() if amt_m and amt_m.group(2) else None
        )
        head = raw[: amt_m.start()].strip() if amt_m else raw.strip()
        # Prefer a known multi-word Type phrase (with any owner prefix) at the
        # tail; only when none is attested do we fall back to the last token.
        type_m = _FD_C_TYPE_RE.search(head)
        if type_m:
            source = head[: type_m.start()].strip()
            income_type = type_m.group(1).strip()
        else:
            parts = head.rsplit(" ", 1)
            if len(parts) == 2 and parts[1]:
                source, income_type = parts[0].strip(), parts[1].strip()
                # An open-vocabulary Type (not in _FD_C_TYPE_PHRASES) still carries
                # the owner column folded in front of it — ``Naborforce Spouse
                # wage`` / ``… Spouse salary`` (GH-0131 strand). The last-token
                # split is owner-blind, so the owner token lands at the tail of
                # ``source``. Peel it back onto ``income_type`` so the owner stays
                # with the Type column, never the Source.
                src_parts = source.rsplit(" ", 1)
                if len(src_parts) == 2 and src_parts[1] in _FD_C_OWNER_TOKENS:
                    source = src_parts[0].strip()
                    income_type = f"{src_parts[1]} {income_type}"
            else:
                source, income_type = head, None
        items.append(
            ScheduleCItem(
                source=_scrub_field(source) or _scrub_field(raw),
                income_type=_scrub_field(income_type),
                amount=amount,
                amount_preceding=amount_preceding,
                raw_text=_scrub_raw_text(raw),
            )
        )
    return items


# A Schedule D ``Date incurred`` — ``Month DD, YYYY``, ``Month YYYY``,
# ``MM/DD/YYYY``, or ``MM/YYYY`` (longer date forms first so the most specific
# match wins). Used both to anchor a liability row's item-start and to extract the
# date, so the two always agree. The optional ``\d{1,2},`` day-comma group lets the
# ``Month DD, YYYY`` form be consumed *whole* (GH-0134): otherwise ``Month\s+\d{4}``
# cannot match the ``Month DD`` head, the bare-year fallback matches only the
# trailing ``YYYY``, and the ``Month DD,`` fragment leaks into ``creditor``.
_FD_DATE_RE = re.compile(
    r"\b("
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(?:\d{1,2},\s+)?\d{4}"
    # Abbreviated month (``Jan 2022`` — GH-0148): the 3-letter forms filers
    # enter alongside the spelled-out month. Listed after the full names so
    # ``January 2016`` matches the spelled-out branch; the trailing whitespace
    # requirement keeps ``Jan`` from claiming the head of ``January``. Without
    # this the bare-year fallback anchored on the year and ``Jan`` leaked into
    # ``creditor`` (``SoFi Jan``).
    r"|(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{4}"
    # A free-text ``Date incurred`` — filers write prose like ``Various dates
    # in 2022`` (GH-0148). The year may be displaced by the column re-flow, so
    # the prose phrase (with any contiguous trailing year) is the date span;
    # anchoring on the bare year instead let the prose + type + amount low
    # bound all collapse into ``creditor``.
    r"|Various dates(?:\s+in)?(?:\s+\d{4})?"
    r"|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}/\d{4})\b"
)


# A bare-year ``Date incurred`` (``2019``) — filers enter these alongside the
# fuller ``Month YYYY``/``MM/YYYY`` forms (GH-0070). The century prefix plus the
# word boundaries keep it from matching inside a formatted amount (the comma
# grouping breaks any 4-digit run) or a longer number.
_FD_D_BARE_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
# A captured ``Date incurred`` that is missing its year (GH-0166 / GH-0165): the
# free-text ``Various dates [in]`` form whose trailing year wrapped into the
# amount column. Used to decide whether to recover a stranded bare year from the
# type slice and re-attach it to the date.
_FD_D_DATE_NEEDS_YEAR_RE = re.compile(
    r"Various dates(?:\s+in)?\s*$", re.IGNORECASE
)
# A per-row ``COMMENTS:`` marker matched mid-string (the de-wrapped row folds the
# comment line in after the columns): a plain ``C :``/``C:`` or the glyphs-lost
# ``C\x00+:`` run. Schedules with no comment field (D) truncate their last column
# here so the comment never bleeds in (GH-0166 / GH-0165). The leading space (or
# string start) keeps it from matching a ``C:`` inside a column value.
_FD_COMMENT_ANYWHERE_RE = re.compile(r"(?:^|\s)C(?:\x00+|\s*):")
# A Schedule-D dangling value-range low bound — a ``$lo -`` whose high bound
# spilled past an intruding column, broadened (vs the A/B opener) to also accept a
# *digit* after the dash, not just a letter. On a free-text/wrapped
# date the re-flow can land the displaced date year between the low bound and its
# dash and the high bound (Schiff: ``Margin loan on portfolio $100,001 - 2022
# $250,000``), so the follower is a digit, which the letter-only A/B pattern
# misses — leaving ``amount_range`` null. A contiguous ``$lo - $hi`` bucket still
# has ``$`` (not ``[A-Za-z\d]``) after the dash, so it is unaffected and takes the
# bucket path. D-local: the A/B value-column logic keeps its stricter pattern.
_FD_D_DANGLING_LOW_RE = re.compile(r"\$([\d,]+)\s+-\s+(?=[A-Za-z\d])")
# The amount column's *range start* (``$lo -`` or ``Over``) — the corroborating
# signal a bare-year D anchor requires (below). The dash is load-bearing: a
# wrapped continuation line can carry a bare year AND the row's wrapped high
# bound (``Established 1999 Branch $500,000``), but only a real row carries the
# range's own ``$lo -`` opening.
_FD_RANGE_START_RE = re.compile(r"\$[\d,]+\s*-|Over\b")


def _fd_d_amount(
    raw: str,
) -> tuple[AmountRange | None, int, list[tuple[int, int]]]:
    """Resolve a Schedule D row's Amount column → ``(range, type_end, strip)``.

    The Amount-of-Liability column sits to the right of the Type column. When the
    Type wraps to a second physical line, pdfplumber re-flows the columns
    left-to-right and the amount's **low bound and dash land *between* the type
    fragments**, with the high bound after the wrapped type continuation
    (GH-0102): ``Mortgage on Rental Property, $100,001 - Washington, DC
    $250,000``. ``_FD_AMOUNT_RE`` then sees no contiguous ``$lo - $hi`` bucket and
    the row silently emits ``amount_range: null`` with the amount tokens swept
    into ``liability_type`` — a present-but-invisible liability amount.

    Returns the resolved range (or ``None``), the offset at which the Type column
    ends, and the character spans to **lift out** of the type slice (the displaced
    amount tokens — a wrapped low+dash and/or high bound that landed amid the type
    text).

    The wrapped-Type case (GH-0102) is why a single ``raw[date:amount-start]``
    slice is wrong: the type spans *both sides* of the amount low bound, so the
    type slice must run to the row end with the amount tokens carved out, not
    truncate at the first ``$``.

    When the amount is **present but unparseable** (a dangling low with no
    recoverable high) the displaced tokens are *not* carved out — they stay in
    ``liability_type`` so the present-but-unparsed amount never vanishes from every
    structured field at once (it would otherwise survive only in ``raw_text``); the
    loss stays visible (CLAUDE.md "never silently drop"), matching Schedule A's
    degrade-but-hold-the-slot rule.
    """
    # 1) A contiguous ``$lo - $hi`` bucket — the unwrapped (or already de-wrapped
    #    contiguous) case. Type ends at its start; nothing displaced.
    bucket = _FD_AMOUNT_RE.search(raw)
    # 2) A dangling low (``$lo -`` with a word or displaced year, not ``$hi``,
    #    right after) — the wrapped-Type / wrapped-date signature: the high bound
    #    spilled past the intruding column continuation.
    dangling = _FD_D_DANGLING_LOW_RE.search(raw)
    if bucket and not (dangling and dangling.start() < bucket.start()):
        # The first complete bucket precedes any dangling low → it is the amount.
        # Type ends at the bucket start; the bucket itself is excluded by the end.
        return _bucket(bucket.group(0)), bucket.start(), []
    if dangling:
        # The dangling-low span (``$lo -``) plus a recovered high bound are amount
        # tokens that landed *inside* the wrapped type, so the type slice runs to
        # the row end and carves them out (lifting them rejoins the split type).
        high_m = _FD_BARE_AMOUNT_RE.search(raw, dangling.end())
        if high_m:
            # _bucket already normalizes the dash spacing of the joined label.
            rng = _bucket(f"{dangling.group(0)}{high_m.group(0)}")
            return rng, len(raw), [(dangling.start(), dangling.end()), high_m.span()]
        # Amount tokens present (a ``$lo -`` opens the column) but no high bound
        # recovered — unparseable, not absent. Leave the low *in* the type slice
        # (no carve) so the present amount stays visible in a structured field and
        # never collapses silently to ``amount_range: null``; ``raw_text`` carries
        # the verbatim row regardless.
        return None, len(raw), []
    return None, len(raw), []


def _parse_schedule_d(lines: list[str]) -> list[ScheduleDItem]:
    """Schedule D (liabilities) → structured items + raw_text.

    Columns are ``Owner | Creditor | Date incurred | Type | Amount``. The amount
    range wraps onto the next line, so item-start anchors on the row bearing a
    ``Date incurred`` and folds the wrapped amount in. A bare-year date
    (``… Loan 2019 Personal Loan $15,001 - …`` — GH-0070) anchors only when the
    line also carries the amount column's *range start*: a year alone (or a
    year next to a wrapped bare high bound) can sit on a continuation line, but
    only a real row opens its own range.
    """

    def starts(s: str) -> bool:
        if _FD_DATE_RE.search(s):
            return True
        return bool(_FD_D_BARE_YEAR_RE.search(s) and _FD_RANGE_START_RE.search(s))

    items: list[ScheduleDItem] = []
    for raw in _group_items(lines, starts_item=starts):
        owner_m = re.match(r"(SP|DC|JT)\b", raw, re.IGNORECASE)
        owner = owner_m.group(1).upper() if owner_m else None
        # The fuller date forms win; a bare year is the fallback (and is what
        # anchored the row when no fuller form is present).
        date_m = _FD_DATE_RE.search(raw) or _FD_D_BARE_YEAR_RE.search(raw)
        date_incurred = date_m.group(1) if date_m else None
        # Creditor = text between any owner token and the date.
        start = owner_m.end() if owner_m else 0
        end = date_m.start() if date_m else len(raw)
        creditor = _scrub_field(raw[start:end].strip())
        # Amount column resolved BEFORE the type is read: a wrapped Type column
        # interleaves the amount's low/high bounds among the type fragments
        # (GH-0102), so the amount tokens must be located and carved out of the
        # type slice — otherwise they sweep into ``liability_type`` and the row
        # silently emits ``amount_range: null`` (the present amount, invisible).
        amount_range, type_end, amt_spans = _fd_d_amount(raw)
        # A per-row ``COMMENTS:`` line folds onto the de-wrapped row after the
        # columns (Schiff D[1]: ``… MD $500,000 C : Mortgage was acquired …``).
        # Schedule D has no comment/description field, so the comment must never
        # bleed into ``liability_type`` (GH-0166 / GH-0165) — truncate the type
        # slice at the comment marker; ``raw_text`` keeps it verbatim.
        comment_m = _FD_COMMENT_ANYWHERE_RE.search(raw)
        if comment_m:
            type_end = min(type_end, comment_m.start())
        ltype = None
        if date_m:
            # Type = text from the date to ``type_end`` with the displaced amount
            # tokens carved out. Rebase the spans to the post-date slice so a
            # wrapped low/high that landed mid-type is removed, rejoining the type.
            d_end = date_m.end()
            # Wrapped ``Date incurred`` year (GH-0166 / GH-0165): a free-text date
            # (``Various dates in``) whose year wrapped into the amount column
            # (Schiff D[0]: ``Various dates in … $100,001 - 2022 $250,000``) leaves
            # the date yearless and the displaced ``2022`` stranded in the type
            # slice. The amount carve already removed the ``$`` tokens around it;
            # lift the bare year out of the type and append it to the date so both
            # fields read true.
            year_span: tuple[int, int] | None = None
            if _FD_D_DATE_NEEDS_YEAR_RE.search(date_incurred or ""):
                year_m = _FD_D_BARE_YEAR_RE.search(raw, d_end, type_end)
                if year_m:
                    date_incurred = f"{date_incurred} {year_m.group(1)}"
                    year_span = year_m.span()
            type_spans = [(s - d_end, e - d_end) for s, e in amt_spans]
            if year_span is not None:
                type_spans.append((year_span[0] - d_end, year_span[1] - d_end))
            ltype = _scrub_field(
                _strip_spans(raw[d_end:], type_end - d_end, type_spans) or None
            )
        items.append(
            ScheduleDItem(
                creditor=creditor or _scrub_field(raw),
                owner=owner,
                date_incurred=date_incurred,
                liability_type=ltype,
                amount_range=amount_range,
                raw_text=_scrub_raw_text(raw),
            )
        )
    return items


# --- Schedules E–J: per-schedule structured columns (#17) --------------------
#
# E–J were raw_text-only in #12; #17 column-parses each, ordered by real-data
# fill rate (positions/agreements/gifts/travel are denser than honoraria and the
# new-filer-only Schedule J). The live form's column headers (verified on the
# committed fixtures) drive each split: E ``Position | Name of Organization``,
# F ``Date | Parties | Terms``, G ``Source | Description | Value``, H ``Source |
# Dates | Location | Items``, I ``Source | Activity | Date | Amount``, J ``Source
# | Description of Duties``. The columns are space-separated on extraction with
# no stable delimiter, so each parser splits only on signals it can read with
# confidence (a leading ``Month YYYY`` date, a trailing dollar figure, a known
# position-title prefix); anything it cannot bisect leaves the field ``None`` and
# the verbatim ``raw_text`` still carries the row in full — completeness over the
# known, explicit residual in the text (CLAUDE.md).

# A trailing dollar figure (gift/honoraria value) at the (de-wrapped) row end.
_FD_TRAILING_DOLLAR_RE = re.compile(r"(\$[\d,]+(?:\.\d{2})?)\s*$")

# The same dollar figure anywhere in the row — the fallback when the amount is not
# the last column (Schiff's Schedule I row "… Article 04/22/2022 $500.00 Burbank
# Temporary Aid Center": the charity-name tail follows the amount, so the trailing
# anchor misses and the source would be lost). Schedules I/G carry a single figure,
# not a ``$lo - $hi`` range, so no range-guard is needed.
_FD_DOLLAR_RE = re.compile(r"(\$[\d,]+(?:\.\d{2})?)")

# A leading ``Month YYYY`` (or ``MM/YYYY`` / ``MM/DD/YYYY``) agreement/travel date.
_FD_LEADING_DATE_RE = re.compile(
    r"^\s*("
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}/\d{4}"
    # A bare-year agreement date (``2014 GENERAL MOTORS LLC …`` — GH-0070);
    # last so the fuller forms win at the same position.
    r"|(?:19|20)\d{2})\b",
    re.IGNORECASE,
)

# Common Schedule E position titles. A row's leading run of words matching one of
# these (case-insensitively) is the ``Position`` column; the remainder is the
# organization. Conservative: an unrecognized title leaves both fields ``None``
# (raw_text intact) rather than guessing a split point.
_FD_POSITION_TITLES = (
    "board member",
    "board of directors",
    "board of trustees",
    "advisory board",
    "trustee emeritus",
    "trustee",
    "president",
    "vice president",
    "chairman",
    "chairperson",
    "chair",
    "secretary",
    "treasurer",
    "director",
    "officer",
    "partner",
    "member",
    "manager",
    "managing member",
    "owner",
    "co-owner",
    "general partner",
    "limited partner",
    "ceo",
    "cfo",
    "coo",
    "consultant",
    "advisor",
    "adviser",
    "delegate",
    "commissioner",
    "governor",
    # Editorial/board roles attested on real filings (Pan 2023/10055778 row 2
    # ``Editor Telos Press`` — GH-0103 — structured null because the title was
    # absent from this list while row 1 ``Treasurer`` split fine).
    "editor in chief",
    "editor",
    "publisher",
)

# Core role words: a leading word-run is a Position only if it carries at least
# one of these (so an organization name whose first words happen to be connectors
# is never mistaken for a title). Single-word titles from the exact-prefix list,
# reduced to the word a multi-word title hinges on (``board of trustees`` →
# ``trustees``), so the run-matcher recognizes compound/conjoined titles the
# exact list cannot enumerate (``Sole Director & President``, GH-0150).
_FD_POSITION_ROLE_WORDS = frozenset(
    {
        "director",
        "directors",
        "president",
        "chairman",
        "chairperson",
        "chair",
        "secretary",
        "treasurer",
        "trustee",
        "trustees",
        "member",
        "officer",
        "partner",
        "manager",
        "owner",
        "ceo",
        "cfo",
        "coo",
        "consultant",
        "advisor",
        "adviser",
        "delegate",
        "commissioner",
        "governor",
        "editor",
        "publisher",
    }
)

# Words that may sit inside the Position run beside a role word — conjunctions,
# articles, and the rank/scope modifiers that prefix a role (``Sole``/``Vice``/
# ``Finance Committee`` …). The run stops at the first word in neither set, which
# is the start of the organization column.
_FD_POSITION_CONNECTORS = frozenset(
    {
        "&",
        "and",
        "of",
        "the",
        "sole",
        "vice",
        "co",
        "deputy",
        "assistant",
        "acting",
        "interim",
        "managing",
        "general",
        "limited",
        "executive",
        "advisory",
        "finance",
        "audit",
        "nominating",
        "governance",
        "committee",
        "board",
        "emeritus",
        "in",
    }
)


def _split_position(raw: str) -> tuple[str | None, str | None]:
    """Split a Schedule E row into ``(position, organization)``.

    The form's two columns merge on extraction with no delimiter, so we split
    only on a recognized opening. First the exact title list (``President`` /
    ``Board of Trustees``); failing that, a leading run of role/connector words
    that carries at least one role word (``Sole Director & President`` /
    ``Finance Committee Member`` — GH-0150) — these compound and conjoined titles
    the flat list cannot enumerate, yet the leading block of a real filing shares
    them across many rows. An unrecognized opening leaves both ``None`` (raw_text
    still carries the row).
    """
    low = raw.lower()
    for title in _FD_POSITION_TITLES:
        if low.startswith(title) and len(raw) > len(title):
            nxt = raw[len(title) : len(title) + 1]
            if nxt == " ":  # a real word boundary, not a longer word
                org = raw[len(title) :].strip()
                return raw[: len(title)].strip(), org or None
    # Run-matcher fallback: walk the leading words while each is a role or
    # connector word, stopping at the organization. Trailing punctuation
    # (``Director,`` / ``President,``) is ignored for the membership test.
    words = raw.split()
    end = 0
    has_role = False
    for w in words:
        key = w.lower().strip(".,;:")  # "&" stays (it is a connector)
        if key in _FD_POSITION_ROLE_WORDS:
            has_role = True
        elif key not in _FD_POSITION_CONNECTORS:
            break
        end += 1
    if has_role and 0 < end < len(words):
        position = " ".join(words[:end]).rstrip(",")
        organization = " ".join(words[end:]).strip()
        return position or None, organization or None
    return None, None


# A per-row ``COMMENTS`` continuation line. The filer's free-text note renders on
# its own line led by the form's small-caps ``C`` glyph (lost to NULs — ``C\x00+:``)
# or, on an intact rendering, a plain ``C :`` / ``C:``. It is NOT a new position —
# it annotates the row above — so it must fold into its parent item, never anchor
# its own row (GH-0150). (The NUL form is also kept out of ``_FD_TRAILER_RE`` by
# that pattern's ``(?!:)`` lookahead, which is what lets it reach this parser.)
_FD_E_COMMENT_RE = re.compile(r"^C(?:\x00+|\s*):", re.IGNORECASE)


def _parse_schedule_e(lines: list[str]) -> list[ScheduleEItem]:
    """Schedule E (positions) → ``position``/``organization`` + raw_text."""
    items: list[ScheduleEItem] = []
    for raw in _group_items(
        lines, starts_item=lambda s: not _FD_E_COMMENT_RE.match(s)
    ):
        scrubbed = _scrub_raw_text(raw)
        position, organization = _split_position(scrubbed)
        items.append(
            ScheduleEItem(
                position=position,
                organization=organization,
                raw_text=scrubbed,
            )
        )
    return items


# Schedule F Terms-column opening keywords. The ``Parties | Terms`` boundary has
# no delimiter, but the Terms free-text reliably opens with one of a small set of
# agreement-description leads (verified across the cited filings: ``Health
# coverage``, ``Pension``, ``Publishing agreement``, ``Restricted Shares``,
# ``Contract for``, ``401(k) plan``, ``I was employed``). A row whose post-date
# text contains one of these (not at position 0 — the Parties column comes first)
# splits there: Parties before, Terms from the keyword on. Conservative: a row
# with no recognized Terms lead leaves ``terms`` None and ``parties`` holds the
# whole post-date text, with ``raw_text`` carrying the verbatim row regardless.
_FD_F_TERMS_LEAD_RE = re.compile(
    r"\b(Health\s+coverage|Pension|Retirement|Publishing\s+agreement|"
    r"Restricted\s+Shares|Contract\s+for|Consulting|Severance|Deferred|"
    r"Employment|Compensation|Royalt|Salary|401\(k\)|I\s+was\s+employed|"
    r"Agreement\s+to|Continued\s+participation)",
    re.IGNORECASE,
)


def _split_parties_terms(rest: str) -> tuple[str | None, str | None]:
    """Best-effort ``Parties | Terms`` split of a Schedule F row's post-date text.

    Splits at the first recognized Terms-opening keyword (``_FD_F_TERMS_LEAD_RE``)
    that is not at the very start (the Parties column always comes first). When no
    keyword is found the whole text is the Parties column and Terms stays ``None``
    — the verbatim ``raw_text`` carries the row in full either way (GH-0163;
    completeness over the known, residual in the text — CLAUDE.md)."""
    if not rest:
        return None, None
    m = _FD_F_TERMS_LEAD_RE.search(rest)
    if m and m.start() > 0:
        parties = rest[: m.start()].strip(" ;,") or None
        terms = rest[m.start() :].strip() or None
        return parties, terms
    return rest, None  # non-empty past the guard above — whole text is Parties


def _f_starts(s: str) -> bool:
    """A Schedule F row opens on a leading Date-column date — but NOT on a date
    embedded in wrapped Terms prose (GH-0166 / GH-0163).

    The F Date column is a ``Month YYYY`` or bare-year value (verified across the
    cited filings); the row's Terms free-text routinely contains an ``MM/DD/YYYY``
    that wraps to a line start (Moore: ``…scheduled to vest on`` / ``08/23/2023.
    Currently valued at $80,731.50.``). Anchoring on any leading date split that
    one agreement into phantom rows. A prose date carries a trailing ``.``/``,``
    (it sits mid-sentence) and is a slash form, never the ``Month YYYY`` Date
    column — so reject a leading date immediately followed by sentence
    punctuation.
    """
    m = _FD_LEADING_DATE_RE.match(s)
    if not m:
        return False
    return s[m.end() : m.end() + 1] not in (".", ",")


def _parse_schedule_f(lines: list[str]) -> list[ScheduleFItem]:
    """Schedule F (agreements) → ``date``/``parties``/``terms`` + raw_text.

    A row anchors on a leading Date-column date (``_f_starts`` — never an
    ``MM/DD/YYYY`` buried in wrapped Terms prose, GH-0163) and folds the heavily
    wrapping Terms continuation lines in. Columns are ``Date | Parties | Terms``;
    the Parties|Terms boundary has no stable delimiter, so the split is
    best-effort on the parties column's natural terminators (``;`` / ``&``
    join lists; a known Terms-opening keyword starts the Terms column). When no
    boundary reads cleanly ``terms`` stays ``None`` and ``raw_text`` carries the
    whole row (completeness over the known, residual in the text — CLAUDE.md).
    """
    items: list[ScheduleFItem] = []
    for raw in _group_items(lines, starts_item=_f_starts):
        scrubbed = _scrub_raw_text(raw)
        date_m = _FD_LEADING_DATE_RE.match(scrubbed)
        agreement_date = date_m.group(1) if date_m else None
        parties = terms = None
        if date_m:
            rest = scrubbed[date_m.end() :].strip()
            parties, terms = _split_parties_terms(rest)
        items.append(
            ScheduleFItem(
                date=agreement_date,
                parties=parties,
                terms=terms,
                raw_text=scrubbed,
            )
        )
    return items


# A Schedule H ``Dates`` column: a single travel date or a start/end date *range*.
# Two date forms join the range: the older single-``Dates``-column dash form
# (``06/01/2020 - 06/03/2020``) and the modern split ``Start``/``End Date`` columns
# that extract space-adjacent (``01/21/2022 01/21/2022`` — #146), so the separator
# is a dash OR plain whitespace. The range tail is matched greedily so the whole
# span (both endpoints) lands in ``dates``, not just the start. ``Month YYYY`` and
# the ``MM/DD/YYYY``/``M/YYYY`` forms mirror the other schedules' date vocabularies
# (1499, 1660); the range tail is optional for a single date. A **yearless** ``M/D``
# form is deliberately NOT matched: this regex also gates row-anchoring
# (``starts_item``), so a bare ``1/2`` / ``9/11`` in a wrapped Location/Items
# continuation line would otherwise spuriously anchor a new trip and fabricate a
# ``dates`` value the filer never wrote (GH-0103 critic). Both endpoints carry a
# year, so the whitespace separator cannot fuse a date with a bare year in the
# itinerary that follows.
_FD_H_DATE = (
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}/\d{4}"
)
_FD_H_DATES_RE = re.compile(
    rf"(?P<dates>(?:{_FD_H_DATE})(?:(?:\s*[-–]\s*|\s+)(?:{_FD_H_DATE}))?)",
    re.IGNORECASE,
)


# The ``Days at Personal Expense`` column — a small integer between the Itinerary
# and the inclusion checkboxes (verified ``0`` on the cited filings). On a wrapped
# trip it is the de-wrapped boundary between the first physical line's tail
# (Itinerary part 1) and the wrapped continuation (the rest of the Source name and
# Itinerary). Anchored to a standalone 1–2 digit run so a year inside a city
# (none occur) or an amount never trips it.
_FD_H_DAYS_RE = re.compile(r"\s(\d{1,2})\s")
# US state/territory postal codes plus the countries seen on the cited
# itineraries — the closed ``region`` vocabulary that tells a real ``City,
# Region`` itinerary token (``Palm Beach, FL`` / ``Mumbai, India``) from an
# org-name fragment that merely has a comma (``Institute, Inc`` — GH-0164). Built
# as a vocabulary because ``Inc``/``LLC``/``PC`` look exactly like a region word
# positionally; only the closed set discriminates. Countries are extended as new
# itineraries surface (raw_text always carries the verbatim row regardless).
_US_STATE_CODES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI "
    "WY DC PR VI GU AS MP US USA UK"
).split()
_FD_H_REGIONS = frozenset(_US_STATE_CODES) | {
    "India", "Israel", "Canada", "Mexico", "Japan", "China", "Germany",
    "France", "Italy", "Spain", "Taiwan", "Korea", "England", "Ireland",
}
# A ``City, Region`` itinerary token where ``Region`` is in the closed vocabulary
# above. Only the *last* city word before the comma is matched (``Beach, FL``, not
# ``Palm Beach, FL``): on a wrapped trip a multi-word city is itself split across
# the wrap (``…- Palm`` / ``Beaches Beach, FL…``), so matching minimally puts the
# resume boundary right at the comma-bearing word and leaves the Source-name
# continuation (``Beaches``) ahead of it (GH-0164).
_FD_H_CITY_RE = re.compile(
    r"[A-Z][\w.]*,\s+(" + "|".join(_FD_H_REGIONS) + r")\b"
)
# An itinerary ``-`` leg separator (``Institute, Inc - Baltimore``): the next leg
# of the trip. The surrounding spaces are load-bearing — a hyphenated name token
# (``Tax-Deferred``) has no spaces around its dash, so this only fires between
# itinerary legs.
_FD_H_LEG_RE = re.compile(r"\s-\s")


def _parse_schedule_h(lines: list[str]) -> list[ScheduleHItem]:
    """Schedule H (travel payments) → ``source``/``dates``/``location`` + raw_text.

    Columns are ``Source | Start/End Dates | Itinerary | Days | <inclusions>``. The
    column-header banner is dropped upstream in :func:`_segment_schedules`, so the
    parser sees only rows; each row anchors on the line bearing the ``Dates``
    column and folds wrapped continuation lines in, so one trip is one item (#147).

    Multi-line reconstruction (GH-0166 / GH-0164). A trip's Source name and
    Itinerary both wrap, and pdfplumber's left-to-right re-flow interleaves them
    at the de-wrapped tail: ``Source(p1) <dates> Itin(p1) <days> Source(p2)
    Itin(p2)``. Reading ``source`` as only the pre-dates text truncated multi-line
    source names (``Conservative Partnership`` for ``Conservative Partnership
    Institute, Inc``) and dropped the Itinerary entirely. We rebuild both: the
    ``<days>`` integer is the boundary between Itin(p1) and the wrapped tail; in
    that tail the wrapped Source name precedes the resumed Itinerary, which is
    marked by the first ``City, Region`` token or a ``-`` leg separator. ``source``
    rejoins p1 + the leading tail; ``location`` (the Itinerary) rejoins Itin(p1) +
    the resumed tail. ``items`` has no stable boundary and stays ``None``; a row
    with no recognizable date keeps everything in ``raw_text``.
    """
    items: list[ScheduleHItem] = []
    for raw in _group_items(
        lines, starts_item=lambda s: bool(_FD_H_DATES_RE.search(s))
    ):
        scrubbed = _scrub_raw_text(raw)
        date_m = _FD_H_DATES_RE.search(scrubbed)
        if not date_m:
            items.append(
                ScheduleHItem(
                    source=None, dates=None, location=None, items=None,
                    raw_text=scrubbed,
                )
            )
            continue
        dates = date_m.group("dates").strip()
        source_p1 = scrubbed[: date_m.start()].strip()
        after = scrubbed[date_m.end() :].strip()
        location = None
        source = _scrub_field(source_p1) or None
        days_m = _FD_H_DAYS_RE.search(after)
        if days_m:
            itin_p1 = after[: days_m.start()].strip()
            tail = after[days_m.end() :].strip()
            # In the wrapped tail the Source-name continuation precedes the resumed
            # Itinerary. The Itinerary resumes at the earlier of two signals: a
            # ``- `` leg separator (the next itinerary leg, e.g. Harris ``Institute,
            # Inc - Baltimore``) or a ``City, Region`` token completing a cut-off
            # leg (Schiff ``Beaches`` then ``Beach, FL``; Waltz ``(MECEA)`` then
            # ``Mumbai, India``). The earliest signal bounds Source(p2); the rest
            # is Itinerary(p2). A tail with neither is all Source continuation.
            city_m = _FD_H_CITY_RE.search(tail)
            leg_m = _FD_H_LEG_RE.search(tail)
            cuts = [m.start() for m in (city_m, leg_m) if m]
            if cuts:
                cut = min(cuts)  # the Itinerary resumes here (0 if no Source wrap)
                source_p2 = tail[:cut].strip(" -")
                itin_p2 = tail[cut:].strip()
            else:
                source_p2, itin_p2 = tail, ""  # no itinerary signal — all Source
            source = (
                _scrub_field(f"{source_p1} {source_p2}".strip()) or None
            )
            location = (
                _scrub_field(f"{itin_p1} {itin_p2}".strip(" -")) or None
            )
        # No Days column: the older single-``Dates``-column (dash-range) format
        # carries no Days marker, so the Itinerary|Items boundary is unrecoverable
        # — leave ``location`` None (the by-design merge) and keep the row in
        # ``raw_text``. Only the modern Start/End + Days layout (the GH-0164 cited
        # filings) reconstructs the Itinerary above.
        items.append(
            ScheduleHItem(
                source=source,
                dates=dates,
                location=location,
                items=None,
                raw_text=scrubbed,
            )
        )
    return items


# Schedule J's ``Source (Name and Address)`` column ends at the closing paren of
# the filer's parenthesized address (``Protect Kids Colorado (Colorado Springs,
# CO, US) Professional Project …``); everything after it is the ``Brief
# Description of Duties`` column. The parenthesized address is the only stable
# interior delimiter the two merged columns offer, so we split there and leave
# both fields ``None`` when no address paren is present (raw_text still carries
# the row).
_FD_J_SOURCE_RE = re.compile(r"^(?P<source>.+?\([^)]*\))\s+(?P<description>\S.*)$")


def _parse_schedule_j(lines: list[str]) -> list[ScheduleJItem]:
    """Schedule J (new-filer comp >$5,000) → ``source``/``description`` + raw_text.

    Columns are ``Source (Name and Address) | Brief Description of Duties``. They
    merge with no delimiter on extraction except the filer's parenthesized address
    closing the ``Source`` column (:data:`_FD_J_SOURCE_RE`): we split there:
    source through the closing paren, description after. A row with no address
    paren leaves both fields ``None`` and ``raw_text`` carries the row in full.
    """
    items: list[ScheduleJItem] = []
    for raw in _group_items(lines, starts_item=lambda s: True):
        scrubbed = _scrub_raw_text(raw)
        m = _FD_J_SOURCE_RE.match(scrubbed)
        if m:
            source = _scrub_field(m.group("source")) or None
            description = _scrub_field(m.group("description")) or None
        else:
            source = description = None
        items.append(
            ScheduleJItem(source=source, description=description, raw_text=scrubbed)
        )
    return items


def _split_trailing_dollar(raw: str) -> tuple[str, str | None, str | None]:
    """Split a row into ``(scrubbed, source, money)`` on a trailing dollar figure.

    Shared by Schedules G (gifts) and I (charity-in-lieu): both have a confident
    trailing dollar value/amount, while the columns before it (description / and
    activity+date) merge with no stable delimiter — so ``source`` holds the whole
    pre-value head and those middle columns stay ``None``. Either part is ``None``
    when absent; the caller keeps the verbatim ``scrubbed`` as ``raw_text``.
    """
    scrubbed = _scrub_raw_text(raw)
    m = _FD_TRAILING_DOLLAR_RE.search(scrubbed) or _FD_DOLLAR_RE.search(scrubbed)
    if not m:
        return scrubbed, None, None
    return scrubbed, scrubbed[: m.start()].strip() or None, m.group(1)


def _parse_schedule_g(lines: list[str]) -> list[ScheduleGItem]:
    """Schedule G (gifts) → ``source``/``value`` + raw_text (``Source | Description
    | Value``; see :func:`_split_trailing_dollar`)."""
    items: list[ScheduleGItem] = []
    for raw in _group_items(lines, starts_item=lambda s: True):
        scrubbed, source, value = _split_trailing_dollar(raw)
        items.append(ScheduleGItem(source=source, value=value, raw_text=scrubbed))
    return items


# A Schedule I ``Date`` column value (``04/22/2022`` / ``04/2022``) — a clean
# interior anchor that lets the Source|Activity head split off from the Date|Amount
# tail (GH-0164). Captured so the field extraction uses the anchoring match.
_FD_I_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4}|\d{1,2}/\d{4})\b")


def _parse_schedule_i(lines: list[str]) -> list[ScheduleIItem]:
    """Schedule I (charity in lieu of honoraria) → ``source``/``activity``/``date``/
    ``amount`` + raw_text (columns ``Source | Activity | Date | Amount``).

    A row anchors on its ``Amount`` column (the one confident I has), so a charity
    name that wrapped onto following lines (Schiff's "Burbank / Temporary Aid /
    Center") folds into its row rather than fabricating per-line items (#147). When
    the ``Date`` column (``MM/DD/YYYY``) is present it is a second clean anchor
    (GH-0166 / GH-0164): the ``Date`` and the ``Amount`` after it carve out their
    columns, the single ``Activity`` word before the date splits off, and the
    remainder is the ``Source`` — so neither the activity nor the date bleeds into
    ``source`` any more. With no date the row keeps the trailing-dollar-only split
    (source = the whole pre-amount head, activity/date ``None``)."""
    items: list[ScheduleIItem] = []
    for raw in _group_items(lines, starts_item=lambda s: bool(_FD_DOLLAR_RE.search(s))):
        scrubbed, head_source, amount = _split_trailing_dollar(raw)
        source, activity, date_str = head_source, None, None
        date_m = _FD_I_DATE_RE.search(scrubbed)
        amt_m = _FD_DOLLAR_RE.search(scrubbed)
        if date_m and amt_m and date_m.end() <= amt_m.start():
            date_str = date_m.group(1)
            # Source | Activity sit before the date; the Activity is the trailing
            # word run before it (a single category word — ``Article``/``Speech``/
            # ``Panel`` on the cited filings). Split the last token off as activity
            # so the source is the clean organization name.
            pre = scrubbed[: date_m.start()].strip()
            parts = pre.rsplit(" ", 1)
            if len(parts) == 2 and parts[1]:
                source, activity = parts[0].strip() or None, parts[1].strip() or None
            else:
                source = pre or None
        items.append(
            ScheduleIItem(
                source=source,
                activity=activity,
                date=date_str,
                amount=amount,
                raw_text=scrubbed,
            )
        )
    return items


# J (new-filer comp) splits its ``Source (Name and Address) | Brief Description``
# columns on the filer's parenthesized address (#146). H (travel) anchors on its
# ``Dates`` column to coalesce wrapped itineraries and split off source/dates
# (GH-0103); its ``Location``/``Items`` still merge, so those stay ``None``
# (best-effort, #17).
_FD_STRUCTURED = {
    "A": _parse_schedule_a,
    "B": _parse_schedule_b,
    "C": _parse_schedule_c,
    "D": _parse_schedule_d,
    "E": _parse_schedule_e,
    "F": _parse_schedule_f,
    "G": _parse_schedule_g,
    "H": _parse_schedule_h,
    "I": _parse_schedule_i,
    "J": _parse_schedule_j,
}

# The item model per schedule letter, used to salvage a segment whose column
# parser anchored no rows into raw_text-only items of the right type rather than
# letting it vanish (CLAUDE.md "never silently drop").
_FD_ITEM_MODEL = {
    "A": ScheduleAItem,
    "B": ScheduleBItem,
    "C": ScheduleCItem,
    "D": ScheduleDItem,
    "E": ScheduleEItem,
    "F": ScheduleFItem,
    "G": ScheduleGItem,
    "H": ScheduleHItem,
    "I": ScheduleIItem,
    "J": ScheduleJItem,
}

# Schedule A/C require a non-Optional first column (``asset`` / ``source``), so a
# salvage item fills it from the raw text; every other schedule's columns are all
# Optional and salvage carries raw_text alone.
_FD_SALVAGE_REQUIRED = {"A": "asset", "B": "asset", "C": "source", "D": "creditor"}


def _salvage_raw(letter: str, lines: list[str]) -> list:
    """Salvage a segment's rows as raw_text-only items of the schedule's model.

    Used when a column parser anchored no rows (or for a letter with no parser):
    each non-blank line becomes one item carrying verbatim ``raw_text`` (plus the
    schedule's required first column, if any, filled from that text) so nothing is
    silently dropped (CLAUDE.md). Every structured column is left ``None``.
    """
    model = _FD_ITEM_MODEL[letter]
    required = _FD_SALVAGE_REQUIRED.get(letter)
    items = []
    for ln in lines:
        scrubbed = _scrub_raw_text(ln)
        if not scrubbed:
            continue
        fields = {"raw_text": scrubbed}
        if required:
            fields[required] = scrubbed
        items.append(model(**fields))
    return items


def _schedule_merge_residual(items: list) -> bool:
    """True if any anchored Schedule A/B row absorbed an un-split sibling.

    A clean row carries exactly one real asset-type code; two or more in one
    row's ``raw_text`` means a wrapped-``[TYPE]`` / ⇒-subholding row that no anchor
    could separate folded in (GH-0100), fusing two assets. Surfaced as a
    ``schedule_incomplete`` residual so the merge is loud, never a silent drop —
    ``raw_text`` keeps the buried row's text verbatim regardless. Tickers and
    footnote brackets are not codes (``_FD_ASSET_TYPE_CODE_RE``), so a name
    carrying ``[VOO]`` beside its real ``[ST]`` tag is not mistaken for a merge.
    """
    def real_codes(raw: str) -> int:
        return sum(1 for t in _FD_TYPE_TAG_RE.findall(raw) if _FD_ASSET_TYPE_CODE_RE.fullmatch(t))

    return any(real_codes(it.raw_text) >= 2 for it in items)


def extract_fd_schedules(pdf_path: Path, *, max_year: int = FALLBACK_MAX_YEAR) -> FdBody:
    """Extract an e-filed annual-FD's §6.3 schedule body from its PDF. Offline.

    Segments the body by schedule **letter** (SPEC §2.2 small-caps caveat),
    column-parses A–D, ships E–J as ``raw_text``-only items, and records a
    ``None disclosed.`` schedule as **absent**. Every line item carries verbatim
    ``raw_text``. Raises :class:`PdfExtractError` on a present-but-unreadable PDF;
    raises :class:`NotAnFdBody` when the PDF carries no schedule headings at all
    (an extension / cover sheet that is not an annual FD body) — the ``parse``
    caller writes no body for that case rather than a misleading empty one.

    ``max_year`` (the command-entry year + 1) bounds the Schedule B
    transaction-date sanity range (GH-0113): an implausible-year date is set
    ``None`` with its raw string kept on ``transaction_date_raw``.
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
    incomplete: list[str] = []  # letters carrying an un-split merge (GH-0100)
    for letter in sorted(segments):
        content = segments[letter]
        if parser := _FD_STRUCTURED.get(letter):
            # Schedule B alone carries a transaction date, so it alone needs the
            # sanity-range bound (GH-0113); the other parsers take only the lines.
            items = (
                parser(content, max_year=max_year)
                if letter == "B"
                else parser(content)
            )
        else:
            items = []
        # No parser, or a parser that anchored no rows: the segment still carries
        # its lines as raw_text rather than vanishing — _salvage_raw is the single
        # fallback. Never silently drop.
        if not items:
            items = _salvage_raw(letter, content)
        # Completeness guard (GH-0070), the FD analog of the PTR FILINg STATUS:
        # block count: an A/B row normally carries one [TYPE] tag — on its
        # anchor line or a wrapped continuation — so the segment's tag count
        # approximates a row count INDEPENDENT of the row anchors (a guard that
        # shares the anchor's failure mode passes 0 == 0 and stays silent,
        # which is exactly how every post-2022-04 PTR parsed as empty). The
        # invariant is approximate, not exact (measured on a 300-doc stratified
        # sample: ~30% of documents drift by a row or two — tag-less rows,
        # filer text carrying brackets, header fragments), so the guard fires
        # only on the two unambiguous failure classes, never on small drift:
        # a TOTAL COLLAPSE (one item where the tags say several rows — the
        # GH-0070 headline failure) and a SEVERE MERGE (half or fewer of the
        # tag-counted rows anchored). Those become extract_failed — explicit
        # in the unparsed manifest — rather than a plausible-but-wrong body
        # with status ok. Only A and B carry per-row tags, so only they are
        # guarded; small drift passes, with verbatim raw_text still complete.
        if letter in ("A", "B"):
            expected = sum(len(_FD_TYPE_TAG_RE.findall(ln)) for ln in content)
            collapse = len(items) == 1 and expected >= 3
            severe = len(items) * 2 <= expected and expected >= 4
            if collapse or severe:
                raise PdfExtractError(
                    f"FD Schedule {letter} extraction incomplete for {pdf_path}: "
                    f"anchored {len(items)} row(s) but the segment carries "
                    f"{expected} [TYPE] tag(s) — rows merged"
                )
            # Short of a collapse: a single anchored row that still fused two
            # assets (≥2 real type codes in its raw_text) is a wrapped-[TYPE] / ⇒
            # merge no anchor could separate (GH-0100). The filing stays sound and
            # keeps its body — its text is all present — but the schedule is
            # flagged as a ``schedule_incomplete`` residual rather than passing
            # silently. After the GH-0100 anchor fix this is rare (a None-value
            # subholding whose tag wrapped past the lookahead's reach).
            if _schedule_merge_residual(items):
                incomplete.append(letter)
        schedules[letter] = [it.model_dump(mode="json") for it in items]

    return FdBody(schedules=schedules, incomplete_schedules=incomplete)
