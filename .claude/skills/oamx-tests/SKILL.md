---
name: oamx-tests
description: pytest with branch coverage, built on two synthetic Amass databases shared through conftest fixtures. Layout-sensitive behaviour is asserted against both generations, and an assertion that still passes on empty output is not an assertion. Load before editing anything under tests/.
---

# oamx test discipline

```bash
.venv/bin/pytest                       # the suite
.venv/bin/pytest --cov=oamx --cov-branch --cov-report=term-missing --cov-fail-under=93
```

The coverage flags are deliberately not in `addopts`. The sdist ships this suite so a downstream packager can verify the build they are shipping, and a packager has `pytest` but no reason to have `pytest-cov` - in `addopts` those flags give them an argparse error rather than a test run. So bare `pytest` runs the suite, and the gate lives in the invocation that CONTRIBUTING and `tests.yml` both use. Dev tooling comes from `pip install -e ".[dev]"`.

Branch coverage rather than line coverage is deliberate. This codebase is mostly branching — schema candidate lists, filter guards, degrade-rather-than-raise fallbacks — and line coverage would call a half-tested `if` fully covered. On lines you write or change the bar is both branches exercised, not the project floor.

## Fixtures come from conftest

`tests/conftest.py` exposes the databases; `tests/fixtures.py` builds them.

| Fixture | What it is |
|---|---|
| `v5_db` | Amass v5: `entities`/`edges`, provenance tags, port relations |
| `v4_db` | the same entities in the v4 layout: label *is* the record type, no port relations, no provenance |
| `any_db` | parametrized over both — take this for anything layout-sensitive |
| `empty_db` | well-formed v5 schema, no rows: the "scan found nothing" case |
| `garbage_db` | a SQLite file that is not an Amass database |
| `frozen_now` | pins `cli.datetime.now()` to `fixtures.NOW` |
| `readonly_conn` | a raw read-only connection, for tests about the connection itself |

Both databases are built once per process via `fixtures.shared_databases()`, so asking for them is cheap. They are read-only to every test; nothing writes to them.

New tests are plain functions taking fixtures. The `unittest.TestCase` classes in `test_oamx.py` run natively under pytest — convert them opportunistically, not in one sweep. A mechanical rewrite of fifty assertions is a good way to weaken a suite without noticing.

## Extend the shared dataset, do not build your own database

Add rows to `ENTITIES` / `EDGES` / `SOURCES` in `fixtures.py` rather than standing up a bespoke database inside a test. Both builders pick the change up and the parity tests keep working. The dataset already carries deliberate awkwardness — reuse it:

- `dev.example.com` (id 4) never resolves — for `--resolved-only`
- `API.Example.COM.` (id 13) duplicates `api.example.com` — for normalisation and merge
- `other.co.uk` (id 11) is a different target — for scope leakage
- `cdn.provider.net` (id 15) is a CNAME target outside scope — for the CDN rule
- `SomeFutureAssetType` (id 16) is a type this release has never heard of
- edge 15 dangles from a nonexistent entity
- a `VulnProperty` tag whose content has a `name` key, to catch provenance code that does not check `ttype`

## Layout-sensitive behaviour is tested against both databases

If the behaviour touches an edge label, a column name, or anything the two generations spell differently, take `any_db`.

Most of the suite is layout-agnostic, which makes this easy to skip: cover layout detection, `dns` and plain `names` against one database and everything looks tested. The flags that read edge labels are the ones that fail, and they fail by returning nothing and exiting 0.

```python
def test_names_are_scoped_on_either_layout(any_db):
    names = _cli("names", "--db", str(any_db), "-d", "example.com")
    assert "www.example.com" in names
    assert "other.co.uk" not in names
```

or assert the two agree explicitly when the point *is* the agreement:

```python
def test_resolved_only_agrees_across_layouts(v5_db, v4_db):
    v5_names = _cli("names", "--db", str(v5_db), "-d", "example.com", "--resolved-only")
    v4_names = _cli("names", "--db", str(v4_db), "-d", "example.com", "--resolved-only")
    assert "www.example.com" in v5_names        # <- contents, not just agreement
    assert v4_names == v5_names
```

Use `@pytest.mark.parametrize` rather than looping inside a test. The parameter shows up in the test id, so a failure names the case:

```
FAILED test_resolved_fqdns_accepts_every_dns_label_spelling[cname_record]
```

## An assertion that passes on empty output is not an assertion

`assert [] == []` is true. `assert x not in []` is true. In a tool whose defining failure mode is producing nothing, negative-only assertions are close to worthless.

```python
# weak - passes just as happily if the command returns nothing at all
assert "dev.example.com" not in names

# strong - pins what should be there as well as what should not
assert "www.example.com" in names
assert "dev.example.com" not in names
```

Every filter test asserts something survives the filter, not only that something was removed.

## Test through the CLI where the CLI is the contract

`_cli(...)` runs the parser, filter translation, selection and emission in one go and returns the non-empty stdout lines — prefer it for anything a user can observe. Drop to a unit test when you are pinning a specific rule (`resolved_fqdns` accepting every label spelling, `_parse_ts` on a malformed stamp) and a CLI test would only tell you *that* something broke.

## Write the failing test first

Commit it on its own, with the failure output in the commit message, before the fix. A test that was never seen to fail has not been shown to test anything.

When you change something load-bearing, the strongest check is to break it on purpose and confirm the suite goes red. Re-introducing the `dns_record` label bug should fail `test_resolved_only_agrees_across_layouts` and three of the four parametrized label cases.

## Anti-patterns

- A bespoke SQLite database built inline instead of extending `fixtures.py`.
- Layout-sensitive behaviour asserted against `v5_db` only.
- A test whose assertions all still pass when the command returns nothing.
- A loop inside a test where `parametrize` would name the failing case.
- Computing a time window from `datetime.now()` instead of taking `frozen_now`.
- Writing to the shared databases. They are session-scoped and opened read-only.
- Asserting the full text of an `OamxError`. Assert the substring a user would search for.
- Adding a test dependency without adding it to the `dev` extra in `pyproject.toml`.
