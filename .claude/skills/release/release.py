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
import subprocess
import sys
from pathlib import Path

# This file lives at <repo>/.claude/skills/release/release.py (GH-0020 pattern).
REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
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
    args = parser.parse_args(argv)
    do_tag = args.tag or args.push

    schema_version = read_schema_version()
    last_tag = last_release_tag()

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
