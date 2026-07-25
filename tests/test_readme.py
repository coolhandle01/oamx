"""The README's sample output has to be the tool's actual output.

A README that shows what a command prints is making a factual claim, and the
`doctor` block in this one was wrong for most of the project's life: it
claimed 22 assets over a breakdown that summed to 15, listed a `time
filtering` line for a code path that no longer exists, and omitted asset types
the fixture had always contained. None of that was catchable by reading it.

So it is asserted instead. The fixture is deterministic, so the only line that
cannot match verbatim is the database path.
"""

from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from oamx.cli import main
from tests import fixtures

README = Path(__file__).resolve().parents[1] / "README.md"


def _fenced_blocks(text: str) -> list[str]:
    """Every ``` fenced block in the document, fence lines stripped."""
    return re.findall(r"^```[a-z]*\n(.*?)^```", text, flags=re.MULTILINE | re.DOTALL)


def _doctor_block() -> str:
    blocks = [b for b in _fenced_blocks(README.read_text(encoding="utf-8")) if "layout " in b]
    assert len(blocks) == 1, f"expected exactly one doctor block, found {len(blocks)}"
    return blocks[0]


def _drop_path_line(text: str) -> list[str]:
    """Every line except the `database` one, which is machine-specific."""
    return [line for line in text.splitlines() if not line.startswith("database ")]


@pytest.fixture(scope="module")
def real_doctor_output() -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["doctor", "--db", str(fixtures.shared_databases()[0])])
    return buf.getvalue()


def test_the_doctor_block_is_what_doctor_prints(real_doctor_output: str) -> None:
    assert _drop_path_line(_doctor_block()) == _drop_path_line(real_doctor_output)


def test_the_asset_counts_actually_add_up(real_doctor_output: str) -> None:
    # The original block claimed a total its own breakdown contradicted, which
    # is the tell that it was written by hand rather than captured.
    lines = _drop_path_line(_doctor_block())
    total = next(int(line.split()[-1]) for line in lines if line.startswith("assets "))
    per_type = [int(line.split()[-1]) for line in lines if line.startswith("  ")]
    assert sum(per_type) == total, f"breakdown sums to {sum(per_type)}, header says {total}"


def test_the_block_shows_a_plausible_database_path(real_doctor_output: str) -> None:
    # The one line that is allowed to differ still has to look like the thing
    # it is standing in for, or the example stops being useful.
    shown = next(line for line in _doctor_block().splitlines() if line.startswith("database "))
    assert shown.split(maxsplit=1)[1].strip().endswith(".sqlite")


def test_no_documented_command_has_been_removed() -> None:
    """Every command in the README's table still exists in the parser.

    `time filtering` outlived the code path it described because nothing tied
    the prose to the program. The command table is the other place that rot
    would show up.
    """
    from oamx.cli import build_parser

    text = README.read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `([a-z]+)` \|", text, flags=re.MULTILINE))
    subparsers = build_parser()._subparsers  # noqa: SLF001 - argparse has no public accessor
    assert subparsers is not None
    known = {
        name
        for action in subparsers._group_actions  # noqa: SLF001
        for name in getattr(action, "choices", {})
    }
    missing = sorted(documented - known)
    assert not missing, f"README documents commands that do not exist: {missing}"
