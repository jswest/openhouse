"""Pydantic models for filing metadata + the FilingType code table.

Two lanes share this module: the **Clerk** lane (filing metadata §6.1, PTR/FD
bodies §6.3) versioned by :data:`SCHEMA_VERSION`, and the **FEC** lane (§13:
``FecCommittee`` / ``FecPacContribution`` / ``FecMemberCandidateLink``)
versioned **independently** by :data:`FEC_SCHEMA_VERSION`. The two version ints
are deliberately decoupled — a reshape in one lane must not force a re-parse of
the other (the same independence as ``inspect``'s ``LABELS_SCHEMA_VERSION``).

The Clerk metadata record below covers only what is always derivable from the
annual index XML.

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
from datetime import date as Date  # alias for fields named ``date`` (see FecPacContribution)
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
# Generation 7 also adds a **parse-time date sanity range** (GH-0113): a
# disclosure date whose year falls outside ``1990 ≤ year ≤ entry_year + 1`` (the
# upper bound derived from the single command-entry timestamp, never the wall
# clock in core logic) is an extraction artifact — a transposed-digit year like
# ``3031`` parses as readily as ``2024``. Such a date is **rejected**, never
# emitted as a valid ``date``: the structured field is ``None`` and the raw
# ``M/D/YYYY`` string is preserved in a sibling ``*_raw`` field (``date_raw`` /
# ``notification_date_raw`` on ``PtrTransaction``, ``transaction_date_raw`` on
# ``ScheduleBItem``). A set ``*_raw`` is the per-row anomaly flag; ``parse``
# surfaces it in ``unparsed-manifest.json`` (reason ``date_out_of_range``)
# without dropping the otherwise-good filing (raw alongside normalized, never a
# silent gap — CLAUDE.md). ``PtrTransaction.transaction_date`` /
# ``notification_date`` therefore become ``Optional``. Re-parse from ``raw/``
# required.
# Generation 8 (GH-0100): FD Schedule A row anchoring recovers wrapped-``[TYPE]``
# and ⇒-subholding rows whose value prints a line above their tag — assets that
# previously folded silently into the row above (the #70 regression). The parser
# now anchors a tag-less line on its ``$lo -`` value low, and a None/Over-value
# row whose tag wrapped via a one-line lookahead at the wrapped tag-tail; a row
# that still cannot be separated (≥2 asset-type codes in one row) is flagged as a
# ``schedule_incomplete`` residual (``unparsed-manifest.json``) without dropping
# the filing — completeness over the known, explicit residual for the rest
# (CLAUDE.md). Recovered rows change parsed output, so re-parse from ``raw/`` is
# required — which the bump forces.
# Generation 9 (GH-0143): FD schedule column-content extraction re-fixes the
# v0.8.0 attempts (#130/#131/#133/#134) missed on harder real-filing variants —
# intact-letter Schedule H/J header suppression + J source/description split
# (#146); a populated trailing Schedule I anchors on its amount column instead of
# fabricating appendix rows (#147); Schedule C owner-prefixed open-vocabulary Type
# no longer bleeds into source and Schedule D non-standard dates no longer collapse
# the row (#148); two-income (candidate) Schedule A maps current-year ``None`` and
# the preceding-year range correctly (#149, confirming #132); Schedule E splits
# identical-position blocks and folds ``C :`` comment lines (#150). All change
# parsed output, so re-parse from ``raw/`` is required — which the bump forces.
# Generation 10 (#174): the source namespace. The on-disk layout moved to
# ``raw/clerk/<year>/`` + ``parsed/clerk/<year>/``, so each record's stored
# ``source_pdf`` now reads ``raw/clerk/<year>/<family>/<DocID>.pdf``. That changes
# ``filings.json`` bytes, so a tree migrated by a bare ``mv`` (which relocates the
# JSON but not its embedded paths) must be re-parsed for ``source_pdf`` to point at
# the moved bytes — the bump makes ``read``'s schema-drift warning surface that.
# Generation 11 (GH-0166): the column/row-reconstruction omnibus. A single
# generalized fix to the shared two-layer reconstruction in ``pdf.py`` resolves a
# family of per-schedule regressions that kept reopening (#160/#162/#163/#164/#165
# + the #76 extension): FD Schedule A wrapped income-type second lines and
# multi-wrapped value/income high bounds that landed at the de-wrapped row tail
# (so ``value_of_asset`` / ``income_type`` / ``income_amount`` were dropped or
# corrupted), exact (non-range) Schedule A income values, and open-ended ``Over
# $X`` value handling; Schedule C source|type boundaries for multi-word/unknown
# Type values; Schedule F Parties/Terms population plus suppression of phantom
# row-splits on dates embedded in Terms prose; Schedule H multi-line source +
# itinerary/location extraction; the Schedule I sibling (activity/date folded out
# of source); and Schedule D wrapped ``date_incurred`` + comment-line bleed out of
# ``liability_type``. All change parsed output bytes, so a re-parse from ``raw/``
# is required — which the bump forces.
SCHEMA_VERSION = 11

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
    - ``transaction_date`` / ``notification_date`` — ISO ``YYYY-MM-DD``, or
      ``None`` when the date failed the parse-time sanity range (year outside
      ``1990 … entry_year+1`` — an extraction artifact such as a transposed-digit
      year). When ``None`` for that reason the raw ``M/D/YYYY`` string is kept in
      ``date_raw`` / ``notification_date_raw`` (GH-0113); ``parse`` then records
      the anomaly in ``unparsed-manifest.json`` rather than dropping the filing.
    - ``date_raw`` / ``notification_date_raw`` — the verbatim out-of-range date
      string, set **only** when the corresponding structured date was rejected;
      ``None`` on every sound row.
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
    transaction_date: Optional[date] = None
    date_raw: Optional[str] = None
    notification_date: Optional[date] = None
    notification_date_raw: Optional[str] = None
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
    # The verbatim date string, set only when ``transaction_date`` was rejected
    # by the parse-time sanity range (GH-0113); ``None`` on every sound row.
    transaction_date_raw: Optional[str] = None
    transaction_type: Optional[str] = None
    amount_range: Optional[AmountRange] = None
    cap_gains_over_200: Optional[bool] = None
    raw_text: str


class ScheduleCItem(BaseModel):
    """Schedule C line item — earned income (SPEC §6.3).

    The member annual form has ONE amount column → ``amount``. The
    Candidate/New-Filer form variant splits it into TWO: "Amount Current Year to
    Filing" and "Amount Preceding Year" (GH-0161). ``amount`` carries the current
    column (the single column on the member form, the first column on the
    candidate form); ``amount_preceding`` carries the candidate form's second
    column and is ``None`` on the member form, which has no such column — the same
    primary-plus-``_preceding`` shape Schedule A uses for its current/preceding
    income pair. Each is a verbatim ``$N`` / ``N/A`` string (not an AmountRange);
    ``raw_text`` carries the whole row regardless.
    """

    source: str
    income_type: Optional[str] = None
    amount: Optional[str] = None
    amount_preceding: Optional[str] = None
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
    # Letters (A/B) whose anchored rows still carry an un-split merge — a
    # wrapped-``[TYPE]`` / ⇒-subholding row no anchor could separate, leaving two
    # assets fused in one row (GH-0100). In-memory only: ``parse`` reads it to
    # emit a ``schedule_incomplete`` residual; it is NOT written to the body file
    # (``_write_fd_body`` persists only ``schedules``), so the on-disk body
    # contract is unchanged.
    incomplete_schedules: list[str] = Field(default_factory=list)


# ===========================================================================
# FEC lane — Path 1: connected-SSF PAC money (SPEC §13, #167/#168).
# ===========================================================================
#
# A *second* data source (#174), legally and structurally distinct from the
# Clerk lane above: FEC bulk/API data is **public domain** (no commercial-use
# restriction — only §30111's "sale or use" bar on soliciting), and it is
# cycle-keyed (2-year, even-year-ending) rather than per-coverage-year. These
# models are the contract the FEC acquisition/normalization/query sub-issues
# build on; no acquisition or query logic lives here yet (that's later #167
# sub-issues).
#
# FEC_SCHEMA_VERSION is INDEPENDENT of the Clerk lane's SCHEMA_VERSION, exactly
# as ``inspect``'s LABELS_SCHEMA_VERSION is (see openhouse/inspect/verdict.py):
# the FEC schema gates a re-parse/re-pull of the FEC tree, never the Clerk one,
# so reshaping an FEC model must not force a Clerk re-parse and vice versa. It is
# stamped into the FEC lane's own parse-manifest (a later sub-issue), the way
# SCHEMA_VERSION is stamped into the Clerk parse-manifest (§6.5).
FEC_SCHEMA_VERSION = 2

# The ``connected_organization`` ``organization_type`` code table (SPEC §13).
# Per FEC: a connected SSF's sponsoring organization is one of these classes.
# "Path 1" is exactly the connected SSFs — these six codes (#167 scope note);
# labor (``L``) is in-scope institutional PAC money, tagged so ``read`` can slice
# it. The raw code is always preserved on the record beside this label, so an
# unmapped/blank type is never an error (mirrors FilingType: raw alongside
# normalized, never a silently dropped record — CLAUDE.md).
FEC_ORG_TYPE_LABELS: dict[str, str] = {
    "C": "corporation",
    "T": "trade",
    "L": "labor",
    "M": "membership",
    "V": "cooperative",
    "W": "corporation_without_capital_stock",
}

# Provenance tag carried on every FEC record so a consumer can tell FEC-sourced
# data from Clerk-sourced data in a mixed downstream (SPEC §13). The Clerk lane's
# records are conceptually ``"clerk"``; FEC records carry ``"fec"``.
PROVENANCE_FEC = "fec"
PROVENANCE_CLERK = "clerk"

# The super-PAC independent-expenditure slice (§13.7, GH-0194) carries a *distinct*
# provenance so it can NEVER be summed with the Path-1 connected-SSF hard money:
# an IE is uncoordinated outside spending that does not reach the member, a
# different legal footing entirely (cross-ref the issue / §13.7). A consumer
# filters on this tag to keep the two footings separate downstream.
PROVENANCE_FEC_IE = "fec_ie"

# The Schedule-E ``sup_opp`` indicator → normalized direction (SPEC §13.5a, the
# §2.3 raw-alongside-normalized pattern). ``S`` = spent FOR the candidate, ``O`` =
# spent AGAINST. The raw single-letter code is always preserved beside the label
# (``support_oppose_raw``); a blank/unmapped code keeps a ``None`` label beside
# its raw value, never a dropped row.
FEC_IE_SUPPORT_OPPOSE_LABELS: dict[str, str] = {
    "S": "support",
    "O": "oppose",
}


class FecCommittee(BaseModel):
    """An FEC committee record (SPEC §13) — a member's principal campaign
    committee OR a contributing PAC, depending on context.

    For a **connected SSF** (the contributor side of Path 1), the sponsor link
    lives here: ``connected_organization_name`` names the sponsoring corporation/
    union/group and ``organization_type`` is its class (the §13 code table). For a
    member's own committee (the recipient side) those are typically ``None``.

    - ``committee_id`` — the FEC committee id (``C########``), opaque string.
    - ``name`` — the committee's name verbatim.
    - ``connected_organization_name`` — the sponsoring organization for a
      connected SSF, else ``None``.
    - ``organization_type`` — the **normalized** sponsor class label (from
      :data:`FEC_ORG_TYPE_LABELS`), or ``None`` when the FEC record carries no
      type; ``organization_type_raw`` keeps the verbatim single-letter code
      beside it (raw alongside normalized — CLAUDE.md). ``None`` exactly when
      ``organization_type`` is.
    - ``committee_type`` — the FEC committee-type code as-is (e.g. ``Q``/``N``),
      opaque here; not interpreted in this scaffold.
    - ``affiliation`` — the affiliated/connected committee id when FEC links this
      committee to another, else ``None``.
    """

    committee_id: str
    name: str
    connected_organization_name: Optional[str] = None
    organization_type: Optional[str] = None
    organization_type_raw: Optional[str] = None
    committee_type: Optional[str] = None
    affiliation: Optional[str] = None
    provenance: str = PROVENANCE_FEC


class FecPacContribution(BaseModel):
    """One PAC→member contribution: an FEC Schedule A **line 11C** receipt
    (receipts from other political committees) on the member's committee (§13).

    This is the atom of Path 1: a connected SSF (``contributor_committee_id``)
    giving hard money to a member's principal committee
    (``recipient_committee_id``). The ``image_number`` + ``transaction_id`` pair
    is the **double-entry key** — the same transaction is disclosed on both
    committees' filings, and these ids let a later sub-issue de-duplicate the two
    halves rather than double-count.

    - ``recipient_committee_id`` / ``contributor_committee_id`` — the member's
      committee and the contributing PAC (both ``C########``).
    - ``amount`` — the contribution amount in dollars (a single exact figure; FEC
      itemizes the actual dollar amount, not a bucket — unlike Clerk PTR ranges).
    - ``date`` — the contribution date, or ``None`` if absent/unparseable.
    - ``line`` — the FEC form line, ``"F3-11C"`` for this path (carried so a later
      multi-line extension stays explicit); defaults to that.
    - ``image_number`` — the FEC image (filing page) number, opaque string.
    - ``transaction_id`` — the FEC transaction id within the filing, opaque
      string. Together with ``image_number`` it keys the double entry.
    """

    recipient_committee_id: str
    contributor_committee_id: str
    amount: float
    # Aliased type (``Date``) because the field is *named* ``date``: under
    # ``from __future__ import annotations`` the ``= None`` default would shadow
    # the ``date`` type in the class namespace when pydantic resolves the string
    # annotation, so the annotation must reference a name the field can't rebind.
    date: Optional[Date] = None
    line: str = "F3-11C"
    image_number: Optional[str] = None
    transaction_id: Optional[str] = None
    provenance: str = PROVENANCE_FEC


class FecMemberCandidateLink(BaseModel):
    """The member ↔ FEC candidate ↔ committee link record (SPEC §13).

    The offline join that anchors a House member (the Clerk lane's identity) to
    their FEC candidate id and principal campaign committee, so PAC receipts can
    be attributed to a member. The join is **not fuzzy**: it rides the CC0
    ``congress-legislators`` ``id.fec[]`` list (already fetched by ``pull`` and
    used for the bioguide ladder, §6.2), so it is a deterministic offline
    extension of that ladder, never a name match.

    - ``bioguide_id`` — the member's bioguide id (the Clerk lane's identity key
      head; SPEC §6.2's ``bioguide:<id>`` ladder).
    - ``candidate_id`` — the FEC candidate id (``H########``), from
      ``congress-legislators`` ``id.fec[]``.
    - ``committee_id`` — the candidate's principal campaign committee
      (``C########``) — the recipient side of an 11C receipt.
    """

    bioguide_id: str
    candidate_id: str
    committee_id: str
    provenance: str = PROVENANCE_FEC


class FecIndependentExpenditure(BaseModel):
    """One super-PAC **independent expenditure** FOR or AGAINST a House candidate
    (FEC Schedule E — uncoordinated outside spending; SPEC §13.7, GH-0194).

    A **separately-footed** slice, legally and structurally distinct from
    :class:`FecPacContribution`: an IE is *uncoordinated* spending by an outside
    committee to influence a candidate's race — the money does **not** go to the
    member and does **not** carry Path 1's "disclosed, candidate-side hard money"
    guarantee. It must never be summed with the connected-SSF set, which the
    distinct :data:`PROVENANCE_FEC_IE` tag enforces downstream.

    Source is the FEC bulk ``independent_expenditure_<cycle>.csv`` (a *headered
    CSV*, unlike the four pipe-delimited Path-1 files — SPEC §13.5a). Both
    directions are kept and tagged; the operator filters either way.

    - ``spender_committee_id`` — the committee that made the expenditure (``spe_id``;
      a ``C########`` FEC committee id or a ``C9#######`` independent-only filer id),
      preserved raw. Joined to ``cm`` where present to surface
      ``connected_organization_name`` (raw, **no industry classification** —
      OpenSecrets-license non-goal, §13.7).
    - ``spender_name`` — the spending committee's name as the IE file carries it
      (``spe_nam``), raw.
    - ``connected_organization_name`` — the spender's sponsoring organization from
      ``cm`` where the join lands, else ``None``. Raw; never industry-classified.
    - ``candidate_id`` — the targeted House candidate (``H########``; ``cand_id``),
      or ``None`` when the IE row carries no candidate id (reported ``unattributed``,
      never dropped — §13.7).
    - ``bioguide_id`` — the member the candidate id resolves to via the CC0
      ``id.fec[]`` bridge (§13.2), or ``None`` when no member link exists.
    - ``office`` — the targeted office, always ``"H"`` for this slice (the parse
      keeps only House IEs; a non-``H`` row is a filtered residual, §13.7).
    - ``support_oppose`` — the normalized direction (``support`` / ``oppose``) from
      :data:`FEC_IE_SUPPORT_OPPOSE_LABELS`, or ``None`` when blank/unmapped;
      ``support_oppose_raw`` keeps the verbatim ``S`` / ``O`` code beside it.
    - ``amount`` — the expenditure amount in dollars (``exp_amo``); blank → ``0.0``.
    - ``date`` — the expenditure date (``exp_date``, ``DD-MON-YY``), or ``None`` if
      absent/unparseable (the date is not the row's identity, so it's never dropped).
    - ``purpose`` — the expenditure's stated purpose/description (``pur``), raw.
    - ``image_number`` + ``transaction_id`` — the FEC image and transaction ids
      (``image_num`` / ``tran_id``), the row's opaque identity key.
    """

    spender_committee_id: str
    spender_name: Optional[str] = None
    connected_organization_name: Optional[str] = None
    candidate_id: Optional[str] = None
    bioguide_id: Optional[str] = None
    office: str = "H"
    support_oppose: Optional[str] = None
    support_oppose_raw: Optional[str] = None
    amount: float
    date: Optional[Date] = None
    purpose: Optional[str] = None
    image_number: Optional[str] = None
    transaction_id: Optional[str] = None
    provenance: str = PROVENANCE_FEC_IE
