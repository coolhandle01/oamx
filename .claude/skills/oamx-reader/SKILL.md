---
name: oamx-reader
description: reader.py introspects whatever Amass schema it is handed instead of pinning to a version, opens the database strictly read-only, and parses timestamps forgivingly. New layouts are supported by adding candidates to a list, never by branching on a version number. Load before editing oamx/reader.py.
---

# oamx reader discipline

`reader.py` is the adapter that absorbs Amass's schema churn so nothing downstream has to know about it. A v4 database (`assets`/`relations`) and a v5 database (`entities`/`edges`) come out of here looking identical.

## Introspect, never pin

The module reads `sqlite_master` and `PRAGMA table_info`, then maps whatever it finds onto one logical schema through `_pick`, which takes an ordered candidate list and returns the first name actually present (case-insensitively).

Supporting a new Amass release means **adding a candidate**, not adding a branch:

```python
# correct - a new column name joins the candidate list
"created": ("created_at", "first_seen", "created", "discovered_at"),

# wrong - now there are two code paths to keep correct forever
if self.generation == "v6":
    created_col = "discovered_at"
```

Candidate order is preference order, so put the newest spelling first. `self.generation` exists for `doctor` to print and for nothing else; do not make behaviour depend on it.

The same principle governs failure. If the entity table is missing entirely, raise `OamxError` naming the file and the tables that *were* found — a user pointed at the wrong SQLite file needs to see that, not a `KeyError`. If a column is merely unrecognised, the message asks them to open an issue with the column list, which is the fastest path to a new candidate entry.

## Read-only is a promise, not a default

```python
uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
self.conn = sqlite3.connect(uri, uri=True)
```

People point this at a database while Amass is mid-enumeration. A write lock or a corrupted asset store would be a far worse outcome than anything oamx could usefully do with write access — and "it cannot start a scan or send traffic" is a stated property of the CrewAI tool, which read-only access is part of.

`test_opens_read_only` pins it. Do not relax the URI, do not add a write path, do not add a `mode=rw` escape hatch for convenience.

**`sqlite3.connect` is lazy.** A file that is not a database opens without complaint and only fails on the first query, which is the one inside `_introspect`. `__init__` wraps that call and re-raises as `OamxError`, because unwrapped it reaches the user as a bare `sqlite3.DatabaseError` traceback — `main` catches `OamxError` only — and silently aborts `open_db`'s discovery loop, which skips `OamxError` only. Discovery globs `*.sqlite` / `*.sqlite3` / `*.db` and searches the working directory, so a stray file with one of those names is ordinary, not exotic. Any new query path that can run before the caller has a usable `AssetDB` needs the same treatment.

## Parse timestamps forgivingly

`_parse_ts` handles what GORM emits across versions: Go nanosecond precision that `datetime.fromisoformat` rejects, `Z` suffixes, space-separated dates, bare epochs, and `None`. It returns `None` rather than raising on anything it cannot read.

That `None` is not an error signal — callers treat it as "keep this asset" (see `oamx-select`). Dropping data because a date would not parse is exactly the silent false negative this tool exists to prevent. If you extend the parser, keep the `None` return; do not start raising.

Naive datetimes are stamped UTC on the way out. Everything downstream compares aware datetimes, so an accidental naive return is a `TypeError` at a distance.

## Query hygiene

- **Chunk `IN` clauses at 900.** `SQLITE_MAX_VARIABLE_NUMBER` defaults to 999 on older builds; `_load_sources` and `assets_by_id` both chunk. Any new bulk lookup does too.
- **Bulk-load, do not query per row.** Provenance for a whole selection is one query per chunk, not one per asset. The graph is loaded once in `select.build` and derived from thereafter.
- **Interpolated identifiers, bound values.** Table and column names come from introspection so they must be interpolated, and they are always wrapped in double quotes. Anything originating from user input is a `?` parameter. Never interpolate a value.
- **Only `SourceProperty` tags are provenance.** The tag tables carry `SimpleProperty` and `VulnProperty` rows too, and a `VulnProperty` has a `name` key that looks exactly like a source name if you are not checking `ttype`. There is a test for this.

## Rows are not dicts

`self.conn.row_factory = sqlite3.Row`, and a `Row` only looks dict-like. It supports `row["col"]` and `row.keys()`, but `__contains__` iterates **values**, not keys:

```python
"created" in row.keys()   # True  - the column exists
"created" in row          # False - there is no *value* equal to "created"
```

So the membership test before reading an optional column has to go through `.keys()`. Ruff's SIM118 flags that as `key in dict.keys()` and offers to rewrite it; taking the offer nulls out `first_seen` and `last_seen` on every asset and silently breaks `--since`, `--new` and the merge window. The two call sites carry `# noqa: SIM118` and a comment saying why. Leave them.

The general shape: when a linter suggests a simplification, check the receiver's actual type first. `Row` is the one in this module that will bite.

## Dangling references are normal

Amass leaves edges pointing at entities that are not there. `edges()` skips a row whose endpoints do not both resolve rather than raising or synthesising a placeholder. Keep that behaviour for any new relation-shaped read.

## Anti-patterns

- Branching on `self.generation` to choose a column, table or behaviour.
- Any connection not opened with `mode=ro`.
- `_parse_ts` raising, or a caller treating its `None` as "drop this".
- An unchunked `IN (...)` built from a set of ids.
- Assuming `content` is a dict without checking — it is parsed from a JSON text column and can be anything, including `null` or a bare string. Both `_row_to_asset` and `edges()` guard for this.
- Treating a `sqlite3.Row` as a dict, in particular `"col" in row`. See above; the linter will suggest it and it is wrong.
- Adding a discovery path to `default_db_paths` that follows symlinks or globs recursively. It is a convenience for the common install layout, not a filesystem search.
