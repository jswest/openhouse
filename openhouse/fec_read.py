"""Query surface: ``openhouse fec read`` (SPEC §13) — offline, read-only, deterministic.

The FEC lane's analogue of the Clerk lane's :mod:`openhouse.read`: a **pure
function over** ``parsed/fec/<cycle>/`` (what :mod:`openhouse.fec_parse` wrote). It
never touches ``raw/`` or the network and never writes a byte. Two subcommands,
inverses of each other over the same Path-1 contribution set:

- ``donors <member> <range>`` — the connected-SSF PACs that gave to a member,
  rolled up to **organization** (key = ``connected_organization_name`` else the
  committee ``name``, exactly as #171 wrote it), each
  ``{org, organization_type, total, n_contributions}``, sorted by total desc. An
  optional ``--org-type`` filter slices the tagged set (corporate-only, labor-only).
- ``pac <org> <range>`` — the inverse: the members an org's PAC(s) supported, each
  ``{bioguide_id, total, n_contributions}``, sorted by total desc.

The sound-or-complete agreement (CLAUDE.md): every response is **complete over the
Path-1 itemized connected-SSF receipts** ``fec parse`` kept, with an explicit
**residual** to stderr — the filtered non-connected PACs + unmatched committees
the parse already counted, the affiliation-not-collapsed caveat, and the framing
that this is the *disclosed candidate-side* slice (hard money on a member's
committee), NOT total influence: no dark money, no super-PAC independent
expenditure, no soft money. The residual numbers are read straight from each
cycle's ``fec-parse-manifest.json`` (#171 already counted them) — never recomputed.

JSON to stdout is the machine/agent contract (``jq``-composable); ``--table`` is
human garnish; all prose, the guarantee, and the residual go to stderr; the exit
code is 0 unless something genuinely failed (an un-parsed cycle in a range is a
clean skip, not a failure).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import cli as cli_mod
from .fec_parse import org_rollup_key
from .schemas import FEC_ORG_TYPE_LABELS, FEC_SCHEMA_VERSION, FecCommittee

# The org-type labels a --org-type filter may name (the §13 connected-SSF classes,
# e.g. "corporation" / "labor"). Surfaced in --help and validated against, so a
# typo fails loudly rather than silently matching nothing.
_ORG_TYPE_LABELS = frozenset(FEC_ORG_TYPE_LABELS.values())


class FecReadError(Exception):
    """An ``fec read`` failed in a way the user must see (stderr, non-zero exit)."""


# ---------------------------------------------------------------------------
# Loading parsed FEC data (offline, read-only). Missing cycles degrade gracefully.
# ---------------------------------------------------------------------------


def _load_json(path: Path):
    """Read+parse one JSON file, or raise :class:`FecReadError` with a clear message."""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise FecReadError(f"could not read {path}: {exc}") from exc


def _cycle_dir(data_dir: Path, cycle: int) -> Path:
    return cli_mod.fec_parsed_dir(data_dir, cycle)


def _resolve_cycles(data_dir: Path, cycles: list[int]) -> tuple[list[int], list[int]]:
    """Split ``cycles`` into (present, skipped) by whether each is parsed on disk.

    "Present" = ``parsed/fec/<cycle>/contributions.json`` exists. Skipped cycles
    are reported on stderr by the caller; the query proceeds over the present ones
    (graceful degradation — SPEC §13 mirrors §5).
    """
    present: list[int] = []
    skipped: list[int] = []
    for cycle in cycles:
        if (_cycle_dir(data_dir, cycle) / "contributions.json").exists():
            present.append(cycle)
        else:
            skipped.append(cycle)
    return present, skipped


def _load_contributions(data_dir: Path, cycle: int) -> list[dict]:
    return _load_json(_cycle_dir(data_dir, cycle) / "contributions.json")


def _load_committees(data_dir: Path, cycle: int) -> dict[str, dict]:
    """``committee_id`` → committee record for one cycle (the contributing SSFs)."""
    records = _load_json(_cycle_dir(data_dir, cycle) / "committees.json")
    return {c["committee_id"]: c for c in records}


def _load_member_links(data_dir: Path, cycle: int) -> list[dict]:
    return _load_json(_cycle_dir(data_dir, cycle) / "member-links.json")


def _load_manifest(data_dir: Path, cycle: int) -> Optional[dict]:
    path = _cycle_dir(data_dir, cycle) / "fec-parse-manifest.json"
    if not path.exists():
        return None
    return _load_json(path)


# ---------------------------------------------------------------------------
# Residual + guarantee (the universal stderr lines). "Complete over the known,
# explicit residual for the unknown" (CLAUDE.md), tied to the parse manifest.
# ---------------------------------------------------------------------------


def _residual_counts(data_dir: Path, cycles: list[int]) -> dict:
    """Tally, across ``cycles``, the parse-manifest counts a query is bounded by.

    Read straight from each cycle's ``fec-parse-manifest.json`` ``counts`` block
    (#171 already counted them — never recomputed here): ``kept`` is the Path-1
    itemized connected-SSF receipts the answer is complete over; ``filtered`` is
    the excluded contributions, split into ``not_connected_ssf`` (in ``cm`` but not
    a connected SSF) + ``unresolved_committee`` (contributor absent from ``cm``).
    """
    kept = filtered = not_connected = unresolved = 0
    for cycle in cycles:
        manifest = _load_manifest(data_dir, cycle)
        if manifest is None:
            continue
        counts = manifest.get("counts", {})
        by_reason = counts.get("filtered_by_reason", {})
        kept += counts.get("contributions_kept", 0)
        filtered += counts.get("contributions_filtered", 0)
        not_connected += by_reason.get("not_connected_ssf", 0)
        unresolved += by_reason.get("unresolved_committee", 0)
    return {
        "kept": kept,
        "filtered": filtered,
        "not_connected_ssf": not_connected,
        "unresolved_committee": unresolved,
    }


def _print_guarantee_and_residual(data_dir: Path, cycles: list[int]) -> None:
    """Emit the SOUND-yet-complete guarantee + the explicit residual to stderr.

    The guarantee: complete over the Path-1 itemized connected-SSF receipts the
    parse kept. The residual: the filtered non-connected PACs + unmatched
    committees the parse counted, plus the affiliation-not-collapsed caveat, plus
    the "disclosed candidate-side slice, not total influence / no dark money"
    framing — so the bound is explicit and tied to the manifest.
    """
    r = _residual_counts(data_dir, cycles)
    print(
        f"guarantee: complete over the {r['kept']} Path-1 itemized connected-SSF "
        f"receipt(s) parsed in range — these are every disclosed corporate/labor/"
        f"trade-PAC contribution to the member's committee, none omitted.",
        file=sys.stderr,
    )
    print(
        f"residual: {r['filtered']} contribution(s) were filtered at parse and are "
        f"NOT represented here ({r['not_connected_ssf']} not_connected_ssf — "
        f"leadership/non-connected/super PACs out of Path-1 scope; "
        f"{r['unresolved_committee']} unresolved_committee — contributor absent "
        f"from the committee master). Affiliated parent+subsidiary PACs are NOT "
        f"collapsed (bulk cm carries no affiliation column, §13.8), so an org may "
        f"appear more than once. This is the DISCLOSED candidate-side slice (hard "
        f"money itemized on the member's committee), not total influence: no dark "
        f"money, no super-PAC independent expenditure, no soft money.",
        file=sys.stderr,
    )


def _warn_schema_drift(data_dir: Path, cycles: list[int]) -> None:
    """Emit ONE stderr warning if any in-range manifest's ``schema_version`` differs.

    ``read`` queries the on-disk JSON shape directly; a tree written by an older
    FEC schema may not match. Per "re-parse, not migrate" (CLAUDE.md) we only warn.
    """
    for cycle in cycles:
        manifest = _load_manifest(data_dir, cycle)
        if manifest is None:
            continue
        version = manifest.get("schema_version")
        if version != FEC_SCHEMA_VERSION:
            print(
                f"warning: parsed FEC tree was written by schema_version "
                f"{version!r}, but this read expects {FEC_SCHEMA_VERSION}. Results "
                f"may not match the current shape; re-run `openhouse fec parse` to "
                f"refresh (re-parse, not migrate).",
                file=sys.stderr,
            )
            return


def _print_skipped(skipped: list[int]) -> None:
    """Report not-yet-parsed cycles on stderr (graceful degradation, SPEC §13)."""
    if skipped:
        print(
            f"note: cycle(s) {skipped} are not parsed (no parsed/fec/<cycle>/); "
            f"answered from the parsed cycles only. Run `openhouse fec parse` for "
            f"them.",
            file=sys.stderr,
        )


def _require_present(data_dir: Path, present: list[int], cycles: list[int]) -> None:
    """Fail LOUDLY when NONE of the requested ``cycles`` are parsed on disk.

    A query over a data dir with no parsed cycles bounds nothing: an empty roll-up
    here would read like a trustworthy zero but is really "nothing parsed to query"
    — the sound/complete contract is violated by reporting it as an empty match.
    Mirrors :func:`openhouse.read._require_present`.
    """
    if present:
        return
    raise FecReadError(
        f"no parsed FEC data for cycle(s) {cycles} under {data_dir} (looked in "
        f"{data_dir / 'parsed' / 'fec'}/<cycle>/). This is NOT an empty match — "
        f"there is nothing parsed here to query. Check --data-dir / "
        f"OPENHOUSE_DATA_DIR points at a parsed corpus, then run `openhouse fec "
        f"parse` if needed."
    )


# ---------------------------------------------------------------------------
# Matching (--member / <org>). Name-string matching, not true identity (§6.2).
# ---------------------------------------------------------------------------


def _ci_contains(haystack: Optional[str], needle: str) -> bool:
    if not haystack:
        return False
    return needle.lower() in haystack.lower()


def _member_bioguides(data_dir: Path, cycles: list[int], needle: str) -> set[str]:
    """The set of ``bioguide_id``s whose member-link matches ``needle``.

    Matching reuses §6.2 semantics: a member is anchored by ``bioguide_id`` (the
    member-links carry no name — they ride the CC0 bioguide ladder, #169). The
    match is case-insensitive substring over ``bioguide_id`` (so a full bioguide
    pins exactly the member, and a fragment is a name-string-style fuzzy match over
    the only identity the parsed link carries). No name resolution is attempted
    offline against the FEC tree — the link has no name to match, and we never
    synthesize one (CLAUDE.md).
    """
    out: set[str] = set()
    for cycle in cycles:
        for link in _load_member_links(data_dir, cycle):
            bid = link.get("bioguide_id")
            if bid and _ci_contains(bid, needle):
                out.add(bid)
    return out


def _member_committees(data_dir: Path, cycles: list[int], needle: str) -> set[str]:
    """The recipient ``committee_id``s belonging to members matching ``needle``."""
    bioguides = _member_bioguides(data_dir, cycles, needle)
    committees: set[str] = set()
    for cycle in cycles:
        for link in _load_member_links(data_dir, cycle):
            if link.get("bioguide_id") in bioguides:
                cid = link.get("committee_id")
                if cid:
                    committees.add(cid)
    return committees


def _committee_to_bioguide(data_dir: Path, cycles: list[int]) -> dict[str, str]:
    """Reverse the member-links: recipient ``committee_id`` → ``bioguide_id``.

    The inverse direction (``pac``) attributes a receipt on a member's committee
    back to the member. First link wins per committee (a committee is one member's
    principal committee); deterministic over cycle then file order.
    """
    out: dict[str, str] = {}
    for cycle in cycles:
        for link in _load_member_links(data_dir, cycle):
            cid = link.get("committee_id")
            bid = link.get("bioguide_id")
            if cid and bid:
                out.setdefault(cid, bid)
    return out


# ---------------------------------------------------------------------------
# Table rendering (human garnish — stdout). Reused shape from read.py.
# ---------------------------------------------------------------------------


def _render_table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append(
            "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def _emit(payload, *, table: bool, table_fn) -> None:
    if table:
        headers, rows = table_fn(payload)
        print(_render_table(rows, headers))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Subcommand: donors <member> <range>
# ---------------------------------------------------------------------------


def _donors_table(rollup: list[dict]):
    headers = ["org", "organization_type", "total", "n_contributions"]
    rows = [
        [
            r["org"] or "",
            r["organization_type"] or "",
            f"{r['total']:.2f}",
            str(r["n_contributions"]),
        ]
        for r in rollup
    ]
    return headers, rows


def _rollup_donors(
    data_dir: Path, cycles: list[int], recipients: set[str], org_type: Optional[str]
) -> list[dict]:
    """Roll the connected-SSF receipts to ``recipients`` up to organization.

    Key = ``org_rollup_key`` (connected_organization_name else committee name, as
    #171 wrote it). Each entry carries the contributing committee's normalized
    ``organization_type`` so ``--org-type`` can slice the tagged set. Sorted by
    total desc, then org name asc for a deterministic tie-break.
    """
    totals: dict[str, dict] = {}
    for cycle in cycles:
        committees = _load_committees(data_dir, cycle)
        for c in _load_contributions(data_dir, cycle):
            if c.get("recipient_committee_id") not in recipients:
                continue
            committee = committees.get(c.get("contributor_committee_id"))
            if committee is None:
                # A kept contribution's contributor is always in committees.json
                # (the parse writes exactly the contributing SSFs), but guard so a
                # truncated tree degrades to skipping rather than crashing.
                continue
            otype = committee.get("organization_type")
            if org_type is not None and otype != org_type:
                continue
            key = org_rollup_key(FecCommittee(**committee))
            entry = totals.setdefault(
                key, {"org": key, "organization_type": otype, "total": 0.0, "n_contributions": 0}
            )
            entry["total"] += c.get("amount", 0.0)
            entry["n_contributions"] += 1
    return sorted(totals.values(), key=lambda e: (-e["total"], e["org"]))


def cmd_donors(args, data_dir: Path, cycles: list[int]) -> int:
    present, skipped = _resolve_cycles(data_dir, cycles)
    _print_skipped(skipped)
    _require_present(data_dir, present, cycles)
    _warn_schema_drift(data_dir, present)

    recipients = _member_committees(data_dir, present, args.member)
    rollup = _rollup_donors(data_dir, present, recipients, args.org_type)

    _emit(rollup, table=args.table, table_fn=_donors_table)
    if args.org_type is not None:
        print(
            f"note: --org-type {args.org_type!r} slices the tagged set to that "
            f"connected-SSF class only.",
            file=sys.stderr,
        )
    _print_guarantee_and_residual(data_dir, present)
    return 0


# ---------------------------------------------------------------------------
# Subcommand: pac <org> <range>
# ---------------------------------------------------------------------------


def _pac_table(rollup: list[dict]):
    headers = ["bioguide_id", "total", "n_contributions"]
    rows = [
        [r["bioguide_id"] or "", f"{r['total']:.2f}", str(r["n_contributions"])]
        for r in rollup
    ]
    return headers, rows


def _rollup_pac(
    data_dir: Path, cycles: list[int], org: str
) -> tuple[list[dict], int]:
    """Roll the receipts FROM committees whose org key matches ``org`` up to member.

    Inverse of ``donors``: select the contributing committees whose
    ``org_rollup_key`` contains ``org`` (case-insensitive substring — a fuzzy
    name-string match over the org name, NOT verified identity), then group their
    receipts by recipient member (``committee_id`` → ``bioguide_id`` via the
    links). A receipt whose recipient committee has no member link is counted in
    the returned ``unattributed`` residual rather than dropped. Sorted by total
    desc, then bioguide asc.
    """
    committee_to_bioguide = _committee_to_bioguide(data_dir, cycles)
    totals: dict[str, dict] = {}
    unattributed = 0
    for cycle in cycles:
        committees = _load_committees(data_dir, cycle)
        matching = {
            cid
            for cid, c in committees.items()
            if _ci_contains(org_rollup_key(FecCommittee(**c)), org)
        }
        for c in _load_contributions(data_dir, cycle):
            if c.get("contributor_committee_id") not in matching:
                continue
            bid = committee_to_bioguide.get(c.get("recipient_committee_id"))
            if bid is None:
                unattributed += 1
                continue
            entry = totals.setdefault(
                bid, {"bioguide_id": bid, "total": 0.0, "n_contributions": 0}
            )
            entry["total"] += c.get("amount", 0.0)
            entry["n_contributions"] += 1
    rollup = sorted(totals.values(), key=lambda e: (-e["total"], e["bioguide_id"]))
    return rollup, unattributed


def cmd_pac(args, data_dir: Path, cycles: list[int]) -> int:
    present, skipped = _resolve_cycles(data_dir, cycles)
    _print_skipped(skipped)
    _require_present(data_dir, present, cycles)
    _warn_schema_drift(data_dir, present)

    rollup, unattributed = _rollup_pac(data_dir, present, args.org)

    _emit(rollup, table=args.table, table_fn=_pac_table)
    if unattributed:
        print(
            f"note: {unattributed} receipt(s) from a matching PAC went to a "
            f"committee with no member link (candidate not in the CC0 roster / no "
            f"principal committee resolved) and are not attributed to a member.",
            file=sys.stderr,
        )
    _print_guarantee_and_residual(data_dir, present)
    return 0


# ---------------------------------------------------------------------------
# Argument parsing + dispatch (driven by cli.py's hand-off).
# ---------------------------------------------------------------------------


def build_read_parser() -> argparse.ArgumentParser:
    # --data-dir/--table on a shared parent so they parse EITHER before or after the
    # subcommand, exactly as clerk read does. SUPPRESS defaults so a value in one
    # position is never clobbered; run() applies the real defaults once.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir", default=argparse.SUPPRESS, help=cli_mod._DATA_DIR_HELP
    )
    common.add_argument(
        "--table",
        action="store_true",
        default=argparse.SUPPRESS,
        help="render a human-readable aligned table instead of JSON",
    )
    parser = argparse.ArgumentParser(
        prog="openhouse fec read",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Query the normalized FEC Path-1 data produced by `fec parse` (offline,\n"
            "read-only). Emits JSON to stdout (jq-composable); --table for humans.\n"
            "Every answer is COMPLETE over the disclosed connected-SSF receipts and\n"
            "prints its guarantee + residual to stderr: this is the candidate-side\n"
            "hard-money slice, NOT total influence (no dark money / super-PAC IE)."
        ),
        epilog=(
            "examples:\n"
            "  openhouse fec read donors A000370 2024\n"
            "  openhouse fec read donors A000370 2024 --org-type labor --table\n"
            "  openhouse fec read pac MACHINISTS 2023-2024"
        ),
        parents=[common],
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # donors <member> <range>
    p_donors = sub.add_parser(
        "donors",
        parents=[common],
        help="connected-SSF PACs that gave to a member, rolled up to organization",
    )
    p_donors.add_argument(
        "member",
        help="case-insensitive substring over the member's bioguide_id "
        "(name-string matching over the only identity the link carries, NOT true "
        "identity — SPEC §6.2)",
    )
    p_donors.add_argument("range", help="YYYY or YYYY-YYYY (expands to the FEC cycle)")
    p_donors.add_argument(
        "--org-type",
        dest="org_type",
        help="slice to one connected-SSF class: "
        + " | ".join(sorted(_ORG_TYPE_LABELS)),
    )

    # pac <org> <range>
    p_pac = sub.add_parser(
        "pac",
        parents=[common],
        help="the members an org's PAC(s) supported (inverse of donors)",
    )
    p_pac.add_argument(
        "org",
        help="case-insensitive substring over the org rollup key "
        "(connected_organization_name else committee name) — a fuzzy name match, "
        "NOT verified identity",
    )
    p_pac.add_argument("range", help="YYYY or YYYY-YYYY (expands to the FEC cycle)")

    return parser


def run(remainder: list[str], *, current_year: int) -> int:
    """Entry point for ``openhouse fec read`` (called from cli.py).

    Parses the subcommand + flags, expands the ``<year|range>`` to its enclosing
    FEC cycle(s) with the shared helpers (exactly as the other fec verbs do, SI-1),
    dispatches, and maps :class:`FecReadError` to a clean non-zero exit.
    ``current_year`` is injected (never read from the clock here) so this stays
    deterministic (SPEC §9).
    """
    parser = build_read_parser()
    args = parser.parse_args(remainder)
    data_dir = cli_mod.resolve_data_dir(getattr(args, "data_dir", None))
    args.table = getattr(args, "table", False)

    if getattr(args, "org_type", None) is not None and args.org_type not in _ORG_TYPE_LABELS:
        print(
            f"error: --org-type {args.org_type!r} is not a connected-SSF class; "
            f"choose one of {sorted(_ORG_TYPE_LABELS)}.",
            file=sys.stderr,
        )
        return 2

    try:
        years = cli_mod.parse_year_range(args.range, current_year)
    except cli_mod.YearRangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    cycles = cli_mod.expand_years_to_cycles(years)
    if cycles != years:
        named = ", ".join(str(y) for y in years)
        resolved = ", ".join(str(c) for c in cycles)
        print(
            f"note: FEC reports on 2-year cycles (even-year-ending); year(s) "
            f"{named} resolve to cycle(s) {resolved}.",
            file=sys.stderr,
        )

    try:
        if args.subcommand == "donors":
            return cmd_donors(args, data_dir, cycles)
        if args.subcommand == "pac":
            return cmd_pac(args, data_dir, cycles)
    except FecReadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown fec read subcommand {args.subcommand!r}")
    return 2  # unreachable; parser.error exits
