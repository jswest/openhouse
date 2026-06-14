"""Pydantic models for filing metadata + the FilingType code table.

This module covers only the **filing-metadata** record (SPEC §6.1) — the record
always derivable from the annual index XML. PTR/FD body schemas (§6.3) belong to
later milestones.

Every nullability and edge case here traces to a *verified* observation in real
2024 index data (SPEC §2.1):

- empty ``StateDst`` / ``FilingDate`` (seen on type ``W``) → both nullable
- non-state ``StateDst`` like ``DC00`` / ``PR00`` → state is any 2-letter postal
  code; never validated against the 50 states
- district ``00`` → ``0`` = at-large / n.a.
- 4-digit ``DocID`` alongside 7- and 8-digit → opaque string, never numeric
- raw ``FilingType`` letter preserved beside its mapped label; an unknown letter
  still yields a valid record (never a silently dropped filing)
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field, model_serializer, model_validator

# Integer schema generation, stamped into ``parse-manifest.json`` (SPEC §6.5) and
# *is* the minor of the release version ``v0.<SCHEMA_VERSION>.<patch>`` (GH-0037).
# It moves iff the parsed-data schema changes — which forces a re-parse, not a
# migrate (CLAUDE.md): bump this, delete old code, re-run ``parse`` from ``raw/``.
# Read by ``parse`` (#6), the per-PDF pass (#7), and the release tool. Generation
# 3 adds PTR body extraction (§6.3 transactions[]); generation 4 adds e-filed FD
# schedule bodies (§6.3 schedules A–D structured, E–J raw_text-only); generation 5
# adds the CC0 ``congress-legislators`` identity join (#16) — a ``bioguide_id`` and
# a two-tier ``filer_id`` ladder (``bioguide:<id>`` / ``name:<slug>``) — and
# structured columns for FD schedules E–J (#17), each item still carrying
# verbatim ``raw_text``; and an ``exact`` point-value on ``AmountRange`` (#49)
# so a single exact-dollar PTR amount (e.g. ``$894.97``) is represented soundly
# rather than coerced into a fake low–high bucket. All of these ride generation 5.
# Generation 6 (GH-0070): ``PtrTransaction.cap_gains_over_200`` becomes nullable
# (None = unknown — the glyphs-lost rendering drops the checkbox from the text
# layer entirely) and ``ScheduleAItem`` gains ``income_preceding`` (the
# Candidate/New-Filer form variant's third income column). The same generation
# re-anchors FD Schedule A/B/D/F row segmentation and adds the A/B completeness
# guard, so a re-parse from ``raw/`` is required — which the bump forces.
# Generation 7 (GH-0114): ``asset_type`` is now **normalized** (uppercased,
# trimmed) on ``PtrTransaction`` / ``ScheduleAItem`` / ``ScheduleBItem`` — the
# Clerk's PDFs render the bracketed tag with inconsistent casing
# (``ST``/``sT``/``Cs``/``gS``), so every consumer had to defensively upper() it.
# The verbatim tag is preserved beside it in a new ``asset_type_raw`` field
# (raw alongside normalized — CLAUDE.md). Re-parse from ``raw/`` required.
SCHEMA_VERSION = 7

# ---------------------------------------------------------------------------
# FilingType code table — single source of truth.
#
# Seeded from SPEC §2.3's 12 verified 2024 codes. Cross-year enumeration is
# folded into M2 (SPEC §10); when new codes are confirmed, add them *here* and
# nowhere else. An unmapped letter is never an error: the raw code is always
# preserved (see ``FilingTypeInfo.from_code``) so no filing is dropped.
# ---------------------------------------------------------------------------
FILING_TYPE_LABELS: dict[str, str] = {
    "C": "candidate_report",
    "X": "extension",
    "P": "periodic_transaction_report",
    "O": "annual_report",
    "A": "amendment",
    "D": "unknown_d",
    "W": "withdrawal",
    "H": "unknown_h",
    "T": "termination",
    "B": "unknown_b",
    "G": "unknown_g",
    "E": "unknown_e",
}

# Label used when a FilingType letter is not in the table. The raw code is still
# carried on the record, so the filing remains identifiable and recoverable.
UNKNOWN_FILING_LABEL = "unknown"


class FilingTypeInfo(BaseModel):
    """A FilingType: its raw single-letter ``code`` plus the mapped ``label``."""

    code: str
    label: str

    @classmethod
    def from_code(cls, code: str) -> "FilingTypeInfo":
        """Build from a raw code, mapping via the table.

        An unrecognized code maps to :data:`UNKNOWN_FILING_LABEL` rather than
        raising — the raw code is preserved so the filing is never dropped.
        """
        return cls(code=code, label=FILING_TYPE_LABELS.get(code, UNKNOWN_FILING_LABEL))


class Filer(BaseModel):
    """The filer's name parts, as they appear in the index.

    ``prefix`` and ``suffix`` are usually null; ``first`` may include middle
    names. Names are preserved verbatim — normalization (``filer_id``) lives in
    ``index.py`` in a later milestone.
    """

    prefix: Optional[str] = None
    first: str
    last: str
    suffix: Optional[str] = None


class StateDistrict(BaseModel):
    """A parsed ``StateDst`` value: ``raw`` + ``state`` + ``district``.

    ``state`` is any 2-letter postal code (incl. ``DC``, ``PR``, territories) —
    deliberately *not* validated against the 50 states. ``district`` is an int
    with ``0`` = at-large / n.a. The whole object is nullable on the record
    (empty ``StateDst`` → ``state_district = None``).
    """

    raw: str
    state: str
    district: int


class FilingMetadata(BaseModel):
    """One filing-metadata record (SPEC §6.1), always derivable from the index."""

    doc_id: str = Field(..., description="Opaque string; 4-, 7-, 8-digit all occur")
    year: int = Field(..., description="Coverage year — never derived from filing_date")
    filer: Filer
    filer_id: str = Field(
        ...,
        description=(
            "Identity key, two-tier ladder (#16): ``bioguide:<id>`` when the filer "
            "matched a CC0 congress-legislators House seat, else the last-resort "
            "``name:<normalized-slug>`` name key (a bounded, unverified claim)"
        ),
    )
    bioguide_id: Optional[str] = Field(
        None,
        description=(
            "The matched congress-legislators bioguide id, or ``None`` when the "
            "filer matched no House-seat record (then filer_id is the ``name:`` key)"
        ),
    )
    state_district: Optional[StateDistrict] = None
    filing_type: FilingTypeInfo
    filing_date: Optional[date] = None
    source_pdf: Optional[str] = None
    pdf_class: Optional[str] = None
    parse_status: Optional[str] = None


class AmountRange(BaseModel):
    """A transaction's disclosed dollar amount (SPEC §6.3).

    Two mutually-exclusive shapes, distinguished on the wire so a consumer can
    tell a *bucket* from a *point* without guessing:

    - **Range** (the usual form): ``low``/``high`` are the parsed integer bounds
      of a ``$LOW - $HIGH`` bucket; ``exact`` is ``None``.
    - **Exact value** (GH-0049): some PTR rows disclose a single exact dollar
      figure (e.g. ``$894.97``) in place of a bucket. That value lands in
      ``exact`` (a float — exact figures carry cents); ``low``/``high`` are
      ``None``. It is **not** coerced into a ``{low: 894.97, high: 894.97}``
      fake range — a point is not a bucket. For comparisons (``read``'s
      ``--min-amount`` filter) an exact value ``X`` is treated as the closed
      point ``[X, X]`` — see ``read._amount_low``.

    ``label`` is the verbatim amount string from the form (``"$1,001 - $15,000"``
    or ``"$894.97"``) so the original wording is never lost. Exactly one of
    {``low``+``high``} / {``exact``} is set — enforced by the validator below;
    a row that is genuinely neither still fails extraction loudly upstream
    (never a fabricated range — CLAUDE.md).
    """

    low: Optional[int] = None
    high: Optional[int] = None
    exact: Optional[float] = None
    label: str

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> "AmountRange":
        is_range = self.low is not None and self.high is not None
        is_exact = self.exact is not None
        if is_exact and (self.low is not None or self.high is not None):
            raise ValueError(
                "AmountRange.exact is mutually exclusive with low/high"
            )
        if not is_exact and not is_range:
            raise ValueError(
                "AmountRange needs either both low and high, or exact"
            )
        return self

    @model_serializer
    def _serialize(self) -> dict:
        """Emit only the shape that applies: a range omits ``exact``, an exact
        value omits ``low``/``high``. This keeps a range's on-wire JSON
        byte-identical to before #49 (no ``"exact": null`` noise on the common
        case) while an exact-dollar row carries a single ``exact`` field — the
        two shapes stay visibly distinct (GH-0049)."""
        if self.exact is not None:
            return {"exact": self.exact, "label": self.label}
        return {"low": self.low, "high": self.high, "label": self.label}


class PtrTransaction(BaseModel):
    """One e-filed PTR transaction row (SPEC §6.3 ``transactions[]``).

    Field semantics (all verbatim-preserving where the form is authoritative):

    - ``owner`` — the leading owner letter (``SP``/``DC``/``JT``); a blank owner
      column means the filer themself, normalized to ``"self"``.
    - ``asset`` — the asset name verbatim, multi-line wraps joined into one
      string. Not "fixed" for the small-caps glyph artifact.
    - ``ticker`` — strict symbol-only: the parenthesized ``(SYMBOL)`` embedded in
      the asset name, **uppercased** to defeat pdfplumber's small-caps glyph
      artifact (raw ``AAPl``/``bRK.b`` → ``AAPL``/``BRK.B``). ``None`` when the
      asset carries no parenthesized symbol (corp bonds ``[CS]``, govt ``[GS]``,
      etc. legitimately have none — ``None`` is correct, never a sentinel). A
      ticker is never inferred from the company name.
    - ``asset_type`` — the bracketed tag (``ST`` from ``[ST]``), **normalized**
      (uppercased, trimmed) so it is comparable across the corpus; ``None`` when
      the row carries no tag (GH-0114).
    - ``asset_type_raw`` — the same tag **verbatim** (preserving the Clerk's
      inconsistent casing, e.g. ``sT``/``Cs``/``gS``); raw alongside normalized
      (CLAUDE.md). ``None`` exactly when ``asset_type`` is.
    - ``transaction_type`` — ``P`` | ``S`` | ``S(partial)`` | ``E`` (the form
      prints ``S (partial)``; normalized to ``S(partial)``).
    - ``transaction_date`` / ``notification_date`` — ISO ``YYYY-MM-DD``.
    - ``amount_range`` — the parsed amount: a ``{low, high, label}`` bucket, or
      an ``{exact, label}`` point when the row discloses a single exact dollar
      value instead of a range (GH-0049).
    - ``cap_gains_over_200`` — the cap-gains checkbox (the form renders
      ``gfedc`` unchecked vs ``gfedcb`` checked at the row's end). ``None``
      means **unknown**: in the glyphs-lost rendering (SPEC §2.2 NUL form,
      dominant for PTRs from ~2022-04 on) the checkbox glyph vanishes from the
      text layer entirely, so the state is unrecoverable — recorded as ``null``,
      never coerced to a boolean (the same unrecoverable-field treatment as the
      FD Schedule B ``cap_gains_over_200``).
    - ``description`` — the ``DESCRIPTION:`` line text if present, else ``None``.
    """

    owner: str
    asset: str
    ticker: Optional[str] = None
    asset_type: Optional[str] = None
    asset_type_raw: Optional[str] = None
    transaction_type: str
    transaction_date: date
    notification_date: date
    amount_range: AmountRange
    cap_gains_over_200: Optional[bool] = None
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# E-filed annual-FD schedule bodies (SPEC §6.3).
#
# An annual FD is a schedule-by-schedule document (A–J), every schedule now
# column-parsed into structured fields (A–D since #12, E–J since #17). *Every*
# line item — structured or not — carries a verbatim
# ``raw_text`` so nothing extracted is lost to a schema gap, and an empty
# schedule (the literal ``None disclosed.``) is recorded as **absent** (its key
# is simply omitted from the body's ``schedules`` map). Headings are matched by
# schedule letter, never by full heading text (SPEC §2.2 small-caps caveat).
#
# The structured column sets below mirror the live e-filed form's headers;
# fields are ``Optional`` because the form leaves many cells blank (a blank
# income column, no ``LOCATION``/``DESCRIPTION`` detail, an open-ended amount the
# range parser cannot bucket, etc.). When a structured column cannot be read the
# field is ``None`` and the verbatim ``raw_text`` still carries the row in full —
# completeness over the known, explicit residual in the text (CLAUDE.md).
# ---------------------------------------------------------------------------


class ScheduleAItem(BaseModel):
    """Schedule A line item — assets & "unearned" income (SPEC §6.3)."""

    asset: str
    owner: Optional[str] = None
    asset_type: Optional[str] = None
    asset_type_raw: Optional[str] = None
    value_of_asset: Optional[AmountRange] = None
    income_type: Optional[str] = None
    income_amount: Optional[AmountRange] = None
    # The Candidate/New-Filer form variant's third income column, "income
    # preceding year" (GH-0070); the member annual form has no such column, so
    # this is None there — None means "column not on this form / not parsed",
    # never a zero.
    income_preceding: Optional[AmountRange] = None
    location: Optional[str] = None
    description: Optional[str] = None
    raw_text: str


class ScheduleBItem(BaseModel):
    """Schedule B line item — transactions (SPEC §6.3)."""

    asset: str
    owner: Optional[str] = None
    asset_type: Optional[str] = None
    asset_type_raw: Optional[str] = None
    transaction_date: Optional[date] = None
    transaction_type: Optional[str] = None
    amount_range: Optional[AmountRange] = None
    cap_gains_over_200: Optional[bool] = None
    raw_text: str


class ScheduleCItem(BaseModel):
    """Schedule C line item — earned income (SPEC §6.3)."""

    source: str
    income_type: Optional[str] = None
    amount: Optional[str] = None
    raw_text: str


class ScheduleDItem(BaseModel):
    """Schedule D line item — liabilities (SPEC §6.3)."""

    creditor: str
    owner: Optional[str] = None
    date_incurred: Optional[str] = None
    liability_type: Optional[str] = None
    amount_range: Optional[AmountRange] = None
    raw_text: str


class ScheduleEItem(BaseModel):
    """Schedule E line item — positions (SPEC §6.3).

    Form columns: ``Position | Name of Organization``. ``position`` is the
    leading role title; ``organization`` the named body. Both ``Optional`` — a
    row the splitter can't cleanly bisect leaves them ``None`` and the verbatim
    ``raw_text`` still carries the whole line.
    """

    position: Optional[str] = None
    organization: Optional[str] = None
    raw_text: str


class ScheduleFItem(BaseModel):
    """Schedule F line item — agreements (SPEC §6.3).

    Form columns: ``Date | Parties | Terms of Agreement``. ``date`` is the
    agreement date as printed (``Month YYYY``); ``parties`` the named parties;
    ``terms`` the (often multi-line) description folded into one string. All
    ``Optional``; ``raw_text`` carries the verbatim, wrap-joined row.
    """

    date: Optional[str] = None
    parties: Optional[str] = None
    terms: Optional[str] = None
    raw_text: str


class ScheduleGItem(BaseModel):
    """Schedule G line item — gifts (SPEC §6.3).

    Form columns: ``Source | Description | Value``. ``value`` is the dollar
    figure as printed (gifts carry an exact value, not an A-style range). The
    fixtures' G is ``None disclosed.`` (absent), so the split is header-driven and
    conservative — anything not cleanly columnar stays in ``raw_text``.
    """

    source: Optional[str] = None
    description: Optional[str] = None
    value: Optional[str] = None
    raw_text: str


class ScheduleHItem(BaseModel):
    """Schedule H line item — travel payments & reimbursements (SPEC §6.3).

    Form columns: ``Source | Dates | Location | Items Provided``. ``dates`` is
    the printed travel date(s); ``location`` the destination; ``items`` what was
    provided. All ``Optional``; ``raw_text`` carries the verbatim row.
    """

    source: Optional[str] = None
    dates: Optional[str] = None
    location: Optional[str] = None
    items: Optional[str] = None
    raw_text: str


class ScheduleIItem(BaseModel):
    """Schedule I line item — payments to charity in lieu of honoraria (§6.3).

    Form columns: ``Source | Activity | Date | Amount``. ``amount`` is the dollar
    figure as printed. All ``Optional``; ``raw_text`` carries the verbatim row.
    """

    source: Optional[str] = None
    activity: Optional[str] = None
    date: Optional[str] = None
    amount: Optional[str] = None
    raw_text: str


class ScheduleJItem(BaseModel):
    """Schedule J line item — compensation in excess of $5,000 (new filers, §6.3).

    Form columns: ``Source | Brief Description of Duties``. Both ``Optional``;
    ``raw_text`` carries the verbatim row.
    """

    source: Optional[str] = None
    description: Optional[str] = None
    raw_text: str


class FdBody(BaseModel):
    """An e-filed annual-FD body — schedules keyed by letter (SPEC §6.3, §6.4).

    ``schedules`` holds only the letters that have data: a schedule rendered
    ``None disclosed.`` is **absent** (omitted), never an empty array, so a
    consumer can tell "disclosed nothing" from "we failed to read it". Every
    schedule (A–J) carries structured items, each retaining a verbatim
    ``raw_text``. Written one-per-body to ``parsed/<year>/fd/<DocID>.json``.
    """

    schedules: dict[str, list] = Field(default_factory=dict)
