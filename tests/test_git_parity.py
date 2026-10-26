"""Differential tests: verify ignordr against real `git check-ignore`.

These are the ground truth. ignordr must agree with git's own implementation
of the ignore rules, or it is useless. Each case builds a real repository,
runs `git check-ignore`, and compares the result to ignordr's verdict.
"""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from ignordr.engine import check_path

GIT = pytest.mark.skipif(
    shutil.which("git") is None, reason="git executable not available"
)


def _make_repo(tmp_path: Path, gitignore: str, subdirs=None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(gitignore, encoding="utf-8")
    for name, content in (subdirs or {}).items():
        d = repo / os.path.dirname(name) if os.path.dirname(name) else repo
        d.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return repo


def _git_ignored(repo: Path, rel: str) -> bool:
    """True when real git considers *rel* ignored."""
    # The path must exist on disk for `git check-ignore` to evaluate it.
    full = repo / rel
    if not full.exists():
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("", encoding="utf-8")
    proc = subprocess.run(
        ["git", "check-ignore", "-q", rel],
        cwd=repo,
        capture_output=True,
    )
    return proc.returncode == 0


CASES = [
    ("*.log\n", "a.log", True),
    ("*.log\n", "src/a.log", True),
    ("/a.log\n", "a.log", True),
    ("/a.log\n", "src/a.log", False),
    ("a.log\n", "a.log", True),
    ("a.log\n", "src/a.log", True),
    ("src/a.log\n", "src/a.log", True),
    ("src/a.log\n", "src/sub/a.log", False),
    ("build/\n", "build/x.log", True),
    ("node_modules/\n", "a/node_modules/b.js", True),
    ("logs/*\n!logs/keep.log\n", "logs/keep.log", False),
    ("logs/*\n!logs/keep.log\n", "logs/other.log", True),
    ("*.log\n!important.log\n", "important.log", False),
    ("!important.log\n*.log\n", "important.log", True),
    ("*.tmp\n", "keep.tmp", True),
    ("a/b.txt\n", "a/b.txt", True),
    ("a/b.txt\n", "a/c.txt", False),
    ("a[bc].txt\n", "a1.txt", False),
    ("a[bc].txt\n", "ab.txt", True),
    ("a[!c].txt\n", "ab.txt", True),
    ("a[!c].txt\n", "ac.txt", False),
    ("a?c.txt\n", "abc.txt", True),
    ("a?c.txt\n", "ac.txt", False),
    ("\\*.log\n", "x.log", False),
    ("\\*.log\n", "*.log", True),
    ("*.log\n!keep/\n", "keep/x.log", True),
]


@GIT
@pytest.mark.parametrize("gitignore,rel,expected", CASES)
def test_matches_real_git(tmp_path, gitignore, rel, expected):
    repo = _make_repo(tmp_path, gitignore)
    assert _git_ignored(repo, rel) is expected, "sanity: git disagrees with the table"
    mine = check_path(str(repo), rel).ignored
    assert mine is expected, (
        f"ignordr disagrees with git on {rel!r} with {gitignore!r}: "
        f"ignordr={mine}, git={expected}"
    )


@GIT
def test_nested_gitignore_scope_matches_git(tmp_path):
    repo = _make_repo(
        tmp_path,
        "build/\n",
        subdirs={"src/.gitignore": "*.tmp\n"},
    )
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "a.tmp").write_text("", encoding="utf-8")
    (repo / "lib").mkdir(exist_ok=True)
    (repo / "lib" / "a.tmp").write_text("", encoding="utf-8")

    assert _git_ignored(repo, "src/a.tmp") is True
    assert _git_ignored(repo, "lib/a.tmp") is False
    assert check_path(str(repo), "src/a.tmp").ignored is True
    assert check_path(str(repo), "lib/a.tmp").ignored is False


@GIT
def test_deeper_gitignore_overrides(tmp_path):
    repo = _make_repo(
        tmp_path,
        "*.tmp\n",
        subdirs={"src/.gitignore": "!*.tmp\n"},
    )
    (repo / "src").mkdir(exist_ok=True)
    (repo / "top.tmp").write_text("", encoding="utf-8")
    (repo / "src" / "inner.tmp").write_text("", encoding="utf-8")

    assert _git_ignored(repo, "top.tmp") is True
    assert _git_ignored(repo, "src/inner.tmp") is False
    assert check_path(str(repo), "top.tmp").ignored is True
    assert check_path(str(repo), "src/inner.tmp").ignored is False


@GIT
def test_ignored_parent_negation_matches_git(tmp_path):
    repo = _make_repo(tmp_path, "logs/\n!logs/keep.log\n")
    (repo / "logs").mkdir(exist_ok=True)
    (repo / "logs" / "keep.log").write_text("", encoding="utf-8")
    # git cannot descend into logs/, so the exception never fires.
    assert _git_ignored(repo, "logs/keep.log") is True
    assert check_path(str(repo), "logs/keep.log").ignored is True
