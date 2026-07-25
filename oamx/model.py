"""Normalised representation of Open Asset Model data.

The point of this module is to be the *stable* layer. Amass has changed its
storage format in every major version (v3 graph files, v4 ``assets``/
``relations``, v5 ``entities``/``edges``, and schema churn within v5 point
releases). Downstream consumers should depend on ``SCHEMA_VERSION`` and the
shape defined here, never on the database layout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "oamx/1"

# ---------------------------------------------------------------------------
# Asset value extraction
# ---------------------------------------------------------------------------
# Each OAM asset type stores its identifying value under a different key in the
# ``content`` JSON blob. This maps asset type -> ordered candidate keys. The
# first key present and non-empty wins.
#
# Types marked VERIFIED were confirmed against the OAM documentation at
# https://owasp-amass.github.io/docs/open_asset_model/assets/ ; the rest are
# best-effort and fall through to GENERIC_VALUE_KEYS if they miss. Nothing here
# raises on an unknown type — new asset types degrade to the generic path
# rather than breaking a pipeline mid-scan.

VALUE_KEYS: dict[str, tuple[str, ...]] = {
    # VERIFIED
    "FQDN": ("name",),
    "IPAddress": ("address",),
    "Service": ("unique_id", "service_type"),
    # `id` before `unique_id`: OAM's Identifier holds the value (an email
    # address, a registry handle) in `id`, and uses `unique_id` as a dedupe
    # key that namespaces it - "email:abuse@example.com". Reporting the key
    # hands the caller something that is not the address it looks like.
    "Identifier": ("id", "unique_id", "value"),
    # Best-effort
    "Netblock": ("cidr", "range"),
    "AutonomousSystem": ("number", "asn"),
    "AutnumRecord": ("handle", "number"),
    "IPNetRecord": ("handle", "cidr"),
    "DomainRecord": ("domain", "name"),
    "TLSCertificate": ("serial_number", "subject_common_name", "common_name"),
    "URL": ("url", "raw"),
    "Organization": ("name", "legal_name"),
    "Person": ("full_name", "name"),
    "ContactRecord": ("discovered_at",),
    "Location": ("address", "formatted_address"),
    "Phone": ("raw", "e164"),
    "File": ("url", "name"),
    "Product": ("name",),
    "ProductRelease": ("name", "version"),
    "Account": ("unique_id", "id", "username"),
    "FundsTransfer": ("unique_id", "id"),
}

# Fallback probe order for asset types we do not know about yet.
GENERIC_VALUE_KEYS: tuple[str, ...] = (
    "name", "address", "cidr", "url", "raw", "unique_id", "handle",
    "id", "number", "domain", "serial_number", "full_name", "value", "title",
)

# DNS resource record numbers -> mnemonic. BasicDNSRelation stores rr_type as
# an integer under header.rr_type; nobody wants to grep for "5".
RR_TYPES: dict[int, str] = {
    1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT",
    17: "RP", 24: "SIG", 25: "KEY", 28: "AAAA", 29: "LOC", 33: "SRV",
    35: "NAPTR", 39: "DNAME", 43: "DS", 44: "SSHFP", 46: "RRSIG", 47: "NSEC",
    48: "DNSKEY", 50: "NSEC3", 51: "NSEC3PARAM", 52: "TLSA", 53: "SMIMEA",
    59: "CDS", 60: "CDNSKEY", 61: "OPENPGPKEY", 64: "SVCB", 65: "HTTPS",
    99: "SPF", 257: "CAA",
}

# Asset types that make sense as network scan targets.
HOSTLIKE_TYPES = frozenset({"FQDN", "IPAddress"})

# view name -> the OAM asset types it selects. One table, here in the stable
# layer, because the CLI and the library each used to keep their own: `emails`
# was in one and not the other, so it worked on the command line and raised
# `unknown view` from `query()`.
#
# An empty tuple means "every type" and is the library-only `all` view.
VIEW_TYPES: dict[str, tuple[str, ...]] = {
    "names": ("FQDN",),
    "ips": ("IPAddress",),
    "cidrs": ("Netblock",),
    "asns": ("AutonomousSystem",),
    "urls": ("URL",),
    "certs": ("TLSCertificate",),
    "services": ("Service",),
    "orgs": ("Organization",),
    "emails": ("Identifier",),
    "all": (),
}

# OAM's id_type for an email address. The Identifier asset covers forty-odd
# schemes - handles, tickers, tax ids, IBANs - so selecting the type alone
# gets you everything except a shortlist of addresses.
EMAIL_ID_TYPE = "email"


def in_view(view: str, asset: Asset) -> bool:
    """Whether ``asset`` belongs in ``view``, beyond matching its type.

    Only ``emails`` narrows further today. Type selection alone cannot
    express it: an email is an ``Identifier`` *whose ``id_type`` says so*,
    and the same asset type carries every other identifier scheme OAM knows.
    """
    if view == "emails":
        return str(asset.attrs.get("id_type", "")).strip().lower() == EMAIL_ID_TYPE
    return True


def extract_value(asset_type: str, content: dict[str, Any]) -> str:
    """Pull the identifying value out of an OAM ``content`` blob.

    Never raises. Returns compact JSON of the whole blob as a last resort so
    that an unrecognised asset still carries its information downstream.
    """
    if not isinstance(content, dict):
        return str(content) if content is not None else ""

    for key in VALUE_KEYS.get(asset_type, ()) + GENERIC_VALUE_KEYS:
        val = content.get(key)
        if val is None or val == "":
            continue
        if isinstance(val, (str, int, float)):
            return str(val)

    return json.dumps(content, separators=(",", ":"), sort_keys=True)


def normalise_fqdn(name: str) -> str:
    """Canonicalise a DNS name: lowercase, strip the root dot and whitespace.

    Amass is generally well behaved here, but names arriving from certificate
    SANs and third-party feeds are not, and a pipeline that scans both
    ``WWW.Example.com.`` and ``www.example.com`` wastes half its budget.
    """
    return name.strip().rstrip(".").lower()


@dataclass(slots=True)
class Source:
    """Provenance: which Amass engine plugin asserted this, and how sure."""

    name: str
    confidence: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.confidence is not None:
            d["confidence"] = self.confidence
        return d


@dataclass(slots=True)
class Asset:
    """A single OAM entity, normalised."""

    id: int
    type: str
    value: str
    attrs: dict[str, Any] = field(default_factory=dict)
    first_seen: str | None = None
    last_seen: str | None = None
    sources: list[Source] = field(default_factory=list)

    @property
    def max_confidence(self) -> int:
        vals = [s.confidence for s in self.sources if s.confidence is not None]
        return max(vals) if vals else 0

    @property
    def source_names(self) -> set[str]:
        return {s.name for s in self.sources}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "kind": "asset",
            "id": self.id,
            "type": self.type,
            "value": self.value,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "sources": [s.to_dict() for s in self.sources],
            "attrs": self.attrs,
        }


def merge_assets(assets: list[Asset]) -> list[Asset]:
    """Collapse assets that normalise to the same (type, value).

    Amass legitimately stores several entities that reduce to one thing —
    ``API.Example.COM.`` from a certificate SAN and ``api.example.com`` from a
    DNS answer are two rows and one host. Emitting both means scanning it
    twice. Provenance is unioned rather than discarded, and the sighting
    window is widened to cover every contributing row.
    """
    from .reader import _parse_ts  # local import: reader imports this module

    def earlier(a: str | None, b: str | None) -> str | None:
        pa, pb = _parse_ts(a), _parse_ts(b)
        if pa is None:
            return b
        if pb is None:
            return a
        return a if pa <= pb else b

    def later(a: str | None, b: str | None) -> str | None:
        pa, pb = _parse_ts(a), _parse_ts(b)
        if pa is None:
            return b
        if pb is None:
            return a
        return a if pa >= pb else b

    merged: dict[tuple[str, str], Asset] = {}
    for asset in assets:
        key = (asset.type, asset.value)
        existing = merged.get(key)
        if existing is None:
            merged[key] = Asset(
                id=asset.id,
                type=asset.type,
                value=asset.value,
                attrs=dict(asset.attrs),
                first_seen=asset.first_seen,
                last_seen=asset.last_seen,
                sources=list(asset.sources),
            )
            continue
        existing.id = min(existing.id, asset.id)
        existing.first_seen = earlier(existing.first_seen, asset.first_seen)
        existing.last_seen = later(existing.last_seen, asset.last_seen)
        by_name = {s.name: s for s in existing.sources}
        for s in asset.sources:
            prior = by_name.get(s.name)
            if prior is None or (s.confidence or 0) > (prior.confidence or 0):
                by_name[s.name] = s
        existing.sources = sorted(by_name.values(), key=lambda s: s.name)
        for k, v in asset.attrs.items():
            existing.attrs.setdefault(k, v)

    return sorted(merged.values(), key=lambda a: (a.type, a.value))


@dataclass(slots=True)
class Edge:
    """A directed relation between two assets, normalised."""

    id: int
    type: str
    label: str
    from_asset: Asset
    to_asset: Asset
    attrs: dict[str, Any] = field(default_factory=dict)
    first_seen: str | None = None
    last_seen: str | None = None
    sources: list[Source] = field(default_factory=list)

    @property
    def is_dns(self) -> bool:
        return self.label == "dns_record" or self.label.endswith("_record")

    @property
    def rr_name(self) -> str | None:
        """Mnemonic DNS record type, when this edge carries one.

        v5 stores a numeric ``header.rr_type``. v4 encoded it in the relation
        name instead (``a_record``, ``cname_record``), so fall back to that.
        """
        rr = self.attrs.get("rr_type")
        if isinstance(rr, int):
            return RR_TYPES.get(rr, f"TYPE{rr}")
        if self.label.endswith("_record"):
            stem = self.label[: -len("_record")].upper()
            return stem or None
        return None

    def to_dict(self) -> dict[str, Any]:
        attrs = dict(self.attrs)
        if (rr := self.rr_name) is not None:
            attrs["rr_name"] = rr
        return {
            "schema": SCHEMA_VERSION,
            "kind": "edge",
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "from": {"type": self.from_asset.type, "value": self.from_asset.value},
            "to": {"type": self.to_asset.type, "value": self.to_asset.value},
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "sources": [s.to_dict() for s in self.sources],
            "attrs": attrs,
        }
