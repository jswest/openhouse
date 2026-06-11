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

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET


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
