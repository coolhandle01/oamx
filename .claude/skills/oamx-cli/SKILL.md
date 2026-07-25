---
name: oamx-cli
description: The CLI is a pipe citizen - stdout carries data and nothing else, exit codes mean specific things, and a broken pipe is normal. Every command shares one filter parser, and the CLI and library surfaces must gain features together or they drift. Load before editing oamx/cli.py or oamx/integrations.py.
---

# oamx output contract

`oamx` exists to sit in the middle of a shell pipeline. Everything about the output surface follows from that.

## stdout is data. Full stop.

Results go to stdout, one per line, nothing else. Diagnostics, warnings and errors go to stderr via `warn()`. A progress message on stdout becomes a hostname that `httpx` tries to resolve.

```python
# correct
warn("no assets matched")

# wrong - this is now a line in somebody's target list
print("no assets matched")
```

`doctor` and `stats` are the deliberate exceptions: they are human-facing reports, not pipe sources, and they say so by not being pipe-shaped.

Output is sorted and deduplicated before it is printed. `oamx names` run twice on an unchanged database produces byte-identical output — that is what makes `diff` a viable monitoring tool.

## Exit codes mean things

| Code | Meaning |
|---|---|
| 0 | ran successfully, including "matched nothing" unless `--fail-empty` |
| 1 | an `OamxError` the user needs to read, or `--fail-empty` with no matches |
| 2 | no subcommand given; argparse prints usage |
| 130 | interrupted |

`BrokenPipeError` returns 0, because `oamx names | head -5` is a normal thing to do and is not a failure.

Note the default: matching nothing is **success**, and the user opts into strictness with `--fail-empty`. That is the right default for a pipe stage, but it is also the exact shape of the bug this tool was written to fix, which is why `--fail-empty` exists and why `doctor` returns 1 on an empty database. Any new command honours `--fail-empty` — `cmd_stats` currently does not under `--json`, and that is a bug, not a precedent.

## One filter parser, shared

Every command inherits the `common` parent parser, so `--db`, `-d`, `--since`, `--source`, `--json` and the rest behave identically everywhere. Add a filter once, to `common`, and it works on every subcommand.

Command-specific flags go on the individual subparser (`--ipv4` on `ips`, `--urls` on `targets`). If a flag would make sense on more than two commands, it belongs in `common`.

`make_filters` is the single translation point from `argparse.Namespace` to `Filters`, and it uses `getattr(args, ..., default)` throughout so a subparser that lacks a flag does not explode. Keep that.

## Keep the CLI and the library in step

`integrations.query` is the programmatic front door — scripts and agent frameworks want data back, not a subprocess. It ships no framework adapter and should not grow one: oamx reads an Amass database, and a caller wanting to hand that to an LLM has their own conventions for tool schemas, return types and error handling. Guessing at those means an optional dependency and a wrong guess. The consumer builds the adapter.

The two surfaces used to keep parallel tables, and they drifted: `emails` was in `cli.TYPE_COMMANDS` and missing from `integrations.VIEWS`, so it worked on the command line and raised `unknown view` from the library.

There is now one table, `model.VIEW_TYPES`, and both derive from it — `TYPE_COMMANDS` filters out the library-only `all`, and `VIEWS` is the table itself. Add a view there and both surfaces get it. `TestNoMoreDrift` pins that they stay in step.

**A view is a set of asset types plus, sometimes, a predicate.** Type selection alone could not express `emails`: an email is an `Identifier` *whose `id_type` says so*, and that one asset type carries every other scheme OAM knows — handles, tickers, tax ids, IBANs. `model.in_view(view, asset)` is where that narrowing lives, and both surfaces apply it. If a new view needs more than a type list, extend `in_view` rather than filtering in one caller.

## Structured output

`--json` emits newline-delimited JSON, one object per line, each carrying `schema` and `kind`. Never emit a JSON array — a consumer should be able to `tail -f` the output and parse line by line. `separators=(",", ":")` keeps it compact.

New record kinds get a `kind` value (`asset`, `edge`, `target`, `stats`) and the current `SCHEMA_VERSION`. The shape rules live in `oamx-model`.

## Anti-patterns

- Anything other than results on stdout.
- A new subcommand that does not take `parents=[common]`.
- Adding to `TYPE_COMMANDS` without adding to `VIEWS`.
- Unsorted or duplicated output.
- A JSON array instead of JSONL.
- Catching `OamxError` anywhere but `main`. Raise it with a message the user can act on and let the single handler print it.
- Importing an agent framework anywhere in `integrations.py`, at module scope or lazily. Zero runtime dependencies is a promise in `pyproject.toml`, and the adapter is the consumer's to write.
