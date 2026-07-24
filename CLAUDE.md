# oamx - AI Contributor Guide

## The rule the whole codebase is downstream of

**An empty result that exits 0 is the worst thing this tool can do.**

oamx exists because Amass v5 kept writing `subs.txt` and stopped putting anything in it. The scan worked, the data was there, the pipeline succeeded, and it found nothing — for months, quietly, on a schedule. Every design decision in this repository is a reaction to that failure mode, and reproducing it inside oamx would be the joke writing itself.

Concretely, that means:

- **Degrade, do not raise.** An asset type invented after this release still has to come out with a usable value. `extract_value` falls through to a generic key list and then to compact JSON rather than throwing.
- **Degrade, do not silently drop.** An unparseable timestamp keeps the asset (`_time_ok` returns `True` on `None`). Dropping data because a date would not parse is a false negative, which is the thing we are here to prevent.
- **A filter that matches nothing is a bug until proven otherwise.** If a new filter can return an empty set on well-formed input, it needs a test that would notice.

The one shipped bug in this repository's history was `--resolved-only` matching v5's `dns_record` edge label exactly, so it discarded every hostname in a v4 database and exited 0. That is the shape to watch for.

## Before you start work

The skills below auto-load on the first matching edit, which is too late — by then you have already chosen an approach. Read the relevant skill when you are scoping the change, while you are still deciding what to build.

Then ask what canonical knowledge the change produces that is not yet in a skill, and update the skill in the same branch.

## Git essentials

- **Branch**: cut from current `main`, named `<type>/<short-description>` where `<type>` matches the commit type (`feat/`, `fix/`, `docs/`, `chore/`, `refactor/`). Do not work on `main`.
- **Commit**: Conventional Commits — `<type>(<scope>)?: <subject>`, lowercase imperative subject.
- **Test-first.** For a bug fix, commit the failing test on its own first, with the failure output in the commit message, then commit the fix. The history should show the bug, not just its absence.
- **Before you push**: `python3 -m unittest discover -s tests` must be green, and `git diff origin/main --stat` should show only what you meant to change.
- **Never** force-push, `push --delete`, or `branch -D` a shared or PR branch without explicit plain-words authorisation in the immediately preceding message. `--force-with-lease` is no exception.
- Never put session URLs (`https://claude.ai/code/session_...`) in commit messages or PR bodies — they reference private conversations.

## Releasing

Version numbers are derived from commit messages, not chosen by hand, which is the other reason Conventional Commits are enforced in CI.

```bash
cz bump          # reads the commits since the last tag, decides the increment
git push --follow-tags
```

`cz bump` updates `[project].version`, `oamx/__init__.py:__version__` and `CHANGELOG.md` together, commits, and creates an annotated `vX.Y.Z` tag. `major_version_zero` keeps a breaking change from jumping 0.x straight to 1.0.

Pushing that tag is the only thing that triggers `release.yml`: build, `twine check`, install the built wheel on 3.10 and 3.13 and check the console script runs, publish to PyPI, then cut a GitHub release. Nothing publishes on an ordinary push or pull request.

PyPI upload uses Trusted Publishing, so there is no API token in the repository — the `pypi` environment and the publisher entry on PyPI have to exist first.

## Invariants that are not negotiable

- **Zero runtime dependencies.** `pyproject.toml` declares none, the test suite is stdlib `unittest`, and CI installs nothing. A recon pipeline that breaks on a transitive dependency resolution is worse than no tool. Framework adapters (`integrations.crewai_tool`) import lazily inside the function.
- **The database is opened read-only**, via a `file:...?mode=ro` URI, because people will point this at a database while Amass is mid-enumeration. There is a test that a `DELETE` raises. Do not relax it.
- **oamx never sends traffic.** It reads what Amass already collected. This is the property that makes it safe to hand to an agent, and it is stated as a promise in the README and the tool description.
- **Python 3.10 is the floor** (`requires-python`), and CI runs 3.10 through 3.13.

## Skills

Skills under `.claude/skills/` auto-load via a `PreToolUse` hook on `Write`/`Edit` wired in `.claude/settings.json`; the matching lives in `.claude/hooks/load-skill.sh`. A skill is injected once per session, on the first matching edit.

| Skill | Triggers on | Carries |
|---|---|---|
| `oamx-model` | `oamx/model.py` | The stable output contract — `SCHEMA_VERSION`, value extraction that never raises, merge semantics, and the `Edge` properties that absorb layout differences |
| `oamx-reader` | `oamx/reader.py` | Schema tolerance — introspect rather than pin, candidate column lists, read-only access, forgiving timestamp parsing |
| `oamx-select` | `oamx/select.py` | Scoping and filter semantics — suffix-matched names, graph-walked everything else, and the order filters must run in |
| `oamx-cli` | `oamx/cli.py`, `oamx/integrations.py`, `oamx/__main__.py` | The pipe contract — stdout is data, exit codes, and keeping the CLI and library surfaces from drifting apart |
| `oamx-tests` | anything under `tests/` | Fixture discipline, and the rule that layout-sensitive behaviour is asserted against both database generations |

One skill per file, no stacking. Where a rule spans modules the skills cross-reference each other rather than both claiming it.

The hook only fires for files inside this repository. That guard matters: a session can hold more than one repo, and a bare `tests/*` pattern would otherwise pull oamx's skills into an unrelated project's test edits.

A second hook, `session-start.sh`, runs at `SessionStart` and reports the one thing this file cannot: which branch you are actually on. "Do not work on `main`" is only actionable if you know you are on `main`, and by the time an edit reveals it you have already made it.

Both hooks fail open — missing `jq`, unexpected stdin, or an absent skill file all exit 0 with no output. A `PreToolUse` hook that failed closed would block the edit it was meant to inform. That also makes silent breakage their natural failure mode, so they carry their own tests:

```bash
.claude/hooks/test-hooks.sh
```

Bash and `jq`, no other dependencies, and CI runs it on every pull request alongside `shellcheck`. If you change a hook, change its tests.

If a hook does not fire in your session, run `/hooks` once or restart — the watcher only sees `.claude/settings.json` if it existed at session start. You can always load a skill by name with the `Skill` tool.
