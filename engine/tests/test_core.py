"""Regression tests. Run with: python -m pytest tests -q

These guard the two things that must never silently break: the parser must
lose nothing, and a partial selection must land in the right place.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from splitter.gitrepo import Repo
from splitter.parser import all_hunks, parse_diff
from splitter.patch import build_patch


def git(repo: Path, *args: str, stdin: str | None = None):
    return subprocess.run(["git", *args], cwd=repo, input=stdin,
                          capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", ".")
    git(tmp_path, "config", "user.email", "t@t.co")
    git(tmp_path, "config", "user.name", "T")
    (tmp_path / "f.py").write_text("\n".join(f"x{i} = {i}" for i in range(1, 61)) + "\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def three_edits(repo: Path) -> None:
    lines = (repo / "f.py").read_text().split("\n")
    lines.insert(4, "A = 1")
    lines.insert(31, "B = 2")
    lines.insert(56, "C = 3")
    (repo / "f.py").write_text("\n".join(lines))


def test_parser_finds_separate_hunks(repo: Path):
    three_edits(repo)
    files = parse_diff(Repo(repo).working_diff())
    assert len(files) == 1
    assert len(files[0].hunks) == 3


def test_hunk_ids_are_stable_across_reparse(repo: Path):
    three_edits(repo)
    r = Repo(repo)
    first = [h.hunk_id for h in all_hunks(parse_diff(r.working_diff()))]
    second = [h.hunk_id for h in all_hunks(parse_diff(r.working_diff()))]
    assert first == second


def test_full_roundtrip_applies(repo: Path):
    three_edits(repo)
    r = Repo(repo)
    files = parse_diff(r.working_diff())
    patch = build_patch(files, {h.hunk_id for h in all_hunks(files)})
    r.apply_cached(patch, check_only=True)  # raises on failure


def test_skipping_middle_hunk_lands_correctly(repo: Path):
    """The regression that matters: offsets must only count applied hunks."""
    three_edits(repo)
    r = Repo(repo)
    files = parse_diff(r.working_diff())
    hunks = all_hunks(files)
    selection = {hunks[0].hunk_id, hunks[2].hunk_id}

    r.apply_cached(build_patch(files, selection))
    r.commit("partial")

    committed = git(repo, "show", "HEAD:f.py").stdout
    assert "A = 1" in committed
    assert "C = 3" in committed
    assert "B = 2" not in committed          # the skipped hunk stayed behind
    assert "B = 2" in (repo / "f.py").read_text()

    # And the leftover must still be a valid, applyable diff.
    leftover = parse_diff(r.working_diff())
    remaining = all_hunks(leftover)
    assert len(remaining) == 1
    r.apply_cached(build_patch(leftover, {remaining[0].hunk_id}), check_only=True)


def test_untracked_file_becomes_visible(repo: Path):
    (repo / "new.py").write_text("print('hi')\n")
    r = Repo(repo)
    r.intent_to_add(r.untracked())
    files = parse_diff(r.working_diff())
    assert any(fd.path == "new.py" for fd in files)
