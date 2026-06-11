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

from pydantic import BaseModel, Field

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
    state_district: Optional[StateDistrict] = None
    filing_type: FilingTypeInfo
    filing_date: Optional[date] = None
    source_pdf: Optional[str] = None
    pdf_class: Optional[str] = None
    parse_status: Optional[str] = None
