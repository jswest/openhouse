"""The ``inspect`` verdict schema and its count/boolean invariant.

A verdict is a clean 2×2 over the sound/complete duality (CLAUDE.md), applied
symmetrically to **entries** (PTR trades / annual-FD line items) and **metadata**
(the index-derived scalars: filer, district, date, filing_type):

- ``is_fully_precise`` / ``is_fully_recalled`` — entry soundness / completeness.
- ``is_metadata_accurate`` / ``is_metadata_fully_complete`` — the same pair for
  metadata.

Booleans are the required fast path (one glance per dimension). Counts are
*optional* magnitude, filled when an entry boolean is false. Entries get counts
because the list is unbounded — *how many* missed is real signal; metadata is a
fixed handful of scalars where a count is noise, so it has none.

The verdict is **snapshot-pinned**: ``snapshot`` is a hash of the parsed record as
it was at review time (see :func:`openhouse.inspect.core.snapshot_hash`). Because
this repo re-parses rather than migrates, a later re-parse that changes a filing
makes its label *stale* — caught by comparing snapshots, never silently blessing
changed output.

This schema is versioned by :data:`LABELS_SCHEMA_VERSION`, **independent of**
``schemas.SCHEMA_VERSION``: the parsed-data version gates a full re-parse, which a
verdict-schema change must not trigger. The snapshot hash is what survives a
re-parse regardless.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator

# Bumped when the verdict/labels schema changes. Pre-v1: no migration — a bump
# invalidates old labels (re-review), it never rewrites them. Distinct from the
# parsed-data ``schemas.SCHEMA_VERSION`` on purpose (see module docstring).
LABELS_SCHEMA_VERSION = 1


class Verdict(BaseModel):
    """One reviewer verdict over a single parsed filing (filing mode)."""

    # ENTRIES — soundness (no hallucinated/wrong entries) and completeness
    # (nothing in the PDF missed).
    is_fully_precise: bool
    is_fully_recalled: bool
    n_incorrect_entries: Optional[int] = Field(
        default=None, ge=0, description="magnitude of imprecision; None = not tallied"
    )
    n_missing_entries: Optional[int] = Field(
        default=None, ge=0, description="magnitude of incompleteness; None = untallied"
    )

    # METADATA — the same sound/complete pair over the index-derived scalars.
    is_metadata_accurate: bool
    is_metadata_fully_complete: bool

    # PROVENANCE
    snapshot: str = Field(..., description="hash of the parsed record at review time")
    note: Optional[str] = Field(
        default=None,
        description="ground truth for scanned cases + correction-agent handoff hint",
    )

    @model_validator(mode="after")
    def _enforce_count_boolean_invariant(self) -> "Verdict":
        """Enforce ``count > 0 ⟺ boolean false`` (``None`` = "wrong, didn't tally").

        A tallied count must agree with its boolean: a positive count means the
        boolean is false, and a zero count means it is true. ``None`` is always
        allowed — it records "this dimension is off, but the reviewer didn't count
        how much".
        """
        _check_pair(
            self.is_fully_precise, self.n_incorrect_entries, "precise", "incorrect"
        )
        _check_pair(
            self.is_fully_recalled, self.n_missing_entries, "recalled", "missing"
        )
        return self


def _check_pair(is_good: bool, count: Optional[int], dim: str, noun: str) -> None:
    if count is None:
        return  # not tallied — always allowed
    if count > 0 and is_good:
        raise ValueError(
            f"is_fully_{dim}=True but n_{noun}_entries={count}: a positive count "
            f"means the entries are not fully {dim}."
        )
    if count == 0 and not is_good:
        raise ValueError(
            f"is_fully_{dim}=False but n_{noun}_entries=0: mark it 0 only when "
            f"fully {dim} (use None to say 'wrong, didn't tally')."
        )
