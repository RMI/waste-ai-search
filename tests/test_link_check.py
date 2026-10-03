"""WP-533: check that cited source URLs resolve before an SME is asked to click them.

Ported from refining-ai-search RDP-63. Every test here uses a fake fetch: the suite never reaches
the network (tests/conftest.py guards that globally).

The policy, as in RDP-63:
- OK is a 200; Broken is a 404, 410 or redirect loop; anything else is Unverified and left alone.
- A Broken URL is retried as two respellings; existing escapes are never blanket-decoded.
- A source whose link is Broken is treated like one with no link at all - not promoted, routed to
  leads - unless --keep-broken-link-evidence is set.

And the one difference: fetching happens in the search phase; arbitration only reads the verdicts,
so it stays offline.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from waste_ai_search.arbitrate import extract_evidence
from waste_ai_search.arbitration import resolve
from waste_ai_search.link_check import (
    BROKEN,
    LINK_CHECK_FILE,
    OK,
    UNVERIFIED,
    LinkResult,
    check_urls,
    classify,
    load_results,
    url_variants,
)


def responder(statuses):
    """A fake fetch answering from a {url: status} map, recording every call."""
    calls = []

    def fetch(session, url, throttle):
        calls.append(url)
        return statuses.get(url, 200)

    fetch.calls = calls
    return fetch


# --- verdicts ---------------------------------------------------------------------------------
@pytest.mark.parametrize("status,verdict", [
    (200, OK), (404, BROKEN), (410, BROKEN), ("TooManyRedirects", BROKEN),
    (403, UNVERIFIED), (429, UNVERIFIED), (500, UNVERIFIED), ("ConnectTimeout", UNVERIFIED), ("SSLError", UNVERIFIED),
])
def test_classification_matches_rdp63(status, verdict):
    """A 403 is a server refusing bots, not a dead page - never called broken."""
    assert classify(status) == verdict


# --- respellings ------------------------------------------------------------------------------
def test_the_variants_tried_are_the_comma_and_the_trailing_slash():
    assert url_variants("https://x.org/story%2C39023") == [
        "https://x.org/story%2C39023", "https://x.org/story,39023", "https://x.org/story%2C39023/",
    ]


def test_a_url_with_a_query_is_not_slash_toggled():
    assert url_variants("https://x.org/a?b=1") == ["https://x.org/a?b=1"]


def test_a_broken_url_is_repaired_by_a_respelling_that_works():
    """The RDP-63 case: a CMS that needs a literal comma."""
    url = "https://www.thenorthernlight.com/stories/turnaround%2C39023"
    fixed = "https://www.thenorthernlight.com/stories/turnaround,39023"
    results = check_urls([url], fetch=responder({url: "TooManyRedirects", fixed: 200}))
    assert results[url].verdict == OK and results[url].resolved_url == fixed


def test_a_broken_url_that_no_respelling_fixes_stays_broken():
    url = "https://x.org/gone.pdf"
    results = check_urls([url], fetch=responder({url: 404, url + "/": 404}))
    assert results[url].verdict == BROKEN and results[url].resolved_url == url


def test_an_unverified_url_is_never_respelled():
    url = "https://x.org/a%2Cb"
    fetch = responder({url: 403})
    results = check_urls([url], fetch=fetch)
    assert results[url].verdict == UNVERIFIED and results[url].resolved_url == url
    assert fetch.calls == [url]


def test_a_working_url_with_an_escape_is_not_decoded():
    url = "https://geocoder.ca/20%20Morrison%20Rd%2C%20Brantford%2C%20ON"
    results = check_urls([url], fetch=responder({url: 200}))
    assert results[url].resolved_url == url


# --- the sidecar is a cache --------------------------------------------------------------------
def test_each_url_is_fetched_once_and_verdicts_are_saved(tmp_path):
    cache = tmp_path / LINK_CHECK_FILE
    fetch = responder({})
    check_urls(["https://a.org", "https://a.org", "https://b.org"], cache_path=cache, fetch=fetch)
    assert fetch.calls == ["https://a.org", "https://b.org"]
    assert set(load_results(cache)) == {"https://a.org", "https://b.org"}


def test_a_later_check_only_fetches_new_urls(tmp_path):
    """A resumed search or the pass-2 follow-up re-checks nothing already verified."""
    cache = tmp_path / LINK_CHECK_FILE
    check_urls(["https://a.org"], cache_path=cache, fetch=responder({}))
    fetch = responder({})
    check_urls(["https://a.org", "https://new.org"], cache_path=cache, fetch=fetch)
    assert fetch.calls == ["https://new.org"]


def test_a_missing_or_corrupt_sidecar_means_no_verdicts(tmp_path):
    assert load_results(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_results(bad) == {}


# --- arbitration applies the verdicts ----------------------------------------------------------
REGULATOR = {"source_title": "Register", "publisher": "Agency", "source_type": "Regulator",
             "publication_date": "2023-01-01"}


def site():
    return {"site_id": "1", "internal_facility_id": "1", "site_name": "Landfill", "country_iso3": "NGA",
            "contributing_data_sources": "osm_2022", "reference_year": "2022"}


def payload(*urls):
    return {"site_id": "1", "attributes": [{
        "attribute_name": "facility_status", "value": "Active", "value_basis": "Direct",
        "confidence_score": "High", "value_date": "2023", "evidence_summary": "s",
        "sources": [dict(REGULATOR, url=u) for u in urls],
    }]}


def arbitrate(p, results, keep=False):
    by, ev, _s, _w = extract_evidence(site(), p, "r", "v", date(2026, 1, 1),
                                      link_results=results, keep_broken_links=keep)
    return by, ev, resolve(site(), "facility_status", by.get("facility_status", []))


def verdict(url, v, resolved=None, status="404"):
    return LinkResult(url=url, verdict=v, status=status, resolved_url=resolved or url)


def test_a_dead_link_is_not_promoted_and_says_why():
    url = "https://x.org/gone.pdf"
    _by, evidence, row = arbitrate(payload(url), {url: verdict(url, BROKEN)})
    assert evidence[0]["promotion_eligible"] == "FALSE"
    assert evidence[0]["exclusion_reason"] == "Source link broken (404); not promoted."
    assert evidence[0]["link_status"] == BROKEN
    assert row["resolution"] == "Not found"


def test_a_value_with_one_dead_and_one_working_source_is_still_promoted_from_the_working_one():
    dead, alive = "https://x.org/gone.pdf", "https://y.org/ok"
    _by, _ev, row = arbitrate(payload(dead, alive), {dead: verdict(dead, BROKEN), alive: verdict(alive, OK, status="200")})
    assert row["resolved_value"] == "Active"
    assert row["winning_source_url"] == alive
    assert row["winning_link_status"] == OK


def test_a_repaired_url_replaces_the_one_the_agent_wrote():
    url, fixed = "https://x.org/a%2C1", "https://x.org/a,1"
    _by, _ev, row = arbitrate(payload(url), {url: verdict(url, OK, resolved=fixed, status="200")})
    assert row["winning_source_url"] == fixed


def test_an_unverified_link_is_promoted_and_flagged_for_the_reviewer():
    url = "https://blocked.org/page"
    _by, _ev, row = arbitrate(payload(url), {url: verdict(url, UNVERIFIED, status="403")})
    assert row["resolved_value"] == "Active"
    assert row["winning_link_status"] == UNVERIFIED


def test_keep_broken_link_evidence_lets_a_dead_source_stay_promotable():
    url = "https://x.org/gone.pdf"
    _by, evidence, row = arbitrate(payload(url), {url: verdict(url, BROKEN)}, keep=True)
    assert evidence[0]["promotion_eligible"] == "TRUE"
    assert row["winning_link_status"] == BROKEN


def test_without_a_link_check_nothing_changes():
    """A run with no link_check.json behaves exactly as before WP-533."""
    url = "https://x.org/a"
    _by, evidence, row = arbitrate(payload(url), {})
    assert evidence[0]["link_status"] == "" and row["winning_link_status"] == ""
    assert row["resolved_value"] == "Active"


# --- end to end: offline arbitration, leads, and Run_Config ------------------------------------
def build_run(tmp_path, links):
    from waste_ai_search.input_loader import write_csv_records
    from waste_ai_search.seed_source import seed_headers

    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", [dict(site(), facility_id="1", year="2022", facility_name="Landfill",
                                                  iso3c_plus="NGA")], seed_headers())
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    (raw / "site_1.json").write_text(json.dumps(payload("https://x.org/gone.pdf")))
    if links is not None:
        (run_dir / LINK_CHECK_FILE).write_text(json.dumps(links))
    return run_dir


def test_arbitration_reads_the_verdicts_without_reaching_the_network(tmp_path, monkeypatch):
    import waste_ai_search.link_check as lc
    from waste_ai_search.arbitrate import run_arbitration
    from waste_ai_search.run_context import PipelineConfig

    monkeypatch.setattr(lc, "_fetch", lambda *a: (_ for _ in ()).throw(AssertionError("arbitrate fetched a URL")))
    run_dir = build_run(tmp_path, {"https://x.org/gone.pdf": {
        "url": "https://x.org/gone.pdf", "verdict": BROKEN, "status": "404", "resolved_url": "https://x.org/gone.pdf"}})
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))

    leads = (run_dir / "supplementary_leads.csv").read_text()
    assert "Source link broken (404)" in leads

    from openpyxl import load_workbook
    config = {r[0]: r[1] for r in load_workbook(run_dir / "run_review.xlsx")["Run_Config"].iter_rows(values_only=True)}
    assert config["links_checked"] == 1 and config["links_broken"] == 1
    assert config["broken_link_route"] == "routed to leads"


def test_the_search_phase_checks_links_and_can_be_told_not_to(tmp_path, monkeypatch):
    from waste_ai_search import search as pl

    seed = tmp_path / "seed.csv"
    seed.write_text("site_id,site_name,country_iso3,latitude,longitude\n1,Landfill,PHL,14.5,121.0\n")

    class _Client:
        def search_site(self, site, prompt):
            return payload("https://x.org/a")

    monkeypatch.setattr(pl, "get_client", lambda config: _Client())
    checked = []
    monkeypatch.setattr(pl, "check_urls", lambda urls, cache_path: checked.append(list(urls)) or {})

    pl.run_search(pl.PipelineConfig(input_csv=seed, run_dir=tmp_path / "run", run_id="a", site_delay_seconds=0))
    assert checked == [["https://x.org/a"]]

    checked.clear()
    pl.run_search(pl.PipelineConfig(input_csv=seed, run_dir=tmp_path / "run2", run_id="b",
                                    site_delay_seconds=0, check_links=False))
    assert checked == []


def test_the_check_links_command_backfills_an_existing_run(tmp_path, monkeypatch):
    from waste_ai_search import cli

    run_dir = build_run(tmp_path, None)
    assert cli.main(["check-links", "--run-id", "run", "--run-dir", str(run_dir)]) == 0
    assert "https://x.org/gone.pdf" in load_results(run_dir / LINK_CHECK_FILE)


# --- Copilot review: only public destinations are ever reached ---------------------------------
import socket as _socket
import threading as _threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def _fake_resolution(monkeypatch, mapping):
    """Answer getaddrinfo from {host: [addresses]}."""
    def getaddrinfo(host, port, *args, **kwargs):
        if host not in mapping:
            raise _socket.gaierror("unknown host")
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", (a, port)) for a in mapping[host]]
    monkeypatch.setattr(_socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254",  # metadata endpoint
    "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "::ffff:10.0.0.1",
])
def test_a_non_public_address_is_refused(monkeypatch, address):
    from waste_ai_search.link_check import BlockedDestination, _public_address

    _fake_resolution(monkeypatch, {"internal.example": [address]})
    with pytest.raises(BlockedDestination):
        _public_address("internal.example", 80)


def test_a_name_with_one_public_and_one_internal_address_is_refused(monkeypatch):
    """Every address is checked, so the internal one cannot be reached by a mixed answer."""
    from waste_ai_search.link_check import BlockedDestination, _public_address

    _fake_resolution(monkeypatch, {"mixed.example": ["93.184.216.34", "10.0.0.5"]})
    with pytest.raises(BlockedDestination):
        _public_address("mixed.example", 443)


def test_a_public_address_is_allowed(monkeypatch):
    from waste_ai_search.link_check import _public_address

    _fake_resolution(monkeypatch, {"public.example": ["93.184.216.34"]})
    assert _public_address("public.example", 443) == "93.184.216.34"


def test_the_connection_goes_to_the_checked_address_not_a_fresh_lookup(monkeypatch):
    """Pinning: the pool is opened on the validated IP, with the real name kept for TLS - so a
    second DNS answer cannot swap in an internal address between the check and the connect."""
    import requests
    from waste_ai_search.link_check import _pinned_adapter_class

    _fake_resolution(monkeypatch, {"public.example": ["93.184.216.34"]})
    adapter = _pinned_adapter_class()()
    seen = {}

    def connection_from_host(host, port=None, scheme=None, pool_kwargs=None):
        seen.update(host=host, port=port, scheme=scheme, **(pool_kwargs or {}))
        return object()

    monkeypatch.setattr(adapter.poolmanager, "connection_from_host", connection_from_host)
    request = requests.Request("GET", "https://public.example/report.pdf").prepare()
    adapter.get_connection_with_tls_context(request, verify=True)

    assert seen["host"] == "93.184.216.34"
    assert seen["server_hostname"] == "public.example"
    assert seen["assert_hostname"] == "public.example"


def test_a_redirect_to_an_internal_address_is_refused(monkeypatch):
    """Each redirect hop passes the same check. A loopback-only server stands in for a public
    site that redirects to an internal one; nothing leaves this machine."""
    import waste_ai_search.link_check as lc

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{internal_port}/secret")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Redirector)
    public_port = server.server_address[1]
    internal_port = public_port + 1
    _threading.Thread(target=server.serve_forever, daemon=True).start()

    real = lc._public_address

    def first_hop_is_public(host, port):
        return "127.0.0.1" if port == public_port else real(host, port)

    monkeypatch.setattr(lc, "_public_address", first_hop_is_public)
    try:
        import requests

        session = requests.Session()
        adapter = lc._pinned_adapter_class()()
        session.mount("http://", adapter)
        status = lc._original_fetch(session, f"http://public.example:{public_port}/", lc._HostThrottle(0))
    finally:
        server.shutdown()
    assert status == "BlockedDestination"
    assert classify(status) == UNVERIFIED


def test_a_blocked_destination_is_unverified_never_broken():
    """Refusing to fetch is not evidence the page is dead."""
    assert classify("BlockedDestination") == UNVERIFIED


# --- Copilot review: a malformed citation cannot abort the pass --------------------------------
def test_a_malformed_url_is_unverified_rather_than_fatal():
    import waste_ai_search.link_check as lc

    assert lc._original_fetch(None, "https://[invalid", lc._HostThrottle(0)) == "InvalidURL"
    assert url_variants("https://[invalid%2C") == ["https://[invalid%2C", "https://[invalid,"]


def test_one_malformed_url_does_not_stop_the_others(monkeypatch):
    import waste_ai_search.link_check as lc

    def fetch(session, url, throttle):
        return lc._original_fetch(session, url, throttle) if "[" in url else 200

    results = check_urls(["https://[invalid", "https://fine.example/a"], fetch=fetch)
    assert results["https://[invalid"].verdict == UNVERIFIED
    assert results["https://fine.example/a"].verdict == OK


# --- Copilot review: a dead source reaches Leads even when the value was filled elsewhere -------
def test_a_dead_source_reaches_leads_when_a_working_source_filled_the_attribute(tmp_path, monkeypatch):
    from waste_ai_search.arbitrate import run_arbitration
    from waste_ai_search.input_loader import write_csv_records
    from waste_ai_search.run_context import PipelineConfig
    from waste_ai_search.seed_source import seed_headers

    run_dir = tmp_path / "run"
    write_csv_records(run_dir / "seed.csv", [dict(site(), facility_id="1", year="2022", facility_name="Landfill",
                                                  iso3c_plus="NGA")], seed_headers())
    raw = run_dir / "raw_foundry_responses"
    raw.mkdir(parents=True)
    dead, alive = "https://x.org/gone.pdf", "https://y.org/ok"
    (raw / "site_1.json").write_text(json.dumps(payload(dead, alive)))
    (run_dir / LINK_CHECK_FILE).write_text(json.dumps({
        dead: {"url": dead, "verdict": BROKEN, "status": "404", "resolved_url": dead},
        alive: {"url": alive, "verdict": OK, "status": "200", "resolved_url": alive},
    }))
    run_arbitration(PipelineConfig(input_csv=None, run_dir=run_dir, run_id="run", dataset_version="v"))

    import csv
    resolved = [r for r in csv.DictReader((run_dir / "resolved.csv").open()) if r["attribute_name"] == "facility_status"]
    assert resolved[0]["resolved_value"] == "Active"  # filled from the working source
    leads = list(csv.DictReader((run_dir / "supplementary_leads.csv").open()))
    assert [l["url"] for l in leads if "Source link broken" in l["exclusion_reason"]] == [dead]


def test_environment_proxies_are_ignored(monkeypatch):
    """Copilot review: a proxy would bypass the pinned adapter and its address check."""
    import requests

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    sessions = []
    real = requests.Session

    def recording_session():
        session = real()
        sessions.append(session)
        return session

    monkeypatch.setattr(requests, "Session", recording_session)
    check_urls(["https://x.org/a"], fetch=responder({}))
    assert sessions and sessions[0].trust_env is False
