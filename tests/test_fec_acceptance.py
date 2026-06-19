"""End-to-end acceptance: the FEC lane's ``pull`` → ``parse`` → ``read`` path (SPEC §13).

The Clerk-lane acceptance test (:mod:`tests.test_acceptance`) closes the loop the
acceptance criteria name for that source; this one does the same for the FEC lane
(#173). Every other FEC test exercises one stage in isolation (the bulk download,
the Path-1 parse classifier, or ``fec read`` over a parsed cycle). This one drives
the *real* code of all three stages over one cycle (2024) and asserts a concrete
``donors``/``pac`` answer comes out with its declared guarantee + residual.

**Fully offline — NO network.** ``fec pull`` is the lane's only network step, so it
is exercised through the same seam the unit tests use: an ``httpx.Client`` wired to
``httpx.MockTransport`` that serves the four bulk zips built in-memory from the
trimmed real fixtures under ``tests/fixtures/fec/`` (the live host's 302-to-storage
and bare-stem inner-member quirks reproduced, GH-0170). The politeness sleep is a
no-op so the suite never waits 10 s. The CC0 ``congress-legislators`` reference set
(fetched by the Clerk ``pull`` into ``raw/reference/``, joined offline by ``parse``
for the member→candidate→committee anchor §13.2) is staged from the checked-in
reference fixtures — the FEC ``pull`` does not re-fetch it.

Ground truth (``tests/fixtures/fec/`` + ``tests/fixtures/reference/``): Adams
(bioguide A000370) → candidate H4NC12100 → principal committee C00546358, with
three connected SSFs contributing seven Path-1 receipts:

- MACHINISTS (labor)    C00002469 → $10,000 over 2 receipts
- MULTIFAMILY (trade)   C00130773 → $10,000 over 3 receipts
- FICO PAC (corporate)  C00792127 → $7,500  over 2 receipts

Three further itpas2 contributors are absent from ``cm`` → filtered to the residual
as ``unresolved_committee`` (declared, never a silent gap).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from openhouse import fec_read
from openhouse.fec_pull import fec_pull
from openhouse.fec_parse import fec_parse

# Reuse the unit suite's mock-transport plumbing verbatim — the same offline seam,
# the same in-memory zips built from the trimmed real fixtures.
from tests.test_fec_pull import CONTACT, make_client, make_handler, no_sleep

REFERENCE_DIR = Path(__file__).parent / "fixtures" / "reference"

CYCLE = 2024
ENTRY_TS = "2026-06-18T00:00:00"
CURRENT_YEAR = 2026


def _stage_reference(data_dir: Path) -> None:
    """Stage the CC0 reference set into ``raw/reference/`` (what Clerk ``pull`` did).

    The FEC ``pull`` fetches only the four FEC bulk files; the shared
    ``congress-legislators`` set is the Clerk lane's responsibility, so the
    acceptance flow stages it from the checked-in fixtures before ``parse``.
    """
    ref = data_dir / "raw" / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    for path in REFERENCE_DIR.glob("*.json"):
        shutil.copy(path, ref / path.name)


def _pull(data_dir: Path) -> None:
    """Run the *real* ``fec pull`` over the offline mock transport (no network)."""
    code = fec_pull(
        [CYCLE],
        data_dir=data_dir,
        contact=CONTACT,
        fetched_at=ENTRY_TS,
        client=make_client(make_handler(CYCLE)),
        sleep=no_sleep,
    )
    assert code == 0


def _full_pipeline(data_dir: Path, capsys=None) -> None:
    """pull (mocked network) → stage reference → parse (offline) — the lane's spine.

    ``fec parse`` emits its own JSON summary to stdout; when a caller will read
    ``fec read``'s stdout next, pass ``capsys`` so that summary is drained and the
    two JSON objects never run together.
    """
    _pull(data_dir)
    _stage_reference(data_dir)
    code = fec_parse([CYCLE], data_dir=data_dir, fetched_at=ENTRY_TS)
    assert code == 0
    if capsys is not None:
        capsys.readouterr()  # drain the parse summary before a later read


def _read(args: list[str], data_dir: Path, capsys):
    """Run the *real* ``fec read`` with ``--data-dir`` injected; (code, stdout, stderr)."""
    code = fec_read.run([*args, "--data-dir", str(data_dir)], current_year=CURRENT_YEAR)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_pull_extracts_four_bulk_files_offline(tmp_path):
    # Acceptance, stage 1: `fec pull` lands the four Path-1 bulk files into
    # raw/fec/<cycle>/ through the mock transport — never the live FEC site.
    _pull(tmp_path)
    raw = tmp_path / "raw" / "fec" / str(CYCLE)
    for inner in ("cn.txt", "ccl.txt", "cm.txt", "itpas2.txt"):
        assert (raw / inner).exists(), inner
    manifest = json.loads((raw / "fec-pull-manifest.json").read_text())
    # Four Path-1 zips + the super-PAC IE CSV (GH-0194).
    assert manifest["count"] == 5
    assert (raw / f"independent_expenditure_{CYCLE}.csv").exists()


def test_parse_reconciles_kept_and_residual(tmp_path):
    # Acceptance, stage 2: the real parse keeps + tags the seven Path-1 receipts
    # and accounts for the three unresolved contributors in the residual — no gap.
    _full_pipeline(tmp_path)
    parsed = tmp_path / "parsed" / "fec" / str(CYCLE)

    counts = json.loads((parsed / "fec-parse-manifest.json").read_text())["counts"]
    assert counts["contributions_kept"] == 7
    assert counts["contributions_filtered"] == 3
    assert counts["by_org_type"] == {"corporation": 2, "labor": 2, "trade": 3}

    unparsed = json.loads((parsed / "fec-unparsed-manifest.json").read_text())
    reasons = {e["reason"] for e in unparsed["filtered_contributions"]}
    assert reasons == {"unresolved_committee"}  # filtered, never dropped


def test_donors_answer_flows_through_with_guarantee(tmp_path, capsys):
    # Acceptance, stage 3: a real `fec read donors` answer comes out of the full
    # pull→parse→read path, rolled up to organization with the org-type tag and
    # the sound/complete guarantee + residual declared on stderr.
    _full_pipeline(tmp_path, capsys)

    code, out, err = _read(["donors", "A000370", "2024"], tmp_path, capsys)
    assert code == 0
    rollup = json.loads(out)

    by_org = {r["org"]: r for r in rollup}
    machinists = by_org["INTERNATIONAL ASSOCIATION OF MACHINISTS AND AEROSPACE WORKERS"]
    assert machinists == {
        "org": "INTERNATIONAL ASSOCIATION OF MACHINISTS AND AEROSPACE WORKERS",
        "organization_type": "labor",
        "total": 10000.0,
        "n_contributions": 2,
    }
    # Labor is included and tagged (not filtered) — a load-bearing scope decision.
    assert any(r["organization_type"] == "labor" for r in rollup)
    assert {r["organization_type"] for r in rollup} == {"labor", "trade", "corporation"}
    assert [r["total"] for r in rollup] == [10000.0, 10000.0, 7500.0]

    # The guarantee + residual is tied to the parse-manifest counts (7 kept, 3
    # filtered), with the framing + affiliation caveat explicit, not implied.
    assert "guarantee: complete over the 7 Path-1 itemized" in err
    assert "3 contribution(s) were filtered" in err
    assert "not total influence" in err
    assert "Affiliated parent+subsidiary PACs are NOT collapsed" in err


def test_pac_inverse_answer_flows_through(tmp_path, capsys):
    # The inverse query also closes the loop end to end: an org's PAC → the
    # members it supported, attributed to Adams' bioguide.
    _full_pipeline(tmp_path, capsys)
    code, out, _ = _read(["pac", "MACHINISTS", "2024"], tmp_path, capsys)
    assert code == 0
    assert json.loads(out) == [
        {"bioguide_id": "A000370", "total": 10000.0, "n_contributions": 2}
    ]
