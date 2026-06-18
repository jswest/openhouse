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
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from .cli import fec_parsed_dir, fec_raw_dir
from .legislators import (
    UNRESOLVED_COMMITTEE,
    build_fec_member_links,
    load_legislator_index,
)
from .schemas import (
    FEC_ORG_TYPE_LABELS,
    FEC_SCHEMA_VERSION,
    FecCommittee,
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
FEC_UNPARSED_REASONS = ("unresolved_committee", "not_connected_ssf")


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


def parse_contributions(
    itpas2_rows: list[list[str]],
    committees: dict[str, FecCommittee],
) -> tuple[list[FecPacContribution], list[dict], dict[str, int]]:
    """Path-1-filter + normalize ``itpas2`` rows into kept contributions + residual.

    For each row (columns per SPEC §13.5a — contributor ``CMTE_ID`` 0, ``IMAGE_NUM``
    4, ``TRANSACTION_DT`` 13, ``TRANSACTION_AMT`` 14, recipient ``OTHER_ID`` 15,
    ``CAND_ID`` 16, ``TRAN_ID`` 17):

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
    flags the $10k invariant, and writes ``committees.json`` + ``contributions.json``
    + ``member-links.json`` + ``fec-parse-manifest.json`` + ``fec-unparsed-manifest.json``.

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

    # Counts reconcile: kept + len(contribution_residual) == itpas2 rows that
    # carried >=18 columns and were not transaction-id duplicates.
    by_org_type_sorted = {k: by_org_type[k] for k in sorted(by_org_type)}
    residual_reason_counts: dict[str, int] = defaultdict(int)
    for entry in contribution_residual:
        residual_reason_counts[entry["reason"]] += 1

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
        f"{len(limit_breaches)} $10k-invariant flags) → {parsed_dir}.",
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
