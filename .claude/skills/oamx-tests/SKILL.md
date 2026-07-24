---
name: oamx-tests
description: The suite is stdlib unittest with no dependencies, built on two synthetic Amass databases. Anything that reads an edge label or a column name is asserted against both layouts, and an assertion that can pass on empty output is not an assertion. Load before editing anything under tests/.
---

# oamx test discipline

The suite is stdlib `unittest`, no dependencies, under a second:

```bash
python3 -m unittest discover -s tests -v
```

That is not minimalism for its own sake. `pyproject.toml` promises zero runtime dependencies and CI installs nothing, so a suite that needed `pytest` would stop proving the thing it is there to prove.

## Extend the shared fixtures, do not build your own database

`tests/fixtures.py` builds two synthetic databases from one shared dataset — `ENTITIES`, `EDGES`, `SOURCES` — reproducing the documented storage layouts:

| Builder | Layout | Notes |
|---|---|---|
| `build_v5` | `entities` / `edges` | labels in the content blob, numeric `header.rr_type`, `entity_tags` provenance |
| `build_v4` | `assets` / `relations` | label *is* the relation type (`a_record`), no port relations, no provenance |
| `build_empty` | v5 schema, no rows | the "scan found nothing" case |
| `build_garbage` | not Amass at all | the "wrong file" case |

Add rows to the shared lists rather than standing up a bespoke database in your test. Both builders then pick the change up, and the v4/v5 parity tests keep working. The dataset already carries deliberate awkwardness — reuse it:

- `dev.example.com` (id 4) never resolves — for `--resolved-only`
- `API.Example.COM.` (id 13) duplicates `api.example.com` — for normalisation and merge
- `other.co.uk` (id 11) is a different target — for scope leakage
- `cdn.provider.net` (id 15) is a CNAME target outside scope — for the CDN rule
- `SomeFutureAssetType` (id 16) is a type this release has never heard of
- edge 15 dangles from a nonexistent entity
- a `VulnProperty` tag whose content has a `name` key, to catch provenance code that does not check `ttype`

Timestamps come from the frozen `NOW` / `RECENT` / `MID` / `OLD` constants, via `ts()` which pads microseconds out to Go's nanoseconds. For anything time-dependent, freeze the clock rather than computing from the real one:

```python
frozen = mock.MagicMock()
frozen.now.return_value = fixtures.NOW
with mock.patch.object(cli, "datetime", frozen):
    ...
```

## Layout-sensitive behaviour is tested against both databases

**This is the rule the suite was missing when `--resolved-only` shipped broken.**

The v4 coverage exercised layout detection, `dns` and plain `names` — none of which depend on how a DNS edge is labelled. The one flag that did was only ever tested against v5, so it returned nothing on v4 and exited 0.

If the behaviour under test touches an edge label, a column name, or anything else the two generations spell differently, assert it against both fixtures:

```python
def test_something_agrees_across_layouts(self):
    v5_out = self.run_cli("names", "--db", str(V5), "-d", "example.com", "--flag")
    v4_out = self.run_cli("names", "--db", str(V4), "-d", "example.com", "--flag")
    self.assertIn("www.example.com", v5_out)   # <- contents, not just agreement
    self.assertEqual(v4_out, v5_out)
```

Use `subTest` when looping over layouts or labels so one failure does not mask the rest.

## An assertion that passes on empty output is not an assertion

`assertEqual([], [])` is true. `assertNotIn(x, [])` is true. In a tool whose defining failure mode is producing nothing, negative-only assertions are close to worthless.

```python
# weak - passes just as happily if the command returns nothing at all
self.assertNotIn("dev.example.com", names)

# strong - pins what should be there as well as what should not
self.assertIn("www.example.com", names)
self.assertNotIn("dev.example.com", names)
```

Every filter test asserts something survives the filter, not only that something was removed.

## Test through the CLI where the CLI is the contract

`CliCase` gives you two runners: `run_cli` returns non-empty stdout lines and asserts the exit code was 0 or 1; `run_cli_full` returns `(code, stdout, stderr)` when the code or the stderr text is the thing under test.

Prefer the CLI-level test for anything a user can observe — it covers argument parsing, filter translation, selection and emission in one go. Drop to a unit test when you are pinning a specific rule (`resolved_fqdns` accepting every DNS label spelling, `_parse_ts` on a malformed stamp) and the CLI would only tell you *that* something broke.

## Write the failing test first

For a bug fix, commit the failing test on its own, with the failure output in the commit message, then commit the fix. The history should show the bug rather than only its absence, and a test that was never seen to fail has not been shown to test anything.

## Anti-patterns

- `pytest`, `hypothesis`, or any other dependency.
- A bespoke SQLite database built inline instead of extending `fixtures.py`.
- Layout-sensitive behaviour asserted against `V5` only.
- A test whose assertions all still pass when the command returns nothing.
- Computing a time window from `datetime.now()` instead of freezing the clock.
- Writing to the fixture databases. They are shared across the module and opened read-only.
- Asserting on the full text of an `OamxError`. Assert the substring a user would search for.
