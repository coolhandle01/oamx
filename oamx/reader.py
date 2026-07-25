"""Read-only access to the Amass asset database.

Design notes
------------
Amass has renamed its tables and columns at least three times. Rather than
pinning to one release, this module *introspects* the database it is handed
and maps whatever it finds onto a single logical schema. A v4 database
(``assets``/``relations``) and a v5 database (``entities``/``edges``) both come
out of here looking identical to callers.

The connection is opened strictly read-only via a ``file:...?mode=ro`` URI so
that pointing this at a database while Amass is mid-enumeration cannot corrupt
it or take a write lock.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .model import Asset, Edge, Source, extract_value, normalise_fqdn


class OamxError(Exception):
    """Anything the user needs to read and act on."""


# --- schema mapping ---------------------------------------------------------

# logical name -> candidate physical names, in preference order
_ENTITY_TABLES = ("entities", "assets")
_EDGE_TABLES = ("edges", "relations")
_ENTITY_TAG_TABLES = ("entity_tags", "asset_tags")
_EDGE_TAG_TABLES = ("edge_tags", "relation_tags")

_COL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "entity_id": ("entity_id", "asset_id", "id"),
    "entity_type": ("etype", "type", "atype"),
    "content": ("content",),
    "created": ("created_at", "first_seen", "created"),
    "updated": ("updated_at", "last_seen", "updated"),
    "edge_id": ("edge_id", "relation_id", "id"),
    "edge_type": ("etype", "type", "rtype"),
    "edge_from": ("from_entity_id", "from_asset_id", "from_id"),
    "edge_to": ("to_entity_id", "to_asset_id", "to_id"),
    "tag_id": ("tag_id", "id"),
    "tag_type": ("ttype", "type"),
}


def _pick(candidates: Iterable[str], available: Iterable[str]) -> str | None:
    have = {c.lower(): c for c in available}
    for cand in candidates:
        if cand.lower() in have:
            return have[cand.lower()]
    return None


def default_db_paths() -> list[Path]:
    """Where Amass keeps its database, per platform, most recent first."""
    dirs: list[Path] = []
    if xdg := os.environ.get("XDG_CONFIG_HOME"):
        dirs.append(Path(xdg) / "amass")
    home = Path.home()
    dirs += [
        home / ".config" / "amass",
        home / "Library" / "Application Support" / "amass",
        Path(os.environ.get("APPDATA", "/nonexistent")) / "amass",
        Path("/etc/amass"),
        Path.cwd(),
    ]

    found: list[Path] = []
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            for pattern in ("*.sqlite", "*.sqlite3", "*.db"):
                found.extend(p for p in d.glob(pattern) if p.is_file())
        except OSError:
            continue

    uniq = {p.resolve(): p for p in found}
    return sorted(uniq.values(), key=lambda p: p.stat().st_mtime, reverse=True)


def _parse_ts(raw: Any) -> datetime | None:
    """Tolerantly parse the assorted timestamp formats GORM emits."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    # Trim sub-second precision beyond microseconds (Go writes nanoseconds).
    cleaned = text.replace("Z", "+00:00").replace(" ", "T", 1)
    if "." in cleaned:
        head, _, tail = cleaned.partition(".")
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        rest = tail[len(digits):]
        cleaned = f"{head}.{digits[:6]}{rest}" if digits else head + rest
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(cleaned[: len(fmt) + 2], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def parse_duration(text: str) -> timedelta:
    """Parse a Go-ish duration: ``30m``, ``24h``, ``7d``, ``2w``."""
    text = text.strip().lower()
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    if not text or text[-1] not in units:
        raise OamxError(f"bad duration {text!r} (expected e.g. 24h, 7d, 30m)")
    try:
        amount = float(text[:-1])
    except ValueError as exc:
        raise OamxError(f"bad duration {text!r}") from exc
    return timedelta(**{units[text[-1]]: amount})


class AssetDB:
    """A read-only, schema-tolerant view over an Amass asset database."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise OamxError(f"no such database: {self.path}")
        uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
        try:
            self.conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise OamxError(f"cannot open {self.path}: {exc}") from exc
        self.conn.row_factory = sqlite3.Row
        self._introspect()

    # -- setup ------------------------------------------------------------

    def _tables(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
        return {r["name"] for r in rows}

    def _columns(self, table: str) -> list[str]:
        try:
            rows = self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        except sqlite3.Error:
            return []
        return [r["name"] for r in rows]

    def _introspect(self) -> None:
        tables = self._tables()

        self.entity_table = _pick(_ENTITY_TABLES, tables)
        if not self.entity_table:
            raise OamxError(
                f"{self.path} does not look like an Amass asset database "
                f"(no entities/assets table; found: {', '.join(sorted(tables)) or 'nothing'})"
            )
        self.edge_table = _pick(_EDGE_TABLES, tables)
        self.entity_tag_table = _pick(_ENTITY_TAG_TABLES, tables)
        self.edge_tag_table = _pick(_EDGE_TAG_TABLES, tables)

        ecols = self._columns(self.entity_table)
        self.c_eid = _pick(_COL_CANDIDATES["entity_id"], ecols)
        self.c_etype = _pick(_COL_CANDIDATES["entity_type"], ecols)
        self.c_econtent = _pick(_COL_CANDIDATES["content"], ecols)
        self.c_ecreated = _pick(_COL_CANDIDATES["created"], ecols)
        self.c_eupdated = _pick(_COL_CANDIDATES["updated"], ecols)
        if not (self.c_eid and self.c_etype and self.c_econtent):
            raise OamxError(
                f"unrecognised layout for table {self.entity_table!r} "
                f"(columns: {', '.join(ecols)}). Please open an issue with this line."
            )

        if self.edge_table:
            rcols = self._columns(self.edge_table)
            self.c_rid = _pick(_COL_CANDIDATES["edge_id"], rcols)
            self.c_rtype = _pick(_COL_CANDIDATES["edge_type"], rcols)
            self.c_rcontent = _pick(_COL_CANDIDATES["content"], rcols)
            self.c_rfrom = _pick(_COL_CANDIDATES["edge_from"], rcols)
            self.c_rto = _pick(_COL_CANDIDATES["edge_to"], rcols)
            self.c_rcreated = _pick(_COL_CANDIDATES["created"], rcols)
            self.c_rupdated = _pick(_COL_CANDIDATES["updated"], rcols)
            if not (self.c_rfrom and self.c_rto):
                self.edge_table = None

        self.generation = "v5" if self.entity_table == "entities" else "v4"

    # -- describe ---------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "generation": self.generation,
            "entity_table": self.entity_table,
            "edge_table": self.edge_table,
            "entity_tag_table": self.entity_tag_table,
        }

    def type_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            f'SELECT "{self.c_etype}" AS t, COUNT(*) AS n '
            f'FROM "{self.entity_table}" GROUP BY t ORDER BY n DESC'
        ).fetchall()
        return {r["t"]: r["n"] for r in rows}

    # -- tags / provenance -------------------------------------------------

    def _load_sources(self, table: str | None, fk: str, ids: set[int]) -> dict[int, list[Source]]:
        """Bulk-load SourceProperty tags for a set of ids. One query, chunked."""
        out: dict[int, list[Source]] = {}
        if not table or not ids:
            return out
        cols = self._columns(table)
        c_type = _pick(_COL_CANDIDATES["tag_type"], cols)
        c_content = _pick(_COL_CANDIDATES["content"], cols)
        c_fk = _pick((fk,), cols)
        if not (c_type and c_content and c_fk):
            return out

        id_list = list(ids)
        for i in range(0, len(id_list), 900):  # stay under SQLITE_MAX_VARIABLE_NUMBER
            chunk = id_list[i : i + 900]
            marks = ",".join("?" * len(chunk))
            try:
                rows = self.conn.execute(
                    f'SELECT "{c_fk}" AS fk, "{c_type}" AS ttype, "{c_content}" AS content '
                    f'FROM "{table}" WHERE "{c_fk}" IN ({marks})',
                    chunk,
                ).fetchall()
            except sqlite3.Error:
                return out
            for r in rows:
                if r["ttype"] != "SourceProperty":
                    continue
                try:
                    body = json.loads(r["content"]) if r["content"] else {}
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(body, dict):
                    continue
                name = body.get("name")
                if not name:
                    continue
                conf = body.get("confidence")
                out.setdefault(r["fk"], []).append(
                    Source(str(name), int(conf) if isinstance(conf, (int, float)) else None)
                )
        return out

    # -- assets ------------------------------------------------------------

    def _row_to_asset(self, row: sqlite3.Row) -> Asset:
        try:
            content = json.loads(row["content"]) if row["content"] else {}
        except (json.JSONDecodeError, TypeError):
            content = {}
        if not isinstance(content, dict):
            content = {"raw": content}

        atype = row["etype"]
        value = extract_value(atype, content)
        if atype == "FQDN":
            value = normalise_fqdn(value)

        # `.keys()` is load-bearing here. `row` is a sqlite3.Row, not a dict, and
        # Row.__contains__ iterates *values*, so `"created" in row` is False even
        # when that column exists. Rewriting these to `in row` nulls out every
        # timestamp and silently breaks --since, --new and the merge window.
        created = row["created"] if "created" in row.keys() else None  # noqa: SIM118
        updated = row["updated"] if "updated" in row.keys() else None  # noqa: SIM118

        return Asset(
            id=row["eid"],
            type=atype,
            value=value,
            attrs=content,
            first_seen=str(created) if created else None,
            last_seen=str(updated) if updated else None,
        )

    def _asset_select(self) -> str:
        created = f'"{self.c_ecreated}"' if self.c_ecreated else "NULL"
        updated = f'"{self.c_eupdated}"' if self.c_eupdated else "NULL"
        return (
            f'SELECT "{self.c_eid}" AS eid, "{self.c_etype}" AS etype, '
            f'"{self.c_econtent}" AS content, {created} AS created, {updated} AS updated '
            f'FROM "{self.entity_table}"'
        )

    def assets_by_id(self, ids: Iterable[int] | None = None) -> dict[int, Asset]:
        """Load assets keyed by id. Passing ``None`` loads everything."""
        sql = self._asset_select()
        params: list[Any] = []
        ids = list(ids) if ids is not None else None
        if ids is not None:
            if not ids:
                return {}
            out: dict[int, Asset] = {}
            for i in range(0, len(ids), 900):
                chunk = ids[i : i + 900]
                q = f'{sql} WHERE "{self.c_eid}" IN ({",".join("?" * len(chunk))})'
                for r in self.conn.execute(q, chunk).fetchall():
                    a = self._row_to_asset(r)
                    out[a.id] = a
            return out
        rows = self.conn.execute(sql, params).fetchall()
        return {a.id: a for a in (self._row_to_asset(r) for r in rows)}

    # -- edges -------------------------------------------------------------

    def edges(
        self,
        assets: dict[int, Asset] | None = None,
        labels: Iterable[str] | None = None,
        since: datetime | None = None,
        with_sources: bool = False,
    ) -> list[Edge]:
        if not self.edge_table:
            return []
        if assets is None:
            assets = self.assets_by_id(None)

        created = f'"{self.c_rcreated}"' if self.c_rcreated else "NULL"
        updated = f'"{self.c_rupdated}"' if self.c_rupdated else "NULL"
        rid = f'"{self.c_rid}"' if self.c_rid else "rowid"
        rtype = f'"{self.c_rtype}"' if self.c_rtype else "''"
        rcontent = f'"{self.c_rcontent}"' if self.c_rcontent else "NULL"
        sql = (
            f"SELECT {rid} AS rid, {rtype} AS rtype, {rcontent} AS content, "
            f'"{self.c_rfrom}" AS rfrom, "{self.c_rto}" AS rto, '
            f"{created} AS created, {updated} AS updated "
            f'FROM "{self.edge_table}"'
        )
        rows = self.conn.execute(sql).fetchall()

        label_set = set(labels) if labels else None
        out: list[Edge] = []
        for r in rows:
            src_a, dst_a = assets.get(r["rfrom"]), assets.get(r["rto"])
            if src_a is None or dst_a is None:
                continue  # dangling edge; Amass leaves a few behind
            try:
                content = json.loads(r["content"]) if r["content"] else {}
            except (json.JSONDecodeError, TypeError):
                content = {}
            if not isinstance(content, dict):
                content = {}

            attrs = dict(content)
            header = attrs.pop("header", None)
            if isinstance(header, dict):
                attrs.update(
                    {k: v for k, v in header.items() if k in ("rr_type", "class", "ttl")}
                )
            # v5 puts the label in the content blob; v4 used the relation type
            # itself as the label ("a_record", "contains", "announces").
            label = str(attrs.get("label", "") or "") or str(r["rtype"] or "")
            if label_set is not None and label not in label_set:
                continue

            out.append(
                Edge(
                    id=r["rid"],
                    type=r["rtype"] or "",
                    label=label,
                    from_asset=src_a,
                    to_asset=dst_a,
                    attrs=attrs,
                    first_seen=str(r["created"]) if r["created"] else None,
                    last_seen=str(r["updated"]) if r["updated"] else None,
                )
            )

        if since is not None:
            out = [
                e for e in out
                if (ts := _parse_ts(e.last_seen)) is None or ts >= since
            ]

        if with_sources:
            src = self._load_sources(
                self.edge_tag_table, self.c_rid or "edge_id", {e.id for e in out}
            )
            for e in out:
                e.sources = src.get(e.id, [])

        return out

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> AssetDB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_db(path: str | None) -> AssetDB:
    """Open an explicit path, or auto-discover the default Amass database."""
    if path:
        return AssetDB(path)
    candidates = default_db_paths()
    for cand in candidates:
        try:
            return AssetDB(cand)
        except OamxError:
            continue
    hint = "\n  ".join(str(c) for c in candidates[:5]) or "(none found)"
    raise OamxError(
        "could not find an Amass asset database. Pass --db /path/to/amass.sqlite.\n"
        f"Looked at:\n  {hint}"
    )


def warn(msg: str) -> None:
    print(f"oamx: {msg}", file=sys.stderr)
