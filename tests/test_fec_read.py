"""Offline tests for the FEC query surface (``openhouse fec read``, #172).

Every test runs offline and deterministically: ``fec read`` is a pure function
over ``parsed/fec/<cycle>/`` (what ``fec parse`` wrote). Each test first parses the
trimmed real fixtures (or a synthetic cycle) into a tmp data dir, then queries it
through :func:`openhouse.fec_read.run` — exercising the donors roll-up (incl.
corporate/labor tagging and the ``--org-type`` slice), the inverse ``pac`` lookup,
the declared guarantee/residual on stderr, and graceful degradation on an un-parsed
cycle.

The trimmed real fixtures (``tests/fixtures/fec/``) + CC0 reference fixtures
resolve Adams (bioguide A000370) → candidate H4NC12100 → principal committee
C00546358, with three connected SSFs contributing seven Path-1 receipts:
- MACHINISTS (labor)    C00002469 → $10,000 over 2 receipts
- MULTIFAMILY (trade)   C00130773 → $10,000 over 3 receipts
- FICO PAC (corporate)  C00792127 → $7,500  over 2 receipts
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from openhouse import fec_read
from openhouse.fec_parse import parse_cycle

FIXTURES = Path(__file__).parent / "fixtures"
FEC_FIXTURES = FIXTURES / "fec"
REFERENCE_DIR = FIXTURES / "reference"

ENTRY_TS = "2026-06-18T00:00:00"
CURRENT_YEAR = 2026


def _seed_and_parse(tmp_path: Path, *, cycle: int = 2024) -> Path:
    """Copy the trimmed real fixtures + CC0 reference, then parse one cycle."""
    raw = tmp_path / "raw" / "fec" / str(cycle)
    raw.mkdir(parents=True, exist_ok=True)
    for name in ("cn.txt", "ccl.txt", "cm.txt", "itpas2.txt"):
        shutil.copy(FEC_FIXTURES / name, raw / name)
    ref = tmp_path / "raw" / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    for path in REFERENCE_DIR.glob("*.json"):
        shutil.copy(path, ref / path.name)
    parse_cycle(cycle, data_dir=tmp_path, fetched_at=ENTRY_TS)
    return tmp_path


def _run(args: list[str], data_dir: Path, capsys):
    """Run ``fec read`` with ``--data-dir`` injected; return (code, stdout, stderr)."""
    code = fec_read.run([*args, "--data-dir", str(data_dir)], current_year=CURRENT_YEAR)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_donors_rollup_sorted_with_org_tags(tmp_path, capsys):
    """``donors`` rolls receipts up to organization with the org-type tag, sorted by
    total desc (labor + trade tie at $10k → name asc tie-break, then corporate)."""
    data = _seed_and_parse(tmp_path)
    code, out, _ = _run(["donors", "A000370", "2024"], data, capsys)
    assert code == 0
    rollup = json.loads(out)

    by_org = {r["org"]: r for r in rollup}
    assert by_org["INTERNATIONAL ASSOCIATION OF MACHINISTS AND AEROSPACE WORKERS"] == {
        "org": "INTERNATIONAL ASSOCIATION OF MACHINISTS AND AEROSPACE WORKERS",
        "organization_type": "labor",
        "total": 10000.0,
        "n_contributions": 2,
    }
    assert by_org["NATIONAL MULTIFAMILY HOUSING COUNCIL"]["organization_type"] == "trade"
    assert by_org["NATIONAL MULTIFAMILY HOUSING COUNCIL"]["total"] == 10000.0
    fico = by_org["FAIR ISAAC CORPORATION POLITICAL ACTION COMMITTEE (FICO PAC)"]
    assert fico["organization_type"] == "corporation"
    assert fico["total"] == 7500.0
    # Sorted by total desc: the two $10k orgs (tie → name asc) precede the $7.5k one.
    assert [r["total"] for r in rollup] == [10000.0, 10000.0, 7500.0]
    assert rollup[-1]["org"].startswith("FAIR ISAAC")


def test_donors_org_type_slice(tmp_path, capsys):
    """``--org-type`` slices the tagged set to one connected-SSF class."""
    data = _seed_and_parse(tmp_path)

    code, out, _ = _run(["donors", "A000370", "2024", "--org-type", "labor"], data, capsys)
    assert code == 0
    labor = json.loads(out)
    assert [r["organization_type"] for r in labor] == ["labor"]
    assert labor[0]["total"] == 10000.0

    code, out, _ = _run(
        ["donors", "A000370", "2024", "--org-type", "corporation"], data, capsys
    )
    corp = json.loads(out)
    assert [r["org"] for r in corp] == [
        "FAIR ISAAC CORPORATION POLITICAL ACTION COMMITTEE (FICO PAC)"
    ]
    assert corp[0]["total"] == 7500.0


def test_donors_bad_org_type_rejected(tmp_path, capsys):
    """An --org-type that is not a connected-SSF class fails loudly (exit 2)."""
    data = _seed_and_parse(tmp_path)
    code, _, err = _run(
        ["donors", "A000370", "2024", "--org-type", "frobnicate"], data, capsys
    )
    assert code == 2
    assert "not a connected-SSF class" in err


def test_pac_inverse_attributes_to_member(tmp_path, capsys):
    """``pac`` is the inverse: an org's PAC → the members it supported."""
    data = _seed_and_parse(tmp_path)
    code, out, _ = _run(["pac", "MACHINISTS", "2024"], data, capsys)
    assert code == 0
    rollup = json.loads(out)
    assert rollup == [
        {"bioguide_id": "A000370", "total": 10000.0, "n_contributions": 2}
    ]


def test_guarantee_and_residual_on_stderr(tmp_path, capsys):
    """Every response declares its guarantee + residual (sound-or-complete), tied to
    the parse-manifest counts (7 kept, 3 filtered unresolved_committee)."""
    data = _seed_and_parse(tmp_path)
    _, _, err = _run(["donors", "A000370", "2024"], data, capsys)
    assert "guarantee: complete over the 7 Path-1 itemized" in err
    assert "3 contribution(s) were filtered" in err
    assert "3 unresolved_committee" in err
    # The framing + affiliation caveat are explicit, not implied.
    assert "not total influence" in err
    assert "no dark money" in err
    assert "Affiliated parent+subsidiary PACs are NOT collapsed" in err


def test_table_output(tmp_path, capsys):
    """``--table`` renders aligned columns to stdout instead of JSON."""
    data = _seed_and_parse(tmp_path)
    code, out, _ = _run(["donors", "A000370", "2024", "--table"], data, capsys)
    assert code == 0
    header = out.splitlines()[0]
    assert header.split() == ["org", "organization_type", "total", "n_contributions"]
    assert "10000.00" in out


def test_graceful_degradation_on_unparsed_cycle(tmp_path, capsys):
    """A range spanning an un-parsed cycle reports the skip on stderr and answers
    from the parsed cycle(s) — never a crash (SPEC §13 mirrors §5)."""
    data = _seed_and_parse(tmp_path, cycle=2024)  # 2022 left un-parsed
    code, out, err = _run(["donors", "A000370", "2021-2024"], data, capsys)
    assert code == 0
    assert "cycle(s) [2022] are not parsed" in err
    # Still answers from 2024.
    assert json.loads(out)[0]["total"] == 10000.0


def test_no_parsed_data_is_not_an_empty_match(tmp_path, capsys):
    """A query over a data dir with nothing parsed fails loudly (exit 1), not a
    misleading empty roll-up (the sound/complete contract — CLAUDE.md)."""
    code, _, err = _run(["donors", "A000370", "2024"], tmp_path, capsys)
    assert code == 1
    assert "NOT an empty match" in err


def test_year_to_cycle_expansion_note(tmp_path, capsys):
    """An odd year expands to its enclosing even-ending cycle with a stderr note."""
    data = _seed_and_parse(tmp_path, cycle=2024)
    code, out, err = _run(["donors", "A000370", "2023"], data, capsys)
    assert code == 0
    assert "resolve to cycle(s) 2024" in err
    assert json.loads(out)[0]["total"] == 10000.0


def test_cli_dispatch_end_to_end(tmp_path, capsys):
    """The CLI routes ``openhouse fec read donors …`` to the real sub-parser."""
    from openhouse.cli import main

    data = _seed_and_parse(tmp_path)
    code = main(
        ["fec", "read", "donors", "A000370", "2024", "--data-dir", str(data)]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)[0]["organization_type"] in {
        "labor",
        "trade",
    }
