---
name: oamx-model
description: model.py is the stable layer downstream consumers depend on while Amass changes underneath. Value extraction never raises, layout differences are absorbed by Edge properties rather than by callers, and the emitted shape is pinned by SCHEMA_VERSION. Load before editing oamx/model.py.
---

# oamx model contract

`model.py` is the only module downstream code is allowed to depend on. Amass has changed its storage in every major version; the promise of this file is that `oamx names` and `oamx json` produce the same shape regardless. Everything here is written to survive input it has never seen.

## `SCHEMA_VERSION` is a public contract

`"oamx/1"` appears in every emitted record. Somebody's ingestion pipeline keys off it.

- Adding an optional field is compatible. Do it freely.
- Removing a field, renaming one, or changing what a value means is not. Bump to `oamx/2` and say so in the README's output-schema section.
- Never make a field's *meaning* depend on the Amass version underneath. Absorbing that difference is this module's entire job.

## Value extraction must never raise

`extract_value` takes an asset type and a `content` blob and always returns a string. The fallback chain is deliberate:

1. the type's entry in `VALUE_KEYS`
2. `GENERIC_VALUE_KEYS`, for a type this release has never heard of
3. compact JSON of the whole blob, so the record still carries its information

```python
# correct - unknown types degrade
extract_value("Quantum", {"weird": 1})   # '{"weird":1}'

# wrong - a KeyError here kills a scan mid-pipeline
return content["name"]
```

`VALUE_KEYS` is split into a VERIFIED block and a best-effort block, and the comment saying which is which is load-bearing. Verified means checked against the [OAM asset docs](https://owasp-amass.github.io/docs/open_asset_model/assets/) or the structs in [owasp-amass/open-asset-model](https://github.com/owasp-amass/open-asset-model); the rest are guesses that happen to work.

If you verify one, **move it into the VERIFIED block in the same commit**, and update the count in the README's known limits. The distinction is only worth anything if it is accurate — a best-effort key list that silently reports the wrong field looks exactly like a correct one until somebody pipes it somewhere.

Ordering inside a type's tuple matters: first key present and non-empty wins, so put the identifying field before the descriptive one.

## Layout differences are absorbed here, not by callers

This is the rule most worth holding on to.

v5 labels every DNS edge `dns_record` and carries the record type as a numeric `header.rr_type`. v4 encoded it in the label itself: `a_record`, `cname_record`, `aaaa_record`. `Edge.is_dns` and `Edge.rr_name` know both spellings.

```python
# correct - the property owns the version difference
if e.is_dns and e.from_asset.type == "FQDN":

# wrong - matches v5 only, silently returns nothing on a v4 database
if e.label == "dns_record" and e.from_asset.type == "FQDN":
```

If you find yourself comparing `Edge.label` to a literal anywhere outside this module, stop: either use an existing property or add one here. A caller that hard-codes one generation's spelling fails closed, and failing closed in this tool means an empty pipeline that exits 0.

`PortRelation` edges are the current exception — `label == "port"` is the same in both generations that support them (v4 has none at all). If that changes, it becomes a property too.

## Merge semantics

`merge_assets` collapses rows that reduce to the same `(type, value)` — a certificate SAN's `API.Example.COM.` and a DNS answer's `api.example.com` are two rows and one host. The rules, each for a reason:

| Field | Rule | Why |
|---|---|---|
| `id` | lowest wins | stable output across runs |
| `first_seen` | earliest | the host was known from then |
| `last_seen` | latest | the host is current if any row is |
| `sources` | union by name, higher confidence wins per name | provenance is evidence; discarding half of it makes the asset look weaker than it is |
| `attrs` | `setdefault`, first row wins | never silently overwrite one row's data with another's |

Merging happens **before** time and provenance filters (see `oamx-select`), so a host is never judged on only one of its sightings. If you add a field to `Asset`, add its merge rule here at the same time — a field with no rule silently takes the first row's value, which may be the wrong one.

## Normalisation

`normalise_fqdn` lowercases, strips the trailing root dot and trims whitespace. It is applied in `reader._row_to_asset` for `FQDN` assets and in `select.in_domain` for the domains a user typed, so both sides of a suffix comparison are canonical. Names arriving from certificate SANs and third-party feeds are not well behaved, and scanning both `WWW.Example.com.` and `www.example.com` wastes half the budget.

Do not normalise other asset types on the way through. IP addresses, serial numbers and URLs are compared verbatim, and lowercasing a URL path changes what it points at.

## Anti-patterns

- Raising from anything in this module on unrecognised input. Degrade instead.
- Adding an asset type to `VALUE_KEYS` under the VERIFIED comment without actually checking the OAM docs.
- Comparing `Edge.label` to a version-specific literal outside this module.
- Adding a field to `Asset` or `Edge` without adding it to `to_dict` — the dataclass is internal, `to_dict` is the contract, and they drift silently.
- Adding a field to `Asset` without a `merge_assets` rule.
