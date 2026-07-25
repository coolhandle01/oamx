"""Unhappy paths in the reader.

``test_oamx.py`` covers what the tool does when the database is fine. This
module covers what it does when the database is missing, unreadable, half a
schema, or full of values nobody anticipated - the paths a user actually
meets, and the ones the suite was silently not exercising.

The bias throughout is degrade-rather-than-raise, and the assertions pin what
survives rather than only that nothing blew up. "It didn't crash" is satisfied
by returning nothing, which is the failure this tool exists to prevent.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from oamx.reader import (
    AssetDB,
    OamxError,
    _parse_ts,
    default_db_paths,
    open_db,
    parse_duration,
)
from tests import fixtures

# --- finding a database -----------------------------------------------------


class TestDatabaseDiscovery:
    """`open_db(None)` is the path every bare `oamx names` invocation takes."""

    def test_discovers_a_database_in_xdg_config_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        amass = tmp_path / "amass"
        amass.mkdir()
        fixtures.build_v5(amass / "amass.sqlite")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nohome"))
        monkeypatch.chdir(tmp_path / "nohome" if (tmp_path / "nohome").exists() else tmp_path)

        assert (amass / "amass.sqlite") in default_db_paths()

    def test_most_recently_modified_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ordering is the whole point of the sort: with several databases
        # around, the one Amass just wrote is the one you meant.
        amass = tmp_path / "amass"
        amass.mkdir()
        older = fixtures.build_v5(amass / "older.sqlite")
        newer = fixtures.build_v5(amass / "newer.sqlite")
        import os

        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nohome"))

        found = [p for p in default_db_paths() if p.parent == amass]
        assert found.index(newer) < found.index(older)

    def test_an_unreadable_directory_is_skipped_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One unreadable candidate directory must not stop discovery finding
        # the database sitting in the next one.
        #
        # The raising path has to be one `default_db_paths` actually calls
        # `is_dir` on - every candidate ends in `amass`, so keying on the name
        # of a *parent* segment silently never fires and the test passes while
        # exercising nothing.
        xdg = tmp_path / "xdg"
        (xdg / "amass").mkdir(parents=True)
        db = fixtures.build_v5(xdg / "amass" / "amass.sqlite")

        home = tmp_path / "home"
        unreadable = home / ".config" / "amass"
        unreadable.mkdir(parents=True)

        real_is_dir = Path.is_dir

        def exploding_is_dir(self: Path) -> bool:
            if self == unreadable:
                raise OSError("permission denied")
            return real_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", exploding_is_dir)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.chdir(tmp_path)

        assert db in default_db_paths()

    def test_a_candidate_that_is_not_a_database_is_stepped_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Discovery globs *.sqlite / *.db, which catches plenty of files that
        # are not Amass. open_db must walk past them to the real one rather
        # than failing on the first candidate.
        amass = tmp_path / "amass"
        amass.mkdir()
        decoy = amass / "aaa-decoy.sqlite"
        conn = sqlite3.connect(decoy)
        conn.execute("CREATE TABLE unrelated (a TEXT)")
        conn.commit()
        conn.close()
        real = fixtures.build_v5(amass / "zzz-real.sqlite")
        import os

        os.utime(decoy, (2_000_000, 2_000_000))  # decoy sorts first
        os.utime(real, (1_000_000, 1_000_000))

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nohome"))
        monkeypatch.chdir(tmp_path)

        with open_db(None) as db:
            assert Path(db.path) == real

    def test_nothing_found_says_what_it_looked_at(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The most-hit error message in the tool. It has to name the flag that
        # fixes it, or the user is left guessing.
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nohome"))
        monkeypatch.chdir(tmp_path)

        with pytest.raises(OamxError) as ctx:
            open_db(None)
        message = str(ctx.value)
        assert "could not find an Amass asset database" in message
        assert "--db" in message, "the message must name the flag that fixes it"

    def test_an_explicit_path_bypasses_discovery(self, tmp_path: Path) -> None:
        db = fixtures.build_v5(tmp_path / "explicit.sqlite")
        with open_db(str(db)) as conn:
            assert conn.generation == "v5"


# --- half a schema ----------------------------------------------------------


def _db_with(tmp_path: Path, ddl: str, name: str = "odd.sqlite") -> Path:
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.executescript(ddl)
    conn.commit()
    conn.close()
    return path


class TestPartialSchemas:
    def test_entities_without_a_recognised_id_column_is_explained(self, tmp_path: Path) -> None:
        path = _db_with(tmp_path, "CREATE TABLE entities (wat TEXT, huh TEXT);")
        with pytest.raises(OamxError) as ctx:
            AssetDB(path)
        message = str(ctx.value)
        assert "unrecognised layout" in message
        # Naming the columns it did find is what turns a bug report into a
        # one-line fix to the candidate list.
        assert "wat" in message and "huh" in message

    def test_an_edge_table_missing_its_endpoints_is_ignored_not_fatal(self, tmp_path: Path) -> None:
        # A relations table we cannot read endpoints from is dropped; the
        # assets are still perfectly readable and that is the common case.
        path = _db_with(
            tmp_path,
            """
            CREATE TABLE entities (entity_id INTEGER PRIMARY KEY, etype TEXT, content JSON);
            CREATE TABLE edges (edge_id INTEGER PRIMARY KEY, etype TEXT);
            INSERT INTO entities VALUES (1, 'FQDN', '{"name":"a.example.com"}');
            """,
        )
        with AssetDB(path) as db:
            assert db.edge_table is None
            assert db.edges() == [], "no edge table means no edges, not a crash"
            assert [a.value for a in db.assets_by_id(None).values()] == ["a.example.com"]

    def test_a_database_with_no_tags_table_yields_no_provenance(self, tmp_path: Path) -> None:
        path = _db_with(
            tmp_path,
            """
            CREATE TABLE entities (entity_id INTEGER PRIMARY KEY, etype TEXT, content JSON);
            INSERT INTO entities VALUES (1, 'FQDN', '{"name":"a.example.com"}');
            """,
        )
        with AssetDB(path) as db:
            assert db.entity_tag_table is None
            assert db._load_sources(None, "entity_id", {1}) == {}


# --- provenance that is not what we hoped -----------------------------------


class TestProvenanceRobustness:
    @pytest.fixture()
    def tagged(self, tmp_path: Path) -> Path:
        """A database whose tag rows are each malformed in a different way."""
        path = tmp_path / "tags.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE entities (entity_id INTEGER PRIMARY KEY, etype TEXT, content JSON);
            CREATE TABLE entity_tags (
                tag_id INTEGER PRIMARY KEY, ttype TEXT, content JSON, entity_id INTEGER
            );
            INSERT INTO entities VALUES (1, 'FQDN', '{"name":"a.example.com"}');
            """
        )
        rows = [
            (1, "SourceProperty", json.dumps({"name": "good", "confidence": 90}), 1),
            (2, "SourceProperty", "{not json at all", 1),
            (3, "SourceProperty", json.dumps(["a", "list"]), 1),
            (4, "SourceProperty", json.dumps({"confidence": 50}), 1),  # no name
            (5, "SourceProperty", json.dumps({"name": "nc", "confidence": "high"}), 1),
            (6, "VulnProperty", json.dumps({"name": "CVE-2026-1"}), 1),
            (7, "SourceProperty", None, 1),
        ]
        conn.executemany("INSERT INTO entity_tags VALUES (?,?,?,?)", rows)
        conn.commit()
        conn.close()
        return path

    def test_malformed_tags_are_skipped_and_the_good_one_survives(self, tagged: Path) -> None:
        with AssetDB(tagged) as db:
            sources = db._load_sources("entity_tags", "entity_id", {1})
        names = [s.name for s in sources[1]]
        # Pin the survivor, not just the absences: a bug that dropped
        # everything would satisfy every `not in` on its own.
        assert "good" in names
        assert "CVE-2026-1" not in names, "VulnProperty is not provenance"

    def test_a_non_numeric_confidence_becomes_unknown_rather_than_zero(self, tagged: Path) -> None:
        # Zero would be indistinguishable from a source that really is 0%
        # confident, and --min-confidence would then silently drop it.
        with AssetDB(tagged) as db:
            sources = db._load_sources("entity_tags", "entity_id", {1})
        nc = next(s for s in sources[1] if s.name == "nc")
        assert nc.confidence is None

    def test_asking_for_no_ids_does_not_query(self, tagged: Path) -> None:
        with AssetDB(tagged) as db:
            assert db._load_sources("entity_tags", "entity_id", set()) == {}


# --- content that is not a dict ---------------------------------------------


class TestMalformedContent:
    def test_unparseable_asset_content_still_yields_an_asset(self, tmp_path: Path) -> None:
        path = _db_with(
            tmp_path,
            """
            CREATE TABLE entities (entity_id INTEGER PRIMARY KEY, etype TEXT, content JSON);
            INSERT INTO entities VALUES (1, 'FQDN', '{broken');
            INSERT INTO entities VALUES (2, 'FQDN', '"just a string"');
            INSERT INTO entities VALUES (3, 'FQDN', NULL);
            """,
        )
        with AssetDB(path) as db:
            assets = db.assets_by_id(None)
        # Three rows in, three assets out. Dropping a row because its blob
        # would not parse is the silent-loss failure mode.
        assert set(assets) == {1, 2, 3}

    def test_a_bare_string_blob_is_kept_under_raw(self, tmp_path: Path) -> None:
        path = _db_with(
            tmp_path,
            """
            CREATE TABLE entities (entity_id INTEGER PRIMARY KEY, etype TEXT, content JSON);
            INSERT INTO entities VALUES (1, 'Weird', '"payload"');
            """,
        )
        with AssetDB(path) as db:
            asset = db.assets_by_id(None)[1]
        assert asset.attrs == {"raw": "payload"}


# --- id chunking ------------------------------------------------------------


class TestIdChunking:
    def test_more_ids_than_sqlite_allows_in_one_statement(self, tmp_path: Path) -> None:
        # SQLITE_MAX_VARIABLE_NUMBER defaults to 999 on older builds, so the
        # reader chunks at 900. Nothing exercised the second chunk.
        path = tmp_path / "many.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE entities (entity_id INTEGER PRIMARY KEY, etype TEXT, content JSON);"
        )
        total = 2100
        conn.executemany(
            "INSERT INTO entities VALUES (?,?,?)",
            [(i, "FQDN", json.dumps({"name": f"h{i}.example.com"})) for i in range(1, total + 1)],
        )
        conn.commit()
        conn.close()

        with AssetDB(path) as db:
            loaded = db.assets_by_id(list(range(1, total + 1)))
        assert len(loaded) == total, "a chunk boundary must not lose rows"

    def test_asking_for_no_ids_returns_nothing(self, tmp_path: Path) -> None:
        with AssetDB(fixtures.shared_databases()[0]) as db:
            assert db.assets_by_id([]) == {}


# --- timestamps -------------------------------------------------------------


class TestTimestampTolerance:
    @pytest.mark.parametrize(
        ("raw", "expected_year"),
        [
            ("2026-07-24 12:00:00.123456789+00:00", 2026),  # Go nanoseconds
            ("2026-07-24T12:00:00Z", 2026),
            ("2026-07-24T12:00:00", 2026),  # naive, stamped UTC
            ("2026-07-24", 2026),
            (1_700_000_000, 2023),  # epoch seconds
            (1_700_000_000.5, 2023),
        ],
    )
    def test_formats_gorm_actually_emits(self, raw: object, expected_year: int) -> None:
        parsed = _parse_ts(raw)
        assert parsed is not None, f"{raw!r} should parse"
        assert parsed.year == expected_year
        assert parsed.tzinfo is not None, "a naive datetime would break every comparison"

    @pytest.mark.parametrize("raw", [None, "", "   ", "nonsense", "not-a-date", 10**20])
    def test_unreadable_stamps_return_none_rather_than_raising(self, raw: object) -> None:
        # None means "keep this asset" to every caller. Raising here would
        # take out a whole query for one bad row.
        assert _parse_ts(raw) is None

    def test_a_naive_stamp_is_treated_as_utc(self) -> None:
        parsed = _parse_ts("2026-07-24T12:00:00")
        assert parsed == datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


class TestDurations:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [("30s", 30), ("30m", 1800), ("24h", 86400), ("7d", 604800), ("2w", 1209600)],
    )
    def test_every_documented_unit(self, text: str, seconds: int) -> None:
        assert parse_duration(text).total_seconds() == seconds

    @pytest.mark.parametrize("text", ["", "soon", "24", "h", "1y", "abch"])
    def test_a_bad_duration_names_the_format(self, text: str) -> None:
        with pytest.raises(OamxError) as ctx:
            parse_duration(text)
        assert "duration" in str(ctx.value)

    def test_case_and_whitespace_are_forgiven(self) -> None:
        assert parse_duration("  24H  ").total_seconds() == 86400


# --- lifecycle --------------------------------------------------------------


class TestEdgeRobustness:
    @pytest.fixture()
    def edgy(self, tmp_path: Path) -> Path:
        """Edges whose content blobs are each broken differently."""
        path = tmp_path / "edges.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE entities (entity_id INTEGER PRIMARY KEY, etype TEXT, content JSON);
            CREATE TABLE edges (
                edge_id INTEGER PRIMARY KEY, etype TEXT, content JSON,
                from_entity_id INTEGER, to_entity_id INTEGER
            );
            CREATE TABLE edge_tags (
                tag_id INTEGER PRIMARY KEY, ttype TEXT, content JSON, edge_id INTEGER
            );
            INSERT INTO entities VALUES (1, 'FQDN', '{"name":"a.example.com"}');
            INSERT INTO entities VALUES (2, 'IPAddress', '{"address":"198.51.100.7"}');
            """
        )
        conn.executemany(
            "INSERT INTO edges VALUES (?,?,?,?,?)",
            [
                (1, "BasicDNSRelation", json.dumps({"label": "dns_record"}), 1, 2),
                (2, "SimpleRelation", "{not json", 1, 2),
                (3, "SimpleRelation", json.dumps(["a", "list"]), 1, 2),
                (4, "SimpleRelation", json.dumps({"label": "node"}), 1, 999),  # dangling
            ],
        )
        conn.execute(
            "INSERT INTO edge_tags VALUES (?,?,?,?)",
            (1, "SourceProperty", json.dumps({"name": "DNS-IP", "confidence": 100}), 1),
        )
        conn.commit()
        conn.close()
        return path

    def test_broken_edge_content_does_not_lose_the_edge(self, edgy: Path) -> None:
        with AssetDB(edgy) as db:
            edges = db.edges()
        # Three readable edges; only the dangling one is dropped, and it is
        # dropped for its endpoint, not its blob.
        assert {e.id for e in edges} == {1, 2, 3}

    def test_a_dangling_endpoint_is_the_only_reason_to_drop_an_edge(self, edgy: Path) -> None:
        with AssetDB(edgy) as db:
            assert 4 not in {e.id for e in db.edges()}

    def test_filtering_by_label_keeps_the_match(self, edgy: Path) -> None:
        with AssetDB(edgy) as db:
            dns = db.edges(labels=["dns_record"])
        assert [e.id for e in dns] == [1], "the labelled edge survives its own filter"

    def test_edge_provenance_loads_when_asked(self, edgy: Path) -> None:
        with AssetDB(edgy) as db:
            edges = db.edges(with_sources=True)
        dns = next(e for e in edges if e.id == 1)
        assert [s.name for s in dns.sources] == ["DNS-IP"]


class TestUnopenableDatabase:
    def test_a_directory_is_not_a_database(self, tmp_path: Path) -> None:
        # `path.exists()` passes for a directory, so this reaches the connect
        # call and has to come back as an OamxError rather than an sqlite3 one.
        target = tmp_path / "adirectory"
        target.mkdir()
        with pytest.raises(OamxError):
            AssetDB(target)

    def test_a_file_that_is_not_sqlite_at_all(self, tmp_path: Path) -> None:
        # sqlite3.connect is lazy, so the failure lands on the first query in
        # _introspect rather than on connect. Unwrapped, that reaches the user
        # as a raw sqlite3.DatabaseError traceback - main() only catches
        # OamxError.
        path = tmp_path / "text.sqlite"
        path.write_text("this is not a database", encoding="utf-8")
        with pytest.raises(OamxError) as ctx:
            AssetDB(path)
        assert str(path) in str(ctx.value), "the message must name the file"

    def test_discovery_steps_over_a_file_that_is_not_sqlite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The sharper consequence. Discovery globs *.sqlite / *.sqlite3 / *.db
        # and searches the current working directory, so a stray file with one
        # of those names is entirely realistic. open_db's loop only catches
        # OamxError, so anything else aborts discovery even though a perfectly
        # good database is sitting next to it.
        amass = tmp_path / "amass"
        amass.mkdir()
        (amass / "aaa-notes.sqlite").write_text("someone's notes", encoding="utf-8")
        real = fixtures.build_v5(amass / "zzz-real.sqlite")
        import os

        os.utime(amass / "aaa-notes.sqlite", (2_000_000, 2_000_000))  # sorts first
        os.utime(real, (1_000_000, 1_000_000))

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nohome"))
        monkeypatch.chdir(tmp_path)

        with open_db(None) as db:
            assert Path(db.path) == real


class TestLifecycle:
    def test_close_releases_the_connection(self, tmp_path: Path) -> None:
        db = AssetDB(fixtures.build_v5(tmp_path / "x.sqlite"))
        db.close()
        with pytest.raises(sqlite3.ProgrammingError):
            db.conn.execute("SELECT 1")

    def test_the_context_manager_closes_on_exit(self, tmp_path: Path) -> None:
        with AssetDB(fixtures.build_v5(tmp_path / "y.sqlite")) as db:
            assert db.type_counts()
        with pytest.raises(sqlite3.ProgrammingError):
            db.conn.execute("SELECT 1")
