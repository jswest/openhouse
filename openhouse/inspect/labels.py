"""Resumable persistence for ``inspect`` verdicts (``data/inspect/<year>/labels.json``).

Labels live under ``data/`` (already gitignored — bulky, regenerable, and they
carry the same legal-use restriction as the disclosures themselves). The file is
keyed by ``doc_id`` so a review session **resumes** across restarts: reload, skip
what's already labelled, continue.

Writes are atomic (temp file + ``os.replace``) so a crash mid-write can't leave a
half-written labels file. The file records :data:`verdict.LABELS_SCHEMA_VERSION`;
a file written by a different version is *not migrated* — pre-v1 the stance is
re-review, not migrate (CLAUDE.md) — so it is ignored with a warning and the next
save rewrites it under the current version.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .verdict import LABELS_SCHEMA_VERSION


def labels_path(data_dir: Path, year: int) -> Path:
    """``<data_dir>/inspect/<year>/labels.json`` — the per-year verdict store."""
    return data_dir / "inspect" / str(year) / "labels.json"


def read_labels(data_dir: Path, year: int) -> dict[str, dict]:
    """Load the ``{doc_id: verdict}`` map, or ``{}`` if absent/foreign-schema.

    A missing file is a clean empty start. A file written by a different
    ``LABELS_SCHEMA_VERSION`` is ignored (with a stderr warning) rather than
    migrated — the next saved verdict rewrites the file under the current schema.
    """
    path = labels_path(data_dir, year)
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    version = doc.get("schema_version")
    if version != LABELS_SCHEMA_VERSION:
        print(
            f"warning: {path} was written by labels schema_version {version!r}, "
            f"but this build expects {LABELS_SCHEMA_VERSION}; ignoring those "
            f"labels (re-review, not migrate). The next saved verdict rewrites it.",
            file=sys.stderr,
        )
        return {}
    return dict(doc.get("labels", {}))


def write_labels(
    data_dir: Path, year: int, labels: dict[str, dict], *, started_at: str
) -> None:
    """Atomically write the full ``{doc_id: verdict}`` map for one year.

    ``started_at`` is the single command-entry timestamp (threaded in, never read
    from the clock here) so the file's provenance stays deterministic-by-input
    like the parse manifests.
    """
    path = labels_path(data_dir, year)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": LABELS_SCHEMA_VERSION,
        "year": year,
        "updated_at": started_at,
        "labels": labels,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True))
    os.replace(tmp, path)
