"""Tests for ignordr.engine - the ignore-pattern decision core."""

import pytest

from ignordr.engine import (
    Rule,
    decide,
    glob_to_regex,
    parse_gitignore_file,
)

SRC = ".gitignore"


def rules_from(text: str, base: str = "", src: str = SRC) -> list:
    return [Rule(pattern=x, base_dir=base) for x in parse_gitignore_file(src, text)]


# --------------------------------------------------------------------------- #
# glob translation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "glob,anchored,path,expected",
    [
        # '*' must not cross a slash, but matches at any depth when unanchored
        ("*.log", False, "debug.log", True),
        ("*.log", False, "src/debug.log", True),
        ("*.log", False, "src/sub/debug.log", True),
        # anchored: leading slash pins to root
        ("/app.log", True, "app.log", True),
        ("/app.log", True, "sub/app.log", False),
        # an interior slash anchors
        ("src/x.log", True, "src/x.log", True),
        ("src/x.log", True, "a/src/x.log", False),
        # '?' is exactly one non-slash character
        ("a?c", False, "abc", True),
        ("a?c", False, "ac", False),
        ("a?c", False, "ab/c", False),
        # character classes ('!' is translated to '^' in the output regex)
        ("[abc].log", False, "a.log", True),
        ("[abc].log", False, "d.log", False),
        ("[!abc].log", False, "d.log", True),
        ("[!abc].log", False, "a.log", False),
        ("[a-z].txt", False, "m.txt", True),
        ("[a-z].txt", False, "M.txt", False),
        # escaped glob metacharacter becomes literal
        ("\\*.log", False, "*.log", True),
        ("\\*.log", False, "x.log", False),
        # trailing-slash form is normalised away for matching purposes
        ("build/", False, "build", True),
        # recursive '**' crosses slashes (documented extension)
        ("a/**/z", False, "a/x/y/z", True),
        ("a/**/z", False, "a/z", True),
        ("a/**/z", False, "b/a/z", True),
        # malformed patterns never match
        ("[abc", False, "abc", False),
        ("abc\\", False, "abc", False),
    ],
)
def test_glob_translation(glob, anchored, path, expected):
    regex = glob_to_regex(glob, anchored)
    got = regex.fullmatch(path) is not None
    assert got is expected, f"{glob!r} (anchored={anchored}) vs {path!r}: got {got}"


def test_glob_star_never_crosses_slash():
    """'*' is FNM_PATHNAME: it must never cross a directory separator."""
    regex = glob_to_regex("*.log", anchored=False)
    assert regex.fullmatch("a/b.log") is not None
    assert regex.fullmatch("a/b/c.log") is not None
    assert glob_to_regex("*.log", anchored=False).fullmatch("a.log") is not None
    # a bare '*' is unanchored, so it matches any basename at any depth -- the
    # prefix handles the descent, and '*' still never crosses a slash by itself.
    assert glob_to_regex("*", anchored=False).fullmatch("a.log") is not None
    assert glob_to_regex("*", anchored=False).fullmatch("a/b") is not None
    assert glob_to_regex("**/x", anchored=False).fullmatch("deep/nested/x") is not None


def test_anchoring_detection():
    assert not parse_gitignore_file(SRC, "*.log")[0].anchored
    assert not parse_gitignore_file(SRC, "build/")[0].anchored
    assert parse_gitignore_file(SRC, "/build")[0].anchored
    assert parse_gitignore_file(SRC, "a/b")[0].anchored
    # '**/' at the start un-anchors the pattern despite the slash
    assert not parse_gitignore_file(SRC, "**/x")[0].anchored
    assert not parse_gitignore_file(SRC, "**/a/b")[0].anchored


def test_double_star_semantics():
    """'**' crosses '/' only as a standalone path component."""
    cross = glob_to_regex("a/**/z", anchored=True)
    assert cross.fullmatch("a/z") is not None
    assert cross.fullmatch("a/x/z") is not None
    assert cross.fullmatch("a/x/y/z") is not None
    assert cross.fullmatch("a/z/x") is None

    # trailing '**' also crosses
    assert glob_to_regex("a/**", anchored=True).fullmatch("a/x/y") is not None

    # 'a**z' is just 'a*z': never crosses a slash
    flat = glob_to_regex("a**z", anchored=True)
    assert flat.fullmatch("axz") is not None
    assert flat.fullmatch("a/z") is None


def test_directory_only_detection():
    assert parse_gitignore_file(SRC, "build/")[0].excludes_dir
    assert not parse_gitignore_file(SRC, "build")[0].excludes_dir


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_parse_skips_blank_and_comments():
    rules = parse_gitignore_file(SRC, "\n# comment\n*.log\n\n  \n!important.log\n")
    assert len(rules) == 2
    assert rules[0].text == "*.log"
    assert rules[0].negated is False
    assert rules[1].text == "important.log"
    assert rules[1].negated is True


def test_parse_records_line_numbers():
    rules = parse_gitignore_file(SRC, "# c\n\n*.log\n*.txt\n")
    assert rules[0].lineno == 3
    assert rules[1].lineno == 4


def test_parse_escapes_leading_hash_and_bang():
    # '\#weird' -> the backslash is stripped and '#weird' becomes a literal
    # pattern (not a comment). '\!name' -> the backslash escapes the '!' so it
    # stays in the text and is NOT treated as a negation.
    rules = parse_gitignore_file(SRC, "\\#weird\n\\!name\n")
    assert [r.text for r in rules] == ["#weird", "\\!name"]
    assert not rules[0].negated and not rules[1].negated
    # Both become literal patterns that git matches verbatim.
    assert glob_to_regex(rules[0].text, anchored=False).fullmatch("sub/#weird") is not None
    assert glob_to_regex(rules[1].text, anchored=False).fullmatch("sub/!name") is not None
    assert glob_to_regex(rules[1].text, anchored=False).fullmatch("sub/name") is None


def test_parse_trailing_spaces_stripped():
    rules = parse_gitignore_file(SRC, "*.log   \n")
    assert rules[0].text == "*.log"


# --------------------------------------------------------------------------- #
# last-match-wins
# --------------------------------------------------------------------------- #
def test_last_match_wins_exception_wins():
    v = decide("important.log", rules_from("*.log\n!important.log\n"), is_dir=False)
    assert v.ignored is False
    assert v.decided_by is not None and v.decided_by.pattern.negated is True


def test_last_match_wins_exclusion_wins():
    v = decide("important.log", rules_from("!important.log\n*.log\n"), is_dir=False)
    assert v.ignored is True


def test_shadowed_exception_detected_same_file():
    """A negation that is re-cancelled by a later exclusion in the same file."""
    rules = rules_from("*.log\n!important.log\n*.tmp\n")
    # *.log excludes, !important.log matches and wins -> NOT ignored, no shadow
    v = decide("important.log", rules, is_dir=False)
    assert v.ignored is False
    assert v.shadowed_exception is None

    # but with a later same-file exclusion the negation becomes a dead shadow
    rules2 = rules_from("*.log\n!important.log\n*.log\n")
    v2 = decide("important.log", rules2, is_dir=False)
    assert v2.ignored is True
    assert v2.shadowed_exception is not None


def test_no_match_is_not_ignored():
    v = decide("readme.md", rules_from("*.log\n"), is_dir=False)
    assert v.ignored is False
    assert v.decided_by is None


# --------------------------------------------------------------------------- #
# directory rules and the ignored-parent trap
# --------------------------------------------------------------------------- #
def test_trailing_slash_matches_directories_only():
    rules = rules_from("build/")
    assert decide("build", rules, is_dir=True).ignored is True
    assert decide("build", rules, is_dir=False).ignored is False


def test_directory_rule_matches_at_any_depth():
    rules = rules_from("node_modules/")
    assert decide("a/node_modules", rules, is_dir=True).ignored is True
    assert decide("a/node_modules/x.js", rules, is_dir=False).ignored is True


def test_file_level_exclusion_does_not_ignore_parent_directory():
    rules = rules_from("build")
    assert decide("build", rules, is_dir=True).ignored is True
    assert decide("build", rules, is_dir=False).ignored is True


def test_ignored_parent_makes_negation_unreachable():
    # logs/ is excluded as a directory, so git never descends into it: the
    # negation is silently unreachable, and the file is still ignored.
    rules = rules_from("logs/\n!logs/keep.log\n")
    v = decide("logs/keep.log", rules, is_dir=False)
    assert v.ignored is True
    assert v.parent_excluded is not None


def test_ignore_contents_not_directory_allows_negation():
    # Ignoring the contents file-by-file lets git descend, so the exception works.
    rules = rules_from("logs/*\n!logs/keep.log\n")
    assert decide("logs/keep.log", rules, is_dir=False).ignored is False
    assert decide("logs/other.log", rules, is_dir=False).ignored is True


# --------------------------------------------------------------------------- #
# nested .gitignore scoping
# --------------------------------------------------------------------------- #
def test_subdirectory_rule_is_scoped_to_that_directory():
    rules = rules_from("*.tmp", base="src")
    assert decide("src/a.tmp", rules, is_dir=False).ignored is True
    assert decide("lib/a.tmp", rules, is_dir=False).ignored is False
    assert decide("a.tmp", rules, is_dir=False).ignored is False


def test_deeper_file_overrides_higher_for_its_subtree():
    root = rules_from("*.tmp")
    nested = [Rule(pattern=x, base_dir="src") for x in parse_gitignore_file("src/.gitignore", "!*.tmp")]
    rules = root + nested
    assert decide("top.tmp", rules, is_dir=False).ignored is True
    assert decide("src/top.tmp", rules, is_dir=False).ignored is False


def test_deeper_exclusion_shadows_higher_negation():
    root = [Rule(pattern=x, base_dir="") for x in parse_gitignore_file(SRC, "!*.tmp")]
    nested = [Rule(pattern=x, base_dir="src") for x in parse_gitignore_file("src/.gitignore", "*.tmp")]
    rules = root + nested
    v = decide("src/top.tmp", rules, is_dir=False)
    assert v.ignored is True
    assert v.shadowed_exception is not None


# --------------------------------------------------------------------------- #
# tracked files
# --------------------------------------------------------------------------- #
def test_tracked_file_never_ignored():
    v = decide("app.log", rules_from("*.log"), is_dir=False, tracked=True)
    assert v.ignored is False
    # but we still report what rule would have matched, for the audit
    assert v.trail


def test_ignored_files_are_not_tracked_flagged():
    v = decide("app.log", rules_from("*.log"), is_dir=False, tracked=False)
    assert v.ignored is True
