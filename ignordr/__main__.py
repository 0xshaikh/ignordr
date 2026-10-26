#!/usr/bin/env python3
"""ignordr - a local, offline debugger for .gitignore files.

Git's ignore rules are famous for doing exactly what they say and nothing
you meant. ignordr answers, for any path in your repository:

    * is it ignored?
    * which file, which line, which pattern decided that?
    * if not: why did my rule fail? (anchoring, shadowing, ignored parent,
      already tracked)

It also audits a whole tree for duplicate rules, dead negations, self-excluding
.gitignore files, and tracked files that a rule now matches.

    $ ignordr explain build/app.js
    $ ignordr audit .
    $ ignordr lint
    $ ignordr tracked

Zero third-party dependencies. Python 3.9+. Read-only: it never rewrites a
.gitignore file unless you pass --fix and you look at the diff first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Optional

from . import __version__
from . import analyze
from .engine import Pattern, Rule, Verdict, check_path, collect_patterns

EXIT_OK = 0
EXIT_ISSUES = 1
EXIT_USAGE = 2

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _use_color(stream=None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    # NOTE: call the method. getattr alone returns the bound method, which is
    # always truthy -- that silently leaked ANSI escapes into pipes and files.
    return bool(isatty()) if callable(isatty) else False


class C:
    """Tiny color helper that no-ops when output is not a tty."""

    def __init__(self, enabled: bool):
        self.on = enabled

    def _w(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.on else text

    def red(self, t):
        return self._w(RED, t)

    def green(self, t):
        return self._w(GREEN, t)

    def yellow(self, t):
        return self._w(YELLOW, t)

    def cyan(self, t):
        return self._w(CYAN, t)

    def bold(self, t):
        return self._w(BOLD, t)

    def dim(self, t):
        return self._w(DIM, t)


def _normalize_arg_path(path: str) -> str:
    return path.strip().strip("/").replace(os.sep, "/")


def _is_dir_hint(path: str) -> Optional[bool]:
    if os.path.isdir(path):
        return True
    if os.path.isfile(path):
        return False
    return None


# --------------------------------------------------------------------------- #
# explain
# --------------------------------------------------------------------------- #
def _explain_verdict(c: C, verdict: Verdict, is_dir: Optional[bool]) -> int:
    status = c.red("IGNORED") if verdict.ignored else c.green("NOT IGNORED")
    kind = "directory" if is_dir else "file"
    print(f"{c.bold(verdict.path)}  ->  {status}  ({kind})")
    print()

    if verdict.decided_by is None:
        print(c.dim("  no .gitignore pattern matched this path"))
    else:
        p = verdict.decided_by.pattern
        verb = "re-included by" if p.negated else "excluded by"
        target = c.bold(p.raw)
        where = c.cyan(f"{p.src}:{p.lineno}")
        print(f"  decided {verb} {target}")
        print(f"  at {where}")
        print()
        print(_explain_pattern(c, p))

    if verdict.trail:
        print(c.bold("\n  Pattern evaluation order (last match wins):"))
        for rule, matched in verdict.trail:
            scope = rule.base_dir if rule.base_dir else "(repo root)"
            mark = c.green("  match") if matched else c.dim("  no")
            scope_s = c.dim(f"  scope {scope}")
            print(f"    {mark}  {rule.pattern.raw!r:<34} {scope_s}")
        print()

    # Advice
    if verdict.ignored and verdict.parent_excluded:
        print(c.red(c.bold("  WHY IT IS STUCK:"))
              + f" the parent exclusion {verdict.parent_excluded} prevents git from")
        print("  descending here, so any negation for this path is unreachable.")
        print()
        print("  FIX: stop ignoring the whole directory. Ignore its contents")
        print("        instead so git still walks into it, e.g.:")
        parent = verdict.path.rsplit("/", 1)[0] or "dir"
        print(f"        {parent}/*")
        print(f"        !{verdict.path}")
        print()
    elif verdict.ignored and verdict.shadowed_exception is not None:
        shadow = verdict.shadowed_exception
        winner = verdict.decided_by
        if winner is None:
            winner = shadow
        se = shadow.pattern
        w = winner.pattern
        same_file = se.src == w.src and shadow.base_depth == winner.base_depth
        print(c.red(c.bold("  WHY IT IS STUCK:"))
              + f" your exception {se.raw!r} in {se.src}:{se.lineno}")
        print("  matched, but it is overridden by a later rule.")
        if same_file:
            print(f"  '  {w.raw!r} in {w.src}:{w.lineno} comes after")
            print("   ' it, and git gives the LAST matching pattern the decision.")
            print("   ' Your exception is dead.")
            print()
            print("  FIX: move the negation below the exclusion it should override:")
            print("        *.log")
            print(f"        {w.raw}")
            print(f"        {se.raw}")
        else:
            print(f"  '  {w.raw!r} in {w.src}:{w.lineno} sits in a")
            print("   ' deeper directory. Git reads deeper .gitignore files last.")
            print()
            print("  FIX: declare the negation in the same .gitignore as the")
            print("        exclusion it overrides, or re-assert it at that depth.")
        print()
    elif not verdict.ignored and verdict.decided_by is None:
        print(c.cyan("  TIP: nothing ignores this path. If you expected a rule to")
                    + " catch it, check anchoring:")
        print("        *.log        matches .log, src/.log, a/b/.log")
        print("        /a.log       matches ONLY ./a.log at the repo root")
        print("        dir/x.log    anchored: matches only ./dir/x.log")
        print()

    return EXIT_ISSUES if verdict.ignored else EXIT_OK


def _explain_pattern(c: C, pattern: Pattern) -> str:
    """Human-readable annotation of one pattern's semantics."""
    bits: List[str] = []
    body = pattern.dir_pattern
    if pattern.negated:
        bits.append("NEGATION - carves an exception out of an earlier exclusion")
    if pattern.anchored:
        bits.append("ANCHORED - '/' in the pattern pins it to its .gitignore's directory")
    else:
        bits.append("UNANCHORED - matches by basename at ANY depth below the tree")
    if pattern.excludes_dir:
        bits.append("DIRECTORY ONLY - trailing '/' restricts it to directories")
    if body.startswith("**"):
        bits.append("RECURSIVE '*' - crosses path separators (treated as '**')")
    elif "*" in body:
        bits.append("'*' matches any run of characters EXCEPT '/'")
    if "?" in body:
        bits.append("'?' matches exactly one character except '/'")
    if "[" in body:
        bits.append("'[]' character class")
    return "\n".join("        - " + b for b in bits)


# --------------------------------------------------------------------------- #
# audit / lint
# --------------------------------------------------------------------------- #
def _cmd_audit(repo_root: str, as_json: bool, color: bool) -> int:
    findings, ignored = analyze.audit_repo(repo_root)
    if as_json:
        payload = {
            "repo": os.path.abspath(repo_root),
            "findings": [
                {
                    "code": f.code,
                    "severity": f.severity,
                    "message": f.message,
                    "path": f.path,
                    "fix": f.fix,
                    "rule": (
                        {
                            "src": f.rule.pattern.src,
                            "lineno": f.rule.pattern.lineno,
                            "raw": f.rule.pattern.raw,
                        }
                        if f.rule is not None
                        else None
                    ),
                }
                for f in findings
            ],
            "ignored_untracked": ignored,
        }
        print(json.dumps(payload, indent=2))
        return EXIT_ISSUES if findings else EXIT_OK

    c = C(color)
    errors = [f for f in findings if f.severity == "error"]
    warns = [f for f in findings if f.severity == "warn"]
    infos = [f for f in findings if f.severity == "info"]

    print(c.bold("ignordr audit") + c.dim(f"  {os.path.abspath(repo_root)}"))
    print(
        c.bold("  ")
        + c.red(f"{len(errors)} error")
        + ", "
        + c.yellow(f"{len(warns)} warning")
        + ", "
        + c.cyan(f"{len(infos)} info")
        + f", {len(ignored)} untracked file(s) ignored")
    print()
    if not findings:
        print(c.green("  no problems found - your ignore rules are clean"))
        return EXIT_OK
    for f in findings:
        print(f.format(color=color))
    return EXIT_ISSUES if errors or warns else EXIT_OK


def _cmd_lint(repo_root: str, as_json: bool, color: bool) -> int:
    findings = analyze.scan_patterns(repo_root=repo_root)
    if as_json:
        print(json.dumps([f.__dict__ for f in findings], indent=2, default=str))
        return EXIT_ISSUES if findings else EXIT_OK
    c = C(color)
    print(c.bold("ignordr lint") + c.dim("  (patterns only, no file scan)"))
    print()
    if not findings:
        print(c.green("  clean"))
        return EXIT_OK
    for f in findings:
        print(f.format(color=color))
    return EXIT_ISSUES if any(f.severity in ("error", "warn") for f in findings) else EXIT_OK


# --------------------------------------------------------------------------- #
# tracked
# --------------------------------------------------------------------------- #
def _cmd_tracked(repo_root: str, as_json: bool, color: bool) -> int:
    hits = analyze.find_tracked_but_ignored(repo_root)
    if as_json:
        print(json.dumps(hits, indent=2))
        return EXIT_ISSUES if hits else EXIT_OK
    c = C(color)
    print(c.bold("ignordr tracked")
          + c.dim("  tracked files that a .gitignore rule also matches"))
    print()
    if not hits:
        print(c.green("  none - tracked and ignored sets are disjoint"))
        return EXIT_OK
    for rel in hits:
        v = check_path(repo_root, rel, tracked=True)
        where = f" @{v.decided_by.pattern.src}:{v.decided_by.pattern.lineno} ({v.decided_by.pattern.raw!r})" if v.decided_by else ""
        print("  " + c.yellow(rel) + c.dim(where))
    print()
    print(c.dim("  git keeps these files; add them to .gitignore only if you meant to:"))
    print(c.dim("    git rm --cached <path>"))
    return EXIT_ISSUES


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
def _cmd_scan(repo_root: str, as_json: bool, color: bool, include_ignored: bool) -> int:
    files = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        rel_dir = os.path.relpath(dirpath, repo_root)
        for name in filenames:
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            rel = rel.replace(os.sep, "/")
            v = check_path(repo_root, rel)
            if v.ignored and not include_ignored:
                continue
            files.append({"path": rel, "ignored": v.ignored, "by": (v.decided_by.pattern.raw if v.decided_by else None)})
    if as_json:
        print(json.dumps(files, indent=2))
        return EXIT_OK
    c = C(color)
    print(c.bold("ignordr scan") + c.dim(f"  {len(files)} file(s) shown"))
    print()
    for entry in files:
        tag = c.red("IGN") if entry["ignored"] else c.green("  ")
        suffix = c.dim(f"  {entry['by']}") if entry["by"] else ""
        print(f"  {tag}  {entry['path']}{suffix}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def _cmd_check(paths: List[str], repo_root: str, as_json: bool, color: bool) -> int:
    results = []
    exit_code = EXIT_OK
    c = C(color)
    for path in paths:
        rel = _normalize_arg_path(path)
        is_dir = _is_dir_hint(os.path.join(repo_root, rel))
        v = check_path(repo_root, rel, is_dir=is_dir)
        results.append(v)
        if v.ignored:
            exit_code = EXIT_ISSUES
        if as_json:
            continue
        status = c.red("IGNORED") if v.ignored else c.green("NOT IGNORED")
        print(f"{status:<12} {rel}")
        if v.decided_by:
            print(c.dim(f"               {v.decided_by.pattern.src}:{v.decided_by.pattern.lineno}  {v.decided_by.pattern.raw!r}"))
    if as_json:
        print(
            json.dumps(
                [
                    {"path": v.path, "ignored": v.ignored, "by": (v.decided_by.pattern.raw if v.decided_by else None)}
                    for v in results
                ],
                indent=2,
            )
        )
    return exit_code


# --------------------------------------------------------------------------- #
# fix
# --------------------------------------------------------------------------- #
def _cmd_fix(repo_root: str, color: bool, apply: bool = False) -> int:
    """Propose a minimal, comment-preserving .gitignore rewrite."""
    import difflib

    c = C(color)
    changed_any = False
    print(c.bold("ignordr fix")
          + c.dim("  proposed .gitignore rewrite (dedupe, keeps comments)"))
    print()

    per_file: Dict[str, List[Rule]] = {}
    for rule in collect_patterns(repo_root):
        per_file.setdefault(rule.pattern.src, []).append(rule)

    for src in sorted(per_file):
        full = os.path.join(repo_root, src)
        try:
            with open(full, "r", encoding="utf-8") as fh:
                original_lines = fh.read().splitlines(keepends=True)
        except OSError:
            continue

        # Walk top-down, keeping every comment and blank line untouched, and
        # only dropping a rule that is a duplicate of one appearing LATER. git
        # gives the last match the decision, so the earlier copy is dead weight.
        drop: set = set()
        seen: Dict[str, int] = {}
        for idx, line in enumerate(original_lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key = _norm(stripped)
            if key in seen:
                drop.add(seen[key])
            seen[key] = idx

        rewritten = "".join(ln for i, ln in enumerate(original_lines) if i not in drop)
        if rewritten == "".join(original_lines):
            continue
        changed_any = True
        print(f"{c.bold(src)}")
        for line in difflib.unified_diff(
            "".join(original_lines).splitlines(),
            rewritten.splitlines(),
            fromfile=f"{src} (current)",
            tofile=f"{src} (proposed)",
            lineterm="",
        ):
            if line.startswith("---") or line.startswith("+++"):
                print("  " + line)
            elif line.startswith("-"):
                print("  " + c.red(line))
            elif line.startswith("+"):
                print("  " + c.green(line))
            else:
                print("  " + c.dim(line))
        print()

        if apply:
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(rewritten)
            print(c.green(f"  wrote {src}"))

    if not changed_any:
        print(c.green("  nothing to change"))
        return EXIT_OK
    if apply:
        print(c.green("  changes written"))
    else:
        print(c.cyan("  dry run. Re-run with --apply to write the changes."))
    return EXIT_ISSUES


def _norm(text: str) -> str:
    return text.rstrip("/").lower()


# --------------------------------------------------------------------------- #
# CLI plumbing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ignordr",
        description=(
            "ignordr - debug, audit and explain .gitignore files locally.\n\n"
            "Why .gitignore is confusing:\n"
            "  - '*.log' matches at EVERY depth; 'a/b.log' is anchored to one dir\n"
            "  - the LAST matching pattern wins, not the first\n"
            "  - '!important.log' inside an ignored directory is silently dead\n"
            "  - already-tracked files ignore .gitignore entirely\n"
            "\nRun 'ignordr explain <path>' first. It tells you the exact file and"
            " line that decided a path's fate, and why your rule did not work."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=True,
    )
    parser.add_argument("--version", action="version", version=f"ignordr {__version__}")
    parser.add_argument(
        "--repo",
        default=".",
        metavar="DIR",
        help="repository root to analyse (default: current directory)",
    )
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="force colour output on or off (default: auto-detect tty)",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser(
        "explain",
        help="why is this path ignored (or not)?",
        description="Trace a single path through every .gitignore and report the deciding rule.",
    )
    p.add_argument("paths", nargs="+", metavar="PATH", help="repo-relative path(s) to explain")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser(
        "audit",
        help="full audit: pattern problems + tracked-but-ignored files",
        description="Scan the whole tree and report every detectable problem.",
    ).add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser(
        "lint",
        help="statically check the patterns without scanning files",
        description="Fast: read .gitignore files only, report duplicates and dead negations.",
    ).add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser(
        "tracked",
        help="list tracked files that an ignore rule also matches",
        description="Requires a git repository. Git keeps these files anyway.",
    ).add_argument("--json", action="store_true", help="machine-readable output")

    sp = sub.add_parser(
        "scan",
        help="walk the tree and mark every file ignored or not",
        description="List all files with the rule that ignores each one.",
    )
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.add_argument(
        "--include-ignored",
        action="store_true",
        help="also list files that ARE ignored (default: only show unignored)",
    )

    cp = sub.add_parser(
        "check",
        help="quick yes/no for one or more paths",
        description="Drop-in replacement for 'git check-ignore' that also works outside a repo.",
    )
    cp.add_argument("paths", nargs="+", metavar="PATH", help="repo-relative paths")
    cp.add_argument("--json", action="store_true", help="machine-readable output")

    fp = sub.add_parser(
        "fix",
        help="propose a deduplicated, sorted rewrite of .gitignore files",
        description="Dry run by default: prints a unified diff of what would change.",
    )
    fp.add_argument("--apply", action="store_true", help="write the changes to disk")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    repo_root = os.path.abspath(args.repo)
    if not os.path.isdir(repo_root):
        print(f"ignordr: not a directory: {args.repo}", file=sys.stderr)
        return EXIT_USAGE

    if args.color == "auto":
        color = _use_color()
    else:
        color = args.color == "always"

    if args.command == "explain":
        code = EXIT_OK
        for path in args.paths:
            rel = _normalize_arg_path(path)
            full = os.path.join(repo_root, rel)
            is_dir = _is_dir_hint(full)
            verdict = check_path(repo_root, rel, is_dir=is_dir)
            if args.json:
                out = {
                    "path": verdict.path,
                    "ignored": verdict.ignored,
                    "decided_by": (
                        {
                            "raw": verdict.decided_by.pattern.raw,
                            "src": verdict.decided_by.pattern.src,
                            "lineno": verdict.decided_by.pattern.lineno,
                            "negated": verdict.decided_by.pattern.negated,
                            "anchored": verdict.decided_by.pattern.anchored,
                            "directory_only": verdict.decided_by.pattern.excludes_dir,
                        }
                        if verdict.decided_by
                        else None
                    ),
                    "parent_excluded": verdict.parent_excluded,
                    "shadowed_exception": (
                        verdict.shadowed_exception.pattern.raw
                        if verdict.shadowed_exception
                        else None
                    ),
                    "trail": [
                        {"raw": r.pattern.raw, "src": r.pattern.src, "lineno": r.pattern.lineno, "matched": m}
                        for r, m in verdict.trail
                    ],
                }
                print(json.dumps(out, indent=2))
            else:
                if len(args.paths) > 1:
                    print()
                code = _explain_verdict(C(color), verdict, is_dir) or code
        return code

    if args.command == "audit":
        return _cmd_audit(repo_root, args.json, color)
    if args.command == "lint":
        return _cmd_lint(repo_root, args.json, color)
    if args.command == "tracked":
        return _cmd_tracked(repo_root, args.json, color)
    if args.command == "scan":
        return _cmd_scan(repo_root, args.json, color, args.include_ignored)
    if args.command == "check":
        return _cmd_check(args.paths, repo_root, args.json, color)
    if args.command == "fix":
        return _cmd_fix(repo_root, color, args.apply)

    parser.error(f"unknown command: {args.command}")
    return EXIT_USAGE  # unreachable


if __name__ == "__main__":
    sys.exit(main())
