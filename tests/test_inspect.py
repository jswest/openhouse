"""Offline tests for the ``inspect`` core (#56).

The core is pure (no I/O, no wall-clock), so these run on hand-built filing dicts
and ``tmp_path`` — no on-disk fixtures needed. They pin the four invariants the
issue's acceptance leans on:

- sampling is **deterministic**, **monotonic** (0.2 ⊇ 0.1), and **stratified**
  (every non-empty cell represented for any sample > 0);
- the verdict's ``count > 0 ⟺ boolean false`` invariant holds (None = untallied);
- the snapshot hash detects a re-parse that changed a reviewed filing;
- the scorecard's doc-level rates and entry-level rollup add up, with stale
  verdicts counted but excluded from the rates.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from openhouse.inspect import core, labels
from openhouse.inspect.verdict import LABELS_SCHEMA_VERSION, Verdict


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _filing(doc_id, *, pdf_class="efiled", code="P", status="ok"):
    return {
        "doc_id": doc_id,
        "pdf_class": pdf_class,
        "parse_status": status,
        "filing_type": {"code": code, "label": "x"},
    }


def _population(n, *, pdf_class="efiled", code="P"):
    return [_filing(f"{pdf_class}-{code}-{i:04d}", pdf_class=pdf_class, code=code) for i in range(n)]


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------
def test_reviewable_keeps_only_ok():
    pop = [_filing("a"), _filing("b", status="error"), _filing("c", status=None)]
    assert [f["doc_id"] for f in core.reviewable(pop)] == ["a"]


def test_select_is_deterministic():
    pop = _population(200)
    a = core.select(pop, 0.1, seed=7)
    b = core.select(pop, 0.1, seed=7)
    assert [f["doc_id"] for f in a] == [f["doc_id"] for f in b]


def test_select_changes_with_seed():
    pop = _population(200)
    a = {f["doc_id"] for f in core.select(pop, 0.1, seed=1)}
    b = {f["doc_id"] for f in core.select(pop, 0.1, seed=2)}
    assert a != b  # different seed → different (overlapping but not identical) set


def test_select_is_monotonic():
    pop = _population(300)
    small = {f["doc_id"] for f in core.select(pop, 0.1, seed=3)}
    large = {f["doc_id"] for f in core.select(pop, 0.2, seed=3)}
    assert small <= large  # 0.2 is a strict superset of 0.1


def test_select_is_stratified_every_cell_represented():
    # Tiny scanned-PTR cell must not be drowned out by a big efiled-other cell.
    pop = _population(400, pdf_class="efiled", code="C") + _population(
        3, pdf_class="scanned", code="P"
    )
    chosen = core.select(pop, 0.05, seed=5)
    cells = {core.stratum_key(f) for f in chosen}
    assert ("scanned", "ptr") in cells  # ceil(0.05*3)=1 → at least one
    assert ("efiled", "other") in cells


def test_select_fraction_is_about_right_per_stratum():
    pop = _population(1000, pdf_class="efiled", code="P")
    chosen = core.select(pop, 0.2, seed=9)
    assert len(chosen) == 200  # ceil(0.2 * 1000)


def test_select_bounds():
    pop = _population(50)
    assert core.select(pop, 0, seed=1) == []
    assert len(core.select(pop, 1, seed=1)) == 50


def test_select_returns_sorted_by_doc_id():
    pop = _population(100)
    chosen = core.select(pop, 0.3, seed=4)
    ids = [f["doc_id"] for f in chosen]
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Snapshot hashing
# ---------------------------------------------------------------------------
def test_snapshot_is_stable_and_order_independent():
    filing = {"doc_id": "x", "a": 1, "b": 2}
    reordered = {"b": 2, "doc_id": "x", "a": 1}
    body = {"transactions": [{"asset": "AAPL"}]}
    assert core.snapshot_hash(filing, body) == core.snapshot_hash(reordered, body)
    assert core.snapshot_hash(filing, body).startswith("sha256:")


def test_snapshot_detects_change():
    filing = {"doc_id": "x", "v": 1}
    h = core.snapshot_hash(filing, None)
    assert core.is_stale(h, {"doc_id": "x", "v": 2}, None)  # re-parse changed it
    assert not core.is_stale(h, {"doc_id": "x", "v": 1}, None)


# ---------------------------------------------------------------------------
# Verdict invariant
# ---------------------------------------------------------------------------
def _verdict(**over):
    base = dict(
        is_fully_precise=True,
        is_fully_recalled=True,
        is_metadata_accurate=True,
        is_metadata_fully_complete=True,
        snapshot="sha256:deadbeef",
    )
    base.update(over)
    return base


def test_verdict_accepts_untallied_none():
    Verdict(**_verdict(is_fully_recalled=False, n_missing_entries=None))  # "wrong, didn't tally"


def test_verdict_positive_count_requires_false_boolean():
    with pytest.raises(ValidationError):
        Verdict(**_verdict(is_fully_precise=True, n_incorrect_entries=2))


def test_verdict_zero_count_requires_true_boolean():
    with pytest.raises(ValidationError):
        Verdict(**_verdict(is_fully_recalled=False, n_missing_entries=0))


def test_verdict_negative_count_rejected():
    with pytest.raises(ValidationError):
        Verdict(**_verdict(is_fully_precise=False, n_incorrect_entries=-1))


def test_verdict_consistent_pair_ok():
    v = Verdict(**_verdict(is_fully_recalled=False, n_missing_entries=12))
    assert v.n_missing_entries == 12


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------
def _reviewed(filing, *, precise=True, recalled=True, n_inc=None, n_miss=None, stale=False):
    return {
        "filing": filing,
        "stale": stale,
        "verdict": _verdict(
            is_fully_precise=precise,
            is_fully_recalled=recalled,
            n_incorrect_entries=n_inc,
            n_missing_entries=n_miss,
        ),
    }


def test_scorecard_doc_level_and_entry_rollup():
    f = lambda i: _filing(f"s{i}", pdf_class="scanned", code="P")
    reviewed = [
        _reviewed(f(0), recalled=False, n_miss=12),
        _reviewed(f(1), recalled=False, n_miss=8),
        _reviewed(f(2), recalled=True),
    ]
    card = core.scorecard(reviewed)
    cell = card["by_stratum"]["scanned/ptr"]
    assert cell["n_reviewed"] == 3
    assert cell["doc_level"]["entry_recall"] == pytest.approx(1 / 3)
    assert cell["entry_level"]["sum_missing"] == 20
    assert card["overall"]["doc_level"]["entry_recall"] == pytest.approx(1 / 3)


def test_scorecard_excludes_stale_from_rates_but_counts_them():
    f = lambda i: _filing(f"e{i}")
    reviewed = [
        _reviewed(f(0), precise=True),
        _reviewed(f(1), precise=False, n_inc=3, stale=True),
    ]
    cell = core.scorecard(reviewed)["overall"]
    assert cell["n_reviewed"] == 2
    assert cell["n_stale"] == 1
    assert cell["doc_level"]["entry_precision"] == 1.0  # only the fresh one counts
    assert cell["entry_level"]["sum_incorrect"] == 0  # stale n_inc excluded


def test_scorecard_empty_rates_are_none():
    cell = core.scorecard([])["overall"]
    assert cell["doc_level"]["entry_precision"] is None


# ---------------------------------------------------------------------------
# Labels persistence
# ---------------------------------------------------------------------------
def test_labels_round_trip(tmp_path):
    v = _verdict(is_fully_recalled=False, n_missing_entries=5)
    labels.write_labels(tmp_path, 2022, {"doc-1": v}, started_at="2026-06-12T00:00:00")
    again = labels.read_labels(tmp_path, 2022)
    assert again["doc-1"]["n_missing_entries"] == 5


def test_labels_absent_year_is_empty(tmp_path):
    assert labels.read_labels(tmp_path, 1999) == {}


def test_labels_foreign_schema_ignored(tmp_path, capsys):
    path = labels.labels_path(tmp_path, 2022)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": LABELS_SCHEMA_VERSION + 99, "labels": {"d": {}}}))
    assert labels.read_labels(tmp_path, 2022) == {}
    assert "schema_version" in capsys.readouterr().err


def test_labels_write_is_atomic_no_tmp_left(tmp_path):
    labels.write_labels(tmp_path, 2022, {"d": _verdict()}, started_at="t")
    leftovers = list(labels.labels_path(tmp_path, 2022).parent.glob("*.tmp"))
    assert leftovers == []
