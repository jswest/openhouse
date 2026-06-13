"""Offline tests for the ``read`` query surface (#10).

``read`` is a pure function over ``parsed/`` — never network, never writes. These
run against hand-authored fixtures under ``tests/fixtures/parsed/`` (the producer
of real parsed data, #9, runs in parallel; these fixtures conform to the on-disk
shapes ``read`` consumes). Each test invokes ``read.run(...)`` with ``--data-dir``
pointed at the fixtures and asserts stdout (JSON / table) and the stderr residual
+ guarantee lines.

The two declared query modes on ``trades`` are the heart of the issue:

- ``--ticker`` is SOUND (exact symbol, no false positives) — asserted by the fact
  that it does NOT match the null-ticker ``[ST]`` whose *name* contains the symbol
  text "AAPL", and that its stderr declares the at-least bound + null-ticker
  residual.
- ``--asset`` is COMPLETENESS-leaning (substring, may over-match) — asserted by
  the fact that it DOES find that null-ticker ``[ST]``, and that its stderr
  declares the at-most bound.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openhouse import read as read_mod

FIXTURES = Path(__file__).parent / "fixtures" / "parsed"
CURRENT_YEAR = 2026


def run(args, data_dir=None):
    """Invoke ``read.run`` with ``--data-dir`` defaulted to the fixtures."""
    dd = str(data_dir if data_dir is not None else FIXTURES.parent)
    return read_mod.run(["--data-dir", dd, *args], current_year=CURRENT_YEAR)


def _data_dir():
    # The fixtures live at tests/fixtures/parsed/<year>/; --data-dir is its parent.
    return FIXTURES.parent


# --- filings ---------------------------------------------------------------


def test_filings_json_default(capsys):
    rc = run(["filings", "2021"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert {f["doc_id"] for f in out} == {
        "20100001", "20100002", "10100003", "8100004"
    }


def test_filings_table(capsys):
    rc = run(["--table", "filings", "2021"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "doc_id" in out and "filer_id" in out
    assert "ny.anders.albert" in out


def test_filings_type_filter(capsys):
    # --type accepts both the label-substring and the raw code.
    run(["filings", "2021", "--type", "ptr"])
    out = json.loads(capsys.readouterr().out)
    assert {f["doc_id"] for f in out} == {"20100001", "20100002", "8100004"}

    run(["filings", "2021", "--type", "O"])
    out = json.loads(capsys.readouterr().out)
    assert {f["doc_id"] for f in out} == {"10100003"}


def test_filings_state_filter(capsys):
    run(["filings", "2021", "--state", "ny"])
    out = json.loads(capsys.readouterr().out)
    assert {f["doc_id"] for f in out} == {"20100001", "8100004"}


def test_filings_date_filters(capsys):
    run(["filings", "2021", "--since", "2021-06-01", "--until", "2021-08-01"])
    out = json.loads(capsys.readouterr().out)
    assert {f["doc_id"] for f in out} == {"20100002"}


def test_filings_residual_line(capsys):
    run(["filings", "2021"])
    err = capsys.readouterr().err
    # The 2021 manifest declares scanned 1 / missing 1 / error 1 → 3 unparsed.
    assert "residual:" in err
    assert "scanned 1" in err and "missing 1" in err and "error 1" in err


# --- member substring (filer_id + raw names) -------------------------------


def test_member_matches_filer_id(capsys):
    run(["filings", "2021", "--member", "anders.albert"])
    out = json.loads(capsys.readouterr().out)
    assert {f["doc_id"] for f in out} == {"20100001"}


def test_member_matches_raw_name(capsys):
    # "Beatrice" is only in the raw first-name field, not the filer_id slug head.
    run(["filings", "2021", "--member", "Beatrice"])
    out = json.loads(capsys.readouterr().out)
    assert {f["doc_id"] for f in out} == {"20100002"}


# --- bioguide exact match (precise, no substring fuzzing) -------------------


def test_filings_bioguide_exact_match(capsys):
    # --bioguide is exact on the verified bioguide_id field: A000001 → Anders only.
    run(["filings", "2021", "--bioguide", "A000001"])
    out = json.loads(capsys.readouterr().out)
    assert {f["doc_id"] for f in out} == {"20100001"}


def test_filings_bioguide_is_case_insensitive(capsys):
    run(["filings", "2021", "--bioguide", "a000001"])
    out = json.loads(capsys.readouterr().out)
    assert {f["doc_id"] for f in out} == {"20100001"}


def test_filings_bioguide_no_substring_fuzzing(capsys):
    # A substring/prefix of a real id must NOT match (exact-only, unlike --member).
    run(["filings", "2021", "--bioguide", "A0000"])
    out = json.loads(capsys.readouterr().out)
    assert out == []


def test_filings_bioguide_unmatched_id_returns_nothing(capsys):
    # A different, valid-looking id matches no record (no false positives).
    run(["filings", "2021", "--bioguide", "Z999999"])
    out = json.loads(capsys.readouterr().out)
    assert out == []


def test_filings_bioguide_and_member_are_anded(capsys):
    # Both filters passed → AND. A000001 (Anders) with a --member that matches
    # Anders keeps the record; with one that matches Bell it drops to empty.
    run(["filings", "2021", "--bioguide", "A000001", "--member", "anders"])
    out = json.loads(capsys.readouterr().out)
    assert {f["doc_id"] for f in out} == {"20100001"}

    run(["filings", "2021", "--bioguide", "A000001", "--member", "bell"])
    out = json.loads(capsys.readouterr().out)
    assert out == []


def test_trades_bioguide_exact_match(capsys):
    # On trades, --bioguide filters on the filer's bioguide_id. A000001 (Anders)
    # owns the 4-transaction body in 20100001.
    run(["trades", "2021", "--bioguide", "A000001"])
    out = json.loads(capsys.readouterr().out)
    assert {t["doc_id"] for t in out} == {"20100001"}
    assert len(out) == 4


def test_trades_bioguide_and_member_are_anded(capsys):
    # AND semantics on trades too: A000001 (Anders) + a --member matching Bell → none.
    run(["trades", "2021", "--bioguide", "A000001", "--member", "bell"])
    out = json.loads(capsys.readouterr().out)
    assert out == []


# --- filing <doc_id> -------------------------------------------------------


def test_filing_with_body(capsys):
    rc = run(["filing", "20100001"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["filing"]["doc_id"] == "20100001"
    assert payload["body"] is not None
    assert len(payload["body"]["transactions"]) == 4


def test_filing_table(capsys):
    rc = run(["--table", "filing", "20100001"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "doc_id" in out and "transactions" in out


def test_filing_fd_body_is_loaded(capsys):
    # #12 writes parsed/<year>/fd/<DocID>.json for an annual FD; cmd_filing must
    # load it under ``body`` (not emit a bare null like a non-PTR used to).
    rc = run(["filing", "10100003"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["filing"]["doc_id"] == "10100003"
    assert payload["body"] is not None
    assert "schedules" in payload["body"]
    assert "A" in payload["body"]["schedules"]


def test_filing_fd_without_body_notes_no_body(capsys):
    # An fd-family filing with no parsed body (a cover sheet/extension) → body null
    # plus a clear "no parsed body" note that now covers FDs, not just PTRs.
    rc = run(["filing", "10200002"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["filing"]["doc_id"] == "10200002"
    assert payload["body"] is None
    assert "no parsed body" in captured.err and "FD" in captured.err


def test_filing_unknown_doc_id_errors(capsys):
    rc = run(["filing", "99999999"])
    assert rc == 1
    assert "no parsed filing" in capsys.readouterr().err


# --- trades: the sound/complete split --------------------------------------


def test_trades_json_default(capsys):
    rc = run(["trades", "2021"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    # 4 transactions in the one parsed PTR body with a doc; the scanned PTR
    # (8100004) has no body file so contributes nothing.
    assert len(out) == 6  # 4 from 20100001 + 2 from 20100002
    # Each trade carries its filer (joined by DocID).
    for t in out:
        assert "filer_id" in t and "transaction" in t


def test_trades_table(capsys):
    rc = run(["--table", "trades", "2021"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ticker" in out and "asset" in out
    # The readable filer name renders alongside the opaque filer_id column.
    assert "filer" in out  # the new name column header
    assert "Anders" in out  # the 2021 fixture filer's name


def test_ticker_is_sound_exact_match(capsys):
    # --ticker AAPL must match the two real AAPL rows (ticker == "AAPL") and must
    # NOT match the null-ticker [ST] whose asset NAME contains "AAPL"
    # ("AAPL Hospitality Trust"). No false positives.
    rc = run(["trades", "2021", "--ticker", "aapl"])
    assert rc == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assets = {t["transaction"]["asset"] for t in out}
    assert assets == {"Apple Inc. (AAPL) [ST]"}  # both real AAPL ticker rows
    assert all(t["transaction"]["ticker"] == "AAPL" for t in out)
    # The name-only "AAPL Hospitality Trust" (null ticker) is NOT matched.
    assert "AAPL Hospitality Trust [ST]" not in assets


def test_ticker_declares_bound_and_null_residual(capsys):
    run(["trades", "2021", "--ticker", "AAPL"])
    err = capsys.readouterr().err
    assert "SOUND" in err and "AT LEAST" in err
    # One in-range [ST] row has a null ticker (AAPL Hospitality Trust); the [CS]
    # bond's null ticker does NOT count (not a tickered asset type).
    assert "blind spot" in err
    assert "1 in-range [ST]/[OP]" in err


def test_asset_is_completeness_leaning(capsys):
    # --asset AAPL substring catches the null-ticker [ST] that --ticker misses,
    # plus the real AAPL rows (their asset text embeds "(AAPL)").
    rc = run(["trades", "2021", "--asset", "aapl"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assets = {t["transaction"]["asset"] for t in out}
    assert "AAPL Hospitality Trust [ST]" in assets  # the row --ticker cannot see
    assert "Apple Inc. (AAPL) [ST]" in assets


def test_asset_declares_at_most_bound(capsys):
    run(["trades", "2021", "--asset", "AAPL"])
    err = capsys.readouterr().err
    assert "COMPLETENESS-leaning" in err and "AT MOST" in err


def test_trades_residual_line(capsys):
    run(["trades", "2021"])
    err = capsys.readouterr().err
    assert "residual:" in err
    assert "scanned 1" in err and "missing 1" in err and "error 1" in err


# --- trades: the narrowing filters -----------------------------------------


def test_trades_owner_filter(capsys):
    run(["trades", "2021", "--owner", "SP"])
    out = json.loads(capsys.readouterr().out)
    assert all(t["transaction"]["owner"] == "SP" for t in out)
    assert len(out) == 2  # AAPL purchase + MSFT S(partial), both SP


def test_trades_type_filter_sale_catches_partial(capsys):
    # --type S catches both "S" and "S(partial)".
    run(["trades", "2021", "--type", "S"])
    out = json.loads(capsys.readouterr().out)
    types = sorted(t["transaction"]["transaction_type"] for t in out)
    assert "S(partial)" in types
    assert all(t.startswith("S") for t in types)


def test_trades_min_amount_filter(capsys):
    run(["trades", "2021", "--min-amount", "50000"])
    out = json.loads(capsys.readouterr().out)
    for t in out:
        assert t["transaction"]["amount_range"]["low"] >= 50000
    # The treasury bond (50001) and the AAPL sale (100001) qualify.
    assert len(out) == 2


def test_min_amount_treats_exact_value_as_its_own_point():
    # #49: an exact-dollar amount {exact: X, label} has no `low`; --min-amount must
    # treat it as the point [X, X], so X clears a threshold <= X and is excluded by
    # one > X. A range {low, high} keeps using `low`. Sound, no fabricated range.
    from argparse import Namespace

    from openhouse.read import _amount_low, _trade_matches

    exact_txn = {
        "amount_range": {"exact": 894.97, "label": "$894.97"},
        "transaction_date": "2021-03-01",
    }
    range_txn = {
        "amount_range": {"low": 1001, "high": 15000, "label": "$1,001 - $15,000"},
        "transaction_date": "2021-03-01",
    }
    assert _amount_low(exact_txn) == 894.97
    assert _amount_low(range_txn) == 1001

    def _args(min_amount):
        return Namespace(
            ticker=None, asset=None, owner=None, type=None,
            since=None, until=None, member=None, bioguide=None,
            min_amount=min_amount,
        )

    # The exact $894.97 clears 500 but not 1000 (point, not a half-open range).
    assert _trade_matches(exact_txn, {}, _args(500)) is True
    assert _trade_matches(exact_txn, {}, _args(1000)) is False


def test_trades_date_filter(capsys):
    run(["trades", "2021", "--since", "2021-06-01"])
    out = json.loads(capsys.readouterr().out)
    for t in out:
        assert t["transaction"]["transaction_date"] >= "2021-06-01"


def test_trades_member_filter(capsys):
    run(["trades", "2021", "--member", "bell"])
    out = json.loads(capsys.readouterr().out)
    assert all(t["filer_id"] == "ca.bell.beatrice" for t in out)
    assert len(out) == 2  # 20100002's two transactions


# --- summary ---------------------------------------------------------------


def test_summary_json(capsys):
    rc = run(["summary", "2021"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["years"]) == 1
    y = payload["years"][0]
    assert y["year"] == 2021
    assert y["counts"]["by_pdf_class"] == {"efiled": 3, "scanned": 1, "missing": 1}
    assert y["counts"]["by_parse_status"]["error"] == 1
    assert y["identity_warnings"] == 1


def test_summary_table(capsys):
    rc = run(["--table", "summary", "2021"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "efiled" in out and "warnings" in out


def test_summary_residual_line(capsys):
    run(["summary", "2021"])
    err = capsys.readouterr().err
    assert "residual:" in err


# --- ranges + graceful missing-year degradation ----------------------------


def test_range_spans_years(capsys):
    rc = run(["trades", "2021-2022"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    years = {t["year"] for t in out}
    assert years == {2021, 2022}


def test_partial_range_reports_skipped_year(capsys):
    # 2023 is not parsed (no fixture dir) → reported on stderr, answered from 2021.
    rc = run(["filings", "2021-2023"])
    assert rc == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert {f["year"] for f in out} == {2021, 2022}
    assert "2023" in captured.err
    assert "not parsed" in captured.err


def test_empty_data_dir_fails_loudly(tmp_path, capsys):
    # #79: a range query against a dir with NO parsed years must fail loudly —
    # non-zero exit + error on stderr naming the resolved data dir — not return a
    # misleading exit-0 empty result that bounds nothing.
    dd = tmp_path / "data"  # no parsed/ tree at all
    for sub in ("filings", "trades", "summary"):
        rc = run([sub, "2021"], data_dir=dd)
        captured = capsys.readouterr()
        assert rc != 0, f"{sub} should fail loudly on an empty data dir"
        assert captured.out == "", f"{sub} must emit no JSON to stdout on failure"
        assert "error:" in captured.err
        assert str(dd) in captured.err
        assert "NOT an empty match" in captured.err


def test_all_requested_years_unparsed_fails_loudly(capsys):
    # Every requested year is absent from the fixtures (2098-2099) → loud failure,
    # not a quiet "answered from the parsed years only" with an empty result.
    rc = run(["trades", "2098-2099"])
    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "error:" in captured.err


def test_real_zero_match_over_parsed_data_stays_exit_0(capsys):
    # #79 guard: a query that DID scan parsed data (2021 is parsed) and simply
    # matched nothing is a legitimate sound zero — it must stay exit 0 with an
    # empty JSON array and the normal residual line, NOT become an error.
    rc = run(["trades", "2021", "--ticker", "ZZZZNOSUCH"])
    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out) == []
    assert "residual" in captured.err
    assert "error:" not in captured.err


def test_range_residual_aggregates_over_present_years(capsys):
    run(["summary", "2021-2022"])
    err = capsys.readouterr().err
    # 2021 contributes scanned 1/missing 1/error 1; 2022 contributes none.
    assert "scanned 1" in err and "missing 1" in err and "error 1" in err


# --- no writes, no network -------------------------------------------------


def test_read_writes_no_files(tmp_path, capsys):
    # Copy fixtures into a tmp dir, snapshot it, run every subcommand, assert the
    # tree is byte-identical afterward (read never writes).
    import shutil

    dd = tmp_path / "data"
    shutil.copytree(FIXTURES, dd / "parsed")

    def snapshot():
        return {
            p.relative_to(dd): p.read_bytes()
            for p in sorted(dd.rglob("*"))
            if p.is_file()
        }

    before = snapshot()
    run(["filings", "2021"], data_dir=dd)
    run(["filing", "20100001"], data_dir=dd)
    run(["trades", "2021-2022", "--ticker", "AAPL"], data_dir=dd)
    run(["trades", "2021", "--asset", "AAPL"], data_dir=dd)
    run(["summary", "2021"], data_dir=dd)
    capsys.readouterr()
    after = snapshot()
    assert before == after


def test_read_makes_no_network_call(monkeypatch, capsys):
    # Poison socket so any network attempt raises; read must complete cleanly.
    import socket

    def _boom(*a, **k):
        raise AssertionError("read attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    rc = run(["trades", "2021-2022"])
    assert rc == 0
    capsys.readouterr()


# --- critic regression: --data-dir / --table accepted in either position -------


def test_dataflags_accepted_after_subcommand(capsys):
    # The natural position (matching `parse 2021 --data-dir X`): flags AFTER the
    # subcommand must be honored, not just before it.
    dd = str(FIXTURES.parent)
    rc = read_mod.run(["filings", "2021", "--data-dir", dd], current_year=CURRENT_YEAR)
    assert rc == 0
    recs = json.loads(capsys.readouterr().out)
    assert isinstance(recs, list) and recs  # --data-dir honored → real records


def test_table_flag_accepted_after_subcommand(capsys):
    dd = str(FIXTURES.parent)
    rc = read_mod.run(
        ["filings", "2021", "--data-dir", dd, "--table"], current_year=CURRENT_YEAR
    )
    assert rc == 0
    out = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)  # --table → aligned text, not JSON


def test_dataflags_accepted_before_subcommand(capsys):
    # The leading position must still work too.
    dd = str(FIXTURES.parent)
    rc = read_mod.run(
        ["--table", "--data-dir", dd, "filings", "2021"], current_year=CURRENT_YEAR
    )
    assert rc == 0


# --- GH-0040 LOW: residual base / not_classified / asymmetry / schema drift -----


def test_trades_residual_base_is_ptr_efiled_not_all_efiled(capsys):
    # The 2021 manifest's by_pdf_class.efiled is 3, but only TWO of those are
    # type-P (PTR) efiled filings (20100001, 20100002); 10100003 is an efiled FD
    # with no PTR body. The trades residual must be complete over the body-bearing
    # type-P base (2), not the manifest's all-efiled total (3).
    run(["trades", "2021"])
    err = capsys.readouterr().err
    # The wording must make clear the base is the e-filed PTR (type-P) population.
    assert "complete over the 2 e-filed PTR (type-P) filings" in err


def test_trades_residual_includes_not_classified(capsys):
    # The 2021 manifest declares not_classified 2 (which INCLUDES the 1 errored
    # record — an errored extraction keeps pdf_class=None, matching real parse
    # output where error ⊆ not_classified). The residual must surface
    # not_classified so the unknown is not under-reported, but must NOT add error
    # in again (that double-counts every error).
    run(["trades", "2021"])
    err = capsys.readouterr().err
    assert "not_classified 2" in err
    assert "of which error 1" in err  # error shown as a sub-breakdown, not added
    # unparsed total = scanned 1 + missing 1 + not_classified 2 = 4 (error NOT added).
    assert "4 did not parse" in err


def test_trades_residual_reconciles_parsed_plus_unparsed_equals_total():
    # The reconciling invariant (matching parse.py): efiled + unparsed == total,
    # where unparsed = scanned + missing + not_classified and error ⊆
    # not_classified. The 2021 fixture's not_classified (2) INCLUDES its 1 errored
    # record, so adding error into the total would over-count (5 unparsed, 8 ≠ 7).
    from openhouse.read import _residual_counts

    manifest = json.loads(
        (FIXTURES / "2021" / "parse-manifest.json").read_text()
    )
    total = manifest["counts"]["total"]
    r = _residual_counts(FIXTURES.parent, [2021])
    assert r["parsed"] + r["unparsed"] == total  # efiled + unparsed == total
    assert r["unparsed"] == r["scanned"] + r["missing"] + r["not_classified"]
    assert r["error"] <= r["not_classified"]  # error ⊆ not_classified, never added


def test_filings_residual_includes_not_classified(capsys):
    # The not_classified bucket surfaces on every range residual, not just trades.
    run(["filings", "2021"])
    err = capsys.readouterr().err
    assert "not_classified 2" in err


def test_ticker_blind_spot_states_filter_asymmetry(capsys):
    # The null-ticker blind-spot count applies --member/dates but NOT
    # --owner/--type/--min-amount; the output must say so (conservative over-report).
    run(["trades", "2021", "--ticker", "AAPL", "--owner", "SP"])
    err = capsys.readouterr().err
    assert "blind spot" in err
    assert "ignoring --owner/--type/--min-amount" in err


def test_schema_version_drift_warns_once(capsys):
    # The fixtures carry schema_version "0.2.0", which differs from the current
    # SCHEMA_VERSION; read must emit exactly ONE drift warning per run.
    run(["trades", "2021-2022"])
    err = capsys.readouterr().err
    assert err.count("warning: parsed tree was written by schema_version") == 1
    assert "re-parse, not migrate" in err


def test_schema_version_drift_warns_on_filing(capsys):
    # The single-filing path warns too (it has no range, resolves to the found year).
    run(["filing", "20100001"])
    err = capsys.readouterr().err
    assert "schema_version" in err and "re-parse, not migrate" in err


def test_no_schema_warning_when_versions_match(tmp_path, capsys):
    # Rewrite a year's manifest to the current SCHEMA_VERSION → no drift warning.
    import shutil

    from openhouse.schemas import SCHEMA_VERSION

    dd = tmp_path / "data"
    shutil.copytree(FIXTURES, dd / "parsed")
    mpath = dd / "parsed" / "2021" / "parse-manifest.json"
    manifest = json.loads(mpath.read_text())
    manifest["schema_version"] = SCHEMA_VERSION
    mpath.write_text(json.dumps(manifest))

    run(["trades", "2021"], data_dir=dd)
    err = capsys.readouterr().err
    assert "schema_version" not in err
