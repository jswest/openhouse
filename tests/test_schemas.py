"""Tests for filing-metadata schemas + FilingType table (SPEC §2.1, §2.3, §6.1).

Each test maps to a *verified* 2024 edge case the schema must handle.
"""

from datetime import date

import pytest
from pydantic import ValidationError

from openhouse.schemas import (
    FILING_TYPE_LABELS,
    UNKNOWN_FILING_LABEL,
    AmountRange,
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
        filer_id="ga.allen.richard",
        state_district=StateDistrict(raw="GA12", state="GA", district=12),
        filing_type=FilingTypeInfo.from_code("P"),
        filing_date=date(2024, 1, 8),
        source_pdf="raw/clerk/2024/ptr/20024277.pdf",
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


# --- AmountRange: range vs. exact-dollar point (#49) ------------------------


def test_amount_range_bucket_serializes_without_exact():
    """The common $LOW-$HIGH bucket has no `exact` and serializes byte-identically
    to before #49 (no `"exact": null` noise)."""
    amt = AmountRange(low=1001, high=15000, label="$1,001 - $15,000")
    assert amt.exact is None
    assert amt.model_dump(mode="json") == {
        "low": 1001, "high": 15000, "label": "$1,001 - $15,000",
    }


def test_amount_range_exact_value_is_a_point_not_a_bucket():
    """An exact-dollar value lands in `exact`; low/high stay None and are omitted
    on serialization (the point form, not a fabricated bucket)."""
    amt = AmountRange(exact=894.97, label="$894.97")
    assert amt.low is None and amt.high is None
    assert amt.model_dump(mode="json") == {"exact": 894.97, "label": "$894.97"}


def test_amount_range_rejects_exact_mixed_with_bounds():
    """`exact` is mutually exclusive with low/high — a value masquerading as a fake
    {low: X, high: X} range is rejected, never silently accepted."""
    with pytest.raises(ValidationError):
        AmountRange(exact=894.97, low=894, high=895, label="$894.97")


def test_amount_range_rejects_neither_shape():
    """A row that is neither a range nor an exact value is invalid (the loud
    extract_failed path is upstream; the model itself never fabricates)."""
    with pytest.raises(ValidationError):
        AmountRange(label="???")


# --- FEC lane schemas (SPEC §13, #168) --------------------------------------

from openhouse.schemas import (  # noqa: E402
    FEC_ORG_TYPE_LABELS,
    FEC_SCHEMA_VERSION,
    PROVENANCE_FEC,
    SCHEMA_VERSION,
    FecCommittee,
    FecMemberCandidateLink,
    FecPacContribution,
)


def test_fec_schema_version_independent_of_clerk():
    """FEC_SCHEMA_VERSION is its own int, not coupled to SCHEMA_VERSION."""
    assert FEC_SCHEMA_VERSION == 1
    assert SCHEMA_VERSION == 10  # untouched by this change


def test_fec_org_type_table_is_the_six_path1_codes():
    assert set(FEC_ORG_TYPE_LABELS) == {"C", "T", "L", "M", "V", "W"}
    assert FEC_ORG_TYPE_LABELS["L"] == "labor"


def test_fec_connected_committee_round_trips():
    c = FecCommittee(
        committee_id="C00401224",
        name="EXAMPLE CORP POLITICAL ACTION COMMITTEE",
        connected_organization_name="Example Corp",
        organization_type=FEC_ORG_TYPE_LABELS["C"],
        organization_type_raw="C",
        committee_type="Q",
        affiliation=None,
    )
    assert c.committee_id == "C00401224"
    assert c.organization_type == "corporation"
    assert c.organization_type_raw == "C"
    assert c.provenance == PROVENANCE_FEC  # defaults to "fec"


def test_fec_committee_recipient_side_has_no_sponsor():
    """A member's own committee carries no connected-org link — both None."""
    c = FecCommittee(committee_id="C00000935", name="PELOSI FOR CONGRESS")
    assert c.connected_organization_name is None
    assert c.organization_type is None
    assert c.organization_type_raw is None


def test_fec_pac_contribution_double_entry_key():
    rec = FecPacContribution(
        recipient_committee_id="C00000935",
        contributor_committee_id="C00401224",
        amount=5000.0,
        date=date(2023, 6, 14),
        image_number="202307159123456789",
        transaction_id="SA11C.4821",
    )
    assert rec.line == "F3-11C"  # the Path-1 default line
    assert rec.amount == 5000.0
    assert rec.image_number == "202307159123456789"
    assert rec.transaction_id == "SA11C.4821"
    assert rec.provenance == PROVENANCE_FEC


def test_fec_member_candidate_link_round_trips():
    link = FecMemberCandidateLink(
        bioguide_id="P000197",
        candidate_id="H8CA05035",
        committee_id="C00000935",
    )
    assert link.bioguide_id == "P000197"
    assert link.candidate_id == "H8CA05035"
    assert link.provenance == PROVENANCE_FEC


def test_fec_records_carry_fec_provenance_by_default():
    """Provenance defaults to "fec" across all three FEC record types (§13)."""
    assert (
        FecCommittee(committee_id="C1", name="x").provenance
        == FecPacContribution(
            recipient_committee_id="C1",
            contributor_committee_id="C2",
            amount=1.0,
        ).provenance
        == FecMemberCandidateLink(
            bioguide_id="b", candidate_id="H1", committee_id="C1"
        ).provenance
        == "fec"
    )
