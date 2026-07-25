"""The `emails` view, and the CLI/library drift it sat in.

OAM's `Identifier` holds the value in `id` and the scheme in `id_type`;
`unique_id` is a dedupe key. `ContactRecord` is a bare join node whose only
field, `discovered_at`, is where a contact was found - a URL, never an
address. Both facts are checked against the upstream struct in
owasp-amass/open-asset-model, not inferred from the field names.

So an email is an `Identifier` with `id_type == "email"`, reported as its
`id`. Anything else in this view is something the user then has to filter
out by hand, which is the job they came here to avoid.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from oamx.cli import TYPE_COMMANDS, main
from oamx.integrations import VIEWS, query, values
from tests import fixtures


def _cli(*argv: str) -> list[str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    assert code in (0, 1), f"unexpected exit code {code}: {err.getvalue()}"
    return [line for line in out.getvalue().splitlines() if line]


@pytest.fixture(scope="module")
def db() -> Path:
    return fixtures.shared_databases()[0]


class TestEmailsView:
    def test_reports_the_address_not_the_dedupe_key(self, db: Path) -> None:
        # `unique_id` is "email:abuse@example.com". Piping that into anything
        # that expects an address gives you a broken address.
        assert _cli("emails", "--db", str(db)) == ["abuse@example.com"]

    def test_a_non_email_identifier_is_not_an_email(self, db: Path) -> None:
        emails = _cli("emails", "--db", str(db))
        assert "abuse@example.com" in emails, "the real address must survive"
        assert "ORG-EX1-RIPE" not in emails, "a registry handle is not an email"

    def test_a_contact_record_is_not_an_email(self, db: Path) -> None:
        emails = _cli("emails", "--db", str(db))
        assert not any("rdap.example" in e for e in emails), (
            "ContactRecord.discovered_at is a URL, not an address"
        )

    def test_every_line_looks_like_an_address(self, db: Path) -> None:
        # The blunt version of the three above, and the one that would catch a
        # future asset type wandering into the view.
        for line in _cli("emails", "--db", str(db)):
            assert "@" in line, f"{line!r} is not an email address"


class TestLibraryParity:
    def test_the_library_can_ask_for_emails_at_all(self, db: Path) -> None:
        # `emails` was in the CLI's table and missing from the library's, so
        # this raised `unknown view` while the command line worked fine.
        assert values("emails", db=str(db)) == ["abuse@example.com"]

    def test_the_library_agrees_with_the_command_line(self, db: Path) -> None:
        assert values("emails", db=str(db)) == _cli("emails", "--db", str(db))

    def test_records_carry_the_identifier_type(self, db: Path) -> None:
        records = query("emails", db=str(db))
        assert [r["attrs"]["id_type"] for r in records] == ["email"]


class TestNoMoreDrift:
    """The structural fix, not just the symptom.

    Two tables listing view names in two modules is what let `emails` exist
    in one and not the other. These pin that they stay in step.
    """

    def test_every_cli_view_is_available_to_the_library(self) -> None:
        missing = sorted(set(TYPE_COMMANDS) - set(VIEWS))
        assert not missing, f"CLI commands with no library view: {missing}"

    def test_the_two_tables_select_the_same_asset_types(self) -> None:
        differing = {
            view: (TYPE_COMMANDS[view], VIEWS[view])
            for view in set(TYPE_COMMANDS) & set(VIEWS)
            if TYPE_COMMANDS[view] != VIEWS[view]
        }
        assert not differing, f"views selecting different types: {differing}"

    def test_the_library_only_adds_the_aggregate_view(self) -> None:
        # `all` is the one library view with no command-line equivalent; if
        # something else appears here, the two surfaces have drifted again.
        assert sorted(set(VIEWS) - set(TYPE_COMMANDS)) == ["all"]
