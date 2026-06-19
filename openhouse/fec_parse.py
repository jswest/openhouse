"""FEC normalization: bulk ``raw/fec/<cycle>/`` → normalized JSON (SPEC §13) — offline.

This module owns ``openhouse fec parse`` — the FEC lane's analogue of the Clerk
lane's :mod:`openhouse.parse`. It is the **offline, deterministic** counterpart to
:mod:`openhouse.fec_pull`: it reads only ``raw/fec/<cycle>/`` (the four bulk files
``fec pull`` extracted) and writes only ``parsed/fec/<cycle>/``, never touching the
network or the wall clock (the single entry-time ``generated_at`` is threaded in,
SPEC §9 / CLAUDE.md).

The four bulk files (pipe-delimited, no header row, latin-1, LF-terminated; column
orders are the verified facts of SPEC §13.5a):

(``cn.txt`` — candidate master — is downloaded by ``fec pull`` but not read here:
``ccl`` already supplies the candidate→principal-committee map Path 1 needs.)

- ``ccl.txt`` — candidate-committee linkage (7 cols): ``CAND_ID`` col 0,
  ``CMTE_ID`` col 3, ``CMTE_DSGN`` col 5 (``P`` = principal). The
  candidate→principal-committee map filled into the #169 link seam.
- ``cm.txt`` — committee master (15 cols): ``CMTE_ID`` col 0, ``CMTE_NM`` col 1,
  ``CMTE_TP`` col 9, ``ORG_TP`` col 12, ``CONNECTED_ORG_NM`` col 13. **No
  affiliation column** — so ``FecCommittee.affiliation`` has no bulk source and
  stays ``None`` (a DECLARED LIMITATION, SPEC §13.8, surfaced in the manifest).
- ``itpas2.txt`` — committee→candidate contributions (22 cols): ``CMTE_ID``
  (contributor) col 0, ``IMAGE_NUM`` col 4, ``TRANSACTION_DT`` (MMDDYYYY) col 13,
  ``TRANSACTION_AMT`` col 14, ``OTHER_ID`` (recipient committee) col 15,
  ``CAND_ID`` col 16, ``TRAN_ID`` col 17.

**Path-1 filter (SPEC §13.1):** keep only a contribution whose *contributing*
committee (joined via ``cm``) carries ``organization_type ∈ {C, T, L, M, V, W}``
(corporate / trade / labor / membership / cooperative / corp-without-capital-stock
— the connected SSFs). Everything excluded → ``fec-unparsed-manifest.json`` with a
``reason`` — **never silently dropped** (CLAUDE.md).

**Org rollup key (SPEC §13):** ``connected_organization_name`` if populated, else
the committee ``name``. (``connected_organization_name`` IS populated in bulk
``cm`` — the earlier "it's null" note was an API-side artifact, #170.)

**Canonical source (SPEC §13.5a):** ``itpas2`` is the single committee→candidate
file; there is no recipient-side file to cross-check against. So we **dedupe by
transaction id** and keep the **$10k/PAC/cycle invariant** as a *sanity flag*
(breach → flagged in the manifest, never dropped — it may legitimately trip where
affiliation can't be collapsed, §13.8).

**Super-PAC IEs (GH-0194, SPEC §13.7):** a fifth source,
``independent_expenditure_<cycle>.csv`` — a *headered CSV*, unlike the four
pipe-delimited files. A **separately-footed** slice: uncoordinated outside
spending FOR/AGAINST a House candidate, NEVER summed with the Path-1 connected-SSF
hard money (a distinct ``provenance`` tag enforces this). Both directions are
kept and tagged; House-only; the spender's ``connected_organization_name`` is
joined from ``cm`` raw (no industry classification); the target candidate joins to
a bioguide via the same ``id.fec[]`` bridge. Emitted to its own
``independent-expenditures.json`` with a complete residual (kept + filtered = raw,
an unattributed House IE kept AND flagged — never dropped).
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from .cli import fec_parsed_dir, fec_raw_dir
from .fec_pull import fec_ie_name
from .legislators import (
    UNRESOLVED_COMMITTEE,
    build_fec_member_links,
    load_legislator_index,
)
from .schemas import (
    FEC_IE_SUPPORT_OPPOSE_LABELS,
    FEC_ORG_TYPE_LABELS,
    FEC_SCHEMA_VERSION,
    FecCommittee,
    FecIndependentExpenditure,
    FecMemberCandidateLink,
    FecPacContribution,
)

# Path-1 connected-SSF organization-type codes (SPEC §13.1 / §13.3) — the *raw*
# single-letter codes (the keys of the label table). A contribution is kept iff its
# contributing committee's ``organization_type_raw`` is one of these; testing the
# raw code (not the normalized label) keeps an unmapped code out by construction.
PATH1_ORG_TYPES = frozenset(FEC_ORG_TYPE_LABELS)

# Bulk files are latin-1 (SPEC §13.5a) — not UTF-8; an accented committee name
# would mojibake or raise under UTF-8, so decode latin-1 explicitly.
BULK_ENCODING = "latin-1"

# The contributing-committee designation in ``ccl`` that names a candidate's
# *principal* campaign committee — the recipient side of an 11C receipt (§13.5a).
PRINCIPAL_DESIGNATION = "P"

# The $10k/PAC/cycle hard-money limit ($5k primary + $5k general). A
# committee→candidate total above this in one cycle is a sanity-flag breach
# (SPEC §13.5a) — surfaced in the manifest, never dropped (it may legitimately
# trip where affiliated PACs can't be collapsed, §13.8).
PAC_CYCLE_LIMIT = 10_000.0

# Residual reasons emitted for an excluded/flagged contribution (never a silent
# drop, CLAUDE.md). Declared here so the residual vocabulary is one stable list:
#   * unresolved_committee — the contributing committee id is absent from ``cm``
#     (no org type to test the Path-1 filter against; can't classify it).
#   * not_connected_ssf    — the committee IS in ``cm`` but its org type is not a
#     connected SSF (leadership/non-connected/ideological/super PAC — out of scope).
#   * malformed_short_row  — the itpas2 row carried fewer columns than the fixed
#     positional layout needs (< 18), so it can't be positionally trusted; kept as
#     a residual (with what's available) and counted, never silently dropped.
FEC_UNPARSED_REASONS = (
    "unresolved_committee",
    "not_connected_ssf",
    "malformed_short_row",
)

# Residual reasons emitted for an IE row that is filtered or unattributed
# (GH-0194; never a silent drop, CLAUDE.md):
#   * not_house_candidate — the IE targets a non-House office (P/S/blank). Out of
#     scope for this House-only slice; counted, never kept.
#   * unresolved_candidate — a House IE whose ``cand_id`` is blank (no candidate to
#     attribute it to). Reported ``unattributed`` and STILL KEPT (the issue is
#     explicit: never dropped); the residual records it so the count reconciles.
#   * malformed_short_row — the CSV row carried fewer columns than the header, so
#     it can't be positionally trusted; kept as a residual, counted, never dropped.
FEC_IE_UNPARSED_REASONS = (
    "not_house_candidate",
    "unresolved_candidate",
    "malformed_short_row",
)

# The IE bulk file is a *headered CSV* (UTF-8, comma-delimited) — NOT the
# pipe-delimited latin-1 layout of the four Path-1 files (SPEC §13.5a, GH-0194).
# Named per cycle (``independent_expenditure_<cycle>.csv``), read by header name.
IE_HOUSE_OFFICE = "H"

# The IE expenditure date is ``DD-MON-YY`` (e.g. ``28-OCT-24``) — a different
# format from itpas2's ``MMDDYYYY``. Month abbreviations are uppercase in the file.
_IE_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


class FecParseError(Exception):
    """An FEC parse failed in a way the user must see (stderr, non-zero exit)."""


def _read_rows(path: Path) -> list[list[str]]:
    """Read one bulk file into a list of pipe-split rows (offline, deterministic).

    Pipe-delimited, no header row, latin-1, LF-terminated (SPEC §13.5a). A blank
    trailing line (FEC files end with a newline) is skipped; every other line is
    split on ``|`` with no further trimming so column positions stay exact.
    """
    rows: list[list[str]] = []
    for line in path.read_text(encoding=BULK_ENCODING).splitlines():
        if not line:
            continue
        rows.append(line.split("|"))
    return rows


def _norm_org_type(raw: str) -> tuple[str | None, str | None]:
    """A committee's ``ORG_TP`` → (normalized label, raw code), the §2.3 pattern.

    A blank type → ``(None, None)`` (no type on the record). A non-blank type maps
    via :data:`FEC_ORG_TYPE_LABELS`; an *unmapped* non-blank code keeps the raw
    code beside a ``None`` label (raw alongside normalized, never dropped) — though
    the Path-1 filter only ever keeps the six mapped codes, so an unmapped type is
    filtered out as ``not_connected_ssf`` regardless.
    """
    code = (raw or "").strip()
    if not code:
        return None, None
    return FEC_ORG_TYPE_LABELS.get(code), code


def build_committees(cm_rows: list[list[str]]) -> dict[str, FecCommittee]:
    """Build the committee master index: ``committee_id`` → :class:`FecCommittee`.

    Columns (SPEC §13.5a): ``CMTE_ID`` 0, ``CMTE_NM`` 1, ``CMTE_TP`` 9, ``ORG_TP``
    12, ``CONNECTED_ORG_NM`` 13. ``affiliation`` is left ``None`` — the bulk ``cm``
    file carries no affiliated-committee column (DECLARED LIMITATION, §13.8). A
    short row (fewer columns than expected) is skipped defensively; the bulk format
    is fixed-width-by-position, so a malformed row can't be positionally trusted.
    """
    committees: dict[str, FecCommittee] = {}
    for row in cm_rows:
        if len(row) < 14:
            continue
        committee_id = row[0].strip()
        if not committee_id:
            continue
        label, raw = _norm_org_type(row[12])
        connected = (row[13] or "").strip() or None
        committees[committee_id] = FecCommittee(
            committee_id=committee_id,
            name=row[1].strip(),
            connected_organization_name=connected,
            organization_type=label,
            organization_type_raw=raw,
            committee_type=(row[9] or "").strip() or None,
            affiliation=None,  # no bulk source (§13.8) — never faked.
        )
    return committees


def build_principal_committees(ccl_rows: list[list[str]]) -> dict[str, str]:
    """Build candidate_id → principal-committee id from ``ccl`` (designation ``P``).

    Columns (SPEC §13.5a): ``CAND_ID`` 0, ``CMTE_ID`` 3, ``CMTE_DSGN`` 5. Only the
    ``P`` (principal) linkage is kept — that committee is the recipient side of an
    11C receipt and the value that fills the #169
    :data:`~openhouse.legislators.UNRESOLVED_COMMITTEE` seam. A candidate with no
    ``P`` linkage simply isn't in the map (the link then stays unresolved, never a
    guessed committee).
    """
    principal: dict[str, str] = {}
    for row in ccl_rows:
        if len(row) < 6:
            continue
        if (row[5] or "").strip() != PRINCIPAL_DESIGNATION:
            continue
        candidate_id = row[0].strip()
        committee_id = row[3].strip()
        if candidate_id and committee_id:
            principal.setdefault(candidate_id, committee_id)
    return principal


def resolve_links(
    links: list[FecMemberCandidateLink], principal: dict[str, str]
) -> tuple[list[FecMemberCandidateLink], list[dict]]:
    """Fill each link's ``committee_id`` seam from ``ccl`` (offline, no network).

    #169 produced links whose ``committee_id`` is the
    :data:`~openhouse.legislators.UNRESOLVED_COMMITTEE` sentinel — the seam this
    pass fills from the candidate→principal-committee map (``ccl`` designation
    ``P``). A candidate absent from that map can't be resolved offline; rather than
    guess a committee, the link is left unresolved and recorded in the returned
    residual (reason ``no_principal_committee``) — sound over complete (CLAUDE.md).

    Returns ``(resolved_links, residual)`` where ``resolved_links`` keeps only the
    links that resolved (a downstream attribution must not carry a sentinel
    committee), and ``residual`` is one ``{bioguide_id, candidate_id, reason}``
    entry per unresolved link.
    """
    resolved: list[FecMemberCandidateLink] = []
    residual: list[dict] = []
    for link in links:
        committee_id = principal.get(link.candidate_id)
        if not committee_id:
            residual.append(
                {
                    "bioguide_id": link.bioguide_id,
                    "candidate_id": link.candidate_id,
                    "reason": "no_principal_committee",
                }
            )
            continue
        resolved.append(
            link.model_copy(update={"committee_id": committee_id})
        )
    return resolved, residual


def _parse_amount(raw: str) -> float:
    """A bulk ``TRANSACTION_AMT`` string → float dollars. Blank → 0.0.

    FEC itemizes the actual dollar amount (not a Clerk-style bucket); the column is
    a plain decimal string. A blank/unparseable amount is treated as 0.0 rather
    than dropping the row — the contribution is preserved (amount is not the row's
    identity), consistent with "never silently drop".
    """
    text = (raw or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_date(raw: str):
    """A bulk ``TRANSACTION_DT`` (``MMDDYYYY``) → ``date``, or ``None`` if absent.

    The FEC bulk date is a bare 8-char ``MMDDYYYY`` with no separators. Anything
    that doesn't parse cleanly (blank, wrong length, impossible month/day) yields
    ``None`` — the contribution is kept regardless (the date is not its identity).
    """
    text = (raw or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[4:8]), int(text[0:2]), int(text[2:4]))
    except ValueError:
        return None


def _read_ie_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read the IE bulk CSV into ``(header, rows)`` (GH-0194, SPEC §13.5a).

    The IE file is a **headered, comma-delimited, UTF-8 CSV** (the by-hand probe)
    — wholly unlike the four pipe-delimited latin-1 Path-1 files, so it gets its
    own reader (``csv`` handles the quoted, comma-containing purpose fields the
    Path-1 splitter would mangle). Returns the header row and the data rows; an
    empty file yields ``([], [])``.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        rows = list(reader)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _parse_ie_date(raw: str):
    """An IE ``exp_date`` (``DD-MON-YY``) → ``date``, or ``None`` if absent.

    The IE file dates as ``28-OCT-24`` (2-digit year, uppercase month abbrev),
    unlike itpas2's ``MMDDYYYY``. The 2-digit year is read as ``20YY`` (FEC cycle
    files are recent). Anything that doesn't parse cleanly yields ``None`` — the
    expenditure is kept regardless (the date is not its identity).
    """
    text = (raw or "").strip().upper()
    parts = text.split("-")
    if len(parts) != 3:
        return None
    day, mon, year = parts
    if not day.isdigit() or not year.isdigit() or mon not in _IE_MONTHS:
        return None
    try:
        return date(2000 + int(year), _IE_MONTHS[mon], int(day))
    except ValueError:
        return None


def _norm_support_oppose(raw: str) -> tuple[str | None, str | None]:
    """An IE ``sup_opp`` code → (normalized label, raw code), the §2.3 pattern.

    ``S`` → ``support``, ``O`` → ``oppose``; a blank/unmapped code keeps a ``None``
    label beside its raw value (raw alongside normalized, never dropped).
    """
    code = (raw or "").strip()
    if not code:
        return None, None
    return FEC_IE_SUPPORT_OPPOSE_LABELS.get(code), code


def parse_independent_expenditures(
    header: list[str],
    rows: list[list[str]],
    committees: dict[str, FecCommittee],
    candidate_to_bioguide: dict[str, str],
) -> tuple[list[FecIndependentExpenditure], list[dict], dict[str, int]]:
    """Filter + normalize IE CSV rows into kept House IEs + residual (GH-0194).

    Columns are read by **header name** (the CSV carries a header, unlike the
    positional Path-1 files): ``cand_id``, ``spe_id``, ``spe_nam``, ``can_office``,
    ``sup_opp``, ``exp_amo``, ``exp_date``, ``pur``, ``image_num``, ``tran_id``.

    Per row:

    0. **Short rows** — a row with fewer columns than the header can't be trusted
       by name, so it lands in the residual ``malformed_short_row`` (with the column
       count) and is counted — NEVER silently dropped (CLAUDE.md).
    1. **House-only filter** — a row whose ``can_office`` is not ``H`` is out of
       scope; it lands in the residual ``not_house_candidate`` and is counted.
    2. **Both directions kept** — a House row is kept whether ``sup_opp`` is ``S``,
       ``O``, or blank; the raw indicator rides along beside the normalized label.
    3. **Attribution** — the targeted ``cand_id`` is joined to a bioguide via the
       CC0 ``id.fec[]`` bridge where the link exists (``bioguide`` else ``None``).
       A House row with a blank ``cand_id`` is ``unattributed``: it is **still
       kept** (never dropped, per the issue) and ALSO recorded in the residual
       ``unresolved_candidate`` so the count reconciles.
    4. **Connected org** — the spender ``spe_id`` is joined to ``cm`` to surface
       ``connected_organization_name`` (raw; no industry classification, §13.7);
       a spender absent from ``cm`` simply leaves it ``None``.

    Returns ``(kept, residual, by_direction)``: ``kept`` in first-appearance row
    order; ``residual`` one entry per filtered/unattributed row; ``by_direction`` a
    count of kept IEs per normalized direction (``support`` / ``oppose`` /
    ``unspecified`` for a blank indicator).
    """
    col = {name: i for i, name in enumerate(header)}
    ncols = len(header)
    kept: list[FecIndependentExpenditure] = []
    residual: list[dict] = []
    by_direction: dict[str, int] = defaultdict(int)

    def cell(row: list[str], name: str) -> str:
        return row[col[name]].strip() if col.get(name) is not None else ""

    for row in rows:
        if len(row) < ncols:
            residual.append({"columns": len(row), "reason": "malformed_short_row"})
            continue

        office = cell(row, "can_office").upper()
        spender = cell(row, "spe_id")
        candidate = cell(row, "cand_id")
        support_oppose, support_oppose_raw = _norm_support_oppose(cell(row, "sup_opp"))
        base = {
            "spender_committee_id": spender,
            "candidate_id": candidate or None,
            "office": office or None,
            "support_oppose_raw": support_oppose_raw,
        }

        if office != IE_HOUSE_OFFICE:
            residual.append({**base, "reason": "not_house_candidate"})
            continue

        # A blank cand_id is unattributed: kept (never dropped) AND flagged.
        if not candidate:
            residual.append({**base, "reason": "unresolved_candidate"})

        committee = committees.get(spender)
        connected = committee.connected_organization_name if committee else None

        kept.append(
            FecIndependentExpenditure(
                spender_committee_id=spender,
                spender_name=cell(row, "spe_nam") or None,
                connected_organization_name=connected,
                candidate_id=candidate or None,
                bioguide_id=candidate_to_bioguide.get(candidate) or None,
                office=IE_HOUSE_OFFICE,
                support_oppose=support_oppose,
                support_oppose_raw=support_oppose_raw,
                amount=_parse_amount(cell(row, "exp_amo")),
                date=_parse_ie_date(cell(row, "exp_date")),
                purpose=cell(row, "pur") or None,
                image_number=cell(row, "image_num") or None,
                transaction_id=cell(row, "tran_id") or None,
            )
        )
        by_direction[support_oppose or "unspecified"] += 1

    return kept, residual, dict(by_direction)


def parse_contributions(
    itpas2_rows: list[list[str]],
    committees: dict[str, FecCommittee],
) -> tuple[list[FecPacContribution], list[dict], dict[str, int]]:
    """Path-1-filter + normalize ``itpas2`` rows into kept contributions + residual.

    For each row (columns per SPEC §13.5a — contributor ``CMTE_ID`` 0, ``IMAGE_NUM``
    4, ``TRANSACTION_DT`` 13, ``TRANSACTION_AMT`` 14, recipient ``OTHER_ID`` 15,
    ``CAND_ID`` 16, ``TRAN_ID`` 17):

    0. **Short rows** — a row with fewer than 18 columns can't be positionally
       trusted, so it is not parsed; but it is NOT silently dropped (CLAUDE.md): it
       lands in the residual with reason ``malformed_short_row`` (carrying the
       contributor field if present + the observed column count) and is counted.
    1. **Dedupe by transaction id** — ``itpas2`` is the single canonical file
       (§13.5a), so the only double-counting risk is a literal repeated ``TRAN_ID``;
       the first occurrence wins, later duplicates are dropped silently (a true
       duplicate is not a lost contribution). A blank ``TRAN_ID`` is never deduped
       (it can't be a duplicate key) — every blank-id row is kept.
    2. **Path-1 filter** — join the contributor to ``cm``: a committee absent from
       ``cm`` → residual ``unresolved_committee``; a committee whose org type is not
       a connected SSF → residual ``not_connected_ssf``. Both land in the residual
       with the contributor id and amount, never silently dropped (CLAUDE.md).
    3. A kept contribution becomes a :class:`FecPacContribution`; the contributing
       committee's normalized ``organization_type`` rides along in ``by_org_type``
       so ``read`` can slice corporate-vs-labor (the manifest counts come from it).

    Returns ``(kept, residual, by_org_type)``: ``kept`` in first-appearance row
    order (deterministic); ``residual`` one entry per excluded row; ``by_org_type``
    a count of kept contributions per normalized org-type label.
    """
    kept: list[FecPacContribution] = []
    residual: list[dict] = []
    by_org_type: dict[str, int] = defaultdict(int)
    seen_tran: set[str] = set()

    for row in itpas2_rows:
        if len(row) < 18:
            # A short row can't be positionally trusted (the layout is fixed by
            # column position, §13.5a), but it is NEVER silently dropped (CLAUDE.md):
            # emit a residual with what's available — the contributor field if the
            # row has one, plus the observed column count — and count it.
            residual.append(
                {
                    "contributor_committee_id": row[0].strip() if row else None,
                    "columns": len(row),
                    "reason": "malformed_short_row",
                }
            )
            continue
        contributor = row[0].strip()
        recipient = row[15].strip()
        amount = _parse_amount(row[14])
        transaction_id = row[17].strip()
        if transaction_id and transaction_id in seen_tran:
            continue
        if transaction_id:
            seen_tran.add(transaction_id)

        base = {
            "contributor_committee_id": contributor,
            "recipient_committee_id": recipient,
            "transaction_id": transaction_id or None,
            "amount": amount,
        }
        committee = committees.get(contributor)
        if committee is None:
            residual.append({**base, "reason": "unresolved_committee"})
            continue
        if committee.organization_type_raw not in PATH1_ORG_TYPES:
            residual.append(
                {
                    **base,
                    "organization_type": committee.organization_type,
                    "organization_type_raw": committee.organization_type_raw,
                    "reason": "not_connected_ssf",
                }
            )
            continue

        kept.append(
            FecPacContribution(
                recipient_committee_id=recipient,
                contributor_committee_id=contributor,
                amount=amount,
                date=_parse_date(row[13]),
                image_number=row[4].strip() or None,
                transaction_id=transaction_id or None,
            )
        )
        by_org_type[committee.organization_type] += 1

    return kept, residual, dict(by_org_type)


def org_rollup_key(committee: FecCommittee) -> str:
    """The org-rollup key for a committee: ``connected_organization_name`` else
    ``name`` (SPEC §13). ``connected_organization_name`` is populated in bulk ``cm``
    (#170 corrected the API-side "it's null" artifact); fall back to the committee
    ``name`` only when it is blank.
    """
    return committee.connected_organization_name or committee.name


def check_pac_limit(
    kept: list[FecPacContribution],
    committees: dict[str, FecCommittee],
) -> list[dict]:
    """Flag any (contributor PAC → recipient) pair over the $10k/cycle limit (§13.5a).

    A *sanity flag*, not a drop: ``itpas2`` is canonical so there's no recipient
    side to cross-check, and an affiliated parent+subsidiary PAC pair can
    legitimately exceed $10k once we can't collapse them (§13.8). So a breach is
    reported in the manifest (with the org-rollup key, so the operator can see the
    likely affiliation collapse), never excluded from the kept set.

    Returns one ``{contributor_committee_id, recipient_committee_id, org, total,
    contributions}`` entry per breaching pair, in first-appearance order.
    """
    # Dicts preserve insertion order, so the first-appearance ordering of the keys
    # is free — no separate order list needed (the §13.5a sanity flag is reported
    # in the order each PAC→recipient pair first appears).
    totals: dict[tuple[str, str], float] = defaultdict(float)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for c in kept:
        key = (c.contributor_committee_id, c.recipient_committee_id)
        totals[key] += c.amount
        counts[key] += 1

    breaches: list[dict] = []
    for (contributor, recipient), total in totals.items():
        if total <= PAC_CYCLE_LIMIT:
            continue
        committee = committees.get(contributor)
        breaches.append(
            {
                "contributor_committee_id": contributor,
                "recipient_committee_id": recipient,
                "org": org_rollup_key(committee) if committee else None,
                "total": total,
                "contributions": counts[(contributor, recipient)],
            }
        )
    return breaches


def _kept_committees(
    kept: list[FecPacContribution], committees: dict[str, FecCommittee]
) -> list[FecCommittee]:
    """The distinct contributing committees behind ``kept``, in first-seen order.

    Only the committees that actually contributed a kept (Path-1) contribution are
    written to ``committees.json`` — the full ``cm`` file is tens of thousands of
    rows of which we keep a tiny connected-SSF slice, so emitting the whole master
    would bury the relevant records. Recipient (member) committees are NOT included
    here; they carry no org type and are reachable via the link records.
    """
    out: list[FecCommittee] = []
    seen: set[str] = set()
    for c in kept:
        cid = c.contributor_committee_id
        if cid in seen:
            continue
        seen.add(cid)
        committee = committees.get(cid)
        if committee is not None:
            out.append(committee)
    return out


def _write_json(path: Path, payload) -> None:
    """Byte-stable JSON write (``indent=2``, ``sort_keys``, trailing newline).

    Matches parse.py's convention so a re-parse from the same ``raw/`` is
    deterministic — two runs produce byte-identical files.
    """
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# The DECLARED affiliation limitation, surfaced verbatim in every manifest so the
# residual stays honest (SPEC §13.8 / CLAUDE.md): a member's PAC totals may
# double-count a parent and its subsidiary PAC as two separate orgs, because bulk
# ``cm`` carries no affiliation column to collapse them.
AFFILIATION_LIMITATION = (
    "Bulk cm has no affiliated-committee column, so FecCommittee.affiliation is "
    "None and affiliated PACs cannot be collapsed from bulk data. A member's "
    "org-level totals may therefore count a parent and its subsidiary PAC as two "
    "separate orgs, and the $10k/PAC/cycle invariant may legitimately trip across "
    "an un-collapsed affiliated pair. Sourcing affiliation is a future enhancement."
)


def parse_cycle(
    cycle: int,
    *,
    data_dir: Path,
    fetched_at: str,
) -> dict | None:
    """Parse one FEC cycle's bulk files into ``parsed/fec/<cycle>/`` (SPEC §13). Offline.

    Reads ``raw/fec/<cycle>/{ccl,cm,itpas2}.txt`` (written by ``fec pull``). If
    the bulk files are absent this is a clean skip (clear stderr message, returns
    ``None``) — not a crash, so a multi-cycle range survives an un-pulled cycle.
    Otherwise builds the committee + principal-committee indexes, resolves the #169
    member-link committee seam from ``ccl``, Path-1-filters + normalizes ``itpas2``,
    flags the $10k invariant, normalizes the separately-footed super-PAC IE slice
    (GH-0194, if the IE CSV is present), and writes ``committees.json`` +
    ``contributions.json`` + ``member-links.json`` +
    ``independent-expenditures.json`` + ``fec-parse-manifest.json`` +
    ``fec-unparsed-manifest.json``.

    Returns a compact summary dict, or ``None`` on a missing cycle.
    """
    raw_dir = fec_raw_dir(data_dir, cycle)
    required = {name: raw_dir / name for name in ("cm.txt", "ccl.txt", "itpas2.txt")}
    missing = [str(p) for p in required.values() if not p.exists()]
    if missing:
        print(
            f"fec {cycle}: bulk files missing ({', '.join(missing)}); skipping "
            f"(run `openhouse fec pull {cycle}` first).",
            file=sys.stderr,
        )
        return None

    cm_rows = _read_rows(required["cm.txt"])
    ccl_rows = _read_rows(required["ccl.txt"])
    itpas2_rows = _read_rows(required["itpas2.txt"])

    committees = build_committees(cm_rows)
    principal = build_principal_committees(ccl_rows)

    # Resolve the #169 member→committee seam from ccl (offline). The bioguides come
    # from the CC0 roster the clerk lane already caches; an empty roster simply
    # yields no links (the FEC parse still runs over the bulk files).
    legislators = load_legislator_index(data_dir)
    all_bioguides = sorted(legislators.by_fec)
    links, no_fec_warnings = build_fec_member_links(all_bioguides, legislators)
    resolved_links, unresolved_links = resolve_links(links, principal)

    kept, contribution_residual, by_org_type = parse_contributions(
        itpas2_rows, committees
    )
    limit_breaches = check_pac_limit(kept, committees)
    contributing = _kept_committees(kept, committees)

    # Super-PAC IEs (GH-0194) — a SEPARATELY-FOOTED slice, read from the headered
    # CSV ``fec pull`` saved beside the four pipe files. The candidate→bioguide map
    # is the *inverse* of the same CC0 ``id.fec[]`` bridge the contribution links
    # ride (§13.2), so an IE attributes to the same member, never a name match. The
    # IE file is optional: an old-pulled cycle without it is a clean skip of just
    # the IE outputs (the contribution parse above still ran).
    ie_path = raw_dir / fec_ie_name(cycle)
    candidate_to_bioguide = {link.candidate_id: link.bioguide_id for link in links}
    if ie_path.exists():
        ie_header, ie_rows = _read_ie_rows(ie_path)
        ie_data_rows = len(ie_rows)
        ie_kept, ie_residual, ie_by_direction = parse_independent_expenditures(
            ie_header, ie_rows, committees, candidate_to_bioguide
        )
    else:
        ie_data_rows = 0
        ie_kept, ie_residual, ie_by_direction = [], [], {}
        print(
            f"fec {cycle}: {fec_ie_name(cycle)} absent; skipping the "
            f"super-PAC IE slice (re-run `openhouse fec pull {cycle}` to acquire it).",
            file=sys.stderr,
        )

    parsed_dir = fec_parsed_dir(data_dir, cycle)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    _write_json(
        parsed_dir / "committees.json",
        [c.model_dump(mode="json") for c in contributing],
    )
    _write_json(
        parsed_dir / "contributions.json",
        [c.model_dump(mode="json") for c in kept],
    )
    _write_json(
        parsed_dir / "member-links.json",
        [link.model_dump(mode="json") for link in resolved_links],
    )
    # The IE slice gets its OWN output file so the separately-footed records never
    # blur into contributions.json (§13.7). Written even when empty so a consumer
    # can tell "no House IEs" from "IE file not pulled" (the latter logs to stderr).
    _write_json(
        parsed_dir / "independent-expenditures.json",
        [ie.model_dump(mode="json") for ie in ie_kept],
    )

    # Counts reconcile against the source: for itpas2, kept + filtered (which now
    # includes every malformed_short_row) + transaction-id duplicates == the total
    # raw itpas2 rows. The reference-file (cm/ccl) short-row drops are counted too,
    # so a positional drop in an index file is visible, not a silent gap (CLAUDE.md).
    by_org_type_sorted = {k: by_org_type[k] for k in sorted(by_org_type)}
    residual_reason_counts: dict[str, int] = defaultdict(int)
    for entry in contribution_residual:
        residual_reason_counts[entry["reason"]] += 1

    # IE residual reconciles the same way: kept + filtered == raw IE data rows
    # (note an unattributed House IE is BOTH kept and in the residual, so the two
    # don't simply sum to the total — ``ie_kept`` already includes it; the residual
    # is the audit trail, not a partition). The counts make this explicit.
    ie_by_direction_sorted = {k: ie_by_direction[k] for k in sorted(ie_by_direction)}
    ie_residual_reason_counts: dict[str, int] = defaultdict(int)
    for entry in ie_residual:
        ie_residual_reason_counts[entry["reason"]] += 1

    cm_short_rows = sum(1 for row in cm_rows if len(row) < 14)
    ccl_short_rows = sum(1 for row in ccl_rows if len(row) < 6)

    manifest = {
        "schema_version": FEC_SCHEMA_VERSION,
        "generated_at": fetched_at,
        "cycle": cycle,
        "counts": {
            "committees_total": len(committees),
            "committees_contributing": len(contributing),
            "contributions_kept": len(kept),
            "contributions_filtered": len(contribution_residual),
            "by_org_type": by_org_type_sorted,
            "filtered_by_reason": {
                k: residual_reason_counts.get(k, 0) for k in FEC_UNPARSED_REASONS
            },
            "member_links_resolved": len(resolved_links),
            "member_links_unresolved": len(unresolved_links),
            "members_without_fec_id": len(no_fec_warnings),
            "pac_limit_breaches": len(limit_breaches),
            # The super-PAC IE slice (GH-0194), separately footed. ``ie_filtered``
            # excludes non-House/short rows; an unattributed House IE is counted in
            # BOTH ie_kept and ie_filtered_by_reason.unresolved_candidate (kept, not
            # dropped — §13.7). ie_data_rows is the raw CSV total (minus header).
            "ie_kept": len(ie_kept),
            "ie_filtered": len(ie_residual),
            "ie_by_direction": ie_by_direction_sorted,
            "ie_filtered_by_reason": {
                k: ie_residual_reason_counts.get(k, 0)
                for k in FEC_IE_UNPARSED_REASONS
            },
            # Raw source row totals + reference-file short-row drops, so kept +
            # filtered (+ tran-id dupes) reconciles against the bulk file itself.
            "source_rows": {
                "itpas2_total": len(itpas2_rows),
                "cm_total": len(cm_rows),
                "cm_short_rows": cm_short_rows,
                "ccl_total": len(ccl_rows),
                "ccl_short_rows": ccl_short_rows,
                "ie_data_rows": ie_data_rows,
            },
        },
        "pac_limit_breaches": limit_breaches,
        "affiliation_limitation": AFFILIATION_LIMITATION,
    }
    _write_json(parsed_dir / "fec-parse-manifest.json", manifest)

    # Every excluded contribution + every unresolved member link, each with a
    # reason — never a silent gap (CLAUDE.md). The affiliation limitation is
    # repeated here so the residual file is self-describing.
    unparsed_manifest = {
        "schema_version": FEC_SCHEMA_VERSION,
        "generated_at": fetched_at,
        "cycle": cycle,
        "filtered_contributions": contribution_residual,
        "filtered_independent_expenditures": ie_residual,
        "unresolved_member_links": unresolved_links,
        "members_without_fec_id": no_fec_warnings,
        "affiliation_limitation": AFFILIATION_LIMITATION,
    }
    _write_json(parsed_dir / "fec-unparsed-manifest.json", unparsed_manifest)

    print(
        f"fec {cycle}: parsed {len(kept)} Path-1 contributions from "
        f"{len(contributing)} connected-SSF committees "
        f"(filtered {len(contribution_residual)}: "
        + ", ".join(
            f"{residual_reason_counts.get(k, 0)} {k}" for k in FEC_UNPARSED_REASONS
        )
        + f"; {len(resolved_links)} member links resolved, "
        f"{len(limit_breaches)} $10k-invariant flags); "
        f"{len(ie_kept)} House super-PAC IEs "
        f"({', '.join(f'{ie_by_direction_sorted[k]} {k}' for k in ie_by_direction_sorted) or 'none'}) "
        f"→ {parsed_dir}.",
        file=sys.stderr,
    )
    return {
        "cycle": cycle,
        "contributions_kept": len(kept),
        "contributions_filtered": len(contribution_residual),
        "by_org_type": by_org_type_sorted,
        "member_links_resolved": len(resolved_links),
        "member_links_unresolved": len(unresolved_links),
        "pac_limit_breaches": len(limit_breaches),
        "ie_kept": len(ie_kept),
        "ie_filtered": len(ie_residual),
        "ie_by_direction": ie_by_direction_sorted,
    }


def fec_parse(
    cycles: list[int],
    *,
    data_dir: Path,
    fetched_at: str,
) -> int:
    """Run ``openhouse fec parse`` for ``cycles`` (SPEC §13). Returns an exit code.

    Entirely offline and deterministic: each cycle reads only ``raw/fec/<cycle>/``
    and writes ``parsed/fec/<cycle>/``, and a re-run from the same ``raw/`` produces
    byte-identical output. A cycle whose bulk files are absent is a clean skip, not
    a crash, so a range survives a not-yet-pulled cycle. ``fetched_at`` is the
    single entry-time timestamp threaded into every manifest (SPEC §9 — no
    wall-clock in core logic).

    Emits one compact JSON summary object to **stdout** (machine-composable,
    CLAUDE.md "JSON to stdout"); progress/warnings go to stderr. Returns
    non-zero if no cycle produced output (every cycle's bulk files were absent).
    """
    summaries: list[dict] = []
    skipped: list[int] = []
    for cycle in cycles:
        summary = parse_cycle(cycle, data_dir=data_dir, fetched_at=fetched_at)
        if summary is None:
            skipped.append(cycle)
        else:
            summaries.append(summary)

    combined = {
        "command": "fec parse",
        "generated_at": fetched_at,
        "cycles": summaries,
        "skipped_cycles": skipped,
    }
    print(json.dumps(combined, indent=2, sort_keys=True))

    if not summaries:
        print(
            f"error: no cycles parsed; bulk files absent for {skipped} "
            f"(run `openhouse fec pull` first).",
            file=sys.stderr,
        )
        return 1
    return 0
