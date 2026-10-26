# ignordr

**[![icon](icon.svg)](https://github.com/0xshaikh/ignordr)** A local, offline debugger and auditor for `.gitignore` files.

[![ci](https://github.com/0xshaikh/ignordr/actions/workflows/ci.yml/badge.svg)](https://github.com/0xshaikh/ignordr/actions/workflows/ci.yml)
[![docs](https://github.com/0xshaikh/ignordr/actions/workflows/pages.yml/badge.svg)](https://0xshaikh.github.io/ignordr/)
[![python](https://img.shields.io/badge/python-3.9%2B-blueviolet)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

`git check-ignore` answers one question: *"is this one path ignored?"* You usually have the opposite problem — you wrote twelve rules over three years, `!important.log` quietly stopped working, half of them are duplicates, and you want to know *why* something ended up in git.

`ignordr` re-implements git's ignore semantics from scratch (no `git` subprocess, no network), then adds the diagnostics git doesn't have.

```
pip install ignordr
ignordr explain logs/important.log
```

## Why `.gitignore` is hard

Every one of these is true, and all of them surprise people:

- `*.log` matches at **every depth** — `a.log`, `src/a.log`, `a/b/c/a.log`. `/app.log` matches only the root.
- **The last match wins**, not the first. `*.log` then `!x.log` keeps `x.log`; `!x.log` then `*.log` drops it.
- A negation inside an ignored directory is **silently dead**. `build` (whole dir) then `!build/keep.log` does nothing — git never descends into a pruned directory.
- Already-tracked files **ignore `.gitignore` entirely**. Your new `*.log` rule will not stop an old committed log file.
- `**` only crosses `/` as a *standalone* component. `a/**/z` crosses; `a**z` does not.

Each of those has bitten someone in production. `ignordr` names which one you hit.

## Commands

| Command | What it does |
| --- | --- |
| `explain PATH...` | Trace a path through every rule; report the file, line, and *reason* it won — or exactly why your exception died |
| `audit` | Full repo audit: pattern problems plus tracked files an ignore rule matches |
| `lint` | Statically check patterns only — no file scan, fast, safe for CI |
| `tracked` | List tracked files that an ignore rule would now exclude |
| `scan` | Walk the tree and mark every file ignored / not |
| `check PATH...` | One-line yes/no, scriptable, exit-code driven |
| `fix` | Propose a deduplicated rewrite as a diff; `--apply` to write |

Everything except `scan`/`audit` is read-only and needs no git repository at all. `--json` on every command for machine output.

> There is an interactive cheatsheet with a browser playground that runs the
> same decision logic: <https://0xshaikh.github.io/ignordr/>

## Examples

### Explain the rule that decided a path

```console
$ ignordr explain important.log
important.log  ->  NOT IGNORED  (file)

  decided re-included by !important.log
  at .gitignore:7

        - NEGATION - carves an exception out of an earlier exclusion
        - UNANCHORED - matches by basename at ANY depth below the tree
```

The rule that won, the exact file and line, and a plain-language gloss of the two
properties of that pattern that made it match.

### See why your exception is dead

```console
$ ignordr explain logs/important.log
logs/important.log  ->  IGNORED  (file)

  decided excluded by logs/*.log
  at .gitignore:8

        - ANCHORED - '/' in the pattern pins it to its .gitignore's directory
        - '*' matches any run of characters EXCEPT '/'

  Pattern evaluation order (last match wins):
      no  'build'                              scope (repo root)
      no  'dist/'                              scope (repo root)
      match  '*.log'                            scope (repo root)
      match  '!important.log'                   scope (repo root)
      match  'logs/*.log'                       scope (repo root)

  WHY IT IS STUCK: your exception '!important.log' in .gitignore:7
  matched, but it is overridden by a later rule.
  '  'logs/*.log' in .gitignore:8 comes after
   ' it, and git gives the LAST matching pattern the decision.
   ' Your exception is dead.

  FIX: move the negation below the exclusion it should override:
        *.log
        logs/*.log
        !important.log
```

Note the evaluation trail. `!important.log` *did* match — the failure is pure
ordering. git has no way to tell you that.

### Audit a whole repository

```console
$ ignordr audit
ignordr audit  /path/to/repo
  0 error, 4 warning, 1 info, 9 untracked file(s) ignored

[BROAD_RULE]   broad rule '*.tmp' matches by basename at every depth, including nested directories  @.gitignore:9
              -> anchor it (e.g. /*.tmp or a/**) if you only meant the root
[BROAD_RULE]   broad rule '**/dist/' matches by basename at every depth, including nested directories  @.gitignore:13
              -> anchor it (e.g. /**/dist or a/**) if you only meant the root
[TRACKED_IGNORED] tracked file matches an ignore rule; git keeps it anyway  (a.log)  @.gitignore:6
              -> run: git rm --cached a.log
[DUPLICATE]    duplicate rule 'build' also appears in .gitignore:2  @.gitignore:12
              -> delete the earlier copy (line 2 of .gitignore)
```

### `TRACKED_IGNORED` — the one `git check-ignore` cannot show you

`git check-ignore a.log` prints nothing for a tracked file, which reads as
"not ignored". But `*.log` on line 6 clearly matches it. The file is committed,
so git honours your intent — but a fresh clone with a `git clean -fdx`, a
submodule, or a path re-add will behave differently from your working tree.

```console
$ ignordr tracked
ignordr tracked  tracked files that a .gitignore rule also matches

  a.log
  src/big.tmp

  git keeps these files; remove them from the index if you meant to ignore:
    git rm --cached <path>
```

### Walk the tree

```console
$ ignordr scan --include-ignored
ignordr scan  15 file(s) shown

  IGN  a.log  *.log
      important.log  !important.log
  IGN  keep.tmp  *.tmp
  IGN  node_modules/x/pkg.js  node_modules/
  IGN  logs/app.log  logs/*.log
  IGN  logs/important.log  logs/*.log
```

`IGN` marks the path and names the rule that caught it. Non-ignored files are
shown without a marker.

### Fix duplicates without losing comments

```console
$ ignordr fix
ignordr fix  proposed .gitignore rewrite (dedupe, keeps comments)

.gitignore
  --- .gitignore (current)
  +++ .gitignore (proposed)
  @@ -1,5 +1,4 @@
   # build artifacts
  -build
   dist/
   node_modules/

  dry run. Re-run with --apply to write the changes.
```

`fix` is deliberately conservative: it only removes an exclusion line that is a
duplicate of one appearing *later* in the same file (git gives the last match the
decision, so the earlier copy is dead weight). Comments, blanks, and ordering are
left exactly as you wrote them. `fix --apply` is idempotent.

## Finding codes

| Code | Severity | Meaning |
| --- | --- | --- |
| `DUPLICATE` | warning | Same pattern appears twice in one file; the earlier copy is dead |
| `DEAD_NEGATION` | warning | A negation cannot override the exclusion it answers (usually wrong order) |
| `BROAD_RULE` | warning | An unanchored basename rule matches at every depth, including nested dirs |
| `SELF_EXCLUDE` | warning | A rule inside a directory also matches that directory's own ignore file |
| `TRACKED_IGNORED` | warning | Tracked file matches an ignore rule; git keeps it anyway |

Exit codes: `0` clean, `1` findings present, `2` usage error. `audit` and `lint`
return `1` on warnings too, so they gate CI cleanly.

## Semantics model

The engine reproduces git's documented behaviour:

- A leading `/` anchors a pattern to its `.gitignore`'s directory and is not part of the match. A `/` anywhere else also anchors. A single trailing `/` does **not** anchor, but restricts matching to directories.
- A leading `**` un-anchors the pattern entirely: `**/x` and `**/a/b` behave like `x` and `a/b` at any depth.
- `**` crosses `/` only as a standalone path component — when followed by `/` or sitting at the end of the pattern. `a/**/z` crosses; `a**z` behaves like `a*z`.
- `*` and `?` never cross `/` (`FNM_PATHNAME`). `[!abc]` is POSIX class negation.
- A backslash escapes the next character, including a leading `!` or `#`. A malformed `[abc` and a trailing `\` never match — git aborts the match.
- **Last match wins.** Rules are ordered by file depth then line number, and depth is what makes a nested `.gitignore` override the root.
- **Directory pruning.** If the nearest excluded ancestor is excluded as a directory (`build`, `build/`, or `dist/`), git never descends into it. A negation written for a path inside is unreachable — `ignordr` reports this separately as `parent_excluded` rather than as an ordering problem, because the fix is different: ignore the directory's *contents* instead of the directory.

The differential tests in `tests/test_git_parity.py` build real repositories, run the real `git check-ignore`, and compare verdicts. They are the correctness proof, not the unit tests.

## Development

```bash
pip install -e '.[dev]'
pytest
```

Zero runtime dependencies. Python 3.9+. Tests require `git` on PATH for the
differential suite (they skip cleanly without it).

## How it compares

- `git check-ignore -v` — one path at a time, single rule, no reasoning about your other rules, and it hides tracked files.
- `.gitignore` linters — mostly detect "this pattern could be a glob" typos, not last-match-wins, pruning, or tracked-file drift.
- ignoredebugger.com — a web tool that uploads your paths to a server. `ignordr` runs entirely on your machine; nothing leaves it.
- `pre-commit` hooks — good for enforcing style, blind to *why* a path is ignored.

`ignordr` is the local, read-only, explain-your-reasoning layer for a problem git deliberately keeps quiet.

## License

MIT — see [LICENSE](LICENSE).
