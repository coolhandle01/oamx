---
name: oamx-select
description: select.py decides what a user actually asked for. Hostnames are scoped by suffix and never by graph proximity, filters run in a specific order around the merge step, and a filter that cannot read its input keeps the asset rather than dropping it. Load before editing oamx/select.py.
---

# oamx selection semantics

`select.py` turns a whole asset database into the subset the user asked for. Every rule here exists because the obvious implementation produced a wrong answer that looked right.

## Scoping: names by suffix, everything else by graph

`compute_scope` seeds on FQDNs whose value is `-d` or a subdomain of it, then walks `--scope-depth` hops to pick up the IPs, netblocks, services and certificates hanging off those names.

Then it does the thing that matters:

```python
# A neighbouring FQDN pulled in by graph proximity is not automatically in
# scope - that is how you end up scanning a shared CDN's other customers.
return {
    aid for aid in scope
    if assets[aid].type != "FQDN" or in_domain(assets[aid].value, domains)
}
```

Without that final filter, one CNAME to a shared CDN drags every other customer of that CDN into the target list, and the user scans hosts they have no authorisation for. **A hostname is in scope if and only if it matches by suffix.** Graph reach adds infrastructure, never names.

Depth is a blunt instrument by design: `2` reaches netblocks through addresses, `1` stays on directly-resolved addresses, `0` is names only. If you need finer control, add a flag — do not make depth mean different things for different asset types.

`in_domain` normalises both sides before comparing, and matches `v == d or v.endswith("." + d)`. The explicit dot is what stops `notexample.com` matching `example.com`.

## Filter order is the design

`Selection.matching` runs filters in a specific order around `merge_assets`, and the split is not arbitrary:

**Properties of a database row, applied before the merge, against ids:** type, scope, resolution. A row either is or is not in the graph neighbourhood; that question is meaningless once rows are collapsed.

**Properties of the thing, applied after the merge, against values:** time window, sources, confidence. A host discovered twice must be judged on all of its sightings — otherwise `--since 24h` drops a host because one of its two rows is stale, and `--exclude-source brute-forcing` discards a name that certificate transparency independently confirmed.

If you add a filter, decide which kind it is before you decide where to put it. Getting this wrong produces a false negative that no one notices, because the output still looks plausible.

## Failing open is the default

```python
# Unparseable or absent timestamps are kept: dropping assets because we
# could not read a date is a silent false negative, which is the failure
# mode this whole tool exists to prevent.
return ts is None or ts >= f.since
```

Every filter in this module keeps the asset when it cannot evaluate its own condition. A user who over-scans loses a little time; a user who under-scans misses the host that was going to be the finding, and never learns they missed it.

`_source_ok` follows the same instinct in a subtler way: `--exclude-source` drops an asset only when **every** source is excluded (`names <= set(f.exclude_sources)`). A name that brute force guessed *and* crt.sh confirmed is a real name, and the corroborating evidence wins.

## Edge labels belong to the model

`resolved_fqdns` asks `Edge.is_dns`, not `e.label == "dns_record"`. v4 names its DNS edges after the record type (`a_record`, `cname_record`), so matching one spelling makes `--resolved-only` discard every hostname in a v4 database and exit 0. That was the one shipped bug in this repository.

The general rule lives in `oamx-model`: version-specific spellings are absorbed by `Edge` properties, and consumers use the properties. If a new filter needs to know something about an edge, add a property there rather than a literal here.

## `build` loads the graph once

`build` reads all assets and all edges up front, because scoping and merging both need the whole picture and a streaming path would make them wrong rather than slow. This is the documented memory limit in the README — if you change it, change that section too.

Provenance is only loaded when a filter actually needs it (`Filters.needs_sources`), which keeps the common `oamx names` path to two queries. If you add a filter that reads `Asset.sources`, add it to `needs_sources` in the same commit, or it will silently see an empty source list for every asset.

## Anti-patterns

- Adding a hostname to scope for any reason other than a suffix match.
- Applying a time or provenance filter before `merge_assets`.
- Applying a scope or resolution filter after it.
- A filter that drops an asset when it cannot evaluate its condition.
- Comparing `Edge.label` to a literal string.
- Reading `Asset.sources` from a filter that is not accounted for in `Filters.needs_sources`.
- Reaching into `db._load_sources` from new code. `build` already does this and it is a wart, not a pattern to copy; if another caller needs it, promote it to a public method first.
