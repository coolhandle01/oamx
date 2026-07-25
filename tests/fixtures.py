"""Build synthetic Amass databases.

These reproduce the storage layouts of Amass v4 (``assets``/``relations``) and
v5 (``entities``/``edges``) as documented by owasp-amass/asset-db, including
the Go-style nanosecond timestamps that GORM writes. They exist so the reader
can be tested without a live enumeration.
"""

from __future__ import annotations

import atexit
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
RECENT = NOW - timedelta(hours=2)
MID = NOW - timedelta(days=7)
OLD = NOW - timedelta(days=30)


def ts(dt: datetime) -> str:
    """GORM/SQLite timestamp: microseconds padded out to Go's nanoseconds."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f") + "000+00:00"


V5_SCHEMA = """
CREATE TABLE entities (
    entity_id  INTEGER PRIMARY KEY,
    created_at datetime DEFAULT CURRENT_TIMESTAMP,
    updated_at datetime DEFAULT CURRENT_TIMESTAMP,
    etype      TEXT,
    content    JSON
);
CREATE TABLE entity_tags (
    tag_id     INTEGER PRIMARY KEY,
    created_at datetime, updated_at datetime,
    ttype      TEXT, content JSON,
    entity_id  INTEGER
);
CREATE TABLE edges (
    edge_id        INTEGER PRIMARY KEY,
    created_at     datetime, updated_at datetime,
    etype          TEXT, content JSON,
    from_entity_id INTEGER, to_entity_id INTEGER
);
CREATE TABLE edge_tags (
    tag_id     INTEGER PRIMARY KEY,
    created_at datetime, updated_at datetime,
    ttype      TEXT, content JSON,
    edge_id    INTEGER
);
"""

V4_SCHEMA = """
CREATE TABLE assets (
    id         INTEGER PRIMARY KEY,
    created_at datetime,
    last_seen  datetime,
    type       TEXT,
    content    JSON
);
CREATE TABLE relations (
    id            INTEGER PRIMARY KEY,
    created_at    datetime,
    last_seen     datetime,
    type          TEXT,
    from_asset_id INTEGER,
    to_asset_id   INTEGER
);
CREATE TABLE gorp_migrations (id TEXT PRIMARY KEY, applied_at datetime);
"""

# id, type, content, first_seen, last_seen
ENTITIES = [
    (1, "FQDN", {"name": "example.com"}, OLD, RECENT),
    (2, "FQDN", {"name": "www.example.com"}, OLD, RECENT),
    (3, "FQDN", {"name": "api.example.com"}, MID, MID),
    (4, "FQDN", {"name": "dev.example.com"}, OLD, OLD),  # never resolved
    (5, "IPAddress", {"address": "93.184.216.34", "type": "IPv4"}, OLD, RECENT),
    (6, "IPAddress", {"address": "2606:2800:220:1::1946", "type": "IPv6"}, OLD, RECENT),
    (7, "Netblock", {"cidr": "93.184.216.0/24", "type": "IPv4"}, OLD, RECENT),
    (8, "AutonomousSystem", {"number": 15133}, OLD, RECENT),
    (
        9,
        "Service",
        {
            "unique_id": "svc-443-www",
            "service_type": "https",
            "output": "HTTP/1.1 200 OK",
            "output_length": 17,
            "attributes": {"server": "ECS"},
        },
        RECENT,
        RECENT,
    ),
    (
        17,
        "Service",
        {
            "unique_id": "svc-80-www",
            "service_type": "http",
            "output": "HTTP/1.1 301 Moved",
            "output_length": 18,
            "attributes": {"server": "ECS"},
        },
        RECENT,
        RECENT,
    ),
    (
        18,
        "Service",
        {
            "unique_id": "svc-22-api",
            "service_type": "ssh",
            "output": "SSH-2.0-OpenSSH_9.6",
            "output_length": 19,
        },
        RECENT,
        RECENT,
    ),
    (
        10,
        "TLSCertificate",
        {"serial_number": "0A1B2C3D", "subject_common_name": "*.example.com"},
        RECENT,
        RECENT,
    ),
    (11, "FQDN", {"name": "other.co.uk"}, OLD, RECENT),  # a different target
    (12, "IPAddress", {"address": "203.0.113.9", "type": "IPv4"}, OLD, RECENT),
    (13, "FQDN", {"name": "API.Example.COM."}, OLD, RECENT),  # needs normalising
    (14, "URL", {"url": "https://www.example.com/login"}, RECENT, RECENT),
    (15, "FQDN", {"name": "cdn.provider.net"}, RECENT, RECENT),  # CNAME target, out of scope
    (16, "SomeFutureAssetType", {"widget": "unknowable"}, RECENT, RECENT),
    (19, "FQDN", {"name": "new.example.com"}, RECENT, RECENT),  # genuinely new
    # Contact vocabulary. OAM's Identifier carries the value in `id` and the
    # scheme in `id_type`; `unique_id` is a dedupe key, not a human value.
    # ContactRecord is a bare join node - `discovered_at` is where the contact
    # was found, so it is a URL and never an address.
    (
        20,
        "Identifier",
        {"id": "abuse@example.com", "id_type": "email", "unique_id": "email:abuse@example.com"},
        RECENT,
        RECENT,
    ),
    (
        21,
        "Identifier",
        {"id": "ORG-EX1-RIPE", "id_type": "handle", "unique_id": "handle:ORG-EX1-RIPE"},
        RECENT,
        RECENT,
    ),
    (22, "ContactRecord", {"discovered_at": "https://rdap.example/entity/1"}, RECENT, RECENT),
]

# id, etype, label, extra content, from, to, first_seen, last_seen
EDGES = [
    (1, "SimpleRelation", "node", {}, 1, 2, OLD, RECENT),
    (2, "SimpleRelation", "node", {}, 1, 3, RECENT, RECENT),
    (3, "SimpleRelation", "node", {}, 1, 4, OLD, OLD),
    (
        4,
        "BasicDNSRelation",
        "dns_record",
        {"header": {"rr_type": 1, "class": 1, "ttl": 300}},
        2,
        5,
        OLD,
        RECENT,
    ),
    (
        5,
        "BasicDNSRelation",
        "dns_record",
        {"header": {"rr_type": 1, "class": 1, "ttl": 300}},
        3,
        5,
        RECENT,
        RECENT,
    ),
    (
        6,
        "BasicDNSRelation",
        "dns_record",
        {"header": {"rr_type": 28, "class": 1, "ttl": 300}},
        1,
        6,
        OLD,
        RECENT,
    ),
    (
        7,
        "BasicDNSRelation",
        "dns_record",
        {"header": {"rr_type": 5, "class": 1, "ttl": 60}},
        2,
        15,
        RECENT,
        RECENT,
    ),
    (8, "SimpleRelation", "ptr_record", {}, 5, 1, OLD, RECENT),
    (9, "SimpleRelation", "contains", {}, 7, 5, OLD, RECENT),
    (10, "SimpleRelation", "announces", {}, 8, 7, OLD, RECENT),
    (11, "PortRelation", "port", {"port_number": 443, "protocol": "tcp"}, 2, 9, RECENT, RECENT),
    (12, "PortRelation", "port", {"port_number": 80, "protocol": "tcp"}, 2, 17, RECENT, RECENT),
    (16, "PortRelation", "port", {"port_number": 22, "protocol": "tcp"}, 3, 18, RECENT, RECENT),
    (17, "SimpleRelation", "node", {}, 1, 19, RECENT, RECENT),
    (
        18,
        "BasicDNSRelation",
        "dns_record",
        {"header": {"rr_type": 1, "class": 1, "ttl": 300}},
        19,
        5,
        RECENT,
        RECENT,
    ),
    (13, "SimpleRelation", "certificate", {}, 9, 10, RECENT, RECENT),
    (
        14,
        "BasicDNSRelation",
        "dns_record",
        {"header": {"rr_type": 1, "class": 1, "ttl": 300}},
        11,
        12,
        OLD,
        RECENT,
    ),
    (15, "SimpleRelation", "node", {}, 999, 5, OLD, RECENT),  # dangling: must be skipped
]

# entity_id -> (source name, confidence)
SOURCES = {
    1: [("DNS-IP", 100)],
    2: [("DNS-IP", 100), ("crtsh", 80)],
    3: [("crtsh", 80)],
    4: [("brute-forcing", 50)],
    5: [("DNS-IP", 100)],
    6: [("DNS-IP", 100)],
    7: [("RDAP", 100)],
    8: [("RDAP", 100)],
    11: [("DNS-IP", 100)],
    13: [("DNS-IP", 100), ("crtsh", 95)],
    15: [("DNS-IP", 100)],
    19: [("DNS-IP", 100)],
}


def build_v5(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(V5_SCHEMA)

    for eid, etype, content, first, last in ENTITIES:
        conn.execute(
            "INSERT INTO entities (entity_id, created_at, updated_at, etype, content) "
            "VALUES (?,?,?,?,?)",
            (eid, ts(first), ts(last), etype, json.dumps(content)),
        )

    for rid, etype, label, extra, src, dst, first, last in EDGES:
        content = {"label": label, **extra}
        conn.execute(
            "INSERT INTO edges (edge_id, created_at, updated_at, etype, content, "
            "from_entity_id, to_entity_id) VALUES (?,?,?,?,?,?,?)",
            (rid, ts(first), ts(last), etype, json.dumps(content), src, dst),
        )

    tag_id = 1
    for eid, sources in SOURCES.items():
        for name, conf in sources:
            conn.execute(
                "INSERT INTO entity_tags (tag_id, created_at, updated_at, ttype, "
                "content, entity_id) VALUES (?,?,?,?,?,?)",
                (
                    tag_id,
                    ts(RECENT),
                    ts(RECENT),
                    "SourceProperty",
                    json.dumps({"name": name, "confidence": conf}),
                    eid,
                ),
            )
            tag_id += 1
    # A non-source tag, to prove we ignore what we do not understand.
    conn.execute(
        "INSERT INTO entity_tags (tag_id, created_at, updated_at, ttype, content, entity_id) "
        "VALUES (?,?,?,?,?,?)",
        (
            tag_id,
            ts(RECENT),
            ts(RECENT),
            "SimpleProperty",
            json.dumps({"property_name": "last_monitored", "property_value": ts(RECENT)}),
            1,
        ),
    )
    conn.execute(
        "INSERT INTO entity_tags (tag_id, created_at, updated_at, ttype, content, entity_id) "
        "VALUES (?,?,?,?,?,?)",
        (
            tag_id + 1,
            ts(RECENT),
            ts(RECENT),
            "VulnProperty",
            json.dumps({"name": "CVE-2026-0001", "severity": "high"}),
            2,
        ),
    )

    conn.commit()
    conn.close()
    return path


def build_v4(path: Path) -> Path:
    """The older layout: singular type column, label encoded in the relation type."""
    conn = sqlite3.connect(path)
    conn.executescript(V4_SCHEMA)

    v4_labels = {1: "a_record", 28: "aaaa_record", 5: "cname_record"}

    for eid, etype, content, first, last in ENTITIES:
        conn.execute(
            "INSERT INTO assets (id, created_at, last_seen, type, content) VALUES (?,?,?,?,?)",
            (eid, ts(first), ts(last), etype, json.dumps(content)),
        )

    for rid, etype, label, extra, src, dst, first, last in EDGES:
        if etype == "BasicDNSRelation":
            rr = extra.get("header", {}).get("rr_type")
            label = v4_labels.get(rr, "dns_record")
        elif etype == "PortRelation":
            continue  # v4 had no port relations
        conn.execute(
            "INSERT INTO relations (id, created_at, last_seen, type, from_asset_id, to_asset_id) "
            "VALUES (?,?,?,?,?,?)",
            (rid, ts(first), ts(last), label, src, dst),
        )

    conn.commit()
    conn.close()
    return path


def build_empty(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(V5_SCHEMA)
    conn.commit()
    conn.close()
    return path


def build_garbage(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated (a TEXT, b TEXT)")
    conn.execute("INSERT INTO unrelated VALUES ('not', 'amass')")
    conn.commit()
    conn.close()
    return path


@lru_cache(maxsize=1)
def shared_databases() -> tuple[Path, Path]:
    """Build the v5 and v4 databases once per process, and return their paths.

    Both the conftest fixtures and the module-level setup in ``test_oamx``
    resolve through here, so the two entry points share one build rather than
    each standing up their own copy. The databases are read-only to every
    caller; nothing in the suite writes to them.
    """
    tmp = tempfile.mkdtemp(prefix="oamx-fixtures-")
    atexit.register(shutil.rmtree, tmp, True)
    root = Path(tmp)
    return build_v5(root / "amass.sqlite"), build_v4(root / "amass_v4.sqlite")
