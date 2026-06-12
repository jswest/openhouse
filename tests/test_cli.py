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
