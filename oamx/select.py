"""Turning a whole asset database into the subset you actually asked for."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from .model import Asset, Edge, merge_assets, normalise_fqdn

DEFAULT_SCOPE_DEPTH = 2
PORT_LABEL = "port"


def in_domain(value: str, domains: Iterable[str]) -> bool:
    """True if ``value`` is one of ``domains`` or a subdomain of one."""
    v = normalise_fqdn(value)
    for d in domains:
        d = normalise_fqdn(d)
        if v == d or v.endswith("." + d):
            return True
    return False


def compute_scope(
    assets: dict[int, Asset],
    edges: list[Edge],
    domains: list[str],
    depth: int = DEFAULT_SCOPE_DEPTH,
) -> set[int]:
    """Entity ids in scope for ``domains``.

    FQDNs are matched by suffix. Everything else — IPs, netblocks,
    certificates, services — is in scope only if it is reachable within
    ``depth`` hops of an in-scope name. Without this, asking for the netblocks
    of one target in a shared database hands you every other target's
    infrastructure too.
    """
    seeds = {
        aid for aid, a in assets.items()
        if a.type == "FQDN" and in_domain(a.value, domains)
    }
    if depth <= 0 or not seeds:
        return seeds

    adjacency: dict[int, set[int]] = {}
    for e in edges:
        adjacency.setdefault(e.from_asset.id, set()).add(e.to_asset.id)
        adjacency.setdefault(e.to_asset.id, set()).add(e.from_asset.id)

    scope = set(seeds)
    frontier = set(seeds)
    for _ in range(depth):
        nxt: set[int] = set()
        for node in frontier:
            for neighbour in adjacency.get(node, ()):
                if neighbour not in scope:
                    nxt.add(neighbour)
        if not nxt:
            break
        scope |= nxt
        frontier = nxt

    # A neighbouring FQDN pulled in by graph proximity is not automatically in
    # scope — that is how you end up scanning a shared CDN's other customers.
    return {
        aid for aid in scope
        if assets[aid].type != "FQDN" or in_domain(assets[aid].value, domains)
    }


def resolved_fqdns(edges: list[Edge]) -> set[int]:
    """Ids of FQDNs with a DNS record pointing at something.

    Defers to ``Edge.is_dns`` rather than matching v5's ``dns_record`` label
    directly, because v4 names its DNS edges after the record type instead
    (``a_record``, ``cname_record``). Matching one spelling makes
    ``--resolved-only`` discard every name in a v4 database and exit 0.
    """
    out: set[int] = set()
    for e in edges:
        if e.is_dns and e.from_asset.type == "FQDN":
            out.add(e.from_asset.id)
    return out


@dataclass(slots=True)
class Filters:
    domains: list[str] = field(default_factory=list)
    scope_depth: int = DEFAULT_SCOPE_DEPTH
    since: datetime | None = None
    since_field: str = "updated"
    sources: list[str] = field(default_factory=list)
    exclude_sources: list[str] = field(default_factory=list)
    min_confidence: int = 0
    resolved_only: bool = False
    want_sources: bool = False

    @property
    def needs_sources(self) -> bool:
        return bool(
            self.sources or self.exclude_sources or self.min_confidence or self.want_sources
        )


@dataclass(slots=True)
class Selection:
    assets: dict[int, Asset]
    edges: list[Edge]
    scope: set[int] | None
    resolved: set[int]

    def matching(self, f: Filters, types: Iterable[str] | None = None) -> list[Asset]:
        """Assets matching ``f``, merged by value.

        Order matters. Scope and resolution are properties of a database row,
        so they are applied first, against ids. Time windows and provenance
        are properties of the *thing*, so they are applied after merging —
        otherwise a host discovered twice could be judged on only one of its
        sightings, and ``--exclude-source`` would discard a name that a
        trusted source also vouched for.
        """
        type_set = set(types) if types else None
        rows: list[Asset] = []
        for aid, a in self.assets.items():
            if type_set is not None and a.type not in type_set:
                continue
            if self.scope is not None and aid not in self.scope:
                continue
            if f.resolved_only and a.type == "FQDN" and aid not in self.resolved:
                continue
            rows.append(a)

        merged = merge_assets(rows)
        return [a for a in merged if _time_ok(a, f) and _source_ok(a, f)]

    def matching_edges(self, f: Filters, labels: Iterable[str] | None = None) -> list[Edge]:
        label_set = set(labels) if labels else None
        out: list[Edge] = []
        for e in self.edges:
            if label_set is not None and e.label not in label_set:
                continue
            if self.scope is not None and (
                e.from_asset.id not in self.scope or e.to_asset.id not in self.scope
            ):
                continue
            if f.since is not None:
                from .reader import _parse_ts

                ts = _parse_ts(e.last_seen)
                if ts is not None and ts < f.since:
                    continue
            out.append(e)
        out.sort(key=lambda e: (e.from_asset.value, e.label, e.to_asset.value))
        return out


def _time_ok(a: Asset, f: Filters) -> bool:
    if f.since is None:
        return True
    from .reader import _parse_ts

    raw = a.last_seen if f.since_field == "updated" else a.first_seen
    ts = _parse_ts(raw)
    # Unparseable or absent timestamps are kept: dropping assets because we
    # could not read a date is a silent false negative, which is the failure
    # mode this whole tool exists to prevent.
    return ts is None or ts >= f.since


def _source_ok(a: Asset, f: Filters) -> bool:
    if not f.needs_sources:
        return True
    names = a.source_names
    # Exclude only when *every* source is excluded. A name that brute force
    # guessed and certificate transparency independently confirmed is still a
    # real name.
    if f.exclude_sources and names and names <= set(f.exclude_sources):
        return False
    if f.sources and not (names & set(f.sources)):
        return False
    if f.min_confidence and a.max_confidence < f.min_confidence:
        return False
    return True


def build(db, f: Filters) -> Selection:
    """Load the graph once and derive everything else from it."""
    assets = db.assets_by_id(None)
    edges = db.edges(assets=assets)

    if f.needs_sources:
        src = db._load_sources(db.entity_tag_table, db.c_eid or "entity_id", set(assets))
        for aid, a in assets.items():
            a.sources = src.get(aid, [])

    scope = compute_scope(assets, edges, f.domains, f.scope_depth) if f.domains else None
    return Selection(assets=assets, edges=edges, scope=scope, resolved=resolved_fqdns(edges))
