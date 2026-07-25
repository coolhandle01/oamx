"""A library-shaped front door, for callers that are not a shell.

The CLI is the primary interface, but scripts and agent frameworks want a
function that returns data rather than a subprocess that returns bytes.
``query`` is that function.

Deliberately free of any framework import. oamx is a tool for reading an
Amass database, not an agent library: a caller that wants to hand this to an
LLM builds the adapter on their side, against their own conventions for tool
schemas, return types and error handling. Trying to ship one here means
guessing at those conventions and carrying an optional dependency to do it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .model import VIEW_TYPES, Asset, in_view
from .reader import OamxError, open_db, parse_duration
from .select import DEFAULT_SCOPE_DEPTH, Filters, build

# The same table the CLI derives its subcommands from. Kept as a module
# attribute because callers import it to enumerate the views.
VIEWS = VIEW_TYPES


def query(
    view: str = "names",
    domains: list[str] | None = None,
    db: str | None = None,
    since: str | None = None,
    new_only: bool = False,
    resolved_only: bool = False,
    min_confidence: int = 0,
    sources: list[str] | None = None,
    exclude_sources: list[str] | None = None,
    scope_depth: int = DEFAULT_SCOPE_DEPTH,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Run one query against the Amass asset database.

    Returns a list of normalised asset dicts (see ``Asset.to_dict``). Raises
    ``OamxError`` with a message intended to be read by a human — or by an
    agent deciding what to do next.
    """
    if view not in VIEWS:
        raise OamxError(f"unknown view {view!r}; try one of: {', '.join(sorted(VIEWS))}")

    cutoff = None
    if since:
        cutoff = datetime.now(timezone.utc) - parse_duration(since)
    elif new_only:
        raise OamxError("new_only requires since, e.g. since='24h'")

    filters = Filters(
        domains=[d.strip() for d in (domains or []) if d.strip()],
        scope_depth=max(0, scope_depth),
        since=cutoff,
        since_field="created" if new_only else "updated",
        sources=sources or [],
        exclude_sources=exclude_sources or [],
        min_confidence=min_confidence,
        resolved_only=resolved_only,
        want_sources=True,
    )

    with open_db(db) as conn:
        selection = build(conn, filters)
        assets: list[Asset] = [
            a for a in selection.matching(filters, VIEWS[view] or None) if in_view(view, a)
        ]

    if limit is not None:
        assets = assets[:limit]
    return [a.to_dict() for a in assets]


def values(view: str = "names", **kwargs: Any) -> list[str]:
    """Just the values, for when you want a list of hostnames and nothing else."""
    return [record["value"] for record in query(view, **kwargs)]
