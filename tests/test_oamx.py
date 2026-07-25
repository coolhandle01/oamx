"""Tests for oamx.

Deliberately stdlib-only, like the tool itself:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oamx.cli import main  # noqa: E402
from oamx.integrations import query, values  # noqa: E402
from oamx.model import Asset, Edge, extract_value, normalise_fqdn  # noqa: E402
from oamx.reader import AssetDB, OamxError, _parse_ts, parse_duration  # noqa: E402
from oamx.select import resolved_fqdns  # noqa: E402
from tests import fixtures  # noqa: E402

# The same two databases the conftest fixtures hand to pytest-style tests;
# built once per process and shared, not rebuilt per entry point.
V5, V4 = fixtures.shared_databases()


class CliCase(unittest.TestCase):
    """Base class providing a CLI runner that captures output."""

    def run_cli(self, *argv: str) -> list[str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        self.assertIn(code, (0, 1), f"unexpected exit code {code}: {err.getvalue()}")
        return [line for line in out.getvalue().splitlines() if line]

    def run_cli_full(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()


# --- unit -------------------------------------------------------------------


class TestUnits(unittest.TestCase):
    def test_normalise_fqdn(self):
        self.assertEqual(normalise_fqdn("WWW.Example.COM."), "www.example.com")
        self.assertEqual(normalise_fqdn("  a.b.  "), "a.b")

    def test_extract_value_known_types(self):
        self.assertEqual(extract_value("FQDN", {"name": "a.com"}), "a.com")
        self.assertEqual(
            extract_value("IPAddress", {"address": "1.2.3.4", "type": "IPv4"}), "1.2.3.4"
        )
        self.assertEqual(extract_value("AutonomousSystem", {"number": 15133}), "15133")
        self.assertEqual(extract_value("Netblock", {"cidr": "10.0.0.0/8"}), "10.0.0.0/8")

    def test_extract_value_survives_unknown_types(self):
        # An asset type invented after this release must still yield something.
        self.assertEqual(extract_value("Quantum", {"name": "q"}), "q")
        self.assertEqual(extract_value("Quantum", {"weird": 1}), '{"weird":1}')
        self.assertEqual(extract_value("Quantum", None), "")

    def test_parse_duration(self):
        self.assertEqual(parse_duration("24h").total_seconds(), 86400)
        self.assertEqual(parse_duration("7d").days, 7)
        self.assertEqual(parse_duration("2w").days, 14)
        with self.assertRaises(OamxError):
            parse_duration("soon")

    def test_parse_go_nanosecond_timestamps(self):
        dt = _parse_ts("2026-07-24 12:00:00.123456789+00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.microsecond, 123456)
        self.assertIsNotNone(_parse_ts("2026-07-24T12:00:00Z"))
        self.assertIsNone(_parse_ts(""))
        self.assertIsNone(_parse_ts("nonsense"))


# --- schema introspection ---------------------------------------------------


class TestSchemaIntrospection(unittest.TestCase):
    def test_detects_v5_layout(self):
        with AssetDB(V5) as db:
            info = db.describe()
        self.assertEqual(info["generation"], "v5")
        self.assertEqual(info["entity_table"], "entities")
        self.assertEqual(info["edge_table"], "edges")
        self.assertEqual(info["entity_tag_table"], "entity_tags")

    def test_detects_v4_layout(self):
        with AssetDB(V4) as db:
            info = db.describe()
        self.assertEqual(info["generation"], "v4")
        self.assertEqual(info["entity_table"], "assets")
        self.assertEqual(info["edge_table"], "relations")

    def test_rejects_non_amass_database(self):
        with tempfile.TemporaryDirectory() as d:
            path = fixtures.build_garbage(Path(d) / "junk.sqlite")
            with self.assertRaises(OamxError) as ctx:
                AssetDB(path)
            self.assertIn("does not look like an Amass", str(ctx.exception))

    def test_missing_file(self):
        with self.assertRaises(OamxError) as ctx:
            AssetDB("/nonexistent/absent.sqlite")
        self.assertIn("no such database", str(ctx.exception))

    def test_opens_read_only(self):
        # Specifically OperationalError, not bare Exception: a typo in the SQL
        # would also raise, and would look exactly like the connection refusing
        # the write.
        with AssetDB(V5) as db, self.assertRaises(sqlite3.OperationalError) as ctx:
            db.conn.execute("DELETE FROM entities")
        self.assertIn("readonly", str(ctx.exception))


# --- names ------------------------------------------------------------------


class TestNames(CliCase):
    def test_scoped_and_normalised(self):
        names = self.run_cli("names", "--db", str(V5), "-d", "example.com")
        self.assertEqual(names, sorted(names), "output should be stable/sorted")
        self.assertIn("www.example.com", names)
        self.assertIn("api.example.com", names)
        self.assertEqual(names.count("api.example.com"), 1, "API.Example.COM. must dedupe")
        self.assertNotIn("other.co.uk", names, "a different target must not leak in")
        self.assertNotIn("cdn.provider.net", names, "CNAME target outside scope must not leak in")

    def test_unscoped_returns_everything(self):
        names = self.run_cli("names", "--db", str(V5))
        self.assertIn("other.co.uk", names)
        self.assertIn("cdn.provider.net", names)

    def test_resolved_only_drops_unresolved(self):
        allnames = self.run_cli("names", "--db", str(V5), "-d", "example.com")
        resolved = self.run_cli("names", "--db", str(V5), "-d", "example.com", "--resolved-only")
        self.assertIn("dev.example.com", allnames)
        self.assertNotIn("dev.example.com", resolved)

    def test_count_flag(self):
        full = self.run_cli("names", "--db", str(V5), "-d", "example.com")
        counted = self.run_cli("names", "--db", str(V5), "-d", "example.com", "--count")
        self.assertEqual(counted, [str(len(full))])

    def test_comma_separated_domains(self):
        names = self.run_cli("names", "--db", str(V5), "-d", "example.com,other.co.uk")
        self.assertIn("www.example.com", names)
        self.assertIn("other.co.uk", names)


# --- graph scoping ----------------------------------------------------------


class TestScoping(CliCase):
    def test_ips_scoped_via_graph(self):
        ips = self.run_cli("ips", "--db", str(V5), "-d", "example.com")
        self.assertIn("93.184.216.34", ips)
        self.assertNotIn("203.0.113.9", ips, "the other target's address must not be in scope")

    def test_ip_family_filter(self):
        v4only = self.run_cli("ips", "--db", str(V5), "-d", "example.com", "--ipv4")
        v6only = self.run_cli("ips", "--db", str(V5), "-d", "example.com", "--ipv6")
        self.assertIn("93.184.216.34", v4only)
        self.assertNotIn("2606:2800:220:1::1946", v4only)
        self.assertIn("2606:2800:220:1::1946", v6only)
        self.assertNotIn("93.184.216.34", v6only)

    def test_scope_depth_controls_reach(self):
        # a netblock is two hops from a name: name -> ip -> netblock
        self.assertEqual(
            self.run_cli("cidrs", "--db", str(V5), "-d", "example.com"), ["93.184.216.0/24"]
        )
        self.assertEqual(
            self.run_cli("cidrs", "--db", str(V5), "-d", "example.com", "--scope-depth", "1"), []
        )
        # the ASN is three hops away
        self.assertEqual(self.run_cli("asns", "--db", str(V5), "-d", "example.com"), [])
        self.assertEqual(
            self.run_cli("asns", "--db", str(V5), "-d", "example.com", "--scope-depth", "3"),
            ["15133"],
        )


# --- targets ----------------------------------------------------------------


class TestTargets(CliCase):
    def test_host_port(self):
        targets = self.run_cli("targets", "--db", str(V5), "-d", "example.com")
        self.assertIn("www.example.com:443", targets)
        self.assertIn("www.example.com:80", targets)

    def test_urls_collapse_default_ports(self):
        urls = self.run_cli("targets", "--db", str(V5), "-d", "example.com", "--urls")
        self.assertIn("https://www.example.com", urls)
        self.assertNotIn("https://www.example.com:443", urls)

    def test_scheme_follows_service_type(self):
        urls = self.run_cli("targets", "--db", str(V5), "-d", "example.com", "--urls")
        self.assertIn("http://www.example.com", urls, "port 80 runs http")
        self.assertIn("https://www.example.com", urls, "port 443 runs https")
        self.assertNotIn("https://www.example.com:80", urls)

    def test_non_web_services_are_omitted_from_urls_but_kept_in_targets(self):
        urls = self.run_cli("targets", "--db", str(V5), "-d", "example.com", "--urls")
        targets = self.run_cli("targets", "--db", str(V5), "-d", "example.com")
        self.assertIn("api.example.com:22", targets)
        self.assertFalse([u for u in urls if ":22" in u], "ssh is not a URL")

    def test_port_filter(self):
        self.assertEqual(
            self.run_cli("targets", "--db", str(V5), "--port", "443"), ["www.example.com:443"]
        )

    def test_json_targets(self):
        lines = self.run_cli("targets", "--db", str(V5), "--port", "443", "--json")
        record = json.loads(lines[0])
        self.assertEqual(record["kind"], "target")
        self.assertEqual(record["port"], 443)
        self.assertEqual(record["service_type"], "https")


# --- dns --------------------------------------------------------------------


class TestDns(CliCase):
    def test_triples_v5(self):
        lines = self.run_cli("dns", "--db", str(V5), "-d", "example.com")
        self.assertIn("www.example.com\tA\t93.184.216.34", lines)
        self.assertIn("example.com\tAAAA\t2606:2800:220:1::1946", lines)

    def test_triples_v4(self):
        lines = self.run_cli("dns", "--db", str(V4))
        self.assertIn("www.example.com\tA\t93.184.216.34", lines)
        self.assertIn("example.com\tAAAA\t2606:2800:220:1::1946", lines)


# --- time filters -----------------------------------------------------------


class TestTimeFilters(CliCase):
    def test_since_window(self):
        import oamx.cli as cli

        frozen = mock.MagicMock()
        frozen.now.return_value = fixtures.NOW
        with mock.patch.object(cli, "datetime", frozen):
            recent = self.run_cli("names", "--db", str(V5), "-d", "example.com", "--since", "24h")
            brand_new = self.run_cli(
                "names", "--db", str(V5), "-d", "example.com", "--new", "--since", "24h"
            )

        self.assertNotIn("dev.example.com", recent, "last seen 30 days ago")
        self.assertIn("www.example.com", recent)
        self.assertIn("new.example.com", brand_new, "first seen 2 hours ago")
        self.assertNotIn("www.example.com", brand_new, "first seen 30 days ago")
        # api.example.com has a row created 2 hours ago, but the same host was
        # already known from a certificate 30 days ago. Rediscovery is not
        # discovery; alerting on it is how monitoring pipelines cry wolf.
        self.assertNotIn("api.example.com", brand_new, "rediscovered, not new")

    def test_new_requires_since(self):
        code, _, err = self.run_cli_full("names", "--db", str(V5), "--new")
        self.assertEqual(code, 1)
        self.assertIn("--new needs --since", err)


# --- provenance -------------------------------------------------------------


class TestProvenance(CliCase):
    def test_source_filter(self):
        only_crtsh = self.run_cli("names", "--db", str(V5), "--source", "crtsh")
        self.assertIn("api.example.com", only_crtsh)
        self.assertNotIn("dev.example.com", only_crtsh)

    def test_exclude_source_drops_brute_force(self):
        names = self.run_cli("names", "--db", str(V5), "--exclude-source", "brute-forcing")
        self.assertNotIn("dev.example.com", names)
        self.assertIn("www.example.com", names)

    def test_min_confidence(self):
        high = self.run_cli("names", "--db", str(V5), "--min-confidence", "90")
        low = self.run_cli("names", "--db", str(V5), "--min-confidence", "40")
        self.assertIn("www.example.com", high)
        self.assertNotIn("dev.example.com", high, "brute force asserts only 50")
        self.assertIn("dev.example.com", low)

    def test_exclude_source_keeps_corroborated_assets(self):
        # www.example.com is asserted by both crtsh and DNS-IP. Excluding one
        # must not discard evidence the other independently provided.
        names = self.run_cli("names", "--db", str(V5), "--exclude-source", "crtsh")
        self.assertIn("www.example.com", names)

    def test_merge_unions_provenance_and_widens_window(self):
        lines = self.run_cli("json", "--db", str(V5), "-d", "example.com")
        records = {json.loads(line)["value"]: json.loads(line) for line in lines}
        api = records["api.example.com"]
        # Two rows, two sources, one host.
        self.assertEqual({s["name"] for s in api["sources"]}, {"crtsh", "DNS-IP"})
        # The window spans both rows: earliest first sighting, latest last.
        self.assertTrue(api["first_seen"].startswith("2026-06-24"), api["first_seen"])
        self.assertTrue(api["last_seen"].startswith("2026-07-24"), api["last_seen"])
        # Where both rows name the same source, the higher confidence wins.
        crtsh = next(s for s in api["sources"] if s["name"] == "crtsh")
        self.assertEqual(crtsh["confidence"], 95)

    def test_merge_keeps_recently_seen_hosts_in_the_since_window(self):
        import oamx.cli as cli

        frozen = mock.MagicMock()
        frozen.now.return_value = fixtures.NOW
        with mock.patch.object(cli, "datetime", frozen):
            recent = self.run_cli("names", "--db", str(V5), "-d", "example.com", "--since", "24h")
        # One row for this host was last seen 30 days ago, the other 2 hours
        # ago. The host is current.
        self.assertIn("api.example.com", recent)


# --- json / graph -----------------------------------------------------------


class TestStructuredOutput(CliCase):
    def test_json_records_carry_provenance(self):
        lines = self.run_cli("json", "--db", str(V5), "-d", "example.com")
        records = [json.loads(line) for line in lines]
        self.assertTrue(all(r["schema"] == "oamx/1" for r in records))
        self.assertTrue(all(r["kind"] == "asset" for r in records))
        www = next(r for r in records if r["value"] == "www.example.com")
        # Only SourceProperty tags are provenance. www.example.com also carries
        # a VulnProperty whose content has a "name" key; it must not be
        # mistaken for a data source.
        self.assertEqual({s["name"] for s in www["sources"]}, {"DNS-IP", "crtsh"})
        self.assertTrue(www["first_seen"])
        self.assertTrue(www["last_seen"])

    def test_graph_records_decode_rr_types(self):
        lines = self.run_cli("graph", "--db", str(V5), "-d", "example.com")
        records = [json.loads(line) for line in lines]
        a_rec = next(
            r for r in records
            if r["label"] == "dns_record"
            and r["from"]["value"] == "www.example.com"
            and r["to"]["value"] == "93.184.216.34"
        )
        self.assertEqual(a_rec["attrs"]["rr_name"], "A")
        self.assertEqual(a_rec["attrs"]["ttl"], 300)

    def test_dangling_edge_is_skipped(self):
        lines = self.run_cli("graph", "--db", str(V5))
        for line in lines:
            self.assertNotEqual(json.loads(line)["from"]["value"], "999")

    def test_unknown_asset_type_survives(self):
        out = self.run_cli("stats", "--db", str(V5))
        self.assertTrue(any("SomeFutureAssetType" in line for line in out))


# --- doctor -----------------------------------------------------------------


class TestDoctor(CliCase):
    def test_reports_layout(self):
        out = "\n".join(self.run_cli("doctor", "--db", str(V5)))
        self.assertIn("v5", out)
        self.assertIn("entities/edges", out)
        self.assertIn("FQDN", out)

    def test_flags_empty_database(self):
        with tempfile.TemporaryDirectory() as d:
            path = fixtures.build_empty(Path(d) / "empty.sqlite")
            code, out, _ = self.run_cli_full("doctor", "--db", str(path))
        self.assertEqual(code, 1)
        self.assertIn("empty", out)


# --- exit codes and v4 parity -----------------------------------------------


class TestExitCodes(CliCase):
    def test_fail_empty(self):
        code, _, _ = self.run_cli_full(
            "names", "--db", str(V5), "-d", "nothing.invalid", "--fail-empty"
        )
        self.assertEqual(code, 1)
        code, _, _ = self.run_cli_full("names", "--db", str(V5), "-d", "nothing.invalid")
        self.assertEqual(code, 0)

    def test_missing_db_is_a_clean_error(self):
        code, _, err = self.run_cli_full("names", "--db", "/nonexistent/x.sqlite")
        self.assertEqual(code, 1)
        self.assertIn("no such database", err)

    def test_v4_end_to_end(self):
        names = self.run_cli("names", "--db", str(V4), "-d", "example.com")
        self.assertIn("www.example.com", names)
        self.assertNotIn("other.co.uk", names)


# --- layout parity (pytest style) -------------------------------------------
#
# Answers that must not depend on which Amass version wrote the database. v5
# labels every DNS edge `dns_record` and carries the record type in a numeric
# `header.rr_type`; v4 encoded it in the label instead (`a_record`,
# `aaaa_record`, `cname_record`). Anything keying off edge labels has to accept
# both spellings, and the cost of getting it wrong is asymmetric: a filter that
# matches nothing empties the pipeline and still exits 0.
#
# These are the pattern for new tests - plain functions, conftest fixtures,
# parametrize instead of hand-rolled loops.


def _cli(*argv: str) -> list[str]:
    """Run the CLI and return its non-empty stdout lines."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    assert code in (0, 1), f"unexpected exit code {code}: {err.getvalue()}"
    return [line for line in out.getvalue().splitlines() if line]


def test_resolved_only_agrees_across_layouts(v5_db, v4_db):
    v5_names = _cli("names", "--db", str(v5_db), "-d", "example.com", "--resolved-only")
    v4_names = _cli("names", "--db", str(v4_db), "-d", "example.com", "--resolved-only")
    # Assert the contents, not only that the two agree: two empty lists are
    # also equal, and empty is exactly the bug being guarded against.
    assert "www.example.com" in v5_names
    assert "dev.example.com" not in v5_names, "no DNS record in either fixture"
    assert v4_names == v5_names


def test_names_are_scoped_on_either_layout(any_db):
    names = _cli("names", "--db", str(any_db), "-d", "example.com")
    assert "www.example.com" in names
    assert "other.co.uk" not in names, "a different target must not leak in"


@pytest.mark.parametrize("label", ["dns_record", "a_record", "aaaa_record", "cname_record"])
def test_resolved_fqdns_accepts_every_dns_label_spelling(label):
    name = Asset(id=1, type="FQDN", value="www.example.com")
    addr = Asset(id=2, type="IPAddress", value="93.184.216.34")
    edge = Edge(id=1, type="BasicDNSRelation", label=label, from_asset=name, to_asset=addr)
    assert resolved_fqdns([edge]) == {1}


def test_resolved_fqdns_ignores_non_dns_edges():
    name = Asset(id=1, type="FQDN", value="www.example.com")
    svc = Asset(id=2, type="Service", value="svc-443-www")
    edge = Edge(id=1, type="PortRelation", label="port", from_asset=name, to_asset=svc)
    assert resolved_fqdns([edge]) == set()


class TestLibraryApi(unittest.TestCase):
    """The programmatic front door, used by agents and scripts."""

    def test_values_matches_the_cli(self):
        self.assertEqual(
            values("names", domains=["example.com"], db=str(V5)),
            ["api.example.com", "dev.example.com", "example.com",
             "new.example.com", "www.example.com"],
        )

    def test_query_returns_normalised_records(self):
        records = query("names", domains=["example.com"], db=str(V5))
        www = next(r for r in records if r["value"] == "www.example.com")
        self.assertEqual(www["schema"], "oamx/1")
        self.assertEqual(www["type"], "FQDN")
        self.assertEqual({s["name"] for s in www["sources"]}, {"DNS-IP", "crtsh"})

    def test_query_honours_filters(self):
        self.assertNotIn(
            "dev.example.com",
            values("names", domains=["example.com"], db=str(V5), resolved_only=True),
        )
        self.assertNotIn(
            "dev.example.com",
            values("names", db=str(V5), min_confidence=90),
        )

    def test_limit(self):
        self.assertEqual(len(values("names", db=str(V5), limit=2)), 2)

    def test_unknown_view_is_a_clear_error(self):
        with self.assertRaises(OamxError) as ctx:
            query("subdomains", db=str(V5))
        self.assertIn("unknown view", str(ctx.exception))

    def test_new_only_requires_since(self):
        with self.assertRaises(OamxError) as ctx:
            query("names", db=str(V5), new_only=True)
        self.assertIn("requires since", str(ctx.exception))

    def test_all_view_spans_types(self):
        types = {r["type"] for r in query("all", domains=["example.com"], db=str(V5))}
        self.assertIn("FQDN", types)
        self.assertIn("IPAddress", types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
