"""Command-line interface: arg parsing, the shared year-range parser, dispatch.

The grammar is **source × verb** (#174): a source noun (``clerk`` / ``fec``)
above the pipeline verbs, e.g. ``openhouse clerk pull 2024``. Today only the
``clerk`` source is implemented; ``fec`` is scaffolded as stubs (#167). The
tool-level verbs ``ready`` (and the repo-local ``release`` skill) stay
**top-level** — they are not source-scoped.

Under ``clerk``: ``pull`` implements the full acquisition path — the index ZIP
(issue #3) and the PDF bodies routed by FilingType (issue #4). ``parse``
implements the offline normalization + PDF-classification pass (issues #6/#7);
``read`` (issue #10) is the offline, read-only query surface — its REMAINDER args
are dispatched into ``openhouse/read.py``, which owns its own sub-parser.

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

from . import __version__
from . import parse as parse_mod
from . import pull as pull_mod
from . import read as read_mod
from . import ready as ready_mod

# SPEC §2.1: the bulk index covers 2008 → present; PTRs (STOCK Act) appear only
# from 2012 onward.
MIN_YEAR = 2008
PTR_START_YEAR = 2012

_LEGAL_NOTICE = (
    "Clerk FD data carries a statutory use restriction: not for commercial use, "
    "soliciting, or establishing credit ratings (news/media dissemination "
    "excepted)."
)

# FEC bulk/API data is PUBLIC DOMAIN — different legal footing from Clerk FD
# (#167). The one statutory bar is 52 U.S.C. §30111(a): contributor information
# may not be sold or used to solicit contributions or for any commercial purpose.
# Full wording lands in #173; this is the scaffold stub (SPEC §13).
_FEC_LEGAL_NOTICE = (
    "FEC data is public domain, but 52 U.S.C. §30111 bars using contributor "
    "information to solicit contributions or for any commercial purpose."
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


def year_to_cycle(year: int) -> int:
    """Expand a named year to its enclosing FEC 2-year cycle (SPEC §13).

    FEC reports on 2-year cycles labelled by their **even** ending year, so an
    odd year folds up into the next even year (2023 → 2024) and an even year is
    its own cycle (2024 → 2024). Pure and wall-clock-free — the caller passes
    already-validated years.
    """
    return year if year % 2 == 0 else year + 1


def expand_years_to_cycles(years: list[int]) -> list[int]:
    """Map a validated year list to its sorted, de-duped enclosing cycles (§13).

    Both years of a cycle collapse to one entry (so ``fec pull 2023`` and
    ``fec pull 2024`` both resolve to the 2024 cycle, and a ``2023-2024`` range is
    a single cycle), preserving ascending order.
    """
    return sorted({year_to_cycle(y) for y in years})


def _parse_one(token: str, arg: str) -> int:
    token = token.strip()
    if not token.isdigit() or len(token) != 4:
        raise YearRangeError(
            f"Invalid year {token!r} in {arg!r}: expected a 4-digit year (YYYY)."
        )
    return int(token)


DATA_DIR_ENV = "OPENHOUSE_DATA_DIR"
# The default store is a single per-user dotfolder in $HOME (resolved at call
# time via Path.home()), NOT cwd-relative — so pull/parse/read land in one
# stable place regardless of which directory the tool is launched from.
DEFAULT_DATA_DIR = "~/.openhouse"

_DATA_DIR_HELP = (
    f"root data directory (precedence: this flag, then ${DATA_DIR_ENV}, then "
    f"the {DEFAULT_DATA_DIR} default)"
)

_shadow_warning_emitted = False


def _warn_shadowed_local_data() -> None:
    """One-time stderr note when the new ``~/.openhouse`` default shadows a
    non-empty cwd-relative ``./data`` — so users from before #80 aren't
    surprised by an apparently-empty store. We do NOT auto-migrate or read from
    ``./data``; this is purely informational.
    """
    global _shadow_warning_emitted
    if _shadow_warning_emitted:
        return
    local = Path("./data")
    try:
        non_empty = local.is_dir() and any(local.iterdir())
    except OSError:
        non_empty = False
    if not non_empty:
        return
    _shadow_warning_emitted = True
    print(
        f"note: a non-empty ./data exists here but the default store is now "
        f"{DEFAULT_DATA_DIR}; ./data is ignored. Pass --data-dir ./data (or set "
        f"${DATA_DIR_ENV}) to use it.",
        file=sys.stderr,
    )


_legacy_layout_warning_emitted = False


def _warn_legacy_clerk_layout(data_dir: Path) -> None:
    """One-time stderr nudge when a pre-namespace ``raw/<year>/`` is detected (#174).

    The on-disk layout moved to ``raw/clerk/<year>/`` + ``parsed/clerk/<year>/``.
    A user from before the source namespace has bare year dirs directly under
    ``raw/`` (a 4-digit-year directory). We do NOT relocate their data — that would
    be silently moving bytes — but we point at the one-time offline ``mv`` that
    migrates it, mirroring the ``./data`` shadow note (SPEC §6.4). Detection keys on
    ``raw/<YYYY>`` because the clerk pull is what created those dirs; the symmetric
    ``parsed/`` move is in the same printed nudge.
    """
    global _legacy_layout_warning_emitted
    if _legacy_layout_warning_emitted:
        return
    raw = data_dir / "raw"
    try:
        legacy_years = sorted(
            child.name
            for child in raw.iterdir()
            if child.is_dir() and len(child.name) == 4 and child.name.isdigit()
        )
    except OSError:
        legacy_years = []
    if not legacy_years:
        return
    _legacy_layout_warning_emitted = True
    print(
        f"note: found pre-namespace clerk data ({', '.join(legacy_years)}) directly "
        f"under {raw}; the layout is now raw/clerk/<year>/ + parsed/clerk/<year>/. "
        f"openhouse does NOT move it for you — run this one-time offline migration:\n"
        f"  mv {data_dir}/raw/<year>    {data_dir}/raw/clerk/<year>\n"
        f"  mv {data_dir}/parsed/<year> {data_dir}/parsed/clerk/<year>\n"
        f"(then re-run `openhouse clerk parse <year>` to refresh each record's "
        f"source_pdf path).",
        file=sys.stderr,
    )


def resolve_data_dir(flag_value: str | None) -> Path:
    """Resolve the data root, precedence: ``--data-dir`` flag → ``OPENHOUSE_DATA_DIR``
    env → ``~/.openhouse`` default.

    ``flag_value`` is the explicitly-passed ``--data-dir`` (or ``None`` if the flag
    was omitted — the flag's argparse default must be ``None`` for this to be
    distinguishable). The environment is read here, not at import time, so a single
    resolver governs all three verbs and tests can drive it with ``monkeypatch``.

    When the default is used (no flag, no env), emit a one-time stderr note if a
    non-empty ``./data`` exists in the cwd and is now being shadowed.
    """
    if flag_value is not None:
        return Path(flag_value)
    env_value = os.environ.get(DATA_DIR_ENV)
    if env_value:
        return Path(env_value)
    _warn_shadowed_local_data()
    return Path.home() / ".openhouse"


def fec_raw_dir(data_dir: Path, cycle: int) -> Path:
    """The FEC lane's cycle-keyed raw directory: ``<data>/raw/fec/<cycle>/`` (§13).

    Cycle-keyed on disk (not per-year), mirroring how the clerk lane builds
    ``<data>/raw/clerk/<year>/`` inline. Same ``--data-dir`` / ``OPENHOUSE_DATA_DIR``
    precedence as everything else (the resolved ``data_dir`` is passed in).
    """
    return data_dir / "raw" / "fec" / str(cycle)


def fec_parsed_dir(data_dir: Path, cycle: int) -> Path:
    """The FEC lane's cycle-keyed parsed directory: ``<data>/parsed/fec/<cycle>/`` (§13)."""
    return data_dir / "parsed" / "fec" / str(cycle)


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


_TOP_EPILOG = """\
typical workflow:
  openhouse clerk pull 2024 --contact "Jane Doe <jane@example.com>"   # network: fetch
  openhouse clerk parse 2024                                          # offline: normalize
  openhouse clerk read trades --ticker AAPL 2024                      # offline: query

sources:
  clerk    U.S. House financial disclosures (annual FDs + PTRs) — implemented
  fec      Federal Election Commission data — scaffolded, not yet implemented (#167)

clerk stages:
  pull     network — download the index + PDFs into <data>/raw/clerk/
  parse    offline — normalize raw artifacts into <data>/parsed/clerk/ JSON
  read     offline — query the parsed JSON (JSON to stdout; --table for humans)
  inspect  offline — sample parsed filings for human accuracy review in a browser

tool-level:
  ready    offline — install the agent skill into ~/.claude/skills/openhouse

data directory (precedence): --data-dir, then $OPENHOUSE_DATA_DIR, then ~/.openhouse
environment: $OPENHOUSE_CONTACT (pull's User-Agent), $OPENHOUSE_DATA_DIR
coverage: annual FDs from 2008; PTRs (STOCK Act) from 2012.

Run `openhouse <source> <verb> --help` for a command's own options."""


# The FEC source verbs (#174). They exist so `openhouse fec …` parses and
# `openhouse fec --help` lists them; `pull` (#170) and `parse` (#171) are now
# real (year/range-scoped like clerk's, expanded to a cycle), while `read` and the
# FEC nouns (donors/pac) remain stubs until later #167 sub-issues. The name is kept
# for the single intercept point below — a verb is dispatched if real, else the
# "not yet implemented" stub message fires.
_FEC_STUB_VERBS = ("pull", "parse", "read", "donors", "pac")


def _run_fec_pull(flag_argv, cycles, *, fec_pull_mod, fetched_at) -> int:
    """Parse the ``fec pull`` flags and run the bulk-data acquisition (#170).

    ``flag_argv`` is everything after ``fec pull <year>`` (the year was already
    consumed + expanded into ``cycles`` by the caller). A small dedicated parser
    handles the flags here because the FEC source is intercepted before the main
    argparse dispatch (so the stub sub-parser carries no flags). The flag surface
    mirrors the clerk lane's network knobs: ``--contact`` / ``--user-agent`` /
    ``--data-dir`` / ``--delay`` / ``--force`` (no ``--types``/``--member`` — the
    FEC lane fetches a fixed four files per cycle).
    """
    p = argparse.ArgumentParser(prog="openhouse fec pull", add_help=True)
    p.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    p.add_argument(
        "--contact",
        default=None,
        help=(
            'REQUIRED: your name and email for the User-Agent (or set '
            "OPENHOUSE_CONTACT). FEC bulk downloads, like the Clerk, want an "
            "identifiable operator."
        ),
    )
    p.add_argument(
        "--user-agent", default=None, help="override the User-Agent string entirely"
    )
    p.add_argument(
        "--delay",
        type=float,
        default=fec_pull_mod.FEC_DEFAULT_DELAY_SECONDS,
        help=(
            f"seconds between file fetches (default: "
            f"{fec_pull_mod.FEC_DEFAULT_DELAY_SECONDS}; the polite floor, grounded "
            f"in fec.gov/robots.txt Crawl-delay: 10)"
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="re-download even if a cycle's files are already present",
    )
    args = p.parse_args(flag_argv)

    data_dir = resolve_data_dir(args.data_dir)
    contact = args.contact or os.environ.get("OPENHOUSE_CONTACT")
    try:
        return fec_pull_mod.fec_pull(
            cycles,
            data_dir=data_dir,
            delay=args.delay,
            contact=contact,
            user_agent=args.user_agent,
            force=args.force,
            fetched_at=fetched_at,
        )
    except pull_mod.PullError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run_fec_parse(flag_argv, cycles, *, fec_parse_mod, fetched_at) -> int:
    """Parse the ``fec parse`` flags and run the offline normalization (#171).

    ``flag_argv`` is everything after ``fec parse <year>`` (the year was already
    consumed + expanded into ``cycles`` by the caller). ``fec parse`` is fully
    offline — it reads only ``raw/fec/<cycle>/`` (the bulk files ``fec pull``
    extracted) and writes ``parsed/fec/<cycle>/``, so the only flag is
    ``--data-dir`` (no network knobs, no ``--types`` — the FEC lane normalizes a
    fixed set of bulk files).
    """
    p = argparse.ArgumentParser(prog="openhouse fec parse", add_help=True)
    p.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    args = p.parse_args(flag_argv)

    data_dir = resolve_data_dir(args.data_dir)
    return fec_parse_mod.fec_parse(
        cycles, data_dir=data_dir, fetched_at=fetched_at
    )


def _add_clerk_verbs(subparsers) -> None:
    """Attach the clerk pipeline verbs (pull/parse/inspect/read) to ``subparsers``.

    These are today's verbs, moved verbatim under the ``clerk`` source (#174):
    behavior and flags are 100% intact, only the grammar gained a source noun.
    ``read`` is a placeholder entry here (its REMAINDER args are intercepted in
    :func:`main` before argparse, exactly as before) so ``clerk --help`` lists it.
    """
    pull_p = subparsers.add_parser(
        "pull",
        help="acquire raw artifacts from the Clerk (network)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Acquire raw artifacts from the Clerk over the network: the annual\n"
            "index ZIP and the per-filing PDF bodies, written under <data>/raw/.\n"
            "This is the only networked stage and the only one that needs a\n"
            "--contact. It is polite by default (sequential, throttled, with an\n"
            "identifiable User-Agent); those defaults are load-bearing, not\n"
            "performance knobs to strip. Re-runs skip an index already on disk\n"
            "unless --force is given."
        ),
        epilog=(
            "examples:\n"
            '  openhouse clerk pull 2024 --contact "Jane Doe <jane@example.com>"\n'
            "  openhouse clerk pull 2020-2024 --types ptr     # only PTRs, five years\n"
            "  openhouse clerk pull 2024 --index-only         # index metadata, no PDFs\n"
            "  openhouse clerk pull 2024 --member Pelosi      # only that filer's PDFs\n"
            "  openhouse clerk pull 2024 --doc-id 20024277    # one filing's PDF\n"
            "  openhouse clerk pull 2020-2024 --newest-first  # 2024 first, 2020 last"
        ),
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
        default=None,
        help=_DATA_DIR_HELP,
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
            'REQUIRED: your name and email for the User-Agent, e.g. '
            '"Jane Doe <jane@example.com>" (or set OPENHOUSE_CONTACT). The Clerk '
            "blocks anonymous shared clients, so an operator must be identifiable. "
            "Bypass only with --user-agent."
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
    pull_p.add_argument(
        "--no-reference",
        dest="reference",
        action="store_false",
        help=(
            "skip the one-time CC0 congress-legislators fetch (the offline "
            "bioguide-identity join in `parse` then falls back to name-only keys)"
        ),
    )
    # Targeted pull (#78): narrow WHICH PDFs download — never faster, just fewer.
    pull_p.add_argument(
        "--doc-id",
        default=None,
        help=(
            "fetch only this single filing's PDF (by its DocID). Still fetches "
            "the year index for the filing's type/metadata, but no other PDF. "
            "REQUIRES exactly one year (the URL is keyed on year). Mutually "
            "exclusive with --member."
        ),
    )
    pull_p.add_argument(
        "--member",
        default=None,
        help=(
            "fetch only the PDFs of filings whose filer matches this name "
            "(case-insensitive substring, the same matcher as `read --member`). "
            "Fetches the full year index, then narrows the downloads. Mutually "
            "exclusive with --doc-id."
        ),
    )
    pull_p.add_argument(
        "--newest-first",
        action="store_true",
        help="process the requested years newest-first (descending) instead of oldest-first",
    )

    parse_p = subparsers.add_parser(
        "parse",
        help="transform raw artifacts into normalized JSON (offline)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Transform the raw artifacts from `pull` into normalized JSON under\n"
            "<data>/parsed/. Fully offline and deterministic: it classifies each\n"
            "PDF, extracts filing metadata and PTR transactions, joins filer\n"
            "identity, and writes a parse-manifest recording what did and did not\n"
            "parse — no filing is ever silently dropped. Re-parsing is cheap by\n"
            "design; a schema change means re-run, not migrate."
        ),
        epilog=(
            "examples:\n"
            "  openhouse clerk parse 2024\n"
            "  openhouse clerk parse 2020-2024 --types ptr\n"
            "  openhouse clerk parse 2024 --strict     # non-zero exit if any filing errors"
        ),
    )
    parse_p.add_argument("years", help="YYYY or YYYY-YYYY")
    parse_p.add_argument(
        "--data-dir",
        default=None,
        help=_DATA_DIR_HELP,
    )
    parse_p.add_argument(
        "--types",
        default="ptr,fd",
        help=(
            "comma-separated families to classify: ptr, fd, or both "
            "(default: ptr,fd); an excluded family is left unclassified"
        ),
    )
    parse_p.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any filing errors (e.g. a corrupt PDF)",
    )

    inspect_p = subparsers.add_parser(
        "inspect",
        help="human accuracy review of parsed filings in a local web app (offline)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Sample already-parsed filings for a single year and serve them in a\n"
            "local web app for human accuracy review (precision/recall verdicts).\n"
            "Fully offline; reads <data>/parsed/ and never touches the network.\n"
            "The sample is reproducible: the same year/sample/seed draws the same\n"
            "set."
        ),
        epilog=(
            "examples:\n"
            "  openhouse clerk inspect 2024 --sample 0.1\n"
            "  openhouse clerk inspect 2024 --sample 0.25 --seed 7"
        ),
    )
    inspect_p.add_argument("year", help="a single coverage year, YYYY")
    inspect_p.add_argument(
        "--sample",
        type=float,
        required=True,
        help="fraction (0–1] of the year's reviewable filings to sample",
    )
    inspect_p.add_argument(
        "--mode",
        choices=("filing",),
        default="filing",
        help="review granularity (only 'filing' now; trade mode is a follow-up)",
    )
    inspect_p.add_argument(
        "--seed",
        type=int,
        default=0,
        help="sampling seed; same year/sample/seed reproduces the set (default: 0)",
    )
    inspect_p.add_argument(
        "--data-dir",
        default=None,
        help=_DATA_DIR_HELP,
    )

    # `clerk read` is intercepted in main() before argparse runs (its
    # REMAINDER-style args, including a leading global flag, defeat argparse
    # subparsing), so this entry exists only so `openhouse clerk --help` lists the
    # verb; its args are never parsed here. See read.py for the real sub-parser.
    subparsers.add_parser(
        "read",
        help="query the normalized JSON (offline, read-only)",
        add_help=False,
    )


def _add_fec_verbs(subparsers) -> None:
    """Scaffold the FEC source verbs as stubs (#174). Real behavior is #167.

    Each verb parses (so `openhouse fec <verb> --help` works and the grammar is
    symmetric with clerk) but :func:`main` short-circuits the FEC source to a
    clear "not yet implemented" error before any of this is dispatched.
    """
    for verb in _FEC_STUB_VERBS:
        subparsers.add_parser(
            verb,
            help=f"{verb} (FEC) — not yet implemented (#167)",
            add_help=False,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openhouse",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Pull, parse, and query U.S. campaign-finance and disclosure data by "
            "source (clerk = U.S. House financial disclosures; fec = scaffolded). "
            + _LEGAL_NOTICE
        ),
        epilog=_TOP_EPILOG,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"openhouse {__version__}",
        help="print the installed openhouse version and exit",
    )
    sources = parser.add_subparsers(dest="source", required=True)

    clerk_p = sources.add_parser(
        "clerk",
        help="U.S. House financial disclosures (annual FDs + PTRs)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Pull, parse, and query U.S. House financial disclosures from the "
            "Office of the Clerk. " + _LEGAL_NOTICE
        ),
    )
    _add_clerk_verbs(clerk_p.add_subparsers(dest="command", required=True))

    fec_p = sources.add_parser(
        "fec",
        help="Federal Election Commission data — scaffolded, not yet implemented (#167)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Federal Election Commission data (Path 1: connected-SSF PAC money). "
            "Scaffolded (#174/#168); the verbs are stubs — real FEC behavior lands "
            "in later sub-issues (#167). " + _FEC_LEGAL_NOTICE
        ),
    )
    _add_fec_verbs(fec_p.add_subparsers(dest="command", required=True))

    # `ready` is a TOOL-LEVEL verb (#174): it installs the agent skill and is not
    # scoped to a data source, so it stays at the top level, not under clerk/fec.
    ready_p = sources.add_parser(
        "ready",
        help="install the agent skill into ~/.claude/skills/openhouse (offline)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Install (or refresh) the packaged agent skill into\n"
            "~/.claude/skills/openhouse so an agent can drive these verbs. Fully\n"
            "offline. Use --check to report up-to-date / stale / hand-edited\n"
            "without writing anything."
        ),
        epilog=(
            "examples:\n"
            "  openhouse ready\n"
            "  openhouse ready --check     # report status, install nothing"
        ),
    )
    ready_p.add_argument(
        "--check",
        action="store_true",
        help="report up-to-date / stale / hand-edited instead of installing",
    )

    return parser


# The bare pre-namespace verbs (#174): removed, but recognized one level up so a
# user typing the old form gets a clear pointer to the new grammar instead of a
# bewildering argparse "invalid choice: 'pull'".
_LEGACY_BARE_VERBS = ("pull", "parse", "read", "inspect")


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv

    # The single wall-clock read for the whole program (SPEC §9): both the
    # range-validation year and the manifest ``fetched-at`` derive from it.
    now = datetime.now()
    current_year = now.year
    fetched_at = now.isoformat()

    # The bare pre-namespace form (`openhouse pull …`) is removed (#174). Catch it
    # one level up and point at the new `openhouse <source> <verb>` grammar — a
    # clear migration message instead of argparse's opaque "invalid choice".
    if raw_argv and raw_argv[0] in _LEGACY_BARE_VERBS:
        verb = raw_argv[0]
        print(
            f"error: bare `openhouse {verb} …` is removed. The CLI is now "
            f"source-scoped: run `openhouse clerk {verb} …` (the House-Clerk "
            f"pipeline lives under the `clerk` source).",
            file=sys.stderr,
        )
        return 2

    # The FEC source is scaffolded only (#174): real behavior is a later sub-issue
    # (#167). Intercept it before argparse so a stub verb can carry args (e.g.
    # `fec pull 2024`) without the stub sub-parser having to model each one, while
    # `openhouse fec --help` still routes through argparse below. A bare
    # `openhouse fec` (no verb) falls through to argparse, which reports the
    # required-verb error.
    #
    # The year→cycle grammar is REAL even though the body is a stub (#168): the
    # year-scoped verbs (pull/parse) parse + validate their <year>/<range> with the
    # shared clerk parser, then expand each named year to its enclosing FEC cycle
    # (§13), emitting the one-line stderr note when expansion happens — exactly the
    # §5 `trades` filing-year-note pattern. Only AFTER parsing + expanding does the
    # stub exit "not yet implemented", so the grammar is testable now.
    if raw_argv[:1] == ["fec"] and len(raw_argv) >= 2 and raw_argv[1] in _FEC_STUB_VERBS:
        verb = raw_argv[1]
        if verb in ("pull", "parse"):
            if len(raw_argv) < 3:
                print(
                    f"error: `openhouse fec {verb}` requires a year (YYYY or "
                    f"YYYY-YYYY).",
                    file=sys.stderr,
                )
                return 2
            try:
                years = parse_year_range(raw_argv[2], current_year)
            except YearRangeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            cycles = expand_years_to_cycles(years)
            if cycles != years:
                named = ", ".join(str(y) for y in years)
                resolved = ", ".join(str(c) for c in cycles)
                print(
                    f"note: FEC reports on 2-year cycles (even-year-ending); "
                    f"year(s) {named} resolve to cycle(s) {resolved}.",
                    file=sys.stderr,
                )
            if verb == "pull":
                # The FEC lane's only network step (#170): bulk-data acquisition of
                # the four Path-1 files per cycle. Reuses the Clerk lane's polite
                # client/User-Agent/backoff (see openhouse/fec_pull.py). Lazy import
                # so the FEC module's httpx/tqdm cost isn't paid by clerk verbs.
                from . import fec_pull as fec_pull_mod

                return _run_fec_pull(
                    raw_argv[3:],
                    cycles,
                    fec_pull_mod=fec_pull_mod,
                    fetched_at=fetched_at,
                )
            if verb == "parse":
                # Offline normalization (#171): bulk cn/ccl/cm/itpas2 → normalized
                # Path-1 records + residual manifest. No network — reads only
                # raw/fec/<cycle>/ (what `fec pull` extracted). Lazy import keeps
                # the clerk verbs free of the FEC module's cost.
                from . import fec_parse as fec_parse_mod

                return _run_fec_parse(
                    raw_argv[3:],
                    cycles,
                    fec_parse_mod=fec_parse_mod,
                    fetched_at=fetched_at,
                )
        print(
            f"error: `openhouse fec {verb}` is not yet implemented "
            f"(the FEC source is scaffolded; see #167).",
            file=sys.stderr,
        )
        return 1

    # `clerk read` owns its own sub-parser (read.py), so hand it everything after
    # the `clerk read` tokens verbatim. read.py accepts `--data-dir`/`--table`
    # before OR after its subcommand (shared parent parser), which a top-level
    # REMAINDER arg could not express.
    if raw_argv[:2] == ["clerk", "read"]:
        # read.py resolves the data dir itself; nudge on legacy layout there too so
        # a pre-namespace user gets the migration note whichever verb they run.
        return read_mod.run(raw_argv[2:], current_year=current_year)

    parser = build_parser()
    args = parser.parse_args(raw_argv)

    if args.source == "ready":
        # Tool-level, offline, no year range: stamp the packaged skill into
        # ~/.claude/skills.
        return ready_mod.run(["--check"] if args.check else [])

    # Past this point the source is `clerk`. Resolve the data root once and nudge
    # if a pre-namespace layout is still on disk (#174) before dispatching a verb.
    data_dir = resolve_data_dir(args.data_dir)
    _warn_legacy_clerk_layout(data_dir)

    if args.command in ("pull", "parse"):
        # Validate the range now so a bad argument fails fast and uniformly.
        try:
            years = parse_year_range(args.years, current_year)
        except YearRangeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.command == "parse":
            # parse: offline metadata mapping + filer_id + identity warnings (#6).
            try:
                types = parse_types(args.types)
            except YearRangeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            try:
                return parse_mod.parse(
                    years,
                    data_dir=data_dir,
                    types=types,
                    strict=args.strict,
                    fetched_at=fetched_at,
                    entry_year=current_year,
                )
            except parse_mod.ParseError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
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
                data_dir=data_dir,
                index_only=args.index_only,
                delay=args.delay,
                concurrency=args.concurrency,
                contact=contact,
                user_agent=args.user_agent,
                force=args.force,
                types=types,
                reference=args.reference,
                member=args.member,
                doc_id=args.doc_id,
                newest_first=args.newest_first,
                fetched_at=fetched_at,
            )
        except pull_mod.PullError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.command == "inspect":
        # A single year — reviewing already-parsed data, one browser session per
        # year (parse_year_range gives the bounds check + sub-2012 PTR warning).
        try:
            years = parse_year_range(args.year, current_year)
        except YearRangeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if len(years) != 1:
            print("error: inspect takes a single year, not a range.", file=sys.stderr)
            return 2
        if not 0 < args.sample <= 1:
            print("error: --sample must be in (0, 1].", file=sys.stderr)
            return 2
        # Lazy import: keep pdfplumber + http.server off the path for pull/parse.
        from .inspect.server import run as inspect_run

        return inspect_run(
            years[0],
            data_dir=data_dir,
            sample=args.sample,
            seed=args.seed,
            started_at=fetched_at,
        )

    # `clerk read` is dispatched at the top of main() (before argparse); reaching
    # here means an unknown clerk command.
    parser.error(f"unknown command {args.command!r}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":
    raise SystemExit(main())
