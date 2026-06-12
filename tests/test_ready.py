"""Tests for ``openhouse ready`` — skill install + marker + --check drift (SPEC §8).

All filesystem effects are confined to pytest's ``tmp_path``; nothing here writes
into the real ``~/.claude``. The hash/marker/status functions are pure over an
explicit ``dest`` + ``files`` dict, so they are exercised directly; the CLI
dispatch is checked via ``ready.run`` with ``Path.home`` patched.
"""

from __future__ import annotations

import json

import pytest

from openhouse import ready


# ---- content hash: stable, order-independent, content-sensitive ----


def test_content_hash_is_order_independent():
    a = {"SKILL.md": b"one", "reference.md": b"two"}
    b = {"reference.md": b"two", "SKILL.md": b"one"}
    assert ready._content_hash(a) == ready._content_hash(b)


def test_content_hash_changes_with_content():
    base = {"SKILL.md": b"one", "reference.md": b"two"}
    edited = {"SKILL.md": b"one!", "reference.md": b"two"}
    assert ready._content_hash(base) != ready._content_hash(edited)


def test_content_hash_excludes_marker_file():
    # The marker is never part of the hash; only the named skill files are.
    files = {"SKILL.md": b"one", "reference.md": b"two"}
    h = ready._content_hash(files)
    files_with_marker = dict(files)  # the hash function only reads SKILL_FILES order
    assert ready._content_hash(files_with_marker) == h


# ---- install: wipe-and-copy, idempotent, writes a faithful marker ----


def test_install_writes_files_and_marker(tmp_path):
    dest = tmp_path / "openhouse"
    files = {"SKILL.md": b"skill body", "reference.md": b"ref body"}
    marker = ready.install(dest, files, "0.5.0")

    assert (dest / "SKILL.md").read_bytes() == b"skill body"
    assert (dest / "reference.md").read_bytes() == b"ref body"

    on_disk = json.loads((dest / ready.MARKER_NAME).read_text())
    assert on_disk == marker
    assert marker["version"] == "0.5.0"
    assert marker["content_hash"] == ready._content_hash(files)
    assert marker["files"] == ["SKILL.md", "reference.md"]


def test_install_is_idempotent(tmp_path):
    dest = tmp_path / "openhouse"
    files = {"SKILL.md": b"a", "reference.md": b"b"}
    m1 = ready.install(dest, files, "0.5.0")
    m2 = ready.install(dest, files, "0.5.0")
    assert m1 == m2


def test_install_wipes_stale_files(tmp_path):
    dest = tmp_path / "openhouse"
    files = {"SKILL.md": b"a", "reference.md": b"b"}
    ready.install(dest, files, "0.5.0")
    # A leftover from an earlier (hypothetical) layout:
    (dest / "old-extra.md").write_text("stale")
    ready.install(dest, files, "0.5.0")
    assert not (dest / "old-extra.md").exists()


# ---- check_status: the four drift states ----


def test_status_absent_when_not_installed(tmp_path):
    dest = tmp_path / "openhouse"
    files = {"SKILL.md": b"a", "reference.md": b"b"}
    assert ready.check_status(dest, files, "0.5.0") == "absent"


def test_status_up_to_date_after_install(tmp_path):
    dest = tmp_path / "openhouse"
    files = {"SKILL.md": b"a", "reference.md": b"b"}
    ready.install(dest, files, "0.5.0")
    assert ready.check_status(dest, files, "0.5.0") == "up-to-date"


def test_status_hand_edited_when_file_changed(tmp_path):
    dest = tmp_path / "openhouse"
    files = {"SKILL.md": b"a", "reference.md": b"b"}
    ready.install(dest, files, "0.5.0")
    (dest / "SKILL.md").write_bytes(b"tampered")
    assert ready.check_status(dest, files, "0.5.0") == "hand-edited"


def test_status_stale_when_packaged_content_advances(tmp_path):
    dest = tmp_path / "openhouse"
    old = {"SKILL.md": b"a", "reference.md": b"b"}
    ready.install(dest, old, "0.5.0")
    # A newer release ships different prose; the install is untouched on disk.
    new = {"SKILL.md": b"a v2", "reference.md": b"b"}
    assert ready.check_status(dest, new, "0.6.0") == "stale"


def test_status_absent_when_marker_missing(tmp_path):
    dest = tmp_path / "openhouse"
    files = {"SKILL.md": b"a", "reference.md": b"b"}
    ready.install(dest, files, "0.5.0")
    (dest / ready.MARKER_NAME).unlink()
    assert ready.check_status(dest, files, "0.5.0") == "absent"


def test_status_absent_when_a_skill_file_missing(tmp_path):
    dest = tmp_path / "openhouse"
    files = {"SKILL.md": b"a", "reference.md": b"b"}
    ready.install(dest, files, "0.5.0")
    (dest / "reference.md").unlink()
    assert ready.check_status(dest, files, "0.5.0") == "absent"


# ---- packaged data + run() dispatch (confined to tmp via Path.home patch) ----


def test_packaged_files_present():
    files = ready._packaged_files()
    assert set(files) == {"SKILL.md", "reference.md"}
    assert files["SKILL.md"].strip()  # non-empty prose
    assert files["reference.md"].strip()


def test_run_install_then_check_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ready.Path, "home", classmethod(lambda cls: tmp_path))

    # Before install: --check reports absent and exits non-zero.
    assert ready.run(["--check"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "absent"

    # Install: exit 0, JSON to stdout records the install.
    assert ready.run([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "installed"
    assert out["files"] == ["SKILL.md", "reference.md"]

    # After install: --check up-to-date, exit 0.
    assert ready.run(["--check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "up-to-date"

    dest = tmp_path / ".claude" / "skills" / "openhouse"
    assert (dest / "SKILL.md").is_file()


def test_run_rejects_unexpected_args(capsys):
    assert ready.run(["--bogus"]) == 2
    assert "unexpected argument" in capsys.readouterr().err
