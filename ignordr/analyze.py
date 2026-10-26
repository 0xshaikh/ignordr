"""ignordr.analyze - audit and lint a repository's ignore configuration.

This is where ignordr earns its keep. ``git check-ignore`` tells you *whether*
one path is ignored. These helpers tell you *what is wrong with the whole
configuration*: dead rules, duplicate patterns, unreachable negations, and
tracked files that were accidentally ignored.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .engine import Rule, Verdict, check_path, collect_patterns

__all__ = [
    "Finding",
    "Severity",
    "audit_repo",
    "find_tracked_but_ignored",
    "scan_patterns",
    "ignored_files",
]

SEV_ERROR = "error"
SEV_WARN = "warn"
SEV_INFO = "info"
Severity = str


@dataclass(frozen=True)
class Finding:
    """One diagnostic produced by an audit pass."""

    code: str
    severity: Severity
    message: str
    path: str = ""
    rule: Optional[Rule] = None
    fix: str = ""

    def format(self, color: bool = False) -> str:
        tag = f"[{self.code}]"
        line = f"{tag:<14} {self.message}"
        if self.path:
            line += f"  ({self.path})"
        if self.rule is not None:
            line += f"  @{self.rule.pattern.src}:{self.rule.pattern.lineno}"
        if color:
            cmap = {SEV_ERROR: "\033[31m", SEV_WARN: "\033[33m", SEV_INFO: "\033[36m"}
            line = cmap.get(self.severity, "") + line + "\033[0m"
        if self.fix:
            line += f"\n{'':<14}-> {self.fix}"
        return line


def _walk_files(repo_root: str) -> Iterable[str]:
    """Yield repo-relative paths (posix) of all files, skipping .git."""
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        rel_dir = os.path.relpath(dirpath, repo_root)
        for name in filenames:
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            yield rel.replace(os.sep, "/")


def ignored_files(repo_root: str) -> List[str]:
    """Every untracked file on disk that gitignore excludes."""
    out = []
    for rel in _walk_files(repo_root):
        if check_path(repo_root, rel).ignored:
            out.append(rel)
    return out


def find_tracked_but_ignored(repo_root: str) -> List[str]:
    """Tracked files that a .gitignore pattern also matches.

    Git stops ignoring these, but it is almost always a sign the author
    intended to exclude them -- e.g. a log committed years ago that a new
    ``*.log`` rule should now catch.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    tracked = [p for p in proc.stdout.split(b"\0") if p]
    hits = []
    for raw in tracked:
        rel = raw.decode("utf-8", "replace")
        # Pass tracked=False on purpose: git never ignores a tracked file, but
        # this asks the sharper question -- "would this path be ignored if it
        # weren't already tracked?" That is the inconsistency worth surfacing.
        verdict = check_path(repo_root, rel, tracked=False)
        if verdict.ignored and verdict.decided_by is not None:
            hits.append(rel)
    return hits


def _normalize_for_dedupe(pattern_text: str) -> str:
    return pattern_text.rstrip("/").lower()


def scan_patterns(rules: Optional[List[Rule]] = None, repo_root: str = ".") -> List[Finding]:
    """Statically lint the patterns themselves, without scanning files."""
    if rules is None:
        rules = collect_patterns(repo_root)

    findings: List[Finding] = []
    seen: Dict[str, Rule] = {}
    shadowed: Set[int] = set()

    # Duplicate patterns: the earlier copy is dead weight.
    for idx, rule in enumerate(rules):
        key = _normalize_for_dedupe(rule.pattern.text)
        if key in seen:
            first = seen[key]
            findings.append(
                Finding(
                    code="DUPLICATE",
                    severity=SEV_INFO,
                    message=(
                        f"duplicate rule {rule.pattern.raw!r} also appears in "
                        f"{first.pattern.src}:{first.pattern.lineno}"
                    ),
                    rule=rule,
                    fix=f"delete the later copy (line {rule.pattern.lineno} of {rule.pattern.src})",
                )
            )
        else:
            seen[key] = rule

    # Dead negation: a '!' rule whose very own pattern is also excluded by an
    # *earlier* rule in the same file at the same directory depth. Because
    # git uses last-match-wins, a negation only helps if nothing after it
    # re-excludes.
    by_file: Dict[str, List[Rule]] = {}
    for rule in rules:
        by_file.setdefault(rule.pattern.src, []).append(rule)

    for src, file_rules in by_file.items():
        active_excludes: List[Rule] = []
        for rule in file_rules:
            if not rule.pattern.negated:
                active_excludes.append(rule)
                continue
            # Does a later exclusion in the same file cancel this exception?
            later = [
                r
                for r in file_rules
                if r is not rule
                and not r.pattern.negated
                and r.pattern.lineno > rule.pattern.lineno
                and _normalize_for_dedupe(r.pattern.text)
                == _normalize_for_dedupe(rule.pattern.text)
            ]
            if later:
                shadowed.add(id(rule))
                findings.append(
                    Finding(
                        code="DEAD_NEGATION",
                        severity=SEV_ERROR,
                        message=(
                            f"negation {rule.pattern.raw!r} is cancelled by a "
                            f"later exclusion in the same file"
                        ),
                        rule=rule,
                        fix="move the negation below the exclusion it carves out",
                    )
                )

    # Rule that ignores the directory it lives in: git never descends there,
    # so the entire .gitignore is inert.
    for rule in rules:
        if (
            rule.pattern.excludes_dir
            and not rule.pattern.negated
            and rule.base_dir
            and rule.pattern.dir_pattern == os.path.basename(rule.base_dir)
        ):
            findings.append(
                Finding(
                    code="SELF_EXCLUDE",
                    severity=SEV_ERROR,
                    message=(
                        f"{rule.pattern.raw!r} excludes the directory that holds "
                        f"its own .gitignore, so this file is never read again"
                    ),
                    rule=rule,
                    fix="remove the rule or move the .gitignore higher in the tree",
                )
            )

    # Broad rule with no escape hatch: '*.ext' at the root with nothing after.
    for rule in rules:
        if (
            not rule.pattern.negated
            and rule.pattern.text.startswith("*")
            and not rule.pattern.anchored
            and not any(
                r.pattern.negated and _normalize_for_dedupe(r.pattern.text).endswith(
                    rule.pattern.dir_pattern[1:]
                )
                for r in rules
            )
        ):
            findings.append(
                Finding(
                    code="BROAD_RULE",
                    severity=SEV_WARN,
                    message=(
                        f"broad rule {rule.pattern.raw!r} matches by basename at "
                        f"every depth, including nested directories"
                    ),
                    rule=rule,
                    fix="anchor it (e.g. /%s or a/**) if you only meant the root"
                    % rule.pattern.dir_pattern,
                )
            )

    # Tracked-but-ignored deserves its own pass only when there is a .git dir.
    if os.path.isdir(os.path.join(repo_root, ".git")):
        for rel in find_tracked_but_ignored(repo_root):
            # tracked=False again on purpose: it yields the verdict for "as if
            # not tracked", which is what decided_by must describe.
            verdict = check_path(repo_root, rel, tracked=False)
            findings.append(
                Finding(
                    code="TRACKED_IGNORED",
                    severity=SEV_WARN,
                    message="tracked file matches an ignore rule; git keeps it anyway",
                    path=rel,
                    rule=verdict.decided_by,
                    fix="run: git rm --cached %s" % rel,
                )
            )

    order = {SEV_ERROR: 0, SEV_WARN: 1, SEV_INFO: 2}
    findings.sort(key=lambda f: (order.get(f.severity, 9), f.code, f.path))
    return findings


def audit_repo(repo_root: str) -> Tuple[List[Finding], List[str]]:
    """Run the full audit. Returns (findings, ignored_untracked_files)."""
    rules = collect_patterns(repo_root)
    findings = scan_patterns(rules, repo_root)
    ignored = ignored_files(repo_root)
    return findings, ignored
