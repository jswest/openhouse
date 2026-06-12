"""Offline CC0 ``congress-legislators`` join: name+state+seat → ``bioguide`` (#16).

This module owns the **identity-enrichment** half of ``parse``. The Clerk index
carries *no* member ID — only name strings that vary across years ("Alma Shealey
Adams" vs "Alma S. Adams"), so :func:`~openhouse.index.compute_name_key` can only
build a normalized *name key*, never a stable identity. The
`@unitedstates/congress-legislators` project publishes two **CC0** bulk files —
``legislators-current.json`` + ``legislators-historical.json`` — that *do* carry a
stable ``id.bioguide`` per legislator alongside name fields and a ``terms[]`` list
(each term has ``type`` ``rep``/``sen``, ``state``, ``district``). Joining the FD
filer to that dataset attaches ``bioguide:<id>`` where it matches.

That dataset is CC0 (public domain) — **no conflict** with the Clerk FD use
restriction, which governs the *disclosure* data, not this reference set. The
**one declared network exception** in the whole product (CLAUDE.md: "``pull`` is
the only network step") is fetching these two files; that lives in ``pull`` and is
cached under ``raw/reference/``. The join here is pure, offline, deterministic:
given the already-on-disk reference JSON it builds an index once and answers
``match(...)`` with no network and no wall-clock.

**The match is bounded and conservative.** We key on the **House seat** —
normalized last name + state + district — because the FD index gives us exactly
that. A House member's ``terms`` of ``type == "rep"`` pin a ``(state, district)``;
we index every such (last-name, state, district) → bioguide. A filer matches iff
its normalized last name + state + district is in that index *and* resolves to a
single bioguide (an ambiguous key resolves to none — completeness over a false
positive, CLAUDE.md). Where no House seat matches we attach **nothing** and the
caller falls back to the last-resort ``name:`` key. We never synthesize a
``bioguide``; we never fold a name-only guess into one.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Where ``pull`` caches the two CC0 bulk files and where ``parse`` reads them.
REFERENCE_SUBDIR = "raw/reference"
LEGISLATORS_FILES = ("legislators-current.json", "legislators-historical.json")


def _norm_name(s: str) -> str:
    """Normalize a name part for keying: lowercase, drop diacritics, trim.

    Mirrors ``index.slug`` intent (NFKD → drop combining marks → lowercase) but
    keeps it punctuation-tolerant by collapsing to a bare lowercased token rather
    than a hyphen slug — the legislators file spells "González-Colón" and the
    Clerk "Gonzalez-Colon"; both must key the same. Internal whitespace and
    hyphens are kept as-is after diacritic stripping so "gonzalez-colon" matches.
    """
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.strip().lower()


def _seat_key(last: str, state: str, district: int) -> tuple[str, str, int]:
    """The House-seat join key: (normalized last name, upper state, district)."""
    return (_norm_name(last), (state or "").upper(), district)


@dataclass(frozen=True)
class LegislatorIndex:
    """An offline (seat → bioguide) index built from the CC0 bulk files.

    ``by_seat`` maps a ``(norm_last, state, district)`` House-seat key to a
    bioguide id, or to ``None`` when two+ distinct bioguides share that seat key
    (an ambiguous key matches nothing — we never guess between two people).
    """

    by_seat: dict[tuple[str, str, int], Optional[str]]

    def match(self, *, last: str, state: Optional[str], district: Optional[int]) -> Optional[str]:
        """Return the ``bioguide`` for this filer's House seat, or ``None``.

        Conservative: a missing state/district, an unknown seat, or a seat that
        resolves ambiguously (two bioguides) all return ``None`` — the caller then
        falls back to the last-resort ``name:`` key. Never a false positive.
        """
        if state is None or district is None:
            return None
        return self.by_seat.get(_seat_key(last, state, district))


def _index_records(records: list[dict], by_seat: dict) -> None:
    """Fold one bulk file's legislator records into the seat index (in place).

    Each legislator has ``id.bioguide``, ``name.last`` (+ ``name.official_full``),
    and ``terms[]``. Only ``type == "rep"`` terms pin a ``(state, district)``
    House seat; we index every distinct seat a rep has held. A seat key already
    pointing at a *different* bioguide is marked ambiguous (``None``) so it can
    never produce a false-positive match.
    """
    for rec in records:
        bioguide = (rec.get("id") or {}).get("bioguide")
        if not bioguide:
            continue
        name = rec.get("name") or {}
        last = name.get("last") or ""
        if not last:
            continue
        for term in rec.get("terms") or []:
            if term.get("type") != "rep":
                continue
            state = term.get("state")
            district = term.get("district")
            if state is None or district is None:
                continue
            key = _seat_key(last, state, int(district))
            existing = by_seat.get(key, "__absent__")
            if existing == "__absent__":
                by_seat[key] = bioguide
            elif existing != bioguide:
                # Two distinct people share this (last, state, district) key —
                # mark it ambiguous so the join never picks one (no false positive).
                by_seat[key] = None


def load_legislator_index(data_dir: Path) -> LegislatorIndex:
    """Build the offline seat→bioguide index from cached CC0 bulk files.

    Reads ``<data_dir>/raw/reference/legislators-{current,historical}.json``
    (written by ``pull``). A missing file is skipped silently — an empty index
    simply matches nothing, so ``parse`` still runs and every filer falls back to
    the ``name:`` key (the reference fetch is optional enrichment, never a gate).
    Pure + offline + deterministic.
    """
    by_seat: dict[tuple[str, str, int], Optional[str]] = {}
    ref_dir = data_dir / REFERENCE_SUBDIR
    for name in LEGISLATORS_FILES:
        path = ref_dir / name
        if not path.exists():
            continue
        try:
            records = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(records, list):
            _index_records(records, by_seat)
    return LegislatorIndex(by_seat=by_seat)
