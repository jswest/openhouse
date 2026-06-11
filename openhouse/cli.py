"""Command-line interface: arg parsing, the shared year-range parser, dispatch.

``pull`` now implements the full acquisition path — the index ZIP (issue #3) and
the PDF bodies routed by FilingType (issue #4); ``parse`` / ``read`` remain stubs
that print "not implemented" to stderr and exit non-zero, landing in later
sub-issues.

The year-range parser is shared infrastructure (SPEC §9). It is kept pure and
wall-clock-free by taking ``current_year`` as a parameter, so it is testable
without ``datetime.now()``. The CLI entry point is the only place that reads the
clock, injecting ``datetime.now().year`` once.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from . import pull as pull_mod

# SPEC §2.1: the bulk index covers 2008 → present; PTRs (STOCK Act) appear only
# from 2012 onward.
MIN_YEAR = 2008
PTR_START_YEAR = 2012

_LEGAL_NOTICE = (
    "Clerk FD data carries a statutory use restriction: not for commercial use, "
    "soliciting, or establishing credit ratings (news/media dissemination "
    "excepted)."
)


class YearRangeError(ValueError):
    """Raised when a year-range argument is malformed or out of bounds."""


def parse_year_range(arg: str, current_year: int) -> list[int]:
    """Parse a ``YYYY`` or ``YYYY-YYYY`` argument into an inclusive year list.

    Validates against the inclusive bound ``[2008, current_year]``. ``current_year``
    is injected (never read from the clock here) so this stays deterministic and
    testable (SPEC §9: no wall-clock in core logic).

    A range covering any year before 2012 emits a warning on stderr that PTRs are
    absent before then (SPEC §2.1) — it is a warning, not an error.

    Raises :class:`YearRangeError` on malformed input, out-of-bounds years, or a
    reversed range.
    """
    parts = arg.split("-")
    if len(parts) == 1:
        start = end = _parse_one(parts[0], arg)
    elif len(parts) == 2:
        start = _parse_one(parts[0], arg)
        end = _parse_one(parts[1], arg)
    else:
        raise YearRangeError(
            f"Invalid year range {arg!r}: expected YYYY or YYYY-YYYY."
        )

    if start > end:
        raise YearRangeError(
            f"Invalid year range {arg!r}: start year {start} is after end year {end}."
        )

    for year in (start, end):
        if year < MIN_YEAR:
            raise YearRangeError(
                f"Year {year} is before {MIN_YEAR}: the Clerk bulk index begins "
                f"in {MIN_YEAR}."
            )
        if year > current_year:
            raise YearRangeError(
                f"Year {year} is after the current year {current_year}."
            )

    if start < PTR_START_YEAR:
        print(
            f"warning: PTRs (Periodic Transaction Reports) are absent before "
            f"{PTR_START_YEAR} (STOCK Act); years {start}-"
            f"{min(end, PTR_START_YEAR - 1)} will carry annual filings only.",
            file=sys.stderr,
        )

    return list(range(start, end + 1))


def _parse_one(token: str, arg: str) -> int:
    token = token.strip()
    if not token.isdigit() or len(token) != 4:
        raise YearRangeError(
            f"Invalid year {token!r} in {arg!r}: expected a 4-digit year (YYYY)."
        )
    return int(token)


VALID_PDF_TYPES = ("ptr", "fd")


def parse_types(arg: str) -> list[str]:
    """Parse the ``--types`` comma-list into the PDF families to fetch.

    Accepts ``ptr``, ``fd``, or both (in any order, case-insensitive). Raises
    :class:`YearRangeError` (the CLI's uniform arg-error type) on an unknown
    token, so a typo fails fast rather than silently fetching nothing.
    """
    families = [t.strip().lower() for t in arg.split(",") if t.strip()]
    if not families:
        raise YearRangeError("--types is empty: expected ptr, fd, or both.")
    unknown = [t for t in families if t not in VALID_PDF_TYPES]
    if unknown:
        raise YearRangeError(
            f"--types has unknown value(s) {unknown}: expected ptr and/or fd."
        )
    # De-dupe while preserving the canonical order.
    return [t for t in VALID_PDF_TYPES if t in families]


def _stub(command: str) -> int:
    """A not-yet-implemented subcommand: explain on stderr, exit non-zero."""
    print(f"openhouse {command}: not implemented", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openhouse",
        description=(
            "Pull, parse, and query U.S. House financial disclosures from the "
            "Office of the Clerk. " + _LEGAL_NOTICE
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    pull_p = subparsers.add_parser(
        "pull", help="acquire raw artifacts from the Clerk (network)"
    )
    pull_p.add_argument("years", help="YYYY or YYYY-YYYY")
    pull_p.add_argument(
        "--index-only",
        action="store_true",
        help="fetch and extract only the annual index ZIP, not the PDF bodies",
    )
    pull_p.add_argument(
        "--types",
        default="ptr,fd",
        help=(
            "comma-separated PDF families to fetch: ptr, fd, or both "
            "(default: ptr,fd)"
        ),
    )
    pull_p.add_argument(
        "--data-dir",
        default="./data",
        help="root data directory (default: ./data)",
    )
    pull_p.add_argument(
        "--delay",
        type=float,
        default=pull_mod.DEFAULT_DELAY_SECONDS,
        help=(
            f"seconds between requests (default: {pull_mod.DEFAULT_DELAY_SECONDS}; "
            "the polite floor — lowering it is a deliberate choice)"
        ),
    )
    pull_p.add_argument(
        "--concurrency",
        type=int,
        default=pull_mod.DEFAULT_CONCURRENCY,
        help=(
            f"concurrent requests (default: {pull_mod.DEFAULT_CONCURRENCY}; "
            "v0.1 is sequential-only — values >1 are accepted but not yet "
            "implemented)"
        ),
    )
    pull_p.add_argument(
        "--contact",
        default=None,
        help=(
            "contact email appended to the User-Agent (or set OPENHOUSE_CONTACT)"
        ),
    )
    pull_p.add_argument(
        "--user-agent",
        default=None,
        help="override the User-Agent string entirely",
    )
    pull_p.add_argument(
        "--force",
        action="store_true",
        help="re-download even if the index is already present (refreshed daily)",
    )

    parse_p = subparsers.add_parser(
        "parse", help="transform raw artifacts into normalized JSON (offline)"
    )
    parse_p.add_argument("years", help="YYYY or YYYY-YYYY")

    read_p = subparsers.add_parser(
        "read", help="query the normalized JSON (offline, read-only)"
    )
    read_p.add_argument(
        "args", nargs=argparse.REMAINDER, help="read subcommand and arguments"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # The single wall-clock read for the whole program (SPEC §9): both the
    # range-validation year and the manifest ``fetched-at`` derive from it.
    now = datetime.now()
    current_year = now.year
    fetched_at = now.isoformat()

    if args.command in ("pull", "parse"):
        # Validate the range now so a bad argument fails fast and uniformly.
        try:
            years = parse_year_range(args.years, current_year)
        except YearRangeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.command == "parse":
            return _stub("parse")
        # pull: index (issue #3) + PDF bodies routed by §2.2 (issue #4).
        try:
            types = parse_types(args.types)
        except YearRangeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        contact = args.contact or os.environ.get("OPENHOUSE_CONTACT")
        try:
            return pull_mod.pull(
                years,
                data_dir=Path(args.data_dir),
                index_only=args.index_only,
                delay=args.delay,
                concurrency=args.concurrency,
                contact=contact,
                user_agent=args.user_agent,
                force=args.force,
                types=types,
                fetched_at=fetched_at,
            )
        except pull_mod.PullError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.command == "read":
        return _stub("read")

    parser.error(f"unknown command {args.command!r}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":
    raise SystemExit(main())
