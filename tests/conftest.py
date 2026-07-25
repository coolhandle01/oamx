"""Shared pytest fixtures.

New tests are plain pytest functions that take the fixtures below. The
remaining ``unittest.TestCase`` classes in ``test_oamx.py`` predate the move to
pytest, run natively under it, and are converted opportunistically rather than
in one sweep - a mechanical rewrite of 50 assertions is a good way to weaken a
suite without noticing.

Both entry points resolve through ``fixtures.shared_databases()``, so the
databases are built once per process no matter which style asks for them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import fixtures


@pytest.fixture(scope="session")
def v5_db() -> Path:
    """An Amass v5 database: `entities`/`edges`, provenance tags, port relations."""
    return fixtures.shared_databases()[0]


@pytest.fixture(scope="session")
def v4_db() -> Path:
    """An Amass v4 database holding the same entities in the older layout.

    Same asset rows as `v5_db`, but relations are named after the DNS record
    type (`a_record`) instead of carrying a numeric `rr_type`, and there are no
    port relations. Anything that reads an edge label or a column name gets
    asserted against both.
    """
    return fixtures.shared_databases()[1]


@pytest.fixture(params=["v5", "v4"])
def any_db(request: pytest.FixtureRequest) -> Path:
    """Both layouts, one test.

    Take this instead of `v5_db` whenever the behaviour under test touches an
    edge label or a column name. `--resolved-only` shipped broken on v4 for
    exactly the want of this fixture.
    """
    v5, v4 = fixtures.shared_databases()
    return v5 if request.param == "v5" else v4


@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    """A well-formed v5 database with no rows - the "scan found nothing" case."""
    return fixtures.build_empty(tmp_path / "empty.sqlite")


@pytest.fixture
def garbage_db(tmp_path: Path) -> Path:
    """A SQLite file that is not an Amass database at all."""
    return fixtures.build_garbage(tmp_path / "junk.sqlite")


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin `cli.datetime.now()` to the fixtures' NOW.

    Time windows are asserted against fixed timestamps in the database, so a
    test that computed its cutoff from the real clock would start failing on
    its own schedule.
    """
    import oamx.cli as cli

    class _Frozen:
        @staticmethod
        def now(tz: object = None) -> object:
            return fixtures.NOW

    monkeypatch.setattr(cli, "datetime", _Frozen)
    yield


@pytest.fixture
def readonly_conn(v5_db: Path) -> Iterator[sqlite3.Connection]:
    """A raw read-only connection, for tests about the connection itself."""
    conn = sqlite3.connect(f"file:{v5_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()
