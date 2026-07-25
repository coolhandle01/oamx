"""oamx — get your data back out of the OWASP Amass asset database."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import __version__
from .model import SCHEMA_VERSION, Asset, Edge
from .reader import OamxError, open_db, parse_duration, warn
from .select import DEFAULT_SCOPE_DEPTH, Filters, Selection, build

EPILOG = """\
examples:
  oamx names -d example.com | httpx -silent
  oamx names -d example.com --resolved-only | dnsx -a -resp
  oamx targets -d example.com --urls | nuclei -severity high,critical
  oamx names -d example.com --new --since 24h        # only found since yesterday
  oamx json  -d example.com > assets.jsonl           # everything, with provenance
  oamx doctor                                        # what does my database contain?
"""

# command -> OAM asset types it selects
TYPE_COMMANDS: dict[str, tuple[str, ...]] = {
    "names": ("FQDN",),
    "ips": ("IPAddress",),
    "cidrs": ("Netblock",),
    "asns": ("AutonomousSystem",),
    "urls": ("URL",),
    "certs": ("TLSCertificate",),
    "services": ("Service",),
    "orgs": ("Organization",),
    "emails": ("Identifier", "ContactRecord"),
}

HTTP_PORTS = {80, 443, 8080, 8443, 8000, 8888, 3000, 4443, 9443}


def _split_list(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out.extend(part.strip() for part in v.split(",") if part.strip())
    return out


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", metavar="PATH",
                        help="path to the Amass SQLite database (default: auto-discover)")
    common.add_argument("-d", "--domain", action="append", metavar="DOMAIN",
                        help="restrict to this root domain (repeatable, comma-separated)")
    common.add_argument("--scope-depth", type=int, default=None, metavar="N",
                        help="graph hops used to scope non-name assets to -d "
                             f"(default: {DEFAULT_SCOPE_DEPTH}, 0 disables)")
    common.add_argument("--since", metavar="DUR",
                        help="only assets seen within this window, e.g. 24h, 7d, 2w")
    common.add_argument("--new", action="store_true",
                        help="with --since, match first-seen instead of last-seen "
                             "(i.e. genuinely new)")
    common.add_argument("--source", action="append", metavar="NAME",
                        help="only assets asserted by this Amass plugin (repeatable)")
    common.add_argument("--exclude-source", action="append", metavar="NAME",
                        help="drop assets whose only sources are these (repeatable)")
    common.add_argument("--min-confidence", type=int, default=0, metavar="N",
                        help="drop assets below this source confidence (0-100)")
    common.add_argument("--resolved-only", action="store_true",
                        help="only names that actually have a DNS record")
    common.add_argument("--json", action="store_true", dest="as_json",
                        help="emit newline-delimited JSON with provenance instead of bare values")
    common.add_argument("--count", action="store_true", help="print only the number of results")
    common.add_argument("--fail-empty", action="store_true",
                        help="exit 1 if nothing matched (useful in CI)")

    p = argparse.ArgumentParser(
        prog="oamx",
        description="Read the OWASP Amass asset database and emit pipe-friendly output.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version",
                   version=f"oamx {__version__} (schema {SCHEMA_VERSION})")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    descriptions = {
        "names": "fully qualified domain names",
        "ips": "IP addresses",
        "cidrs": "netblocks in CIDR notation",
        "asns": "autonomous system numbers",
        "urls": "discovered URLs",
        "certs": "TLS certificates",
        "services": "responding network services",
        "orgs": "organisations",
        "emails": "contact identifiers",
    }
    for name, desc in descriptions.items():
        sp = sub.add_parser(name, parents=[common], help=desc, description=desc)
        if name == "ips":
            sp.add_argument("--ipv4", action="store_true", help="IPv4 only")
            sp.add_argument("--ipv6", action="store_true", help="IPv6 only")

    sp = sub.add_parser("targets", parents=[common],
                        help="host:port pairs with a responding service",
                        description="host:port pairs with a responding service")
    sp.add_argument("--urls", action="store_true", dest="as_urls",
                    help="emit scheme://host:port instead of host:port")
    sp.add_argument("--port", action="append", metavar="N", help="only these ports (repeatable)")

    sub.add_parser("dns", parents=[common], help="DNS records as name/type/target triples",
                   description="DNS records as name/type/target triples")
    sub.add_parser("json", parents=[common], help="every matching asset as JSONL",
                   description="every matching asset as JSONL")
    sub.add_parser("graph", parents=[common], help="every matching relation as JSONL",
                   description="every matching relation as JSONL")
    sub.add_parser("stats", parents=[common], help="counts by asset type",
                   description="counts by asset type")

    dp = sub.add_parser("doctor", help="inspect the database and report what is in it",
                        description="inspect the database and report what is in it")
    dp.add_argument("--db", metavar="PATH", help="path to the Amass SQLite database")

    return p


def _scope_depth(args: argparse.Namespace) -> int:
    depth = getattr(args, "scope_depth", None)
    return DEFAULT_SCOPE_DEPTH if depth is None else max(0, depth)


def make_filters(args: argparse.Namespace, want_sources: bool = False) -> Filters:
    since = None
    if getattr(args, "since", None):
        since = datetime.now(timezone.utc) - parse_duration(args.since)
    elif getattr(args, "new", False):
        raise OamxError("--new needs --since, e.g. --new --since 24h")

    return Filters(
        domains=_split_list(getattr(args, "domain", None)),
        scope_depth=_scope_depth(args),
        since=since,
        since_field="created" if getattr(args, "new", False) else "updated",
        sources=_split_list(getattr(args, "source", None)),
        exclude_sources=_split_list(getattr(args, "exclude_source", None)),
        min_confidence=getattr(args, "min_confidence", 0),
        resolved_only=getattr(args, "resolved_only", False),
        want_sources=want_sources or getattr(args, "as_json", False),
    )


# --- emitters ---------------------------------------------------------------


def emit_lines(values: list[str], args: argparse.Namespace) -> int:
    if args.count:
        print(len(values))
        return 0 if values or not args.fail_empty else 1
    for v in values:
        print(v)
    return 0 if values or not args.fail_empty else 1


def emit_assets(assets: list[Asset], args: argparse.Namespace) -> int:
    if args.as_json:
        if args.count:
            print(len(assets))
        else:
            for a in assets:
                print(json.dumps(a.to_dict(), separators=(",", ":")))
        return 0 if assets or not args.fail_empty else 1
    return emit_lines([a.value for a in assets], args)


def cmd_types(sel: Selection, f: Filters, args: argparse.Namespace) -> int:
    assets = sel.matching(f, TYPE_COMMANDS[args.command])

    if args.command == "ips" and (args.ipv4 or args.ipv6):
        want = set()
        if args.ipv4:
            want.add("ipv4")
        if args.ipv6:
            want.add("ipv6")
        assets = [
            a for a in assets
            if str(a.attrs.get("type", "")).lower() in want
            or (not a.attrs.get("type") and (("." in a.value) == ("ipv4" in want)))
        ]

    return emit_assets(assets, args)


def cmd_targets(sel: Selection, f: Filters, args: argparse.Namespace) -> int:
    ports = {int(p) for p in _split_list(args.port)} if args.port else None
    rows: list[tuple[str, int, str]] = []  # host, port, service_type

    for e in sel.matching_edges(f, labels=["port"]):
        host = e.from_asset
        if host.type not in ("FQDN", "IPAddress"):
            continue
        if f.resolved_only and host.type == "FQDN" and host.id not in sel.resolved:
            continue
        port = e.attrs.get("port_number")
        if not isinstance(port, int):
            try:
                port = int(str(port))
            except (TypeError, ValueError):
                continue
        if ports is not None and port not in ports:
            continue
        stype = str(e.to_asset.attrs.get("service_type", "") or "")
        rows.append((host.value, port, stype))

    rows = sorted(set(rows))

    if args.as_json:
        if args.count:
            print(len(rows))
            return 0 if rows or not args.fail_empty else 1
        for host, port, stype in rows:
            print(json.dumps(
                {"schema": SCHEMA_VERSION, "kind": "target", "host": host,
                 "port": port, "service_type": stype},
                separators=(",", ":"),
            ))
        return 0 if rows or not args.fail_empty else 1

    out = []
    for host, port, stype in rows:
        if args.as_urls:
            scheme = _scheme_for(port, stype)
            if scheme is None:
                continue
            default = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
            out.append(f"{scheme}://{host}" if default else f"{scheme}://{host}:{port}")
        else:
            out.append(f"{host}:{port}")
    return emit_lines(sorted(set(out)), args)


def _scheme_for(port: int, service_type: str) -> str | None:
    st = service_type.lower()
    if "https" in st or "ssl" in st or "tls" in st:
        return "https"
    if "http" in st:
        return "http"
    if port in (443, 8443, 4443, 9443):
        return "https"
    if port in HTTP_PORTS:
        return "http"
    return None


def cmd_dns(sel: Selection, f: Filters, args: argparse.Namespace) -> int:
    lines = []
    for e in sel.matching_edges(f):
        if not e.is_dns:
            continue
        lines.append(f"{e.from_asset.value}\t{e.rr_name or '?'}\t{e.to_asset.value}")
    return emit_lines(sorted(set(lines)), args)


def cmd_json(sel: Selection, f: Filters, args: argparse.Namespace) -> int:
    args.as_json = True
    return emit_assets(sel.matching(f), args)


def cmd_graph(sel: Selection, f: Filters, args: argparse.Namespace) -> int:
    edges: list[Edge] = sel.matching_edges(f)
    if args.count:
        print(len(edges))
    else:
        for e in edges:
            print(json.dumps(e.to_dict(), separators=(",", ":")))
    return 0 if edges or not args.fail_empty else 1


def cmd_stats(sel: Selection, f: Filters, args: argparse.Namespace) -> int:
    assets = sel.matching(f)
    counts: dict[str, int] = {}
    for a in assets:
        counts[a.type] = counts.get(a.type, 0) + 1
    if args.as_json:
        print(json.dumps({"schema": SCHEMA_VERSION, "kind": "stats",
                          "total": len(assets), "types": counts},
                         separators=(",", ":")))
        return 0
    if not counts:
        warn("no assets matched")
        return 1 if args.fail_empty else 0
    width = max(len(t) for t in counts)
    for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"{t.ljust(width)}  {n}")
    print(f"{'TOTAL'.ljust(width)}  {len(assets)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    with open_db(args.db) as db:
        info = db.describe()
        print(f"database        {info['path']}")
        print(f"layout          {info['generation']} ({info['entity_table']}"
              f"{'/' + info['edge_table'] if info['edge_table'] else ', no relations table'})")
        print(f"provenance      {'yes' if info['entity_tag_table'] else 'not available'}")
        print(f"time filtering  {'SQL' if info['sql_time_pushdown'] else 'in-process'}")
        counts = db.type_counts()
        total = sum(counts.values())
        print(f"assets          {total}")
        if not total:
            print()
            print("The database is empty. If a scan reported findings but this shows zero,")
            print("Amass wrote to a different database — check -dir / config.yaml / AMASS_DB_*.")
            return 1
        width = max(len(t) for t in counts)
        print()
        for t, n in counts.items():
            print(f"  {t.ljust(width)}  {n}")
    return 0


DISPATCH = {
    "targets": cmd_targets,
    "dns": cmd_dns,
    "json": cmd_json,
    "graph": cmd_graph,
    "stats": cmd_stats,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 2

    try:
        if args.command == "doctor":
            return cmd_doctor(args)

        want_sources = args.command in ("json", "graph")
        f = make_filters(args, want_sources=want_sources)

        with open_db(args.db) as db:
            sel = build(db, f)
            if args.command in TYPE_COMMANDS:
                return cmd_types(sel, f, args)
            return DISPATCH[args.command](sel, f, args)

    except OamxError as exc:
        warn(str(exc))
        return 1
    except BrokenPipeError:
        # `oamx names | head` is a normal thing to do.
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
