"""The pure, offline heart of ``inspect``: sampling, snapshot hashing, scorecard.

Nothing here touches the disk, the network, or the wall clock — every function is
deterministic in its inputs, so the whole module is unit-testable on fixtures
(CLAUDE.md: ``parse``/``read``-style offline determinism). The web surface
(:mod:`openhouse.inspect.server`) is the only piece that does I/O.

Three concerns:

- **Sampling** — pick a fraction of the reviewable filings. Seeded (reproducible,
  no wall-clock), monotonic (``0.2`` ⊇ ``0.1``), and **stratified** by
  ``pdf_class`` × is-PTR so the hard cells (scanned PTRs) are always represented.
- **Snapshot hashing** — pin a verdict to the parsed record it reviewed, so a
  later re-parse that changes the filing flags the label stale.
- **Scorecard** — roll reviewed verdicts up into per-stratum doc-level
  precision/recall rates plus an entry-level magnitude rollup.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Optional

# Only ``parse_status: ok`` filings are reviewable; error/unparsed filings are
# already accounted for in the manifest residual (the "M not reviewable" line).
REVIEWABLE_STATUS = "ok"


# ---------------------------------------------------------------------------
# Stratification + sampling
# ---------------------------------------------------------------------------
def stratum_key(filing: dict) -> tuple[str, str]:
    """The ``(pdf_class, kind)`` cell a filing belongs to.

    ``kind`` is ``"ptr"`` for FilingType ``P`` else ``"other"`` — the scanned-PTR
    cell is the one most prone to silent recall failure, so it gets its own
    stratum rather than being drowned out by efiled candidate reports.
    """
    pdf_class = filing.get("pdf_class") or "unknown"
    code = (filing.get("filing_type") or {}).get("code")
    return (pdf_class, "ptr" if code == "P" else "other")


def _unit_interval(seed: int, doc_id: str) -> float:
    """Map ``(seed, doc_id)`` to a stable point in ``[0, 1)`` via SHA-256.

    Deterministic and independent of any clock or set ordering, so the same
    ``(seed, doc_id)`` always lands in the same place — the basis for both
    reproducibility and monotonicity.
    """
    digest = hashlib.sha256(f"{seed}:{doc_id}".encode()).hexdigest()
    return int(digest[:16], 16) / float(1 << 64)


def reviewable(filings: list[dict]) -> list[dict]:
    """The ``parse_status: ok`` subset — the only filings ``inspect`` samples."""
    return [f for f in filings if f.get("parse_status") == REVIEWABLE_STATUS]


def select(filings: list[dict], sample: float, seed: int) -> list[dict]:
    """Choose a stratified ``sample`` fraction of ``filings``, deterministically.

    Within each stratum the filings are ranked by their ``_unit_interval`` and the
    smallest ``ceil(sample * n)`` are taken. This makes selection:

    - **reproducible** — the rank key is a pure hash of ``(seed, doc_id)``;
    - **monotonic** — top-``k`` sets are nested, so a larger ``sample`` is a strict
      superset of a smaller one (re-widen without re-reviewing);
    - **stratified** — every non-empty stratum yields at least one filing for any
      ``sample > 0`` (``ceil`` rounds up), so scanned PTRs are never dropped by
      chance.

    Returns the selected filings ordered by ``doc_id`` for a stable review queue.
    """
    if sample <= 0:
        return []
    if sample >= 1:
        return sorted(filings, key=lambda f: f["doc_id"])

    by_stratum: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for f in filings:
        by_stratum[stratum_key(f)].append(f)

    chosen: list[dict] = []
    for docs in by_stratum.values():
        ranked = sorted(
            docs, key=lambda f: (_unit_interval(seed, f["doc_id"]), f["doc_id"])
        )
        k = min(len(ranked), math.ceil(sample * len(ranked)))
        chosen.extend(ranked[:k])
    return sorted(chosen, key=lambda f: f["doc_id"])


# ---------------------------------------------------------------------------
# Snapshot hashing
# ---------------------------------------------------------------------------
def snapshot_hash(filing: dict, body: Optional[dict]) -> str:
    """A content hash of the parsed record (metadata + body) at review time.

    Canonical JSON (sorted keys, no whitespace, dates already ISO strings) makes
    the hash stable across runs and Python versions. ``body`` is the PTR/FD body
    dict or ``None`` (metadata-only filings). The returned ``"sha256:…"`` string
    is stored on the verdict; a mismatch on reload means the parse changed and the
    label is stale.
    """
    payload = {"filing": filing, "body": body}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def is_stale(stored_snapshot: str, filing: dict, body: Optional[dict]) -> bool:
    """True when the parsed record no longer matches the verdict's snapshot."""
    return stored_snapshot != snapshot_hash(filing, body)


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------
def scorecard(reviewed: list[dict]) -> dict:
    """Roll reviewed filings up into a per-stratum + overall accuracy scorecard.

    Each item in ``reviewed`` is ``{"filing": <dict>, "verdict": <dict>, "stale":
    <bool>}``. Two complementary views per stratum (and overall):

    - **doc-level** precision/recall — the fraction of reviewed filings whose
      entries (resp. metadata) were marked fully precise / fully recalled. The
      "how often is a filing clean" rate.
    - **entry-level** rollup — ``Σ n_incorrect`` / ``Σ n_missing`` over the
      filings that tallied them. The number that says *where* the parser leaks
      (e.g. scanned PTRs: many missing trades).

    Stale verdicts are counted but excluded from the rates — a label pinned to a
    superseded parse should not score the current one.
    """
    by_stratum: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in reviewed:
        by_stratum[stratum_key(item["filing"])].append(item)

    strata = {}
    for key, items in sorted(by_stratum.items()):
        strata[f"{key[0]}/{key[1]}"] = _stratum_stats(items)

    return {
        "overall": _stratum_stats(reviewed),
        "by_stratum": strata,
    }


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """A fraction in ``[0, 1]``, or ``None`` when there is nothing to divide."""
    return None if denominator == 0 else numerator / denominator


def _stratum_stats(items: list[dict]) -> dict:
    n_reviewed = len(items)
    fresh = [it for it in items if not it.get("stale")]
    n_fresh = len(fresh)

    def fresh_count(field: str) -> int:
        return sum(1 for it in fresh if it["verdict"].get(field))

    def entry_sum(field: str) -> int:
        return sum(
            it["verdict"][field]
            for it in fresh
            if it["verdict"].get(field) is not None
        )

    return {
        "n_reviewed": n_reviewed,
        "n_stale": n_reviewed - n_fresh,
        "doc_level": {
            "entry_precision": _rate(fresh_count("is_fully_precise"), n_fresh),
            "entry_recall": _rate(fresh_count("is_fully_recalled"), n_fresh),
            "metadata_accuracy": _rate(fresh_count("is_metadata_accurate"), n_fresh),
            "metadata_completeness": _rate(
                fresh_count("is_metadata_fully_complete"), n_fresh
            ),
        },
        "entry_level": {
            "sum_incorrect": entry_sum("n_incorrect_entries"),
            "sum_missing": entry_sum("n_missing_entries"),
        },
    }
