#!/usr/bin/env python
"""Cut an openhouse release.

Versioning — ``v0.<SCHEMA_VERSION>.<patch>`` (GH-0037):

* **MINOR == SCHEMA_VERSION** — the integer parsed-schema generation, read from
  ``openhouse/schemas.py``. It moves iff the parsed-data schema changes, so the
  version self-documents data compatibility: ``v0.3.2`` means "schema 3, patch
  2", and the same integer is stamped in every ``parse-manifest.json``. A minor
  bump forces a **re-parse** — openhouse has no in-place migration; ``parse`` is
  cheap and offline, so the remedy for any schema change is always to re-run it.
* **PATCH** increments for every release at the same schema; resets to 0 on a
  schema bump.
* **MAJOR** is a human call (the v1 cutover, #13) — never automated here; carried
  forward from the last tag unchanged.

The constant *is* the minor, so computing the next version needs no diffing —
just the schema integer and the last tag.

Usage (from the repo root)::

    uv run python .claude/skills/release/release.py              # dry run
    uv run python .claude/skills/release/release.py --tag        # tag locally
    uv run python .claude/skills/release/release.py --tag --push # tag, push, GH release

Run from ``main`` after the change has merged. The ``/release`` skill enforces
the on-main / clean / synced preconditions around this script.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# This file lives at <repo>/.claude/skills/release/release.py (GH-0020 pattern).
REPO_ROOT = Path(__file__).resolve().parents[3]

# The committed fingerprint of the parsed-schema models (GH-0043). Lives beside
# ``schemas.py`` and is refreshed in the same diff as any schema change, so the
# working-tree copy always tracks the live models (a test enforces it). Its job
# is to make the *next* tag carry an accurate fingerprint; the drift guard reads
# the *last* tag's copy from the tagged tree (see ``tag_fingerprint``).
FINGERPRINT_PATH = REPO_ROOT / "openhouse" / "schemas.fingerprint"


# ---------------------------------------------------------------------------
# Schema-drift guard (GH-0043)
#
# A re-parse, not a migrate (CLAUDE.md): the only signal that ``parsed/`` shape
# changed is ``SCHEMA_VERSION`` rising. This guard makes that self-enforcing —
# if the pydantic models in ``openhouse/schemas.py`` change shape but the int
# stays put, the release refuses to tag. The fingerprint is a *pure* function of
# the models' normalized JSON schemas. Drift is (live != the fingerprint the
# LAST TAG shipped) at a static SCHEMA_VERSION — the guard reads the tag's copy
# via ``tag_fingerprint``, never the working-tree file (which a test pins to
# live, so comparing against it would be tautologically equal and never fire).
# ---------------------------------------------------------------------------

# Keys in ``model_json_schema()`` that are documentation, not structure: prose
# docstrings (``description``) and name-derived labels (``title``). Dropping them
# means a reworded docstring or a renamed-for-readability nothing-else change
# doesn't false-positive as drift; a field add/remove/retype still does.
_NOISE_KEYS = frozenset({"description", "title"})


def _strip_noise(node: Any) -> Any:
    """Recursively drop documentation keys and sort dict keys for stable output."""
    if isinstance(node, dict):
        return {
            k: _strip_noise(v)
            for k, v in sorted(node.items())
            if k not in _NOISE_KEYS
        }
    if isinstance(node, list):
        return [_strip_noise(v) for v in node]
    return node


def fingerprint(models: list[type]) -> str:
    """A stable SHA-256 over the normalized JSON schemas of ``models``.

    Pure function of the models' structure: each model's ``model_json_schema()``
    is stripped of documentation noise (docstrings, name-derived titles) and
    key-sorted, then the lot is dumped canonically and hashed. Incidental
    ordering or prose edits don't move it; a structural change (a field added,
    removed, retyped, or made (non-)optional) does.
    """
    normalized = {
        m.__name__: _strip_noise(m.model_json_schema())
        for m in sorted(models, key=lambda m: m.__name__)
    }
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check_drift(
    live_fingerprint: str,
    recorded_fingerprint: str | None,
    schema_version: int,
    last_tag: str | None,
) -> str | None:
    """Return an error message when the schema drifted without a version bump.

    Drift is exactly: the model fingerprint changed since the last tag, yet
    ``SCHEMA_VERSION`` still matches that tag's minor. That is the one state the
    release must refuse — a reshaped ``parsed/`` output with a static generation
    int, which would ship un-re-parsed data under an unchanged compatibility
    signal. Returns ``None`` (no drift) when there's no prior tag/fingerprint,
    when the fingerprint is unchanged, or when the schema int already moved
    (the bump *is* the acknowledgement).
    """
    if last_tag is None or recorded_fingerprint is None:
        return None
    if schema_moved(schema_version, last_tag):
        return None
    if live_fingerprint == recorded_fingerprint:
        return None
    return (
        "error: parsed-schema models changed but SCHEMA_VERSION is still "
        f"{schema_version} (last tag {last_tag}). A reshaped `parsed/` output "
        "must bump SCHEMA_VERSION — re-parse, not migrate (CLAUDE.md). Bump the "
        "int in openhouse/schemas.py and refresh openhouse/schemas.fingerprint "
        "(release.py --write-fingerprint), then retag."
    )


# ---------------------------------------------------------------------------
# Version arithmetic + notes (unit-tested)
# ---------------------------------------------------------------------------

def parse_version_tag(tag: str) -> tuple[int, int, int]:
    """``"v0.3.2"`` -> ``(0, 3, 2)``. Accepts an optional leading ``v``."""
    core = tag.lstrip("v").split("+", 1)[0]
    parts = core.split(".")
    if len(parts) != 3:
        raise ValueError(f"not a vMAJOR.MINOR.PATCH tag: {tag!r}")
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


def compute_next_version(schema_version: int, last_tag: str | None) -> str:
    """Derive the next ``MAJOR.MINOR.PATCH`` from the schema int + last tag.

    No prior tag -> baseline ``0.<schema>.0``. Schema moved since the last tag
    -> reset patch to 0 at the new minor. Otherwise bump patch.
    """
    if last_tag is None:
        return f"0.{schema_version}.0"
    major, minor, patch = parse_version_tag(last_tag)
    if schema_version != minor:
        return f"{major}.{schema_version}.0"
    return f"{major}.{minor}.{patch + 1}"


def schema_moved(schema_version: int, last_tag: str | None) -> bool:
    """True when this release crosses a schema boundary (re-parse required)."""
    if last_tag is None:
        return False
    return parse_version_tag(last_tag)[1] != schema_version


def build_release_notes(
    log_lines: list[str],
    *,
    schema_from: int | None,
    schema_to: int | None,
) -> str:
    """Assemble GitHub Release notes from the git log, with a re-parse banner
    prepended on a schema bump.

    openhouse has no in-place migration: a parsed-data schema change means
    existing ``parsed/`` data must be re-parsed from ``raw/`` (cheap, offline).
    """
    parts: list[str] = []
    if schema_from is not None and schema_to is not None and schema_from != schema_to:
        parts.append(
            f"⚠️ **This release changes the parsed-data schema "
            f"({schema_from} → {schema_to}). Existing `parsed/` data must be "
            f"re-parsed from `raw/`: re-run `openhouse parse`.**\n"
        )
    parts.append("## Changes\n")
    if log_lines:
        parts.extend(f"- {line}" for line in log_lines)
    else:
        parts.append("- Baseline release.")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# git / gh side-effects
# ---------------------------------------------------------------------------

def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def read_schema_version() -> int:
    """The current integer schema generation (the release minor)."""
    from openhouse.schemas import SCHEMA_VERSION

    return SCHEMA_VERSION


def parsed_schema_models() -> list[type]:
    """Every pydantic model defined in ``openhouse.schemas`` (the parsed shape).

    Auto-discovered so there's no list to maintain — a model added to the module
    is fingerprinted automatically. Filtered to classes *defined* in the module
    (not imported ``BaseModel`` itself or anything pulled in).
    """
    import pydantic
    from openhouse import schemas

    return [
        obj
        for _, obj in inspect.getmembers(schemas, inspect.isclass)
        if issubclass(obj, pydantic.BaseModel)
        and obj is not pydantic.BaseModel
        and obj.__module__ == "openhouse.schemas"
    ]


def live_fingerprint() -> str:
    """The fingerprint of the schema models as they are right now."""
    return fingerprint(parsed_schema_models())


def committed_fingerprint() -> str | None:
    """The fingerprint in the working tree's ``schemas.fingerprint``, or None.

    Refreshed (``release.py --write-fingerprint``) in the same commit as any
    schema change, so it always tracks the live models — a test enforces this.
    Its job is to make the *next* tag carry an accurate fingerprint; the drift
    guard itself reads the *last* tag's copy via :func:`tag_fingerprint`.
    """
    if not FINGERPRINT_PATH.exists():
        return None
    return FINGERPRINT_PATH.read_text(encoding="utf-8").strip() or None


def tag_fingerprint(last_tag: str | None) -> str | None:
    """The schema fingerprint recorded *at the last release tag*, or None.

    Read straight from the tagged tree
    (``git show <tag>:openhouse/schemas.fingerprint``) — this is the value the
    guard compares ``live`` against. Returns None when there is no prior tag, or
    the tag predates the fingerprint file (so there is nothing to compare and the
    baseline case applies). Reading the *tag* — not the working tree — is what
    makes the guard sound: the working-tree copy always tracks live, so comparing
    live against it is tautologically equal and could never catch a schema edit
    that skipped a ``SCHEMA_VERSION`` bump.
    """
    if last_tag is None:
        return None
    # Derive the in-repo path from FINGERPRINT_PATH rather than re-hardcoding the
    # literal: if the file ever moves, a stale literal here would make every
    # ``git show`` miss → silently return None forever → the guard goes inert
    # again (the exact failure this fix exists to prevent).
    rel = FINGERPRINT_PATH.relative_to(REPO_ROOT).as_posix()
    result = subprocess.run(
        ["git", "show", f"{last_tag}:{rel}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def write_fingerprint() -> str:
    """Write the live fingerprint to ``schemas.fingerprint`` and return it."""
    fp = live_fingerprint()
    FINGERPRINT_PATH.write_text(fp + "\n", encoding="utf-8")
    return fp


def last_release_tag() -> str | None:
    """The most recent ``v0.*`` tag reachable from HEAD, or None if untagged."""
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def commits_since(ref: str | None) -> list[str]:
    spec = f"{ref}..HEAD" if ref else "HEAD"
    out = _git("log", spec, "--no-merges", "--pretty=format:%s")
    return [line for line in out.splitlines() if line.strip()]


def working_tree_dirty() -> bool:
    return bool(_git("status", "--porcelain"))


def current_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cut an openhouse release.")
    parser.add_argument(
        "--tag", action="store_true",
        help="Create the git tag locally (default is a dry run).",
    )
    parser.add_argument(
        "--push", action="store_true",
        help="Push the tag and publish a GitHub Release (implies --tag).",
    )
    parser.add_argument(
        "--allow-dirty", action="store_true",
        help="Permit a tag on a dirty working tree (refused by default).",
    )
    parser.add_argument(
        "--write-fingerprint", action="store_true",
        help="Record the current schema fingerprint to openhouse/schemas.fingerprint "
             "and exit (do this in the same commit as a SCHEMA_VERSION bump).",
    )
    args = parser.parse_args(argv)
    do_tag = args.tag or args.push

    if args.write_fingerprint:
        fp = write_fingerprint()
        print(f"Wrote schema fingerprint {fp[:12]}… to {FINGERPRINT_PATH}.",
              file=sys.stderr)
        return 0

    schema_version = read_schema_version()
    last_tag = last_release_tag()

    drift = check_drift(
        live_fingerprint(), tag_fingerprint(last_tag), schema_version, last_tag,
    )
    if drift is not None:
        print(drift, file=sys.stderr)
        return 1

    next_version = compute_next_version(schema_version, last_tag)
    new_tag = f"v{next_version}"
    moved = schema_moved(schema_version, last_tag)
    schema_from = parse_version_tag(last_tag)[1] if moved else None

    # The baseline release gets a clean "Baseline release." note rather than the
    # project's entire history; subsequent releases list commits since the tag.
    commits = commits_since(last_tag) if last_tag is not None else []
    if last_tag is not None and not commits:
        print(f"error: no commits since {last_tag}; nothing to release.", file=sys.stderr)
        return 1

    notes = build_release_notes(
        commits, schema_from=schema_from, schema_to=schema_version if moved else None,
    )

    disposition = "  (schema bumped → re-parse)" if moved else ""
    print(f"Last tag:     {last_tag or '(none)'}")
    print(f"Schema:       {schema_version}{disposition}")
    print(f"Next release: {new_tag}")
    print()
    print(notes)

    if not do_tag:
        print("Dry run — pass --tag to create the tag, --push to publish.", file=sys.stderr)
        return 0

    if working_tree_dirty() and not args.allow_dirty:
        print("error: working tree is dirty; commit or stash first (or --allow-dirty).",
              file=sys.stderr)
        return 1
    branch = current_branch()
    if branch != "main":
        print(f"warning: releasing from '{branch}', not 'main'.", file=sys.stderr)

    _git("tag", "-a", new_tag, "-m", f"Release {new_tag}")
    print(f"Created tag {new_tag}.", file=sys.stderr)

    if args.push:
        _git("push", "origin", new_tag)
        subprocess.run(
            ["gh", "release", "create", new_tag, "--title", new_tag, "--notes", notes],
            cwd=REPO_ROOT, check=True,
        )
        print(f"Pushed {new_tag} and published the GitHub Release.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
