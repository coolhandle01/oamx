# Contributing to oamx

Thanks for working on this. This file is the contributor surface: the checks every change must pass, the rules they must follow, and the invariants that hold across the codebase. If you are working with Claude Code, also read `CLAUDE.md` for the AI-specific instructions and the skill catalogue.

For what the tool does and how to install it, see `README.md`.

## The rule everything else is downstream of

**An empty result that exits 0 is the worst thing this tool can do.**

oamx exists because Amass v5 kept writing `subs.txt` and stopped putting anything in it. The scan worked, the data was there, the pipeline succeeded, and it found nothing — quietly, on a schedule, for months. Reproducing that failure mode inside oamx would be the joke writing itself, and it is easy to do by accident — a filter that matches one spelling of something Amass spells two ways discards everything, exits 0, and is indistinguishable from a target with nothing on it.

In practice: degrade rather than raise, degrade rather than silently drop, and treat a filter that can return nothing on well-formed input as a bug until a test says otherwise.

## Before you commit

Work inside a virtualenv with the dev extra installed. Without it `mypy` cannot resolve the optional framework imports and `pytest` has no coverage plugin.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Python 3.10 is the `requires-python` floor and CI runs 3.10 through 3.13. Then run the full stack locally, in this order. All five must pass before you push — never "push and let CI tell me":

```bash
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pylint oamx
.venv/bin/pytest --cov=oamx --cov-branch --cov-report=term-missing --cov-fail-under=93
.claude/hooks/test-hooks.sh
```

The coverage flags are spelled out rather than sitting in `addopts`, because the sdist ships this suite for downstream packagers and they have `pytest` but no reason to have `pytest-cov` - in `addopts` those flags hand them an argparse error instead of a test run. Bare `.venv/bin/pytest` works and skips the gate; CI always applies it. `mypy` runs `--strict` against `oamx/`. `pylint` is scoped to design and length checks and gates on a score, not on style — ruff owns style.

`ruff format` is deliberately **not** enforced. The source is hand-wrapped: aligned `argparse` calls, deliberate string continuations. Running the formatter rewrites ten files to no benefit. If that ever changes it should be its own PR with its own diff, never a drive-by.

Never push a change you have not actually executed.

## Test-first discipline

For any bug fix, the discipline is:

1. **Write the failing test first.** Re-run it and read the failure message — it must fail for the *right reason*.
2. **Commit that test on its own**, with the failure output in the commit message. The history should show the bug, not only its absence.
3. **Write the minimum code** to make it pass.
4. **Re-run everything.** Green.

A test that was never seen to fail has not been shown to test anything. Bug-fix PRs without a regression test are rejected.

### Branch coverage, not line coverage

The project floor is in `[tool.pytest.ini_options]` and catches drift. On the lines you write or change, the bar is higher: **every conditional has both branches exercised**. This codebase is mostly branching — schema candidates, filter guards, degrade-rather-than-raise fallbacks — and line coverage would call a half-tested `if` fully covered.

`--cov-report=term-missing` is already in the default invocation; cross-reference the missing branch numbers against your diff before pushing. If a branch is genuinely unreachable, mark it `# pragma: no cover` with a one-line reason. Silent suppression is the same anti-pattern as a bare `# noqa`.

### An assertion that passes on empty output is not an assertion

`assertEqual([], [])` is true. `x not in []` is true. In a tool whose defining failure is producing nothing, negative-only assertions are close to worthless.

```python
# weak - passes just as happily if the command returns nothing at all
assert "dev.example.com" not in names

# strong - pins what should be there as well as what should not
assert "www.example.com" in names
assert "dev.example.com" not in names
```

### Layout-sensitive behaviour is tested against both databases

If what you are testing touches an edge label, a column name, or anything the two Amass generations spell differently, take the `any_db` fixture or assert against `v5_db` and `v4_db` explicitly. Most of the suite is layout-agnostic, so it is easy to cover a lot against one database and leave the label-reading flags — the ones that actually differ — tested against neither.

## Universal rules

### Minimal diff

Change what the task requires. Reformatting, renaming, and reordering that you happen to prefer belong in their own commit, or nowhere.

### Preserve names, comments, and structure unless the change is the task

The comments in this codebase explain decisions, not mechanics. If you are about to delete one, you are either fixing the decision it documents — say so — or losing the only record of why the code is shaped that way.

### Linter findings are engineering signal

Each finding is a tool flagging an assumption it could not verify. Before suppressing:

1. Read the finding. What is the tool worried about?
2. Identify the unstated assumption. Why is the code actually correct?
3. Prefer making that assumption explicit over silencing the warning.
4. If you must suppress, the suppression carries a one-line reason. No bare `# noqa`, `# type: ignore`, or `# pylint: disable`.

```python
# acceptable - the why is in the comment
created = row["created"] if "created" in row.keys() else None  # noqa: SIM118

# not acceptable
created = row["created"] if "created" in row.keys() else None  # noqa
```

**Autofixes are suggestions, not corrections.** Ruff's SIM118 offers to rewrite `"created" in row.keys()` to `"created" in row`. `row` is a `sqlite3.Row`, and `Row.__contains__` iterates *values*: the rewrite is False whenever the column exists, nulls out every timestamp, and silently breaks `--since`, `--new` and the merge window. Check the receiver's actual type before accepting a simplification.

The flip side: a suppression in unfamiliar code is the codebase telling you where a load-bearing assumption lives. Read the reason before changing anything that depends on it.

### Pylint says split, not suppress

Pylint is scoped in `[tool.pylint]` to the design and length rules that fire when a module or function outgrows one responsibility. Its limits are set as a **ratchet** — just above the current shape — so they catch growth rather than demanding an immediate refactor. When it fails on code you edited, split the unit. Suppression is for when the unit genuinely is one thing.

Two places already sit near their limits and would benefit most from decomposition: `AssetDB._introspect` and `AssetDB.edges`.

### FIXME and TODO grammar

State what is wrong and what would fix it, not that something is wrong.

```python
# FIXME: assets() and its SQL time-pushdown branch have no callers; doctor
#        still advertises "time filtering SQL" for a path no query uses.
# TODO: ship tests/ in the sdist so downstream packagers can run the suite.
```

### Surface concerns, do not silently override

If a change looks wrong, say so in the PR and then do the work as asked. Quietly narrowing the scope is worse than disagreeing out loud.

## Safety invariants — do not weaken

These exist for reasons that are not obvious from the code alone.

- **Read-only database access.** `AssetDB` opens `file:...?mode=ro`. People point this at a database while Amass is mid-enumeration, and "cannot start a scan or send traffic" is a stated property of the CrewAI tool. `test_opens_read_only` pins it.
- **Suffix matching in `in_domain`**: `v == d or v.endswith("." + d)`. The explicit dot is what stops `notexample.com` matching `example.com`.
- **Hostnames are never scoped in by graph proximity.** `compute_scope` walks the graph for infrastructure but re-filters FQDNs by suffix at the end. Without it one CNAME to a shared CDN drags that CDN's other customers into the target list — hosts the user has no authorisation to scan.
- **Filter order around the merge.** Scope and resolution are row properties and run before `merge_assets`; time and provenance are properties of the thing and run after. Reversing either produces a silent false negative.
- **`.keys()` on a `sqlite3.Row`.** See above. It is not a dict.
- **Zero runtime dependencies.** `pyproject.toml` declares none and the `no-deps` CI job proves it by installing the built wheel alone and running the CLI. Framework adapters import lazily inside the function that needs them.
- **`extract_value` never raises.** An asset type invented after this release still has to come out with a usable value.

## Branches and commits

Branch from current `main` as `<type>/<short-description>`; do not work on `main`. Commits are [Conventional Commits](https://www.conventionalcommits.org/) — CI checks them with `cz check`, and `cz bump` derives the version from them.

Never force-push, `push --delete`, or `branch -D` a shared or PR branch without explicit authorisation in the immediately preceding message.

Never put session URLs (`https://claude.ai/code/session_...`) in a commit message or a pull request description. They link to a private AI-assistant conversation, and a public repository is a durable record — indexed, forked, and mirrored — so a pasted session link leaks that conversation the day the repo, a fork, or an upstream breach exposes it. The plain `https://claude.ai/code` attribution link carries no session id and is fine; the `session_...` path is the part that must never land in git history.

## Pull requests

If you push a branch, open a pull request for it, and stay subscribed so review comments and CI events reach you. The repository has a pull request template; fill it in rather than deleting it.

## Where to find more

- `README.md` — what the tool does, commands, filters, output schema
- `CLAUDE.md` — AI-contributor instructions, the skill catalogue, and the release flow
- `.claude/skills/` — per-module conventions, auto-loaded on the first matching edit
