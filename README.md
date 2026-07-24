# oamx

**Read the OWASP Amass asset database and pipe it into everything else.**

Zero dependencies. Read-only. Works across Amass v4 and v5.

---

## The problem

This is the recon one-liner. Some version of it appears in every Amass tutorial,
blog post and cheat sheet written in the last five years:

```bash
amass enum -passive -d example.com -o subs.txt
httpx -l subs.txt -silent | nuclei -severity high,critical
```

Since Amass v5 (August 2025), it writes an **empty** `subs.txt`.

v5 moved results into an asset database and stopped populating the text output.
The scan works. The data is there. But `-o` produces nothing, httpx probes
nothing, nuclei finds nothing, and the pipeline **exits 0**. It doesn't fail —
it succeeds at doing nothing, quietly, on a schedule, possibly for months.

Amass is a producer with no consumer story. The documented way out is
`amass subs -names -d example.com`, which gives you a flat list of hostnames and
nothing else — no IPs, no netblocks, no ports, no provenance, no scoping, no
"what changed since yesterday". Everyone else is pasting `sqlite3 json_extract`
incantations out of GitHub issues.

`oamx` is the consumer.

```bash
oamx names -d example.com --resolved-only | httpx -silent | nuclei -severity high,critical
```

---

## Install

```bash
pipx install oamx          # or: pip install oamx
```

Python 3.10+. No runtime dependencies — deliberately. A recon pipeline that
breaks because of a transitive dependency resolution is worse than no tool.

---

## Quickstart

```bash
# What is actually in my database?
oamx doctor

# Names, scoped to one target, that actually resolve
oamx names -d example.com --resolved-only

# Everything Amass found listening, ready for httpx or nuclei
oamx targets -d example.com --urls

# What genuinely appeared in the last day
oamx names -d example.com --new --since 24h

# The whole picture, with provenance, for storage or an agent to reason over
oamx json -d example.com > assets.jsonl
```

`oamx doctor` is the one to run first when a scan "found nothing":

```
database        /home/you/.config/amass/amass.sqlite
layout          v5 (entities/edges)
provenance      yes
time filtering  SQL
assets          16

  FQDN                 7
  IPAddress            3
  URL                  1
  TLSCertificate       1
  Service              1
  Netblock             1
  AutonomousSystem     1
```

---

## Commands

| Command | Output |
| --- | --- |
| `names` | fully qualified domain names |
| `ips` | IP addresses (`--ipv4` / `--ipv6`) |
| `cidrs` | netblocks in CIDR notation |
| `asns` | autonomous system numbers |
| `urls` | discovered URLs |
| `certs` | TLS certificates |
| `services` | responding network services |
| `orgs` | organisations |
| `emails` | contact identifiers |
| `targets` | `host:port` pairs, or `--urls` for `scheme://host:port` |
| `dns` | `name<TAB>RRTYPE<TAB>target` triples |
| `json` | every matching asset as JSONL, with provenance |
| `graph` | every matching relation as JSONL |
| `stats` | counts by asset type |
| `doctor` | what the database is and what is in it |

## Filters

Every command takes the same set:

| Flag | Effect |
| --- | --- |
| `--db PATH` | database path (default: auto-discover the usual per-platform locations) |
| `-d, --domain` | scope to a root domain; repeatable and comma-separated |
| `--scope-depth N` | graph hops used to scope non-name assets (default 2, `0` disables) |
| `--since DUR` | last seen within `24h`, `7d`, `2w` … |
| `--new` | with `--since`, match *first* seen — genuinely new, not re-confirmed |
| `--resolved-only` | only names with a DNS record |
| `--source NAME` | only assets asserted by this Amass plugin |
| `--exclude-source NAME` | drop assets whose *only* sources are these |
| `--min-confidence N` | drop assets below this source confidence (0–100) |
| `--json` | JSONL with provenance instead of bare values |
| `--count` | just the number |
| `--fail-empty` | exit 1 if nothing matched, for CI |

---

## Why it works the way it does

Four decisions worth knowing about, because they change what you get back.

**It introspects the schema instead of pinning to a version.**
Amass has renamed its storage at least three times — v3 graph files, v4
`assets`/`relations`, v5 `entities`/`edges`, plus column churn inside v5 point
releases. `oamx` reads whatever tables and columns it finds and maps them onto
one logical model. Asset types that didn't exist when this was written still
come out with a usable value rather than raising. The adapter absorbs the churn
so your pipeline doesn't.

**Scoping follows the graph, but names are matched by suffix.**
`-d example.com` seeds on hostname suffix, then walks out `--scope-depth` hops
to pick up the IPs, netblocks and certificates attached to those names. But a
*hostname* reached by graph proximity is never pulled into scope. Without that
rule, one CNAME to a shared CDN drags the CDN's other customers into your
target list. Use `--scope-depth 1` to stay on directly-resolved addresses, or
`0` for names only.

**Rows that reduce to the same thing are merged.**
`API.Example.COM.` from a certificate SAN and `api.example.com` from a DNS
answer are two database rows and one host. `oamx` merges them: provenance is
unioned, the higher confidence wins, and the sighting window widens to cover
every contributing row. Merging happens *before* time and source filters, so a
host is never judged on only one of its sightings.

**Rediscovery is not discovery.**
`--new --since 24h` reports hosts first seen in that window. A host you've known
for a month, which a second data source just re-confirmed, is not new and won't
alert. That false-positive class is precisely what trains people to ignore their
monitoring.

---

## Output schema

`--json` emits one object per line, versioned so downstream code can depend on
it even as Amass changes underneath:

```json
{
  "schema": "oamx/1",
  "kind": "asset",
  "id": 3,
  "type": "FQDN",
  "value": "api.example.com",
  "first_seen": "2026-06-24 12:00:00.000000000+00:00",
  "last_seen": "2026-07-24 10:00:00.000000000+00:00",
  "sources": [{"name": "crtsh", "confidence": 95}, {"name": "DNS-IP", "confidence": 100}],
  "attrs": {"name": "api.example.com"}
}
```

`graph` emits `"kind": "edge"` records with `from`, `to`, `label`, and decoded
DNS record types (`rr_type: 1` becomes `rr_name: "A"`, because nobody wants to
grep for `5`).

---

## As a library

```python
from oamx.integrations import query, values

hosts = values("names", domains=["example.com"], resolved_only=True)
records = query("all", domains=["example.com"], since="7d")
```

For CrewAI:

```python
from oamx.integrations import crewai_tool

recon_agent = Agent(role="OSINT Analyst", tools=[crewai_tool()])
```

The tool is read-only by construction — it cannot start a scan or send traffic,
only report what Amass already collected. That is a useful property when an
agent is holding it.

---

## Known limits

Stated plainly, because a recon tool that overstates itself is worse than useless.

- **SQLite only.** Amass also supports PostgreSQL and Neo4j. The reader is
  written behind an interface so those are clean additions, but shipping
  untested database drivers would defeat the point.
- **Field names are verified for `FQDN`, `IPAddress` and `Service`** against the
  Open Asset Model documentation. The remaining ~18 asset types use a
  best-effort key list with a generic fallback. If one of them extracts the
  wrong field for you, that's a one-line fix in `model.py` and a very welcome
  issue.
- **The whole graph is loaded into memory** to make scoping and merging correct.
  Fine for target-scoped databases; if yours has millions of entities, this will
  want a streaming path.
- **Amass v3 graph files are not supported.** Only the SQLite asset database.

## Compatibility

| Amass | Layout | Status |
| --- | --- | --- |
| v5.x | `entities` / `edges` | tested |
| v4.x | `assets` / `relations` | tested |
| v3.x | graph files | not supported |

Both layouts are covered by fixtures built from the documented schemas, not from
guesses.

---

## Development

```bash
python3 -m unittest discover -s tests -v
```

50 tests, no dependencies, under a second. The suite has been mutation-tested:
17 deliberate regressions injected into the scoping, merging, provenance,
filtering and read-only guarantees, 17 caught. If you add behaviour, break it on
purpose first and check something goes red.

## Upstream

The gap this fills is arguably an Amass issue, not a third-party one. If the
project wants an `amass export` that emits structured, scoped, provenance-carrying
output, the schema in `oamx/model.py` is a reasonable starting point and this
tool should stop being necessary. That would be the good outcome.

Licensed Apache-2.0, matching the Amass ecosystem.
