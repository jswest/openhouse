"""Tests for the shared year-range parser (SPEC §9, §2.1)."""

import pytest

from openhouse.cli import MIN_YEAR, YearRangeError, parse_year_range

CURRENT_YEAR = 2026  # injected, never read from the clock


def test_single_year():
    assert parse_year_range("2024", CURRENT_YEAR) == [2024]


def test_inclusive_range():
    assert parse_year_range("2019-2024", CURRENT_YEAR) == [2019, 2020, 2021, 2022, 2023, 2024]


def test_single_year_at_lower_bound():
    assert parse_year_range(str(MIN_YEAR), CURRENT_YEAR) == [MIN_YEAR]


def test_single_year_at_upper_bound():
    assert parse_year_range(str(CURRENT_YEAR), CURRENT_YEAR) == [CURRENT_YEAR]


def test_range_spanning_full_bounds():
    years = parse_year_range(f"{MIN_YEAR}-{CURRENT_YEAR}", CURRENT_YEAR)
    assert years[0] == MIN_YEAR
    assert years[-1] == CURRENT_YEAR
    assert years == list(range(MIN_YEAR, CURRENT_YEAR + 1))


def test_year_before_2008_rejected():
    with pytest.raises(YearRangeError):
        parse_year_range("2007", CURRENT_YEAR)


def test_range_starting_before_2008_rejected():
    with pytest.raises(YearRangeError):
        parse_year_range("2005-2010", CURRENT_YEAR)


def test_year_after_current_rejected():
    with pytest.raises(YearRangeError):
        parse_year_range("2027", CURRENT_YEAR)


def test_reversed_range_rejected():
    with pytest.raises(YearRangeError):
        parse_year_range("2024-2019", CURRENT_YEAR)


def test_non_numeric_rejected():
    with pytest.raises(YearRangeError):
        parse_year_range("twenty24", CURRENT_YEAR)


def test_non_four_digit_rejected():
    with pytest.raises(YearRangeError):
        parse_year_range("24", CURRENT_YEAR)


def test_too_many_parts_rejected():
    with pytest.raises(YearRangeError):
        parse_year_range("2019-2020-2021", CURRENT_YEAR)


def test_pre_2012_emits_ptr_warning(capsys):
    parse_year_range("2008-2010", CURRENT_YEAR)
    err = capsys.readouterr().err
    assert "PTR" in err and "2012" in err


def test_post_2012_no_ptr_warning(capsys):
    parse_year_range("2013-2014", CURRENT_YEAR)
    assert capsys.readouterr().err == ""


# --- `inspect` dispatch (#56) ------------------------------------------------
from openhouse.cli import main  # noqa: E402


def test_inspect_rejects_year_range():
    assert main(["clerk", "inspect", "2021-2022", "--sample", "0.5"]) == 2


def test_inspect_rejects_out_of_range_sample():
    assert main(["clerk", "inspect", "2022", "--sample", "1.5"]) == 2
    assert main(["clerk", "inspect", "2022", "--sample", "0"]) == 2


def test_inspect_unparsed_year_exits_1(tmp_path, capsys):
    rc = main(["clerk", "inspect", "2022", "--sample", "0.5", "--data-dir", str(tmp_path)])
    assert rc == 1
    assert "not parsed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Data-dir resolution (#50, #80): flag → OPENHOUSE_DATA_DIR env → ~/.openhouse,
# applied uniformly across pull / parse / read via one shared resolver.
# ---------------------------------------------------------------------------

from pathlib import Path

from openhouse import cli as cli_mod
from openhouse import parse as parse_mod
from openhouse import pull as pull_mod
from openhouse.cli import DATA_DIR_ENV, resolve_data_dir


def test_resolve_flag_beats_env(monkeypatch):
    monkeypatch.setenv(DATA_DIR_ENV, "/env/store")
    assert resolve_data_dir("/flag/store") == Path("/flag/store")


def test_resolve_env_when_no_flag(monkeypatch):
    monkeypatch.setenv(DATA_DIR_ENV, "/env/store")
    assert resolve_data_dir(None) == Path("/env/store")


def test_resolve_default_when_neither(monkeypatch, tmp_path):
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert resolve_data_dir(None) == tmp_path / ".openhouse"


def test_resolve_empty_env_falls_through_to_default(monkeypatch, tmp_path):
    # An empty env var is treated as unset, not as a data root named "".
    monkeypatch.setenv(DATA_DIR_ENV, "")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert resolve_data_dir(None) == tmp_path / ".openhouse"


def test_resolve_default_is_home_relative_not_cwd(monkeypatch, tmp_path):
    # #80: the default is a per-user dotfolder in $HOME, independent of cwd.
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    resolved = resolve_data_dir(None)
    assert resolved.is_absolute()
    assert resolved == tmp_path / ".openhouse"


def test_shadow_warning_when_local_data_nonempty(monkeypatch, tmp_path, capsys):
    # #80: a non-empty ./data in cwd, shadowed by the new default, gets a
    # one-time stderr note (no flag, no env). We never read from ./data.
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "stale.json").write_text("{}")
    cli_mod._shadow_warning_emitted = False

    resolve_data_dir(None)
    first = capsys.readouterr()
    assert "./data" in first.err
    assert "~/.openhouse" in first.err

    # One-time: a second call in the same process stays quiet.
    resolve_data_dir(None)
    assert capsys.readouterr().err == ""


def test_no_shadow_warning_when_local_data_absent(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.chdir(tmp_path)  # no ./data here
    cli_mod._shadow_warning_emitted = False
    resolve_data_dir(None)
    assert capsys.readouterr().err == ""


def test_no_shadow_warning_when_flag_or_env(monkeypatch, tmp_path, capsys):
    # The note only fires when the default is actually in use.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "stale.json").write_text("{}")

    cli_mod._shadow_warning_emitted = False
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    resolve_data_dir("/flag/store")
    assert capsys.readouterr().err == ""

    cli_mod._shadow_warning_emitted = False
    monkeypatch.setenv(DATA_DIR_ENV, "/env/store")
    resolve_data_dir(None)
    assert capsys.readouterr().err == ""


def _capture_pull_data_dir(monkeypatch):
    captured = {}

    def fake_pull(years, *, data_dir, **kwargs):
        captured["data_dir"] = data_dir
        return 0

    monkeypatch.setattr(pull_mod, "pull", fake_pull)
    return captured


def _capture_parse_data_dir(monkeypatch):
    captured = {}

    def fake_parse(years, *, data_dir, **kwargs):
        captured["data_dir"] = data_dir
        return 0

    monkeypatch.setattr(parse_mod, "parse", fake_parse)
    return captured


def _capture_read_data_dir(monkeypatch):
    captured = {}
    real_resolve = cli_mod.resolve_data_dir

    def spy_resolve(flag_value):
        result = real_resolve(flag_value)
        captured["data_dir"] = result
        return result

    # read.py calls cli_mod.resolve_data_dir at run time; spy on it and let the
    # real run path execute, so the production resolution line is exercised. The
    # absent dirs here now make `read` exit non-zero (it fails loudly on a data
    # dir with no parsed years — #79); this test only cares that the SPY captured
    # the resolved path, so the caller asserts on cap["data_dir"], not the rc.
    monkeypatch.setattr(cli_mod, "resolve_data_dir", spy_resolve)
    return captured


@pytest.mark.parametrize("verb", ["pull", "parse", "read"])
def test_data_dir_precedence_uniform_across_verbs(verb, monkeypatch, tmp_path):
    # Isolate HOME so the default resolves to a tmp dotfolder, not the real one.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if verb == "pull":
        cap = _capture_pull_data_dir(monkeypatch)
        argv_flag = ["clerk", "pull", "2024", "--data-dir", "/flag/store"]
        argv_env = ["clerk", "pull", "2024"]
        argv_default = ["clerk", "pull", "2024"]
    elif verb == "parse":
        cap = _capture_parse_data_dir(monkeypatch)
        argv_flag = ["clerk", "parse", "2024", "--data-dir", "/flag/store"]
        argv_env = ["clerk", "parse", "2024"]
        argv_default = ["clerk", "parse", "2024"]
    else:
        cap = _capture_read_data_dir(monkeypatch)
        argv_flag = ["clerk", "read", "filings", "2024", "--data-dir", "/flag/store"]
        argv_env = ["clerk", "read", "filings", "2024"]
        argv_default = ["clerk", "read", "filings", "2024"]

    # `read` against these absent dirs now fails loudly (#79); that is fine — this
    # test asserts the spy captured the RESOLVED path, not the exit code. pull/parse
    # are stubbed and still return 0.
    def _run(argv):
        rc = cli_mod.main(argv)
        if verb != "read":
            assert rc == 0

    # flag wins even with env set
    monkeypatch.setenv(DATA_DIR_ENV, "/env/store")
    _run(argv_flag)
    assert cap["data_dir"] == Path("/flag/store")

    # env used when no flag
    monkeypatch.setenv(DATA_DIR_ENV, "/env/store")
    _run(argv_env)
    assert cap["data_dir"] == Path("/env/store")

    # default when neither
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    _run(argv_default)
    assert cap["data_dir"] == tmp_path / ".openhouse"


def test_version_flag_prints_version(capsys):
    # `action="version"` exits 0 after printing; --version is a top-level flag,
    # so it never reaches the `read` interception in main().
    from openhouse import __version__

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"openhouse {__version__}"


def test_top_help_shows_pipeline_epilog(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    # The epilog orients the reader: the pipeline, env vars, coverage bounds.
    assert "typical workflow:" in out
    assert "OPENHOUSE_DATA_DIR" in out
    assert "PTRs (STOCK Act) from 2012" in out


def test_subcommand_help_has_examples(capsys):
    for verb in ("pull", "parse", "read"):
        with pytest.raises(SystemExit) as exc:
            cli_mod.main(["clerk", verb, "--help"])
        assert exc.value.code == 0
        assert "examples:" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Targeted pull flags (#78) wire through main() into pull_mod.pull.
# ---------------------------------------------------------------------------
def test_pull_targeted_flags_thread_into_pull(monkeypatch):
    captured = {}

    def fake_pull(years, **kwargs):
        captured["years"] = years
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(pull_mod, "pull", fake_pull)
    rc = cli_mod.main(
        [
            "clerk", "pull", "2020-2024",
            "--contact", "Jane Doe <jane@example.com>",
            "--member", "Pelosi",
            "--newest-first",
        ]
    )
    assert rc == 0
    assert captured["member"] == "Pelosi"
    assert captured["doc_id"] is None
    assert captured["newest_first"] is True


def test_pull_doc_id_flag_threads_into_pull(monkeypatch):
    captured = {}

    def fake_pull(years, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(pull_mod, "pull", fake_pull)
    rc = cli_mod.main(
        ["clerk", "pull", "2024", "--contact", "Jane Doe <jane@example.com>",
         "--doc-id", "20024277"]
    )
    assert rc == 0
    assert captured["doc_id"] == "20024277"
    assert captured["newest_first"] is False


def test_pull_help_lists_targeted_examples(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["clerk", "pull", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--member" in out and "--doc-id" in out and "--newest-first" in out


# --- FEC lane: year→cycle expansion + stderr note (#168) --------------------

from openhouse.cli import (  # noqa: E402
    expand_years_to_cycles,
    fec_parsed_dir,
    fec_raw_dir,
    year_to_cycle,
)


def test_year_to_cycle_odd_rolls_up():
    """An odd year folds into the next even-ending cycle (2023 → 2024)."""
    assert year_to_cycle(2023) == 2024


def test_year_to_cycle_even_is_its_own_cycle():
    assert year_to_cycle(2024) == 2024


def test_expand_collapses_both_halves_of_a_cycle():
    """Both years of a cycle de-dupe to one entry; a 2023-2024 range is one cycle."""
    assert expand_years_to_cycles([2023, 2024]) == [2024]


def test_expand_multiple_cycles_sorted_unique():
    assert expand_years_to_cycles([2021, 2022, 2023, 2024]) == [2022, 2024]


def test_fec_pull_odd_year_emits_cycle_note(capsys, monkeypatch):
    # `fec pull` is now implemented (#170); with no contact it stops at the
    # User-Agent gate (rc 1), but the year→cycle note still prints first.
    monkeypatch.delenv("OPENHOUSE_CONTACT", raising=False)
    rc = cli_mod.main(["fec", "pull", "2023"])
    err = capsys.readouterr().err
    assert "2-year cycles" in err
    assert "2024" in err  # expanded cycle
    assert "contact" in err.lower()  # the User-Agent gate, not a stub
    assert rc == 1


def test_fec_pull_even_year_no_cycle_note(capsys, monkeypatch):
    monkeypatch.delenv("OPENHOUSE_CONTACT", raising=False)
    rc = cli_mod.main(["fec", "pull", "2024"])
    err = capsys.readouterr().err
    assert "2-year cycles" not in err  # no expansion happened
    assert "contact" in err.lower()
    assert rc == 1


def test_fec_parse_range_collapses_to_one_cycle_note(capsys):
    rc = cli_mod.main(["fec", "parse", "2023-2024"])
    err = capsys.readouterr().err
    assert "2-year cycles" in err
    assert "not yet implemented" in err
    assert rc == 1


def test_fec_pull_bad_year_fails_arg_validation(capsys):
    rc = cli_mod.main(["fec", "pull", "nope"])
    assert rc == 2
    assert "not yet implemented" not in capsys.readouterr().err


def test_fec_pull_requires_a_year(capsys):
    rc = cli_mod.main(["fec", "pull"])
    assert rc == 2
    assert "requires a year" in capsys.readouterr().err


def test_fec_pull_dispatches_to_module_with_expanded_cycle(monkeypatch, tmp_path):
    # `fec pull 2023` reaches the real module with the expanded cycle (2024), the
    # resolved data dir, and the parsed flags — proof the stub is gone (#170).
    import openhouse.fec_pull as fec_pull_mod

    captured = {}

    def fake(cycles, **kwargs):
        captured["cycles"] = cycles
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(fec_pull_mod, "fec_pull", fake)
    monkeypatch.setenv("OPENHOUSE_CONTACT", "Jane Doe <jane@example.com>")
    rc = cli_mod.main(
        ["fec", "pull", "2023", "--data-dir", str(tmp_path)]
    )
    assert rc == 0
    assert captured["cycles"] == [2024]
    assert captured["kwargs"]["data_dir"] == tmp_path
    assert captured["kwargs"]["contact"] == "Jane Doe <jane@example.com>"


def test_fec_cycle_keyed_path_helpers():
    """fec pull 2023 and 2024 both resolve to .../fec/2024 (cycle-keyed)."""
    root = Path("/data")
    assert fec_raw_dir(root, year_to_cycle(2023)) == root / "raw" / "fec" / "2024"
    assert fec_raw_dir(root, year_to_cycle(2024)) == root / "raw" / "fec" / "2024"
    assert fec_parsed_dir(root, 2024) == root / "parsed" / "fec" / "2024"
