"""Tests for filing-metadata schemas + FilingType table (SPEC §2.1, §2.3, §6.1).

Each test maps to a *verified* 2024 edge case the schema must handle.
"""

from datetime import date

from openhouse.schemas import (
    FILING_TYPE_LABELS,
    UNKNOWN_FILING_LABEL,
    Filer,
    FilingMetadata,
    FilingTypeInfo,
    StateDistrict,
)


def _base_kwargs(**overrides):
    kwargs = dict(
        doc_id="20024277",
        year=2024,
        filer=Filer(first="Richard W.", last="Allen"),
        state_district=StateDistrict(raw="GA12", state="GA", district=12),
        filing_type=FilingTypeInfo.from_code("P"),
        filing_date=date(2024, 1, 8),
        source_pdf="raw/2024/ptr/20024277.pdf",
        pdf_class="efiled",
        parse_status="ok",
    )
    kwargs.update(overrides)
    return kwargs


def test_full_record_round_trips():
    rec = FilingMetadata(**_base_kwargs())
    assert rec.doc_id == "20024277"
    assert rec.year == 2024
    assert rec.filing_type.code == "P"
    assert rec.filing_type.label == "periodic_transaction_report"
    assert rec.state_district.district == 12


def test_empty_statedst_is_null():
    """Empty StateDst (seen on type W) → state_district is None."""
    rec = FilingMetadata(**_base_kwargs(state_district=None))
    assert rec.state_district is None


def test_empty_filing_date_is_null():
    """Empty FilingDate (seen on type W) → filing_date is None."""
    rec = FilingMetadata(**_base_kwargs(filing_date=None))
    assert rec.filing_date is None


def test_dc00_accepted():
    rec = FilingMetadata(
        **_base_kwargs(state_district=StateDistrict(raw="DC00", state="DC", district=0))
    )
    assert rec.state_district.state == "DC"
    assert rec.state_district.district == 0


def test_pr00_accepted():
    rec = FilingMetadata(
        **_base_kwargs(state_district=StateDistrict(raw="PR00", state="PR", district=0))
    )
    assert rec.state_district.state == "PR"
    assert rec.state_district.district == 0


def test_district_zero_at_large():
    """District 0 = at-large / n.a. — a plain in-state at-large seat."""
    rec = FilingMetadata(
        **_base_kwargs(state_district=StateDistrict(raw="WY00", state="WY", district=0))
    )
    assert rec.state_district.district == 0


def test_four_digit_doc_id_is_opaque_string():
    rec = FilingMetadata(**_base_kwargs(doc_id="7940"))
    assert rec.doc_id == "7940"
    assert isinstance(rec.doc_id, str)


def test_year_independent_of_filing_date():
    """Year=2024 with a filing_date in 2025 is valid — never cross-validated."""
    rec = FilingMetadata(**_base_kwargs(year=2024, filing_date=date(2025, 4, 29)))
    assert rec.year == 2024
    assert rec.filing_date.year == 2025


def test_filer_prefix_suffix_default_null():
    f = Filer(first="Alma", last="Adams")
    assert f.prefix is None
    assert f.suffix is None


def test_filer_suffix_preserved():
    f = Filer(first="John", last="Doe", suffix="Jr.")
    assert f.suffix == "Jr."


def test_unknown_filing_type_preserves_raw_code():
    """An unmapped letter still yields a valid record; raw code preserved."""
    info = FilingTypeInfo.from_code("Z")
    assert info.code == "Z"
    assert info.label == UNKNOWN_FILING_LABEL
    rec = FilingMetadata(**_base_kwargs(filing_type=info))
    assert rec.filing_type.code == "Z"
    assert rec.filing_type.label == UNKNOWN_FILING_LABEL


def test_all_twelve_verified_codes_map():
    for code in ["C", "X", "P", "O", "A", "D", "W", "H", "T", "B", "G", "E"]:
        info = FilingTypeInfo.from_code(code)
        assert info.code == code
        assert info.label != UNKNOWN_FILING_LABEL
        assert FILING_TYPE_LABELS[code] == info.label
