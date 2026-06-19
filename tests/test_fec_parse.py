"""Offline tests for the FEC bulk normalization (``openhouse fec parse``, #171).

Every test runs offline and deterministically: the parse reads only
``raw/fec/<cycle>/`` (the four pipe-delimited bulk files) and writes
``parsed/fec/<cycle>/``, never the network or the wall clock. Two styles are
mixed: synthetic per-test bulk files (a handful of hand-written rows that pin one
behaviour each — the Path-1 filter, the residual reasons, the $10k flag) and an
end-to-end pass over the **trimmed real fixtures** under ``tests/fixtures/fec/``
(the labor/trade/corporate committees + Adams' real candidate/committee/linkage).

The ``ccl`` committee-resolution test additionally seeds the CC0 reference
fixtures so the #169 member→candidate→committee seam resolves end to end: Adams
(bioguide A000370) → candidate H4NC12100 → principal committee C00546358.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from openhouse.fec_parse import (
    PAC_CYCLE_LIMIT,
    build_committees,
    build_principal_committees,
    check_pac_limit,
    fec_parse,
    org_rollup_key,
    parse_cycle,
    parse_independent_expenditures,
)
from openhouse.schemas import FEC_SCHEMA_VERSION, FecPacContribution

FIXTURES = Path(__file__).parent / "fixtures"
FEC_FIXTURES = FIXTURES / "fec"
REFERENCE_DIR = FIXTURES / "reference"

ENTRY_TS = "2026-06-18T00:00:00"


def _seed_cycle(tmp_path: Path, *, cycle: int = 2024, **files: str) -> Path:
    """Write the named bulk files into ``raw/fec/<cycle>/``; return the data dir.

    ``files`` maps a bulk file name (``cm.txt`` etc.) to its raw text. Missing
    files are simply not written — the parse skips a cycle whose required files are
    absent, which one test exercises.
    """
    raw = tmp_path / "raw" / "fec" / str(cycle)
    raw.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (raw / name).write_text(text, encoding="latin-1")
    return tmp_path


def _seed_from_fixtures(tmp_path: Path, *, cycle: int = 2024, ie: bool = False) -> Path:
    """Copy the trimmed real fec fixtures into ``raw/fec/<cycle>/``.

    ``ie`` also copies the super-PAC IE CSV (GH-0194); off by default so the
    Path-1 tests keep their exact counts and the IE slice is exercised on its own.
    """
    raw = tmp_path / "raw" / "fec" / str(cycle)
    raw.mkdir(parents=True, exist_ok=True)
    for name in ("cn.txt", "ccl.txt", "cm.txt", "itpas2.txt"):
        shutil.copy(FEC_FIXTURES / name, raw / name)
    if ie:
        shutil.copy(
            FEC_FIXTURES / "independent_expenditure_2024.csv",
            raw / f"independent_expenditure_{cycle}.csv",
        )
    return tmp_path


def _seed_reference(tmp_path: Path) -> Path:
    """Copy the CC0 congress-legislators fixtures into ``raw/reference/``."""
    ref = tmp_path / "raw" / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    for path in REFERENCE_DIR.glob("*.json"):
        shutil.copy(path, ref / path.name)
    return tmp_path


def _read(parsed_dir: Path, name: str):
    return json.loads((parsed_dir / name).read_text())


# A minimal cm with one labor (L, kept+tagged), one corporate (C, kept), one
# leadership/non-connected PAC (blank org type, filtered), and the recipient
# member committee (blank org type — never a contributor here).
_CM = (
    "C00000001|LABOR PAC|TREASURER||||DC|20001|B|Q||M|L|BIG UNION|\n"
    "C00000002|CORP PAC|TREASURER||||DC|20001|B|Q||M|C|BIG CORP|\n"
    "C00000003|SOME LEADERSHIP PAC|TREASURER||||DC|20001|B|N||M||NONE|\n"
    "C00000099|MEMBER FOR CONGRESS|TREASURER||||NC|28231|P|H|DEM|Q||NONE|H0XX00001\n"
)


def _pas2_row(contributor, recipient, amount, tran_id, cand="H0XX00001", dt="06012024"):
    """Build one 18+-column itpas2 row (only the columns parse reads are populated)."""
    cols = [""] * 22
    cols[0] = contributor
    cols[4] = f"IMG{tran_id}"
    cols[13] = dt
    cols[14] = str(amount)
    cols[15] = recipient
    cols[16] = cand
    cols[17] = tran_id
    return "|".join(cols)


def test_path1_filter_keeps_and_tags_labor(tmp_path):
    """A labor (L) PAC contribution is kept and tagged ``labor``; a corporate one
    is kept and tagged ``corporation`` — both connected SSFs (Path-1)."""
    pas2 = "\n".join(
        [
            _pas2_row("C00000001", "C00000099", 5000, "T1"),  # labor → kept
            _pas2_row("C00000002", "C00000099", 2500, "T2"),  # corp  → kept
        ]
    ) + "\n"
    data = _seed_cycle(tmp_path, **{"cm.txt": _CM, "ccl.txt": "", "itpas2.txt": pas2})
    parsed = data / "parsed" / "fec" / "2024"
    summary = parse_cycle(2024, data_dir=data, fetched_at=ENTRY_TS)

    contributions = _read(parsed, "contributions.json")
    assert summary["contributions_kept"] == 2
    assert summary["by_org_type"] == {"corporation": 1, "labor": 1}

    committees = {c["committee_id"]: c for c in _read(parsed, "committees.json")}
    assert committees["C00000001"]["organization_type"] == "labor"
    assert committees["C00000001"]["organization_type_raw"] == "L"
    assert committees["C00000002"]["organization_type"] == "corporation"
    # Affiliation is the declared limitation — never faked from bulk.
    assert committees["C00000001"]["affiliation"] is None
    # The kept contributions name the right contributor/recipient/amount.
    by_contrib = {c["contributor_committee_id"]: c for c in contributions}
    assert by_contrib["C00000001"]["amount"] == 5000.0
    assert by_contrib["C00000001"]["recipient_committee_id"] == "C00000099"
    assert by_contrib["C00000001"]["date"] == "2024-06-01"


def test_non_connected_committee_filtered_to_residual(tmp_path):
    """A committee present in ``cm`` but NOT a connected SSF (blank org type) lands
    in the residual manifest with reason ``not_connected_ssf`` — never dropped."""
    pas2 = "\n".join(
        [
            _pas2_row("C00000001", "C00000099", 5000, "T1"),  # labor → kept
            _pas2_row("C00000003", "C00000099", 5000, "T2"),  # leadership → filtered
        ]
    ) + "\n"
    data = _seed_cycle(tmp_path, **{"cm.txt": _CM, "ccl.txt": "", "itpas2.txt": pas2})
    parse_cycle(2024, data_dir=data, fetched_at=ENTRY_TS)
    parsed = data / "parsed" / "fec" / "2024"

    unparsed = _read(parsed, "fec-unparsed-manifest.json")
    filtered = unparsed["filtered_contributions"]
    assert len(filtered) == 1
    assert filtered[0]["contributor_committee_id"] == "C00000003"
    assert filtered[0]["reason"] == "not_connected_ssf"
    # The kept set is exactly the labor one.
    assert [c["contributor_committee_id"] for c in _read(parsed, "contributions.json")] == [
        "C00000001"
    ]


def test_unresolved_committee_filtered_to_residual(tmp_path):
    """A contributing committee absent from ``cm`` can't be classified → residual
    reason ``unresolved_committee`` — never silently dropped."""
    pas2 = _pas2_row("C00000777", "C00000099", 5000, "T1") + "\n"  # not in cm
    data = _seed_cycle(tmp_path, **{"cm.txt": _CM, "ccl.txt": "", "itpas2.txt": pas2})
    parse_cycle(2024, data_dir=data, fetched_at=ENTRY_TS)
    parsed = data / "parsed" / "fec" / "2024"

    unparsed = _read(parsed, "fec-unparsed-manifest.json")
    assert _read(parsed, "contributions.json") == []
    filtered = unparsed["filtered_contributions"]
    assert len(filtered) == 1
    assert filtered[0]["contributor_committee_id"] == "C00000777"
    assert filtered[0]["reason"] == "unresolved_committee"


def test_short_itpas2_row_is_residual_not_dropped(tmp_path):
    """An itpas2 row with too few columns (< 18) lands in the residual with reason
    ``malformed_short_row`` and is counted — never silently dropped (CLAUDE.md).
    The raw itpas2 row total is recorded so kept + filtered reconciles."""
    short = "|".join(["C00000001", "TREASURER", "X"])  # only 3 columns
    pas2 = "\n".join(
        [
            _pas2_row("C00000001", "C00000099", 5000, "T1"),  # labor → kept
            short,  # malformed → residual
        ]
    ) + "\n"
    data = _seed_cycle(tmp_path, **{"cm.txt": _CM, "ccl.txt": "", "itpas2.txt": pas2})
    summary = parse_cycle(2024, data_dir=data, fetched_at=ENTRY_TS)
    parsed = data / "parsed" / "fec" / "2024"

    assert summary["contributions_kept"] == 1
    assert summary["contributions_filtered"] == 1

    unparsed = _read(parsed, "fec-unparsed-manifest.json")
    short_entries = [
        e
        for e in unparsed["filtered_contributions"]
        if e["reason"] == "malformed_short_row"
    ]
    assert len(short_entries) == 1
    assert short_entries[0]["contributor_committee_id"] == "C00000001"
    assert short_entries[0]["columns"] == 3

    manifest = _read(parsed, "fec-parse-manifest.json")
    counts = manifest["counts"]
    assert counts["filtered_by_reason"]["malformed_short_row"] == 1
    # The raw itpas2 row total is recorded so kept + filtered reconciles (2 rows).
    assert counts["source_rows"]["itpas2_total"] == 2
    assert counts["contributions_kept"] + counts["contributions_filtered"] == 2


def test_pac_cycle_limit_breach_is_flagged_not_dropped(tmp_path):
    """A PAC→candidate total over $10k/cycle is flagged in the manifest but every
    contribution is still kept (a sanity flag, not a drop — §13.5a)."""
    pas2 = "\n".join(
        [
            _pas2_row("C00000001", "C00000099", 7000, "T1"),  # labor
            _pas2_row("C00000001", "C00000099", 6000, "T2"),  # labor → total 13000
        ]
    ) + "\n"
    data = _seed_cycle(tmp_path, **{"cm.txt": _CM, "ccl.txt": "", "itpas2.txt": pas2})
    summary = parse_cycle(2024, data_dir=data, fetched_at=ENTRY_TS)
    parsed = data / "parsed" / "fec" / "2024"

    assert summary["contributions_kept"] == 2  # both kept
    assert summary["pac_limit_breaches"] == 1
    manifest = _read(parsed, "fec-parse-manifest.json")
    breach = manifest["pac_limit_breaches"][0]
    assert breach["contributor_committee_id"] == "C00000001"
    assert breach["recipient_committee_id"] == "C00000099"
    assert breach["total"] == 13000.0
    assert breach["org"] == "BIG UNION"  # rollup key = connected_organization_name
    assert breach["total"] > PAC_CYCLE_LIMIT


def test_transaction_id_dedup(tmp_path):
    """A literally repeated TRAN_ID is deduped (itpas2 is the single canonical
    file — a repeat is a true duplicate, not a second contribution)."""
    pas2 = "\n".join(
        [
            _pas2_row("C00000001", "C00000099", 5000, "DUP"),
            _pas2_row("C00000001", "C00000099", 5000, "DUP"),  # same id → dropped
        ]
    ) + "\n"
    data = _seed_cycle(tmp_path, **{"cm.txt": _CM, "ccl.txt": "", "itpas2.txt": pas2})
    summary = parse_cycle(2024, data_dir=data, fetched_at=ENTRY_TS)
    assert summary["contributions_kept"] == 1


def test_ccl_resolution_fills_committee_seam(tmp_path):
    """The #169 member-link committee seam (sentinel) is filled from ``ccl``:
    Adams (A000370) → candidate H4NC12100 → principal committee C00546358."""
    _seed_from_fixtures(tmp_path)
    _seed_reference(tmp_path)
    summary = parse_cycle(2024, data_dir=tmp_path, fetched_at=ENTRY_TS)
    parsed = tmp_path / "parsed" / "fec" / "2024"

    links = _read(parsed, "member-links.json")
    assert summary["member_links_resolved"] >= 1
    adams = [link for link in links if link["bioguide_id"] == "A000370"]
    assert len(adams) == 1
    assert adams[0]["candidate_id"] == "H4NC12100"
    # The sentinel is gone — resolved to the real principal committee from ccl.
    assert adams[0]["committee_id"] == "C00546358"
    assert adams[0]["committee_id"] != ""


def test_end_to_end_over_real_fixtures(tmp_path):
    """Full parse over the trimmed real fixtures: the labor/trade/corporate
    committees are kept and tagged; the three contributors absent from ``cm`` land
    in the residual as ``unresolved_committee``."""
    _seed_from_fixtures(tmp_path)
    summary = parse_cycle(2024, data_dir=tmp_path, fetched_at=ENTRY_TS)
    parsed = tmp_path / "parsed" / "fec" / "2024"

    assert summary["by_org_type"] == {"corporation": 2, "labor": 2, "trade": 3}
    assert summary["contributions_kept"] == 7
    assert summary["contributions_filtered"] == 3

    unparsed = _read(parsed, "fec-unparsed-manifest.json")
    reasons = {e["reason"] for e in unparsed["filtered_contributions"]}
    assert reasons == {"unresolved_committee"}
    # Affiliation limitation is stated in both manifests (declared, not a gap).
    manifest = _read(parsed, "fec-parse-manifest.json")
    assert "affiliation" in manifest["affiliation_limitation"].lower()
    assert manifest["schema_version"] == FEC_SCHEMA_VERSION
    assert "affiliation" in unparsed["affiliation_limitation"].lower()


def test_deterministic_rerun_is_byte_identical(tmp_path):
    """A re-parse from the same raw/ produces byte-identical output (offline,
    deterministic — re-parse, not migrate)."""
    _seed_from_fixtures(tmp_path)
    parse_cycle(2024, data_dir=tmp_path, fetched_at=ENTRY_TS)
    parsed = tmp_path / "parsed" / "fec" / "2024"
    first = {p.name: p.read_bytes() for p in parsed.iterdir()}
    parse_cycle(2024, data_dir=tmp_path, fetched_at=ENTRY_TS)
    second = {p.name: p.read_bytes() for p in parsed.iterdir()}
    assert first == second


def test_missing_bulk_files_skips_cleanly(tmp_path):
    """A cycle whose required bulk files are absent is a clean skip (None), not a
    crash — so a multi-cycle range survives an un-pulled cycle."""
    assert parse_cycle(2024, data_dir=tmp_path, fetched_at=ENTRY_TS) is None


def test_fec_parse_returns_nonzero_when_nothing_parsed(tmp_path, capsys):
    """``fec parse`` over only-missing cycles emits the summary to stdout and
    exits non-zero (nothing parsed)."""
    code = fec_parse([2024], data_dir=tmp_path, fetched_at=ENTRY_TS)
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "fec parse"
    assert out["cycles"] == []
    assert out["skipped_cycles"] == [2024]


def test_unit_builders(tmp_path):
    """The small index builders behave: cm → committee, ccl → principal map."""
    committees = build_committees([r.split("|") for r in _CM.splitlines()])
    assert committees["C00000001"].organization_type == "labor"
    assert org_rollup_key(committees["C00000001"]) == "BIG UNION"
    # A committee with no org type is normalized to (None, None).
    assert committees["C00000003"].organization_type is None
    assert committees["C00000003"].organization_type_raw is None

    principal = build_principal_committees(
        [r.split("|") for r in (FEC_FIXTURES / "ccl.txt").read_text().splitlines()]
    )
    assert principal["H4NC12100"] == "C00546358"

    # An over-limit pair is flagged; an at-limit pair is not.
    over = [
        FecPacContribution(
            recipient_committee_id="C00000099",
            contributor_committee_id="C00000001",
            amount=11000.0,
        )
    ]
    assert check_pac_limit(over, committees)
    at = [
        FecPacContribution(
            recipient_committee_id="C00000099",
            contributor_committee_id="C00000001",
            amount=10000.0,
        )
    ]
    assert check_pac_limit(at, committees) == []


# ---------------------------------------------------------------------------
# Super-PAC independent expenditures (GH-0194) — the separately-footed slice.
# ---------------------------------------------------------------------------
def test_ie_keeps_both_directions_filters_non_house(tmp_path):
    """Both support and oppose House IEs are kept and tagged; a non-House (Senate)
    IE is filtered ``not_house_candidate``; both kept directions reconcile."""
    _seed_from_fixtures(tmp_path, ie=True)
    _seed_reference(tmp_path)
    summary = parse_cycle(2024, data_dir=tmp_path, fetched_at=ENTRY_TS)
    parsed = tmp_path / "parsed" / "fec" / "2024"

    ies = _read(parsed, "independent-expenditures.json")
    directions = sorted(ie["support_oppose"] or "unspecified" for ie in ies)
    # Fixture House rows: Adams(S), Evans(S), Avlon(O), unattributed(S), Tran(blank).
    assert summary["ie_kept"] == 5
    assert directions == ["oppose", "support", "support", "support", "unspecified"]
    assert summary["ie_by_direction"] == {"oppose": 1, "support": 3, "unspecified": 1}

    # Every kept IE is office H, tagged with both raw + normalized direction.
    assert all(ie["office"] == "H" for ie in ies)
    avlon = next(ie for ie in ies if ie["support_oppose"] == "oppose")
    assert avlon["support_oppose_raw"] == "O"

    # The Senate row is filtered, never kept.
    unparsed = _read(parsed, "fec-unparsed-manifest.json")
    reasons = [e["reason"] for e in unparsed["filtered_independent_expenditures"]]
    assert "not_house_candidate" in reasons


def test_ie_unattributed_house_row_is_kept_not_dropped(tmp_path):
    """A House IE with a blank ``cand_id`` is reported ``unattributed`` in the
    residual AND still kept (never dropped, §13.7)."""
    _seed_from_fixtures(tmp_path, ie=True)
    _seed_reference(tmp_path)
    summary = parse_cycle(2024, data_dir=tmp_path, fetched_at=ENTRY_TS)
    parsed = tmp_path / "parsed" / "fec" / "2024"

    ies = _read(parsed, "independent-expenditures.json")
    blank = [ie for ie in ies if ie["candidate_id"] is None]
    assert len(blank) == 1  # the Jones (NY-17) row, blank cand_id — kept
    assert blank[0]["bioguide_id"] is None

    counts = _read(parsed, "fec-parse-manifest.json")["counts"]
    assert counts["ie_filtered_by_reason"]["unresolved_candidate"] == 1
    # The unattributed row is in BOTH kept and the residual (audit, not a drop).
    unparsed = _read(parsed, "fec-unparsed-manifest.json")
    assert any(
        e["reason"] == "unresolved_candidate"
        for e in unparsed["filtered_independent_expenditures"]
    )


def test_ie_joins_connected_org_and_bioguide(tmp_path):
    """A House IE whose spender is in ``cm`` carries the raw
    ``connected_organization_name``; one targeting a member in the CC0 bridge
    carries the bioguide."""
    _seed_from_fixtures(tmp_path, ie=True)
    _seed_reference(tmp_path)
    parse_cycle(2024, data_dir=tmp_path, fetched_at=ENTRY_TS)
    parsed = tmp_path / "parsed" / "fec" / "2024"

    ies = _read(parsed, "independent-expenditures.json")
    # The Adams-targeted IE: spender C00002469 is the machinists labor PAC in cm,
    # and H4NC12100 bridges to Adams (A000370) via id.fec[].
    adams_ie = next(ie for ie in ies if ie["candidate_id"] == "H4NC12100")
    assert adams_ie["bioguide_id"] == "A000370"
    assert (
        adams_ie["connected_organization_name"]
        == "INTERNATIONAL ASSOCIATION OF MACHINISTS AND AEROSPACE WORKERS"
    )
    assert adams_ie["provenance"] == "fec_ie"
    # A spender absent from cm (the C90… IE-only filer) leaves connected org None.
    evans_ie = next(ie for ie in ies if ie["candidate_id"] == "H4CO08034")
    assert evans_ie["connected_organization_name"] is None


def test_ie_short_row_is_residual_not_dropped(tmp_path):
    """A malformed short IE row lands in the residual ``malformed_short_row``,
    counted, never silently dropped."""
    _seed_from_fixtures(tmp_path, ie=True)
    _seed_reference(tmp_path)
    parse_cycle(2024, data_dir=tmp_path, fetched_at=ENTRY_TS)
    parsed = tmp_path / "parsed" / "fec" / "2024"
    counts = _read(parsed, "fec-parse-manifest.json")["counts"]
    assert counts["ie_filtered_by_reason"]["malformed_short_row"] == 1


def test_ie_absent_file_is_clean_skip(tmp_path):
    """A cycle pulled before GH-0194 (no IE CSV) parses contributions normally and
    emits an empty IE output — not a crash."""
    _seed_from_fixtures(tmp_path, ie=False)
    summary = parse_cycle(2024, data_dir=tmp_path, fetched_at=ENTRY_TS)
    parsed = tmp_path / "parsed" / "fec" / "2024"
    assert summary["ie_kept"] == 0
    assert _read(parsed, "independent-expenditures.json") == []


def test_ie_date_and_amount_parse(tmp_path):
    """``DD-MON-YY`` dates and decimal amounts parse; a blank date is None, kept."""
    header = (
        "cand_id,spe_id,spe_nam,can_office,sup_opp,exp_amo,exp_date,pur,"
        "image_num,tran_id"
    ).split(",")
    rows = [
        ["H0XX00001", "C00000001", "PAC", "H", "S", "1234.50", "28-OCT-24", "ads", "I1", "T1"],
        ["H0XX00001", "C00000001", "PAC", "H", "O", "", "", "", "I2", "T2"],
    ]
    kept, residual, by_dir = parse_independent_expenditures(header, rows, {}, {})
    assert [str(ie.date) for ie in kept] == ["2024-10-28", "None"]
    assert kept[0].amount == 1234.50 and kept[1].amount == 0.0
    assert by_dir == {"support": 1, "oppose": 1}
    assert residual == []
