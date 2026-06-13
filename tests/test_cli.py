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
    assert main(["inspect", "2021-2022", "--sample", "0.5"]) == 2


def test_inspect_rejects_out_of_range_sample():
    assert main(["inspect", "2022", "--sample", "1.5"]) == 2
    assert main(["inspect", "2022", "--sample", "0"]) == 2


def test_inspect_unparsed_year_exits_1(tmp_path, capsys):
    rc = main(["inspect", "2022", "--sample", "0.5", "--data-dir", str(tmp_path)])
    assert rc == 1
    assert "not parsed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Data-dir resolution (#50): flag → OPENHOUSE_DATA_DIR env → ./data, applied
# uniformly across pull / parse / read via one shared resolver.
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


def test_resolve_default_when_neither(monkeypatch):
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    assert resolve_data_dir(None) == Path("./data")


def test_resolve_empty_env_falls_through_to_default(monkeypatch):
    # An empty env var is treated as unset, not as a data root named "".
    monkeypatch.setenv(DATA_DIR_ENV, "")
    assert resolve_data_dir(None) == Path("./data")


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
    # real run path execute (scanning an absent data dir returns 0 cleanly — no
    # fixtures needed), so the production resolution line is exercised.
    monkeypatch.setattr(cli_mod, "resolve_data_dir", spy_resolve)
    return captured


@pytest.mark.parametrize("verb", ["pull", "parse", "read"])
def test_data_dir_precedence_uniform_across_verbs(verb, monkeypatch):
    if verb == "pull":
        cap = _capture_pull_data_dir(monkeypatch)
        argv_flag = ["pull", "2024", "--data-dir", "/flag/store"]
        argv_env = ["pull", "2024"]
        argv_default = ["pull", "2024"]
    elif verb == "parse":
        cap = _capture_parse_data_dir(monkeypatch)
        argv_flag = ["parse", "2024", "--data-dir", "/flag/store"]
        argv_env = ["parse", "2024"]
        argv_default = ["parse", "2024"]
    else:
        cap = _capture_read_data_dir(monkeypatch)
        argv_flag = ["read", "filings", "2024", "--data-dir", "/flag/store"]
        argv_env = ["read", "filings", "2024"]
        argv_default = ["read", "filings", "2024"]

    # flag wins even with env set
    monkeypatch.setenv(DATA_DIR_ENV, "/env/store")
    assert cli_mod.main(argv_flag) == 0
    assert cap["data_dir"] == Path("/flag/store")

    # env used when no flag
    monkeypatch.setenv(DATA_DIR_ENV, "/env/store")
    assert cli_mod.main(argv_env) == 0
    assert cap["data_dir"] == Path("/env/store")

    # default when neither
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    assert cli_mod.main(argv_default) == 0
    assert cap["data_dir"] == Path("./data")


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
            cli_mod.main([verb, "--help"])
        assert exc.value.code == 0
        assert "examples:" in capsys.readouterr().out
