"""End-to-end acceptance: the offline ``parse`` → ``read`` pipeline (SPEC §11).

Every other test exercises one stage in isolation (PDF extraction, the parse
classifier, or ``read`` over hand-authored ``parsed/`` fixtures). This one closes
the loop the acceptance criteria actually name: seed a raw tree from the committed
PDF fixtures, run the *real* ``parse_year`` over it, then drive ``read.run`` over
the produced ``parsed/`` — asserting that an FD schedule body and a recovered PTR
trade both survive the round trip into read-shaped output, with the sound/complete
residual contract intact. No network, no hand-authored parsed JSON.

Fixtures used (ground truth in ``tests/fixtures/pdf/README.md``):

- ``efiled_fd_10042852.pdf`` (Hon. Bennie G. Thompson, type ``O``) — exercises the
  #12 FD-schedule path: Schedules A/C/E/F extract and a body file is written.
- ``efiled_ptr_wrap_20013811.pdf`` (type ``P``) — exercises the #46 amount-wrap
  recovery: three transactions, one whose ``$HIGH`` bound wrapped to the next line
  folds back into ``$50,001 - $100,000`` rather than being dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

from openhouse import read as read_mod
from openhouse.parse import parse_year

PDF_FIXTURES = Path(__file__).parent / "fixtures" / "pdf"
FD_PDF = PDF_FIXTURES / "efiled_fd_10042852.pdf"
PTR_PDF = PDF_FIXTURES / "efiled_ptr_wrap_20013811.pdf"

# A two-filing index: one annual FD (schedule body) and one PTR (trades). Both
# are 2020 e-filed bodies, so a single parse run produces both an fd/ and a ptr/
# body and the manifest reconciles with zero unparsed.
_INDEX_XML = """<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
  <Member>
    <Last>Thompson</Last><First>Bennie G.</First><Suffix></Suffix>
    <FilingType>O</FilingType><StateDst>MS02</StateDst>
    <Year>2020</Year><FilingDate>8/12/2021</FilingDate><DocID>10042852</DocID>
  </Member>
  <Member>
    <Last>Gaetz</Last><First>Matt</First><Suffix></Suffix>
    <FilingType>P</FilingType><StateDst>FL01</StateDst>
    <Year>2020</Year><FilingDate>2/14/2020</FilingDate><DocID>20013811</DocID>
  </Member>
</FinancialDisclosure>
"""

YEAR = 2020
FIXED_TS = "2026-06-12T00:00:00"


def _seed(data_dir: Path) -> None:
    raw = data_dir / "raw" / "clerk" / str(YEAR)
    (raw / "fd").mkdir(parents=True, exist_ok=True)
    (raw / "ptr").mkdir(parents=True, exist_ok=True)
    (raw / f"{YEAR}FD.xml").write_text(_INDEX_XML)
    (raw / "fd" / "10042852.pdf").write_bytes(FD_PDF.read_bytes())
    (raw / "ptr" / "20013811.pdf").write_bytes(PTR_PDF.read_bytes())


def _parse(data_dir: Path) -> None:
    parse_year(YEAR, data_dir=data_dir, fetched_at=FIXED_TS)


def test_parse_reconciles_with_no_gaps(tmp_path):
    # Acceptance: every filing accounted for — both e-filed bodies parse ok,
    # nothing lands in the unparsed manifest, and the manifest total matches.
    _seed(tmp_path)
    _parse(tmp_path)

    manifest = json.loads(
        (tmp_path / "parsed" / "clerk" / str(YEAR) / "parse-manifest.json").read_text()
    )
    counts = manifest["counts"]
    assert counts["total"] == 2
    assert counts["by_parse_status"].get("ok") == 2
    assert counts["by_parse_status"].get("error", 0) == 0
    assert counts["by_pdf_class"].get("efiled") == 2

    unparsed = json.loads(
        (tmp_path / "parsed" / "clerk" / str(YEAR) / "unparsed-manifest.json").read_text()
    )["unparsed"]
    assert unparsed == []


def test_fd_schedule_body_flows_into_read_filing(tmp_path, capsys):
    # The #12 FD path: parse writes a schedule body, and `read filing` surfaces it.
    _seed(tmp_path)
    _parse(tmp_path)

    body = json.loads(
        (tmp_path / "parsed" / "clerk" / str(YEAR) / "fd" / "10042852.json").read_text()
    )
    assert sorted(body["schedules"]) == ["A", "C", "E", "F"]

    rc = read_mod.run(
        ["--data-dir", str(tmp_path), "filing", "10042852"], current_year=YEAR
    )
    assert rc == 0
    detail = json.loads(capsys.readouterr().out)
    # `read filing` joins metadata + the extracted FD body, so the schedules
    # surface through read (not just on disk).
    assert detail["filing"]["doc_id"] == "10042852"
    assert sorted(detail["body"]["schedules"]) == ["A", "C", "E", "F"]


def test_recovered_ptr_trade_flows_into_read_trades(tmp_path, capsys):
    # The #46 path: the wrapped-$HIGH PTR row is recovered through parse and shows
    # up in `read trades` as $50,001 - $100,000 (never dropped, never half-ranged).
    _seed(tmp_path)
    _parse(tmp_path)

    rc = read_mod.run(
        ["--data-dir", str(tmp_path), "trades", str(YEAR)], current_year=YEAR
    )
    assert rc == 0
    out = capsys.readouterr()
    trades = json.loads(out.out)
    assert isinstance(trades, list)

    gaetz = [t for t in trades if t["doc_id"] == "20013811"]
    assert len(gaetz) == 3
    ranges = {
        (t["transaction"]["amount_range"]["low"], t["transaction"]["amount_range"]["high"])
        for t in gaetz
    }
    assert (50001, 100000) in ranges  # the recovered wrapped bound

    # Every trade carries its filer attached (the read contract) and the sound/
    # complete residual is declared on stderr relative to the parsed PTR set.
    assert all(t["filer_id"] for t in gaetz)
    assert "residual:" in out.err


def test_read_trades_is_deterministic_and_offline(tmp_path, capsys):
    # `read` is a pure function over parsed/: two invocations give byte-identical
    # stdout. (Offline by construction — read opens only files under --data-dir.)
    _seed(tmp_path)
    _parse(tmp_path)

    read_mod.run(["--data-dir", str(tmp_path), "trades", str(YEAR)], current_year=YEAR)
    first = capsys.readouterr().out
    read_mod.run(["--data-dir", str(tmp_path), "trades", str(YEAR)], current_year=YEAR)
    second = capsys.readouterr().out
    assert first == second
