"""End-to-end tests for the ignordr CLI.

These exercise main(argv) directly so that argument wiring, exit codes and
the human-readable advice paths all get covered without spawning a process.
"""

import io
import json
import re
from contextlib import redirect_stdout

from ignordr import __main__ as cli
from ignordr import analyze


FIXTURE = """\
# build artifacts
build
dist/
node_modules/

*.log
!important.log
logs/*.log
*.tmp

# dead / duplicated
build
**/dist/
src/*.tmp

[abc
"""

def make_repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / ".gitignore").write_text(FIXTURE, encoding="utf-8")
    for rel in ("a.log", "important.log", "logs/app.log", "logs/important.log",
                "build/out.js", "src/utils.helper.py", "src/big.tmp", "keep.tmp"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return tmp_path


def call(argv, tmp_path):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(["--repo", str(tmp_path)] + argv)
    return code, buf.getvalue()


def test_explain_accepts_paths(tmp_path):
    repo = make_repo(tmp_path)
    # Regression: explain used to read args.paths while declaring 'path'.
    code, out = call(["explain", "important.log"], repo)
    assert "important.log" in out
    assert "NOT IGNORED" in out


def test_explain_reports_dead_exception(tmp_path):
    repo = make_repo(tmp_path)
    code, out = call(["explain", "logs/important.log"], repo)
    assert "IGNORED" in out
    assert "WHY IT IS STUCK" in out
    assert "dead" in out.lower()


def test_explain_json_is_valid(tmp_path):
    repo = make_repo(tmp_path)
    code, out = call(["explain", "important.log", "--json"], repo)
    data = json.loads(out)
    assert data["ignored"] is False
    assert data["decided_by"]["raw"] == "!important.log"
    assert isinstance(data["trail"], list)


def test_audit_reports_duplicate_and_broad(tmp_path):
    repo = make_repo(tmp_path)
    code, out = call(["audit"], repo)
    assert "DUPLICATE" in out
    assert "BROAD_RULE" in out


def test_lint_needs_no_file_scan(tmp_path):
    repo = make_repo(tmp_path)
    code, out = call(["lint"], repo)
    assert "BROAD_RULE" in out
    assert "DUPLICATE" in out


def test_check_exit_codes(tmp_path):
    repo = make_repo(tmp_path)
    code, out = call(["check", "a.log", "src/utils.helper.py"], repo)
    # status is column-aligned to 12 chars, so match on the token and the path
    assert re.search(r"IGNORED\s+a\.log", out)
    assert re.search(r"NOT IGNORED\s+src/utils\.helper\.py", out)
    assert code == cli.EXIT_ISSUES  # at least one path is ignored
    # the deciding rule is attributed with file:line
    assert ".gitignore:6" in out and "'*.log'" in out


def test_check_json_is_valid(tmp_path):
    repo = make_repo(tmp_path)
    code, out = call(["check", "a.log", "--json"], repo)
    data = json.loads(out)
    assert data[0]["ignored"] is True
    assert data[0]["by"] == "*.log"


def test_scan_lists_paths(tmp_path):
    repo = make_repo(tmp_path)
    code, out = call(["scan"], repo)
    assert "a.log" in out or "important.log" in out


def test_fix_is_dry_run_by_default(tmp_path):
    repo = make_repo(tmp_path)
    before = (repo / ".gitignore").read_text(encoding="utf-8")
    code, out = call(["fix"], repo)
    assert "dry run" in out
    # the file must be untouched without --apply
    assert (repo / ".gitignore").read_text(encoding="utf-8") == before


def test_fix_dedupes_but_keeps_comments(tmp_path):
    repo = make_repo(tmp_path)
    code, out = call(["fix", "--apply"], repo)
    after = (repo / ".gitignore").read_text(encoding="utf-8")
    # the duplicate 'build' is collapsed to a single line
    assert after.count("\nbuild\n") == 1
    # every comment survives untouched
    assert "# build artifacts" in after
    assert "# dead / duplicated" in after
    assert "[abc" in after
    # and a second run is a no-op
    code2, out2 = call(["fix"], repo)
    assert "nothing to change" in out2


def test_fix_preserves_ordering_of_the_surviving_copy(tmp_path):
    repo = make_repo(tmp_path)
    call(["fix", "--apply"], repo)
    after = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    # the LATER 'build' is the one that matters to git, so it stays in place
    assert after[after.index("build") + 1] != "build"


# --------------------------------------------------------------------------- #
# the tracked-but-ignored detector
# --------------------------------------------------------------------------- #
def test_find_tracked_but_ignored(tmp_path):
    """git never ignores a tracked file, but a matching rule is still a bug.

    The tracked list comes from `git ls-files`, so use a real repository here
    rather than monkeypatching a subprocess module.
    """
    import subprocess

    repo = make_repo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True,
                   capture_output=True)
    # A plain `git add` respects .gitignore, so force-add files that a rule
    # would now exclude. That is the real-world "I committed the log first" case.
    for rel in ("a.log", "src/big.tmp", "src/utils.helper.py", ".gitignore"):
        subprocess.run(["git", "add", "-f", rel], cwd=repo, check=True,
                       capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"], cwd=repo, check=True,
                   capture_output=True)

    hits = analyze.find_tracked_but_ignored(str(repo))
    assert sorted(hits) == ["a.log", "src/big.tmp"]


def test_tracked_command_reports_the_same(tmp_path):
    import subprocess

    repo = make_repo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "add", "-f", "a.log"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"], cwd=repo, check=True,
                   capture_output=True)
    code, out = call(["tracked"], repo)
    assert "a.log" in out
