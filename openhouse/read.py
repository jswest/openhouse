"""Query surface: ``openhouse read`` (SPEC §5) — offline, read-only, deterministic.

``read`` is a **pure function over** ``parsed/`` — it never touches ``raw/`` or
the network and never writes a byte. There is **no database**: at this scale
scanning the JSON in place is milliseconds, and skipping a load step means
``read`` can never disagree with the last ``parse`` (SPEC §5). Records are
consumed as plain dicts by key — the on-disk JSON shape is the only contract, so
this module imports no body/transaction schema class (the producer of PTR bodies
runs in parallel; coupling to its class would be a cross-branch hazard).

Four subcommands: ``filings`` / ``filing`` / ``trades`` / ``summary``. JSON to
stdout is the machine/agent contract (``jq``-composable); ``--table`` is human
garnish; all prose, progress, and **residuals** go to stderr; the exit code is 0
unless something genuinely failed (a partial range is *not* a failure).

The sound-or-complete agreement (CLAUDE.md) is the heart of ``trades``:

- ``--ticker`` is the **sound** query — case-insensitive *exact* match on the
  ``ticker`` field. No false positives: every hit is a real symbol match. It
  bounds the truth from below ("at least these"). It never matches an asset name
  and never infers a symbol from one (offline, names are ambiguous).
- ``--asset`` is the **completeness-leaning** query — case-insensitive substring
  over the verbatim ``asset`` text (which *includes* the embedded ``(TICKER)
  [TYPE]``). It bounds the truth from above ("at most these"): it over-matches,
  and a human discards the spurious hits. This is the tool to reach for when you
  would rather not miss a trade ("prefer completeness").

Every range query (``filings`` / ``trades`` / ``summary``) prints a **residual**
line to stderr: the manifest's count of in-range filings that did *not* parse
(scanned / missing / error), so the answer is explicitly "complete over the K
parsed filings; M did not parse". For ``--ticker`` it additionally reports the
in-range ``[ST]``/``[OP]`` transactions whose ``ticker`` is null — the symbol the
filer omitted, which the sound query cannot search.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import cli as cli_mod

# Asset types that *should* carry a tradable ticker symbol (stocks, options). A
# null ticker on one of these is a real gap in what ``--ticker`` can search — the
# filer omitted the symbol — so the residual surfaces it. Bonds, funds, real
# estate, etc. legitimately have no symbol and are not counted as a gap.
_TICKERED_ASSET_TYPES = frozenset({"ST", "OP"})


class ReadError(Exception):
    """A ``read`` failed in a way the user must see (stderr, non-zero exit)."""


# ---------------------------------------------------------------------------
# Loading parsed data (offline, read-only). Missing years degrade gracefully.
# ---------------------------------------------------------------------------


def _load_json(path: Path):
    """Read+parse one JSON file, or raise :class:`ReadError` with a clear message."""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReadError(f"could not read {path}: {exc}") from exc


def _year_dir(data_dir: Path, year: int) -> Path:
    return data_dir / "parsed" / str(year)


def _load_year_filings(data_dir: Path, year: int) -> Optional[list[dict]]:
    """The ``filings.json`` array for one year, or ``None`` if the year isn't parsed.

    A not-yet-parsed year (no ``parsed/<year>/filings.json``) is a clean skip —
    the caller reports it on stderr and answers from the rest (SPEC §5: missing
    years degrade gracefully).
    """
    path = _year_dir(data_dir, year) / "filings.json"
    if not path.exists():
        return None
    return _load_json(path)


def _load_ptr_body(data_dir: Path, year: int, doc_id: str) -> Optional[dict]:
    """One PTR body (``ptr/<DocID>.json``) for a year, or ``None`` if absent."""
    path = _year_dir(data_dir, year) / "ptr" / f"{doc_id}.json"
    if not path.exists():
        return None
    return _load_json(path)


def _load_manifest(data_dir: Path, year: int) -> Optional[dict]:
    """The ``parse-manifest.json`` for one year, or ``None`` if the year isn't parsed."""
    path = _year_dir(data_dir, year) / "parse-manifest.json"
    if not path.exists():
        return None
    return _load_json(path)


def _resolve_years(data_dir: Path, years: list[int]) -> tuple[list[int], list[int]]:
    """Split ``years`` into (present, skipped) by whether each is parsed on disk.

    "Present" = ``parsed/<year>/filings.json`` exists. Skipped years are reported
    on stderr by the caller; the query proceeds over the present ones.
    """
    present: list[int] = []
    skipped: list[int] = []
    for year in years:
        if (_year_dir(data_dir, year) / "filings.json").exists():
            present.append(year)
        else:
            skipped.append(year)
    return present, skipped


# ---------------------------------------------------------------------------
# Residuals (the universal stderr line). "Complete over the known, explicit
# residual for the unknown" (CLAUDE.md).
# ---------------------------------------------------------------------------


def _residual_counts(data_dir: Path, years: list[int]) -> dict:
    """Tally, across ``years``, how many in-range filings did NOT parse.

    Sourced from each year's ``parse-manifest.json`` ``counts`` block: ``scanned``
    + ``missing`` (``by_pdf_class``) and ``error`` (``by_parse_status``) — the
    filings present in the index but absent from the usable parsed set. Returns
    ``{"parsed", "unparsed", "scanned", "missing", "error"}``; ``parsed`` is the
    e-filed count the answer is complete over.
    """
    scanned = missing = error = efiled = 0
    for year in years:
        manifest = _load_manifest(data_dir, year)
        if manifest is None:
            continue
        counts = manifest.get("counts", {})
        by_class = counts.get("by_pdf_class", {})
        by_status = counts.get("by_parse_status", {})
        efiled += by_class.get("efiled", 0)
        scanned += by_class.get("scanned", 0)
        missing += by_class.get("missing", 0)
        error += by_status.get("error", 0)
    return {
        "parsed": efiled,
        "unparsed": scanned + missing + error,
        "scanned": scanned,
        "missing": missing,
        "error": error,
    }


def _print_residual(data_dir: Path, years: list[int]) -> None:
    """Emit the universal residual line to stderr for a range query (SPEC §5)."""
    r = _residual_counts(data_dir, years)
    print(
        f"residual: complete over the {r['parsed']} e-filed filings parsed in "
        f"range; {r['unparsed']} did not parse "
        f"(scanned {r['scanned']} / missing {r['missing']} / error {r['error']}) "
        f"and are not represented in these results.",
        file=sys.stderr,
    )


def _print_skipped(skipped: list[int]) -> None:
    """Report not-yet-parsed years on stderr (graceful degradation, SPEC §5)."""
    if skipped:
        print(
            f"note: years {skipped} are not parsed (no parsed/<year>/); "
            f"answered from the parsed years only. Run `openhouse parse` for them.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Filters (shared predicates over filing-metadata dicts).
# ---------------------------------------------------------------------------


def _ci_contains(haystack: Optional[str], needle: str) -> bool:
    """Case-insensitive substring test; a ``None``/empty haystack never matches."""
    if not haystack:
        return False
    return needle.lower() in haystack.lower()


def _member_matches(filing: dict, needle: str) -> bool:
    """``--member`` match: case-insensitive substring over ``filer_id`` + raw names.

    This is **name-string matching, not true identity** (SPEC §6.2): it tests the
    normalized ``filer_id`` and every raw name part (prefix/first/last/suffix), so
    a substring of either the slug or the printed name hits.
    """
    if _ci_contains(filing.get("filer_id"), needle):
        return True
    filer = filing.get("filer") or {}
    return any(
        _ci_contains(filer.get(part), needle)
        for part in ("prefix", "first", "last", "suffix")
    )


def _state_matches(filing: dict, needle: str) -> bool:
    """``--state`` match: exact (case-insensitive) on the 2-letter postal code."""
    sd = filing.get("state_district") or {}
    state = sd.get("state")
    return bool(state) and state.upper() == needle.upper()


# Convenience aliases for ``--type``: the SPEC §5 examples use ``ptr`` / ``fd``,
# which are the PDF *families*, not the filing-type label. ``ptr`` → the PTR code
# ``P``; ``fd`` → every non-PTR (annual/amendment/extension/etc.) family.
_TYPE_ALIASES = {"ptr": "P"}


def _type_matches(filing: dict, needle: str) -> bool:
    """``--type`` match on the filing type: a code, label-substring, or family alias.

    Accepts the single-letter code (``P``), a substring of the mapped label
    (``periodic``), or the family aliases ``ptr`` / ``fd`` from the SPEC §5
    examples — all case-insensitive, so a human need not memorize the letters.
    """
    ft = filing.get("filing_type") or {}
    code = ft.get("code") or ""
    label = ft.get("label") or ""
    n = needle.lower()
    if n == "fd":
        return code.upper() != "P"  # the "fd" family is everything that isn't a PTR
    if n in _TYPE_ALIASES:
        return code.upper() == _TYPE_ALIASES[n]
    if len(n) == 1:
        # A single letter is the raw code (exact), never a label substring — "o"
        # must not match "periodic_transaction_repOrt".
        return code.lower() == n
    return n in label.lower()


def _date_in_range(
    value: Optional[str], since: Optional[str], until: Optional[str]
) -> bool:
    """Inclusive ISO-date window test.

    A ``None`` value is excluded when either bound is set (it cannot be shown to
    fall inside the window); with no bounds everything passes. ISO ``YYYY-MM-DD``
    strings compare correctly as plain strings, so no date parsing is needed
    (deterministic, dependency-free).
    """
    if since is None and until is None:
        return True
    if not value:
        return False
    if since is not None and value < since:
        return False
    if until is not None and value > until:
        return False
    return True


def _filter_filings(filings: list[dict], args) -> list[dict]:
    """Apply the ``filings`` filters (``--type``/``--member``/``--state``/dates)."""
    out = []
    for f in filings:
        if args.type and not _type_matches(f, args.type):
            continue
        if args.member and not _member_matches(f, args.member):
            continue
        if args.state and not _state_matches(f, args.state):
            continue
        if not _date_in_range(f.get("filing_date"), args.since, args.until):
            continue
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# Table rendering (human garnish — stdout, aligned columns).
# ---------------------------------------------------------------------------


def _render_table(rows: list[list[str]], headers: list[str]) -> str:
    """Render aligned columns. Empty ``rows`` → just the header line."""
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
    """Emit ``payload`` to stdout: JSON by default, or a rendered table.

    ``table_fn`` builds ``(headers, rows)`` from the payload only when ``--table``
    is set, so the JSON path never pays for table formatting.
    """
    if table:
        headers, rows = table_fn(payload)
        print(_render_table(rows, headers))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Subcommand: filings
# ---------------------------------------------------------------------------


def _filing_state(f: dict) -> str:
    sd = f.get("state_district") or {}
    return sd.get("raw") or ""


def _filings_table(filings: list[dict]):
    headers = ["doc_id", "year", "type", "filer_id", "state", "filing_date"]
    rows = [
        [
            f.get("doc_id", ""),
            str(f.get("year", "")),
            (f.get("filing_type") or {}).get("code", ""),
            f.get("filer_id", ""),
            _filing_state(f),
            f.get("filing_date") or "",
        ]
        for f in filings
    ]
    return headers, rows


def cmd_filings(args, data_dir: Path, years: list[int]) -> int:
    present, skipped = _resolve_years(data_dir, years)
    _print_skipped(skipped)

    matched: list[dict] = []
    for year in present:
        filings = _load_year_filings(data_dir, year) or []
        matched.extend(_filter_filings(filings, args))

    _emit(matched, table=args.table, table_fn=_filings_table)
    _print_residual(data_dir, present)
    return 0


# ---------------------------------------------------------------------------
# Subcommand: filing <doc_id>
# ---------------------------------------------------------------------------


def _find_filing(data_dir: Path, doc_id: str) -> Optional[tuple[int, dict]]:
    """Locate a filing by DocID across all parsed years → ``(year, record)``.

    Scans every ``parsed/<year>/`` present on disk (DocID is unique within the
    Clerk's corpus; the first match wins, lowest year first for determinism).
    """
    base = data_dir / "parsed"
    if not base.exists():
        return None
    for year_dir in sorted(base.iterdir()):
        if not year_dir.name.isdigit():
            continue
        filings = _load_year_filings(data_dir, int(year_dir.name)) or []
        for f in filings:
            if f.get("doc_id") == doc_id:
                return int(year_dir.name), f
    return None


def _filing_detail_table(payload: dict):
    f = payload["filing"]
    body = payload.get("body")
    txn_cell = (
        str(len(body.get("transactions", []))) if body else "(no body)"
    )
    headers = ["field", "value"]
    rows = [
        ["doc_id", f.get("doc_id", "")],
        ["year", str(f.get("year", ""))],
        ["type", (f.get("filing_type") or {}).get("code", "")],
        ["filer_id", f.get("filer_id", "")],
        ["state", _filing_state(f)],
        ["filing_date", f.get("filing_date") or ""],
        ["pdf_class", f.get("pdf_class") or ""],
        ["transactions", txn_cell],
    ]
    return headers, rows


def cmd_filing(args, data_dir: Path) -> int:
    found = _find_filing(data_dir, args.doc_id)
    if found is None:
        print(
            f"error: no parsed filing with doc_id {args.doc_id!r} "
            f"(is the year parsed? try `openhouse read filings`).",
            file=sys.stderr,
        )
        return 1
    year, filing = found

    body = None
    # A body only exists for PTRs that parsed to an e-filed JSON. Its absence is
    # not an error (scanned/missing/non-PTR filings have no body), but say so.
    if (filing.get("filing_type") or {}).get("code") == "P":
        body = _load_ptr_body(data_dir, year, args.doc_id)
        if body is None:
            print(
                f"note: filing {args.doc_id} is a PTR but has no parsed body "
                f"(pdf_class={filing.get('pdf_class')!r}); metadata only.",
                file=sys.stderr,
            )

    payload = {"filing": filing, "body": body}
    _emit(payload, table=args.table, table_fn=_filing_detail_table)
    return 0


# ---------------------------------------------------------------------------
# Subcommand: trades <range>
# ---------------------------------------------------------------------------


def _amount_low(txn: dict) -> Optional[int]:
    """The low end of a transaction's amount range, or ``None`` if absent."""
    amt = txn.get("amount_range") or {}
    return amt.get("low")


def _trade_matches(txn: dict, filing: dict, args) -> bool:
    """Apply the ``trades`` transaction-level filters to one (txn, filer) pair.

    ``--ticker`` is the SOUND filter (exact symbol, no false positives);
    ``--asset`` is the COMPLETENESS-leaning filter (substring over verbatim asset
    text, may over-match). The remaining filters (member/owner/type/dates/amount)
    narrow within whichever of those was chosen.
    """
    # SOUND: exact, case-insensitive ticker. Never matches the asset name; never
    # infers a symbol. A null ticker can never match (no false positives).
    if args.ticker:
        tk = txn.get("ticker")
        if not tk or tk.upper() != args.ticker.upper():
            return False
    # COMPLETENESS-leaning: substring over verbatim asset (includes embedded
    # "(TICKER) [TYPE]"). Over-matches; spurious hits are for a human to discard.
    if args.asset and not _ci_contains(txn.get("asset"), args.asset):
        return False
    if args.owner and (txn.get("owner") or "").upper() != args.owner.upper():
        return False
    if args.type:
        # P / S over the transaction_type; "S" also catches "S(partial)".
        tt = (txn.get("transaction_type") or "").upper()
        if not tt.startswith(args.type.upper()):
            return False
    if not _date_in_range(txn.get("transaction_date"), args.since, args.until):
        return False
    if args.min_amount is not None:
        low = _amount_low(txn)
        if low is None or low < args.min_amount:
            return False
    if args.member and not _member_matches(filing, args.member):
        return False
    return True


def _null_ticker_residual(data_dir: Path, years: list[int], args) -> int:
    """Count in-range ``[ST]``/``[OP]`` transactions whose ``ticker`` is null.

    These are trades whose asset type *should* carry a symbol but the filer
    omitted it — exactly what the sound ``--ticker`` query cannot find. Counted
    over the same population the query scanned (so member/date filters that scope
    the query also scope this residual, but ticker/asset filters do not — those
    are the very thing whose blind spot we are reporting).
    """
    count = 0
    for year in years:
        filings = _load_year_filings(data_dir, year) or []
        for filing in filings:
            if (filing.get("filing_type") or {}).get("code") != "P":
                continue
            if args.member and not _member_matches(filing, args.member):
                continue
            body = _load_ptr_body(data_dir, year, filing.get("doc_id"))
            if not body:
                continue
            for txn in body.get("transactions", []):
                if txn.get("ticker"):
                    continue
                if (txn.get("asset_type") or "").upper() not in _TICKERED_ASSET_TYPES:
                    continue
                if not _date_in_range(
                    txn.get("transaction_date"), args.since, args.until
                ):
                    continue
                count += 1
    return count


def _collect_trades(data_dir: Path, years: list[int], args) -> list[dict]:
    """Flatten PTR transactions across ``years``, each with its filer attached.

    Joins a body to its filer by **DocID** (the body filename) against the year's
    ``filings.json``. Only PTR (type ``P``) filings have bodies; a filing whose
    body JSON is absent (scanned/missing/non-PTR) simply contributes nothing.
    Output order is deterministic: year, then filings.json order, then txn order.
    """
    trades: list[dict] = []
    for year in years:
        filings = _load_year_filings(data_dir, year) or []
        for filing in filings:
            if (filing.get("filing_type") or {}).get("code") != "P":
                continue
            doc_id = filing.get("doc_id")
            body = _load_ptr_body(data_dir, year, doc_id)
            if not body:
                continue
            for txn in body.get("transactions", []):
                if _trade_matches(txn, filing, args):
                    trades.append(
                        {
                            "doc_id": doc_id,
                            "year": year,
                            "filer_id": filing.get("filer_id"),
                            "filer": filing.get("filer"),
                            "state_district": filing.get("state_district"),
                            "transaction": txn,
                        }
                    )
    return trades


def _trades_table(trades: list[dict]):
    headers = [
        "doc_id", "filer_id", "owner", "ticker", "asset",
        "type", "txn_date", "amount",
    ]
    rows = []
    for t in trades:
        txn = t["transaction"]
        amt = txn.get("amount_range") or {}
        rows.append(
            [
                t.get("doc_id", ""),
                t.get("filer_id") or "",
                txn.get("owner") or "",
                txn.get("ticker") or "",
                (txn.get("asset") or "")[:48],
                txn.get("transaction_type") or "",
                txn.get("transaction_date") or "",
                amt.get("label") or "",
            ]
        )
    return headers, rows


def cmd_trades(args, data_dir: Path, years: list[int]) -> int:
    present, skipped = _resolve_years(data_dir, years)
    _print_skipped(skipped)

    trades = _collect_trades(data_dir, present, args)
    _emit(trades, table=args.table, table_fn=_trades_table)

    # Declared-guarantee notes on stderr, so the bound is visible per query mode.
    if args.ticker:
        print(
            f"guarantee: --ticker is SOUND — exact symbol match, no false "
            f"positives; these are AT LEAST the {args.ticker!r} trades (every hit "
            f"is a real symbol match). It cannot see a trade whose filer omitted "
            f"the symbol.",
            file=sys.stderr,
        )
        null_tickered = _null_ticker_residual(data_dir, present, args)
        print(
            f"residual (--ticker blind spot): {null_tickered} in-range [ST]/[OP] "
            f"transaction(s) have a null ticker and could not be searched by "
            f"symbol; use --asset to catch trades by asset name.",
            file=sys.stderr,
        )
    if args.asset:
        print(
            f"guarantee: --asset is COMPLETENESS-leaning — substring over the "
            f"verbatim asset text; these are AT MOST the {args.asset!r} trades "
            f"(may include spurious hits to discard).",
            file=sys.stderr,
        )

    _print_residual(data_dir, present)
    return 0


# ---------------------------------------------------------------------------
# Subcommand: summary <range>
# ---------------------------------------------------------------------------


def _summary_table(payload: dict):
    headers = [
        "year", "total", "efiled", "scanned", "missing",
        "error", "warnings",
    ]
    rows = []
    for y in payload["years"]:
        c = y["counts"]
        pc = c.get("by_pdf_class", {})
        ps = c.get("by_parse_status", {})
        rows.append(
            [
                str(y["year"]),
                str(c.get("total", 0)),
                str(pc.get("efiled", 0)),
                str(pc.get("scanned", 0)),
                str(pc.get("missing", 0)),
                str(ps.get("error", 0)),
                str(y.get("identity_warnings", 0)),
            ]
        )
    return headers, rows


def cmd_summary(args, data_dir: Path, years: list[int]) -> int:
    present, skipped = _resolve_years(data_dir, years)
    _print_skipped(skipped)

    year_summaries = []
    for year in present:
        manifest = _load_manifest(data_dir, year)
        if manifest is None:
            continue
        year_summaries.append(
            {
                "year": year,
                "counts": manifest.get("counts", {}),
                "identity_warnings": len(manifest.get("identity_warnings", [])),
            }
        )

    payload = {"years": year_summaries}
    _emit(payload, table=args.table, table_fn=_summary_table)
    _print_residual(data_dir, present)
    return 0


# ---------------------------------------------------------------------------
# Argument parsing + dispatch (driven by cli.py's REMAINDER hand-off).
# ---------------------------------------------------------------------------


def _add_range_arg(p) -> None:
    p.add_argument("range", help="YYYY or YYYY-YYYY")


def build_read_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openhouse read",
        description="Query parsed House financial disclosures (offline, read-only).",
    )
    parser.add_argument(
        "--data-dir", default="./data", help="root data directory (default: ./data)"
    )
    parser.add_argument(
        "--table",
        action="store_true",
        help="render a human-readable aligned table instead of JSON",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # filings <range>
    p_filings = sub.add_parser("filings", help="matching filing-metadata records")
    _add_range_arg(p_filings)
    p_filings.add_argument("--type", help="filing type: a code (P) or label (ptr)")
    p_filings.add_argument(
        "--member",
        help="case-insensitive substring over filer_id AND raw names "
        "(name-string matching, NOT true identity — SPEC §6.2)",
    )
    p_filings.add_argument("--state", help="2-letter postal code (exact, e.g. NY)")
    p_filings.add_argument(
        "--since", help="earliest filing_date (YYYY-MM-DD, inclusive)"
    )
    p_filings.add_argument(
        "--until", help="latest filing_date (YYYY-MM-DD, inclusive)"
    )

    # filing <doc_id>
    p_filing = sub.add_parser(
        "filing", help="one filing: metadata + body (if parsed)"
    )
    p_filing.add_argument("doc_id", help="the filing's DocID")

    # trades <range>
    p_trades = sub.add_parser(
        "trades", help="PTR transactions flattened across the range, filer attached"
    )
    _add_range_arg(p_trades)
    p_trades.add_argument(
        "--ticker",
        help="SOUND query: exact (case-insensitive) ticker match. No false "
        "positives — these are AT LEAST the trades in that symbol; every hit is a "
        "real symbol match. Never matches the asset name, never infers a symbol. "
        "Cannot see a trade whose filer omitted the symbol (use --asset for that).",
    )
    p_trades.add_argument(
        "--asset",
        help="COMPLETENESS-leaning query: case-insensitive substring over the "
        "verbatim asset text (which includes the embedded (TICKER) [TYPE]). It "
        "over-matches — these are AT MOST the matching trades, possibly with "
        "spurious hits to discard. Reach for this when you would rather not miss a "
        "trade.",
    )
    p_trades.add_argument(
        "--member",
        help="case-insensitive substring over filer_id AND raw names "
        "(name-string matching, NOT true identity — SPEC §6.2)",
    )
    p_trades.add_argument("--owner", help="owner code: SP | DC | JT | self (exact)")
    p_trades.add_argument(
        "--type",
        dest="type",
        help="transaction type: P (purchase) or S (sale; also catches S(partial))",
    )
    p_trades.add_argument("--since", help="earliest transaction_date (YYYY-MM-DD)")
    p_trades.add_argument("--until", help="latest transaction_date (YYYY-MM-DD)")
    p_trades.add_argument(
        "--min-amount",
        type=int,
        dest="min_amount",
        help="minimum amount_range low end (dollars); excludes trades with no range",
    )

    # summary <range>
    p_summary = sub.add_parser("summary", help="per-year roll-up from the manifests")
    _add_range_arg(p_summary)

    return parser


def run(remainder: list[str], *, current_year: int) -> int:
    """Entry point for ``openhouse read`` (called from cli.py with the REMAINDER args).

    Parses the subcommand + flags, validates the year range with the shared
    parser, dispatches, and maps :class:`ReadError` to a clean non-zero exit.
    ``current_year`` is injected (never read from the clock here) so this stays
    deterministic (SPEC §9).
    """
    parser = build_read_parser()
    args = parser.parse_args(remainder)
    data_dir = Path(args.data_dir)

    try:
        if args.subcommand == "filing":
            return cmd_filing(args, data_dir)

        # The range subcommands share validation via the shared year-range parser.
        try:
            years = cli_mod.parse_year_range(args.range, current_year)
        except cli_mod.YearRangeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        if args.subcommand == "filings":
            return cmd_filings(args, data_dir, years)
        if args.subcommand == "trades":
            return cmd_trades(args, data_dir, years)
        if args.subcommand == "summary":
            return cmd_summary(args, data_dir, years)
    except ReadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown read subcommand {args.subcommand!r}")
    return 2  # unreachable; parser.error exits
