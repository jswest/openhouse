"""``openhouse ready`` — stamp the packaged agent skill into ``~/.claude/skills``.

The skill prose (``openhouse/skill/SKILL.md`` + ``reference.md``) ships as
**package data** — the repo is the only source of truth. ``ready`` installs a
**blessed copy** into ``~/.claude/skills/openhouse`` rather than pointing at a
checkout, so parallel agent sessions run the last released version, not whatever
mid-refactor state a working tree is in (SPEC §8).

Install is **wipe-and-copy**: the destination is removed and recreated from the
packaged files, so it is idempotent and never accumulates stale files. A hidden
marker (``.openhouse-skill.json``) records the **producing package version** plus
a **content hash** over the installed skill files. ``--check`` compares the
marker against what is packaged now and reports one of:

- ``up-to-date`` — installed hash matches the packaged hash.
- ``stale`` — the packaged files differ from the installed ones (the marker
  still matches what's on disk, but the packaged content hash has moved):
  re-run ``openhouse ready``.
- ``hand-edited`` — the installed files were modified after install (the marker's
  recorded hash no longer matches the files on disk): your edits would be lost.

No ``skill_runner`` / dispatch layer — three CLI verbs don't warrant it (SPEC §8).
Like every command: JSON to stdout, prose to stderr, non-zero exit on error.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from importlib import resources
from pathlib import Path

from . import __version__

MARKER_NAME = ".openhouse-skill.json"
# The packaged skill files, relative to the ``openhouse.skill`` package. Sorted,
# so the content hash is order-stable.
SKILL_FILES = ("SKILL.md", "reference.md")


def _skill_dest() -> Path:
    """``~/.claude/skills/openhouse`` — the blessed install location (SPEC §8)."""
    return Path.home() / ".claude" / "skills" / "openhouse"


def _packaged_files() -> dict[str, bytes]:
    """The packaged skill files as ``{name: bytes}``, resolved via importlib."""
    pkg = resources.files("openhouse.skill")
    return {name: (pkg / name).read_bytes() for name in SKILL_FILES}


def _content_hash(files: dict[str, bytes]) -> str:
    """A stable sha256 over the skill files.

    Hashes each ``name\\0bytes`` pair in sorted-name order so the digest depends
    only on content, never on dict/iteration order. The marker file itself is
    never part of the hash.
    """
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(files[name])
        h.update(b"\0")
    return h.hexdigest()


def _installed_files(dest: Path) -> dict[str, bytes] | None:
    """The installed skill files (excluding the marker), or ``None`` if absent.

    Returns ``None`` if the destination or any expected skill file is missing —
    i.e. there is no usable install to compare against.
    """
    if not dest.is_dir():
        return None
    out: dict[str, bytes] = {}
    for name in SKILL_FILES:
        path = dest / name
        if not path.is_file():
            return None
        out[name] = path.read_bytes()
    return out


def _read_marker(dest: Path) -> dict | None:
    """The parsed hidden marker, or ``None`` if missing/unreadable."""
    path = dest / MARKER_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def install(dest: Path, files: dict[str, bytes], version: str) -> dict:
    """Wipe ``dest`` and recreate it from ``files`` + a fresh marker.

    Returns the marker dict that was written. Idempotent: a second call with the
    same inputs yields a byte-identical install and marker.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name, data in files.items():
        (dest / name).write_bytes(data)
    marker = {
        "version": version,
        "content_hash": _content_hash(files),
        "files": sorted(files),
    }
    (dest / MARKER_NAME).write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return marker


def check_status(dest: Path, packaged: dict[str, bytes]) -> str:
    """Classify the installed skill against what is packaged now.

    Returns one of ``"absent"``, ``"hand-edited"``, ``"stale"``, ``"up-to-date"``.
    Pure (no I/O beyond reading ``dest``): caller decides messaging/exit code.
    """
    installed = _installed_files(dest)
    marker = _read_marker(dest)
    if installed is None or marker is None:
        return "absent"

    packaged_hash = _content_hash(packaged)
    installed_hash = _content_hash(installed)
    marker_hash = marker.get("content_hash")

    # Files changed since install (marker no longer describes them) → hand-edited.
    if marker_hash != installed_hash:
        return "hand-edited"
    # Marker matches the files, but a different version is packaged now → stale.
    if installed_hash != packaged_hash:
        return "stale"
    return "up-to-date"


def run(argv: list[str]) -> int:
    """Entry point for ``openhouse ready`` (dispatched from cli.py).

    With ``--check``, report status and exit non-zero unless up-to-date. Without
    it, perform the wipe-and-copy install. JSON to stdout, prose to stderr.
    """
    check = "--check" in argv
    extra = [a for a in argv if a != "--check"]
    if extra:
        print(
            f"error: unexpected argument(s) {extra}: ready takes only --check.",
            file=sys.stderr,
        )
        return 2

    dest = _skill_dest()
    packaged = _packaged_files()
    version = __version__

    if check:
        status = check_status(dest, packaged)
        result = {
            "command": "ready",
            "status": status,
            "skill_dir": str(dest),
            "version": version,
        }
        messages = {
            "up-to-date": "skill is up to date.",
            "stale": "a newer skill is packaged — run `openhouse ready` to update.",
            "hand-edited": (
                "installed skill was hand-edited — `openhouse ready` would "
                "overwrite it."
            ),
            "absent": "skill is not installed — run `openhouse ready`.",
        }
        print(messages[status], file=sys.stderr)
        print(json.dumps(result, indent=2))
        return 0 if status == "up-to-date" else 1

    marker = install(dest, packaged, version)
    result = {
        "command": "ready",
        "status": "installed",
        "skill_dir": str(dest),
        "version": version,
        "content_hash": marker["content_hash"],
        "files": marker["files"],
    }
    print(f"installed openhouse skill into {dest}", file=sys.stderr)
    print(json.dumps(result, indent=2))
    return 0
