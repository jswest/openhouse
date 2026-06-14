"""Tier-2 invariant checks (scripts/sweep_invariants.py).

Crafted records, not the fixture corpus — the checked-in fixtures predate the
asset_type_raw / *_raw schema (v7), so the asset_type and date invariants only
have teeth on freshly-parsed data. We exercise each invariant directly.
"""

import importlib.util
import json
from pathlib import Path

# The checker is a standalone script (not a package module), so load it by path.
_SPEC = importlib.util.spec_from_file_location(
    "sweep_invariants",
    Path(__file__).resolve().parent.parent / "scripts" / "sweep_invariants.py")
si = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(si)


def _clean_txn():
    return {"owner": "self", "asset": "Apple Inc. (AAPL) [ST]", "ticker": "AAPL",
            "asset_type": "ST", "asset_type_raw": "sT", "transaction_type": "P",
            "transaction_date": "2024-02-01", "date_raw": None,
            "notification_date": "2024-02-10", "notification_date_raw": None,
            "amount_range": {"low": 1001, "high": 15000, "label": "$1,001 - $15,000"},
            "cap_gains_over_200": None, "description": None}


# --- check_amount_range -----------------------------------------------------

def test_amount_range_clean_shapes_pass():
    assert si.check_amount_range({"low": 1, "high": 2, "label": "x"}) is None
    assert si.check_amount_range({"exact": 894.97, "label": "$894.97"}) is None


def test_amount_range_both_shapes_flagged():
    assert si.check_amount_range(
        {"low": 1, "high": 2, "exact": 1.5, "label": "x"}) is not None


def test_amount_range_neither_shape_flagged():
    assert si.check_amount_range({"label": "x"}) is not None


def test_amount_range_missing_label_flagged():
    assert si.check_amount_range({"low": 1, "high": 2}) is not None


def test_amount_range_partial_range_flagged():
    assert si.check_amount_range({"low": 1, "label": "x"}) is not None


def test_amount_range_null_exact_flagged():
    assert si.check_amount_range({"exact": None, "label": "x"}) is not None


# --- ptr_violations ---------------------------------------------------------

def test_ptr_clean_record_has_no_violations():
    assert si.ptr_violations(_clean_txn()) == []


def test_ptr_asset_type_not_normalized_flagged():
    txn = _clean_txn()
    txn["asset_type"] = "sT"  # should be normalized to "ST"
    invs = {v["invariant"] for v in si.ptr_violations(txn)}
    assert "asset_type_normalized" in invs


def test_ptr_ticker_disagrees_with_asset_flagged():
    txn = _clean_txn()
    txn["ticker"] = "WRONG"
    invs = {v["invariant"] for v in si.ptr_violations(txn)}
    assert "ticker_from_asset" in invs


def test_ptr_date_and_raw_both_set_flagged():
    txn = _clean_txn()
    txn["date_raw"] = "04/30/3031"  # date_raw set while transaction_date is also set
    invs = {v["invariant"] for v in si.ptr_violations(txn)}
    assert "date_raw_exclusive" in invs


def test_ptr_bad_amount_range_flagged():
    txn = _clean_txn()
    txn["amount_range"] = {"low": 1, "high": 2, "exact": 3.0, "label": "x"}
    invs = {v["invariant"] for v in si.ptr_violations(txn)}
    assert "amount_range_shape" in invs


def test_ptr_missing_asset_type_raw_skips_check():
    txn = _clean_txn()
    del txn["asset_type_raw"]  # pre-v7 record: can't compare, must not false-flag
    assert all(v["invariant"] != "asset_type_normalized"
               for v in si.ptr_violations(txn))


# --- fd_item_violations -----------------------------------------------------

def test_fd_schedule_a_clean_has_no_violations():
    item = {"asset": "Apple Inc. (AAPL)", "asset_type": "ST", "asset_type_raw": "sT",
            "value_of_asset": {"low": 1001, "high": 15000, "label": "$1,001 - $15,000"},
            "income_amount": {"exact": 12.0, "label": "$12.00"}, "raw_text": "..."}
    assert si.fd_item_violations("A", item) == []


def test_fd_schedule_a_bad_amount_field_flagged():
    item = {"asset": "X", "value_of_asset": {"label": "x"}}  # neither shape
    invs = {v["invariant"] for v in si.fd_item_violations("A", item)}
    assert "amount_range_shape" in invs


def test_fd_schedule_b_date_exclusive_flagged():
    item = {"asset": "X", "transaction_date": "2024-01-01",
            "transaction_date_raw": "01/01/3031"}
    invs = {v["invariant"] for v in si.fd_item_violations("B", item)}
    assert "date_raw_exclusive" in invs


# --- iter_violations end-to-end ---------------------------------------------

def test_iter_violations_walks_corpus(tmp_path):
    ptr_dir = tmp_path / "parsed" / "2024" / "ptr"
    ptr_dir.mkdir(parents=True)
    bad = _clean_txn()
    bad["ticker"] = "WRONG"
    (ptr_dir / "20012345.json").write_text(json.dumps({"transactions": [bad]}))
    found = list(si.iter_violations(tmp_path))
    assert len(found) == 1
    assert found[0]["doc_id"] == "20012345"
    assert found[0]["year"] == "2024"
    assert found[0]["location"] == "transactions[0]"


def test_iter_violations_clean_fixtures_have_none():
    fixtures = Path(__file__).resolve().parent / "fixtures"
    assert list(si.iter_violations(fixtures)) == []


def test_iter_violations_missing_parsed_dir_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        list(si.iter_violations(tmp_path))
