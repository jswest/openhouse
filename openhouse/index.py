"""Index enumeration: ``<YEAR>FD.xml`` → ``(DocID, FilingType, year)`` targets.

This module is intentionally **minimal**. ``pull`` (issue #4) needs only enough
of the index to drive the PDF download loop: each filing's ``DocID``, its raw
``FilingType`` letter (to route by the SPEC §2.2 rule), and the coverage
``Year``. The full metadata→record mapping — ``filer_id``, name normalization,
``StateDst`` parsing, the §6.1 record — belongs to the ``parse`` milestone (M4)
and is deliberately *not* built here.

Parsing is stdlib ``xml.etree`` (SPEC §9). The raw ``FilingType`` letter is
preserved verbatim on every target so routing never silently drops a filing.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional
from xml.etree import ElementTree as ET

from .schemas import (
    Filer,
    FilingMetadata,
    FilingTypeInfo,
    StateDistrict,
)

if TYPE_CHECKING:
    from .legislators import LegislatorIndex


@dataclass(frozen=True)
class IndexTarget:
    """One PDF-download target enumerated from the index.

    Just the fields the §4 download loop needs: the opaque ``doc_id`` string, the
    raw single-letter ``filing_type`` (preserved verbatim for §2.2 routing), and
    the coverage ``year``. ``family`` derives the §2.2 route (``ptr`` for ``P``,
    else ``fd``).
    """

    doc_id: str
    filing_type: str
    year: int

    @property
    def family(self) -> str:
        """The §2.2 routing family: ``ptr`` if ``FilingType == 'P'`` else ``fd``."""
        return "ptr" if self.filing_type == "P" else "fd"


def _text(member: ET.Element, tag: str) -> str:
    """The stripped text of ``member``'s ``tag`` child, or ``""`` if absent/empty."""
    child = member.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def enumerate_targets(xml_path: Path, year: int) -> Iterator[IndexTarget]:
    """Yield one :class:`IndexTarget` per ``<Member>`` in ``<year>FD.xml``.

    Reads the index from disk (``pull`` always writes it first) and yields the
    minimal ``(DocID, FilingType, year)`` tuple for each filing. A ``<Member>``
    with no ``DocID`` is skipped (it has no PDF to fetch) — but that is not a
    silently dropped *filing*: such rows carry no body to acquire, and the
    metadata milestone records them from the same XML.
    """
    root = ET.parse(xml_path).getroot()
    for member in root.findall("Member"):
        doc_id = _text(member, "DocID")
        if not doc_id:
            continue
        filing_type = _text(member, "FilingType")
        yield IndexTarget(doc_id=doc_id, filing_type=filing_type, year=year)


# ---------------------------------------------------------------------------
# Metadata mapping: <Member> → FilingMetadata record (SPEC §6.1, §6.2) — parse (#6)
#
# Unlike ``enumerate_targets`` (which exists for ``pull`` and skips no-DocID rows
# because they carry no PDF body to fetch), this path NEVER skips a <Member>:
# every entry in the index is a filing with metadata, and dropping one would be a
# silent gap (CLAUDE.md). A row with no DocID still yields a record.
# ---------------------------------------------------------------------------

# Strip everything that isn't a word char or whitespace, so punctuation in raw
# names (``Maryam.``, ``Gonzalez-Colon``) never leaks into a filer_id segment.
_NON_SLUG_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+", flags=re.UNICODE)


def slug(s: str) -> str:
    """Normalize a name part into a filer_id segment (SPEC §6.2).

    Lowercases, strips diacritics (Unicode NFKD → drop combining marks), removes
    punctuation, and collapses internal whitespace runs to a single ``-``. Empty
    or whitespace-only input → ``""``.
    """
    if not s:
        return ""
    # NFKD splits accented characters into base + combining mark; dropping the
    # marks (category ``Mn``) strips the diacritic while keeping the base letter.
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    no_punct = _NON_SLUG_RE.sub(" ", stripped)
    collapsed = _WHITESPACE_RE.sub("-", no_punct.strip())
    return collapsed.lower()


def _first_token(s: str) -> str:
    """The first whitespace-delimited token of ``s`` (SPEC §6.2: only the first
    token of ``First`` participates), or ``""`` if there is none."""
    parts = s.split()
    return parts[0] if parts else ""


def compute_name_key(
    *, last: str, first: str, suffix: str, state: Optional[str]
) -> str:
    """Compute the normalized **name key** (SPEC §6.2) — the last-resort tier.

    ``lower(state) "." slug(Last) "." slug(first_token(First)) ["." slug(Suffix)]``.
    Only the first whitespace token of ``First`` participates (middle names /
    initials are the main source of cross-year variation). The suffix segment is
    appended only when ``Suffix`` is present and non-empty. An empty/missing
    state → the ``unk`` state segment.

    This is a *normalized key*, not a true member ID. Since #16 it is the
    **second** rung of the identity ladder: a filer that matches no CC0
    congress-legislators House seat falls back to ``name:<this>`` — a bounded,
    unverified name-string claim, never a synthesized bioguide.
    """
    state_seg = state.lower() if state else "unk"
    segments = [state_seg, slug(last), slug(_first_token(first))]
    suffix_seg = slug(suffix)
    if suffix_seg:
        segments.append(suffix_seg)
    return ".".join(segments)


def _parse_state_district(raw: str) -> Optional[StateDistrict]:
    """Parse a ``StateDst`` value into a :class:`StateDistrict`, or ``None``.

    ``state`` = the first 2 chars (any 2-letter postal code incl. DC/PR/
    territories — never validated against the 50 states). ``district`` = int of
    the remainder, with ``00`` / missing → ``0`` (at-large / n.a.). An empty
    ``StateDst`` → ``None`` (SPEC §2.1: seen on type ``W``).
    """
    raw = raw.strip()
    if not raw:
        return None
    state = raw[:2]
    rest = raw[2:].strip()
    try:
        district = int(rest) if rest else 0
    except ValueError:
        district = 0
    return StateDistrict(raw=raw, state=state, district=district)


def _parse_filing_date(raw: str) -> Optional[date]:
    """Parse a ``FilingDate`` (``M/D/YYYY``) into a :class:`date`, or ``None``.

    Empty ``FilingDate`` → ``None`` (SPEC §2.1: seen on type ``W``). The coverage
    ``Year`` is never derived from or cross-validated against this (a 2024 report
    can carry a 2025 filing date)."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError:
        return None


def build_filing_records(
    xml_path: Path,
    year: int,
    legislators: Optional["LegislatorIndex"] = None,
) -> list[FilingMetadata]:
    """Parse every ``<Member>`` in ``<year>FD.xml`` into a :class:`FilingMetadata`.

    One record per filing, in XML order (deterministic). Every ``<Member>`` yields
    a record — including a row with no ``DocID`` (it has metadata, just no body to
    fetch) — so no filing is ever silently dropped (CLAUDE.md). Raw values are
    preserved: the FilingType letter on ``filing_type.code``, the original
    ``StateDst`` string on ``state_district.raw``.

    **Identity ladder (#16).** ``filer_id`` is a two-tier key. When ``legislators``
    is supplied and the filer's House seat (normalized last name + state +
    district) matches a single CC0 congress-legislators bioguide, ``bioguide_id``
    is that id and ``filer_id`` is ``bioguide:<id>`` — a stable identity, shared
    across years and name spellings. Otherwise ``bioguide_id`` is ``None`` and
    ``filer_id`` is the last-resort ``name:<normalized-slug>`` key (a bounded,
    unverified name-string claim). With no ``legislators`` index every filer falls
    back to ``name:`` — the enrichment is optional, never a gate.

    ``source_pdf`` is the relative path the body *would* live at —
    ``raw/<year>/<family>/<doc_id>.pdf`` (``ptr`` for FilingType ``P``, else
    ``fd``, the §2.2 routing) — or ``None`` when the row carries no DocID.
    ``pdf_class`` is left ``None`` here (the per-PDF classification pass is #7);
    ``parse_status`` is ``"ok"`` (the metadata record always parses).
    """
    root = ET.parse(xml_path).getroot()
    records: list[FilingMetadata] = []
    for member in root.findall("Member"):
        doc_id = _text(member, "DocID")
        first = _text(member, "First")
        last = _text(member, "Last")
        suffix = _text(member, "Suffix")
        prefix = _text(member, "Prefix")
        raw_type = _text(member, "FilingType")
        state_district = _parse_state_district(_text(member, "StateDst"))

        family = "ptr" if raw_type == "P" else "fd"
        source_pdf = (
            f"raw/{year}/{family}/{doc_id}.pdf" if doc_id else None
        )

        name_key = compute_name_key(
            last=last,
            first=first,
            suffix=suffix,
            state=state_district.state if state_district else None,
        )
        # The seat join pins a filer to the member who HELD that (state, district)
        # seat. A candidate report (FilingType "C") is filed by someone RUNNING for
        # the seat — definitionally not its holder — so a surname+seat collision
        # with any current or historical rep of that seat would be a false-positive
        # identity, and (since the warning only fires on UNmatched filers) a silent
        # one. Candidates get the honest last-resort name: key instead. (GH-0016 /
        # critic finding: never a false-positive bioguide.)
        bioguide_id = (
            legislators.match(
                last=last,
                state=state_district.state if state_district else None,
                district=state_district.district if state_district else None,
            )
            if legislators is not None and raw_type != "C"
            else None
        )
        # Two-tier ladder: bioguide where it matched, else the name key. Where
        # bioguide exists we do NOT mint a synthesized id alongside it (#16 scope).
        filer_id = f"bioguide:{bioguide_id}" if bioguide_id else f"name:{name_key}"

        record = FilingMetadata(
            doc_id=doc_id,
            year=year,
            filer=Filer(
                prefix=prefix or None,
                first=first,
                last=last,
                suffix=suffix or None,
            ),
            filer_id=filer_id,
            bioguide_id=bioguide_id,
            state_district=state_district,
            filing_type=FilingTypeInfo.from_code(raw_type),
            filing_date=_parse_filing_date(_text(member, "FilingDate")),
            source_pdf=source_pdf,
            pdf_class=None,
            parse_status="ok",
        )
        records.append(record)
    return records
