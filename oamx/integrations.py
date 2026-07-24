"""A library-shaped front door, for callers that are not a shell.

The CLI is the primary interface, but agent frameworks and scripts want a
function that returns data rather than a subprocess that returns bytes.
``query`` is that function. It is deliberately free of any framework import so
it can be tested on its own; the framework adapters below are thin wrappers
around it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .model import Asset
from .reader import OamxError, open_db, parse_duration
from .select import DEFAULT_SCOPE_DEPTH, Filters, build

# What each named view selects. Mirrors the CLI subcommands.
VIEWS: dict[str, tuple[str, ...]] = {
    "names": ("FQDN",),
    "ips": ("IPAddress",),
    "cidrs": ("Netblock",),
    "asns": ("AutonomousSystem",),
    "urls": ("URL",),
    "certs": ("TLSCertificate",),
    "services": ("Service",),
    "orgs": ("Organization",),
    "all": (),
}


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
        assets: list[Asset] = selection.matching(filters, VIEWS[view] or None)

    if limit is not None:
        assets = assets[:limit]
    return [a.to_dict() for a in assets]


def values(view: str = "names", **kwargs: Any) -> list[str]:
    """Just the values, for when you want a list of hostnames and nothing else."""
    return [record["value"] for record in query(view, **kwargs)]


# --- framework adapters -----------------------------------------------------

_TOOL_DESCRIPTION = """\
Read reconnaissance results that OWASP Amass has already collected into its
asset database. Does NOT run any scan or send any traffic; it only reads what
is already stored.

view: one of names, ips, cidrs, asns, urls, certs, services, orgs, all
domains: root domains to scope results to, e.g. ["example.com"]
since: only assets seen within this window, e.g. "24h", "7d"
new_only: with since, return only assets first discovered in that window
resolved_only: only hostnames that actually have a DNS record
min_confidence: drop assets below this source confidence (0-100)
"""


def crewai_tool(default_db: str | None = None, **defaults: Any):
    """Build a CrewAI tool wrapping :func:`query`.

    Imported lazily so that CrewAI is never a hard dependency of oamx.
    """
    try:
        from crewai.tools import BaseTool
    except ImportError as exc:  # pragma: no cover - depends on the caller's env
        raise OamxError(
            "crewai is not installed. `pip install oamx[crewai]`, or use "
            "oamx.integrations.query() directly."
        ) from exc

    from pydantic import BaseModel, Field

    class OamxInput(BaseModel):
        view: str = Field(default="names", description="names, ips, cidrs, asns, urls, certs, services, orgs, all")
        domains: list[str] = Field(default_factory=list, description="root domains to scope to")
        since: str | None = Field(default=None, description='time window, e.g. "24h"')
        new_only: bool = Field(default=False, description="only newly discovered assets")
        resolved_only: bool = Field(default=False, description="only names with a DNS record")
        min_confidence: int = Field(default=0, description="minimum source confidence, 0-100")
        limit: int | None = Field(default=None, description="cap the number of results")

    class OamxTool(BaseTool):  # pragma: no cover - requires crewai at runtime
        name: str = "amass_asset_db"
        description: str = _TOOL_DESCRIPTION
        args_schema: type[BaseModel] = OamxInput

        def _run(self, **kwargs: Any) -> str:
            params = {**defaults, **{k: v for k, v in kwargs.items() if v is not None}}
            params.setdefault("db", default_db)
            try:
                records = query(**params)
            except OamxError as exc:
                return f"error: {exc}"
            if not records:
                return "no matching assets in the Amass database"
            lines = [f"{r['type']}\t{r['value']}" for r in records]
            return "\n".join(lines)

    return OamxTool()
