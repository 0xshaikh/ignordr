"""ignordr.engine - a faithful reimplementation of Git's ignore-pattern semantics.

This module answers one hard question: *is this path ignored, by which rule,
and why?* Everything else in ignordr (audit, explain, fix, check) is built on
:func:`decide`.

The semantics implemented here follow ``git-check-ignore``. They were derived
empirically against a real git binary -- see ``tests/test_git_parity.py`` --
because Git's behaviour differs from a naive reading of the docs in several
documented places.

The essentials:

* Patterns come from ``.gitignore`` files at every level of the tree. A
  pattern found in a subdirectory's ``.gitignore`` only governs paths at or
  below that directory.
* The **last matching pattern wins** (order matters, not first-match).
* A ``!`` prefix negates an earlier exclusion (an *exception*).
* A pattern containing ``/`` anywhere other than as a trailing separator is
  *anchored* to the ``.gitignore`` that declared it. A pattern with no ``/``
  matches by basename at any depth.
* ``**`` crosses directory separators **only** when it appears as its own
  path component (``a/**/z``). ``a**z`` behaves like ``a*z`` and never crosses
  a slash. ``a/**`` requires at least one component after ``a/``.
* **Directory pruning** is the part that surprises everyone. Git walks the
  tree and descends into a directory only if that directory is *not* excluded.
  Once a directory is excluded, Git never reads inside it, so a negation for a
  file beneath an excluded directory is silently unreachable.
  Concretely: ``dir`` and ``dir/`` prune the directory (negations inside are
  dead), but ``dir/*`` does not prune the directory -- it excludes only the
  contents, so a negation like ``!dir/keep.txt`` still works.
* Tracked files are never ignored by git; the rule is simply skipped.

The glob-to-regex translation mirrors ``wildmatch(3)`` with ``FNM_PATHNAME``,
the same flag Git passes -- so a bare ``*`` never crosses a ``/``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

__all__ = [
    "Pattern",
    "Rule",
    "Verdict",
    "decide",
    "check_path",
    "collect_patterns",
    "glob_to_regex",
    "parse_gitignore_file",
    "REPO_GITIGNORE_BASENAME",
]

REPO_GITIGNORE_BASENAME = ".gitignore"

# Zero or more complete path components, each written as `component/`.
# A crossing '**' expands to this; when '**' ends the pattern it also gets a
# trailing `[^/]*` so a final partial component still matches.
_RE_COMPONENTS = "(?:[^/]+/)*"


# --------------------------------------------------------------------------- #
# Pattern representation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Pattern:
    """A single parsed ``.gitignore`` line."""

    raw: str  # the literal line as it appeared in the file
    text: str  # the pattern text after the optional leading '!'
    negated: bool  # True when the line starts with '!'
    src: str  # which .gitignore file the pattern came from
    lineno: int  # 1-based line number within that file

    @property
    def excludes_dir(self) -> bool:
        """Did the author restrict this to directories (trailing slash)?"""
        return self.text.endswith("/")

    @property
    def dir_pattern(self) -> str:
        """Pattern text with a single trailing slash removed."""
        return self.text[:-1] if self.text.endswith("/") else self.text

    @property
    def anchored(self) -> bool:
        """Is this pattern anchored to its declaring .gitignore's directory?

        Anchored when there is a ``/`` anywhere in the pattern other than a
        single trailing one. A *leading* ``**`` un-anchors the whole pattern
        regardless of the slashes it contains: git treats ``**/x`` and
        ``**/a/b`` as if they were ``x`` and ``a/b`` at any depth.
        """
        if self.dir_pattern.startswith("**"):
            return False
        return "/" in self.dir_pattern


# --------------------------------------------------------------------------- #
# Glob -> regex
# --------------------------------------------------------------------------- #
def glob_to_regex(pattern: str, anchored: bool) -> "re.Pattern[str]":
    """Translate a gitignore glob into a compiled, full-match regex.

    ``*`` matches any run of characters except ``/`` (FNM_PATHNAME). ``?``
    matches exactly one non-slash character. ``[...]`` character classes are
    supported, including ``[!a-z]`` negation. A backslash escapes the next
    character.

    A leading ``/`` is a pure anchor marker: it is consumed and does not take
    part in the match (so ``/app.log`` matches ``app.log`` but never
    ``sub/app.log``). Callers pass it through *anchored*.

    ``**`` crosses ``/`` **only** when it is a standalone path component
    (preceded by ``/``, followed by ``/`` or end-of-pattern). ``a/**/z``
    matches ``a/z`` and ``a/x/y/z``; ``a**z`` is just ``a*z`` and does not
    cross a slash; ``a/**`` matches ``a/x`` and ``a/x/y`` but not ``a`` itself.

    When *anchored* the pattern is matched from the root of the tree;
    otherwise a prefix is prepended so it can match at any depth.
    """
    body = pattern[:-1] if pattern.endswith("/") else pattern
    # '**' at the very start makes the pattern unanchored regardless of the
    # slashes it contains (git treats '**/x' like 'x' at any depth).
    leading_double_star = body.startswith("**")
    if body.startswith("/"):
        # The leading slash only anchors; it is not part of the match.
        body = body[1:]
    out: List[str] = []
    never = False
    i = 0
    n = len(body)

    while i < n:
        c = body[i]

        if c == "\\":
            if i + 1 >= n:
                # A trailing backslash is an invalid pattern in git: it never
                # matches. Emit an impossible lookahead.
                never = True
                break
            out.append(re.escape(body[i + 1]))
            i += 2
            continue

        if c == "*":
            if body[i : i + 2] == "**":
                # '**' crosses '/' as a standalone path component -- when it is
                # followed by '/' or sits at the end of the pattern. A leading
                # '**' also crosses even when followed by an ordinary character:
                # '**z' matches 'a/b/z' just like '**/z'. Any other '**' is just
                # a greedy star, so 'a**z' behaves like 'a*z' and never crosses
                # a slash.
                if i + 2 < n and body[i + 2] == "/":
                    out.append(_RE_COMPONENTS)
                    i += 3
                    continue
                if i + 2 == n:
                    out.append(_RE_COMPONENTS + "[^/]*")
                    i += 2
                    continue
                if i == 0:
                    out.append(_RE_COMPONENTS + "[^/]*")
                    i += 2
                    continue
                out.append("[^/]*")
                i += 2
                continue
            # Plain '*': any run of non-slash characters.
            out.append("[^/]*")
            i += 1
            continue

        if c == "?":
            out.append("[^/]")
            i += 1
            continue

        if c == "[":
            # Locate the closing bracket first. An absent one means the class is
            # malformed: git aborts the match and the pattern never matches.
            j = i + 1
            while j < n and body[j] != "]":
                if body[j] == "\\" and j + 1 < n:
                    j += 2
                else:
                    j += 1
            if j >= n:
                out.append("(?:)")
                never = True
                break
            # A leading '!' in a class is POSIX negation; translate to '^'.
            if body[i + 1] == "!":
                out.append("[^")
                j0 = i + 2
            else:
                out.append("[")
                j0 = i + 1
            if j0 < j and body[j0] == "]":
                # ']' immediately after '[' is a literal member.
                out.append("]")
                j0 += 1
            k = j0
            while k < j:
                ch = body[k]
                if ch == "\\":
                    # A backslash escapes the next character inside a class;
                    # it becomes literal, so no regex escaping is needed.
                    if k + 1 < j:
                        out.append(body[k + 1])
                        k += 2
                        continue
                    break
                # Keep '-' as a range operator when it sits between two
                # characters; otherwise escape it so it stays literal.
                if ch == "-":
                    prev_ch = out[-1] if out else ""
                    nxt_ch = body[k + 1] if k + 1 < j else ""
                    if (
                        len(prev_ch) == 1
                        and nxt_ch
                        and nxt_ch not in "]^\\-"
                    ):
                        out.append("-")
                    else:
                        out.append(re.escape(ch))
                else:
                    out.append(re.escape(ch))
                k += 1
            out.append("]")
            i = j + 1
            continue

        out.append(re.escape(c))
        i += 1

    regex = "".join(out)
    if not anchored:
        # An unanchored pattern matches by basename at any depth. A leading
        # '**/' already consumes whole path components in its own right, so the
        # generic '.*/' prefix is applied only to the part after it.
        if leading_double_star and regex.startswith(_RE_COMPONENTS):
            regex = "(?:^|.*/)?" + regex[len(_RE_COMPONENTS) :]
        else:
            regex = "(?:^|.*/)?" + regex
    if never:
        regex = regex + "(?=.)" + "(?!.)"
    return re.compile(regex + "$")


# --------------------------------------------------------------------------- #
# File parsing
# --------------------------------------------------------------------------- #
def parse_gitignore_file(src: str, text: str) -> List[Pattern]:
    """Parse the text of a .gitignore into :class:`Pattern` objects.

    Follows Git's quirky unescaping rule: a leading backslash is stripped
    **only** when it precedes ``#`` (making ``\\#name`` a literal pattern
    ``#name`` instead of a comment). A leading ``\\!`` is left alone, so the
    ``!`` still acts as negation and the backslash becomes a literal escape.
    """
    patterns: List[Pattern] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        negated = False
        if line.startswith("\\") and len(line) > 1 and line[1] == "#":
            # '\#name' -> literal pattern '#name', not a comment.
            line = line[1:]
            negated = False
        elif line.startswith("!"):
            negated = True
            line = line[1:]
        if not line:
            continue
        patterns.append(
            Pattern(raw=raw_line, text=line, negated=negated, src=src, lineno=lineno)
        )
    return patterns


@dataclass
class Rule:
    """A pattern together with the directory it was declared from."""

    pattern: Pattern
    base_dir: str  # repo-relative directory holding the .gitignore

    @property
    def base_depth(self) -> int:
        if self.base_dir in ("", "."):
            return 0
        return self.base_dir.rstrip("/").count("/") + 1


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
def collect_patterns(
    repo_root: str,
    root_patterns: Optional[Sequence[Pattern]] = None,
    exclude_files: Optional[Sequence[Tuple[str, str]]] = None,
) -> List[Rule]:
    """Walk *repo_root* and collect every rule, in git's precedence order.

    *root_patterns* (and *exclude_files*) are virtual patterns that apply from
    the repo root without needing a real file -- used by ``.git/info/exclude``
    and ``core.excludesFile``.
    """
    root_patterns = list(root_patterns or [])
    exclude_files = list(exclude_files or [])

    rules: List[Rule] = []
    for src, text in exclude_files:
        for p in parse_gitignore_file(src, text):
            rules.append(Rule(pattern=p, base_dir=""))

    for p in root_patterns:
        rules.append(Rule(pattern=p, base_dir=""))

    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames.sort()
        rel = os.path.relpath(dirpath, repo_root)
        base = "" if rel == "." else rel.replace(os.sep, "/")
        keep = [name for name in dirnames if name != ".git"]
        dirnames[:] = keep
        for fname in sorted(filenames):
            if fname == REPO_GITIGNORE_BASENAME:
                full = os.path.join(dirpath, fname)
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                display = (
                    REPO_GITIGNORE_BASENAME
                    if base == ""
                    else f"{base}/{REPO_GITIGNORE_BASENAME}"
                )
                for p in parse_gitignore_file(display, text):
                    rules.append(Rule(pattern=p, base_dir=base))
    return rules


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
@dataclass
class Verdict:
    """The outcome of evaluating a single path."""

    path: str
    ignored: bool
    decided_by: Optional[Rule]
    trail: List[Tuple[Rule, bool]] = field(default_factory=list)
    # A negation matched but an exclusion in a deeper directory won anyway.
    shadowed_exception: Optional[Rule] = None
    # Ancestor directory exclusion that makes a negation unreachable.
    parent_excluded: Optional[str] = None

    @property
    def reason(self) -> str:
        if self.decided_by is None:
            return "no pattern matched"
        p = self.decided_by.pattern
        verb = "re-included" if p.negated else "excluded"
        return f"{verb} by {p.src}:{p.lineno} ({p.raw!r})"


# --------------------------------------------------------------------------- #
# Matching helpers
# --------------------------------------------------------------------------- #
def _candidate(rule: Rule, rel_path: str) -> Optional[str]:
    """The path segment *rule* should be matched against, or None if out of scope."""
    if rule.base_dir:
        if rel_path != rule.base_dir and not rel_path.startswith(rule.base_dir + "/"):
            return None
        candidate = (
            rel_path[len(rule.base_dir) :] if rel_path != rule.base_dir else ""
        )
        if not candidate:
            return None
        return candidate
    return rel_path


def _match_at(
    rules: Sequence[Rule], path: str, is_dir: bool
) -> List[Tuple[Rule, bool]]:
    """Evaluate every in-scope rule against *path*; return [(rule, matched)].

    *is_dir=True* evaluates as a directory entry (so a trailing-``/`` pattern
    such as ``dir/`` matches ``dir``, and a bare ``dir`` also matches the
    directory itself). *is_dir=False* evaluates as a regular file, where a
    trailing-``/`` pattern never matches.
    """
    hits: List[Tuple[Rule, bool]] = []
    for rule in rules:
        candidate = _candidate(rule, path)
        if candidate is None:
            continue
        p = rule.pattern
        if p.excludes_dir:
            # A trailing-slash pattern matches only directory entries.
            ok = is_dir and bool(
                glob_to_regex(p.dir_pattern, p.anchored).fullmatch(candidate)
            )
        else:
            ok = bool(glob_to_regex(p.dir_pattern, p.anchored).fullmatch(candidate))
        hits.append((rule, ok))
    return hits


def _last_match(hits: List[Tuple[Rule, bool]]) -> Optional[Rule]:
    """The winning rule among matching ones, or None if nothing matched."""
    winner: Optional[Rule] = None
    for rule, matched in hits:
        if matched:
            winner = rule
    return winner


def _pruned_ancestor(
    rules: Sequence[Rule], rel_path: str
) -> Optional[Tuple[str, Rule, bool]]:
    """Return (ancestor, rule, excluded) for the nearest excluded ancestor.

    Mirrors Git's tree walk: starting at the closest ancestor, check whether it
    is excluded (last match non-negated). Return on the first exclusion -- that
    is where Git stops descending.
    """
    parts = rel_path.split("/")
    for depth in range(len(parts) - 1, 0, -1):
        ancestor = "/".join(parts[:depth])
        winner = _last_match(_match_at(rules, ancestor, is_dir=True))
        if winner is not None:
            return ancestor, winner, not winner.pattern.negated
    return None


# --------------------------------------------------------------------------- #
# Decision
# --------------------------------------------------------------------------- #
def decide(
    rel_path: str,
    rules: Sequence[Rule],
    is_dir: bool = False,
    tracked: bool = False,
) -> Verdict:
    """Pure decision function: evaluate *rel_path* against an explicit rule set.

    This is the heart of ignordr and is exercised directly by the test suite.
    :func:`check_path` is a thin filesystem wrapper around it.
    """
    rel_path = rel_path.strip("/")
    hits = _match_at(rules, rel_path, is_dir=is_dir)

    if tracked:
        return Verdict(path=rel_path, ignored=False, decided_by=None, trail=hits)

    # 1. Directory pruning: if a nearest ancestor is excluded as a directory,
    #    git never descends into it. The path is ignored and any negation
    #    written for it is unreachable.
    pruned = _pruned_ancestor(rules, rel_path)
    if pruned is not None and pruned[2]:
        ancestor, pruner, _ = pruned
        parent_excluded = (
            f"{pruner.pattern.raw!r} at {pruner.pattern.src}:{pruner.pattern.lineno}"
        )
        winner = _last_match(hits)
        # Report the winning leaf rule if present, else the pruner itself.
        decided = winner if winner is not None else pruner
        return Verdict(
            path=rel_path,
            ignored=True,
            decided_by=decided,
            trail=hits,
            parent_excluded=parent_excluded,
        )

    # 2. Otherwise git descended all the way; last matching pattern wins.
    winner = _last_match(hits)
    if winner is None:
        return Verdict(path=rel_path, ignored=False, decided_by=None, trail=hits)

    ignored = not winner.pattern.negated

    # 3. Shadowed exception: the path ended up ignored even though a negation
    #    matched it. The winner is by construction the LAST match, so any
    #    matched negation must sit earlier in the trail and was overridden.
    #    That is always worth warning about -- either an ordering error within
    #    one .gitignore, or a rule in a deeper directory beating an exception
    #    declared above it (git reads deeper .gitignore files last).
    shadowed: Optional[Rule] = None
    if ignored:
        last_negation: Optional[Rule] = None
        for rule, matched in hits:
            if matched and rule.pattern.negated:
                last_negation = rule
        shadowed = last_negation

    return Verdict(
        path=rel_path,
        ignored=ignored,
        decided_by=winner,
        trail=hits,
        shadowed_exception=shadowed,
    )


def check_path(
    repo_root: str,
    rel_path: str,
    is_dir: Optional[bool] = None,
    tracked: bool = False,
) -> Verdict:
    """Evaluate whether *rel_path* is ignored, and exactly why.

    *rel_path* is repo-relative, using ``/`` separators. *is_dir* is inferred
    from the filesystem when omitted; pass it explicitly for paths that do
    not exist on disk (the normal case for auditing). *tracked* short-circuits
    to "not ignored", because Git never ignores tracked paths.
    """
    if is_dir is None:
        full = os.path.join(repo_root, rel_path.strip("/").replace("/", os.sep))
        is_dir = os.path.isdir(full)
    return decide(rel_path, collect_patterns(repo_root), is_dir=is_dir, tracked=tracked)
