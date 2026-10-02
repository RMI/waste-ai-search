"""Check that cited source URLs resolve, before an SME is asked to click them.

Ported from the sibling refining-ai-search repo (RDP-63, `link_check.py`), where 16 of 71 rejected
events were rejected as "link broken". Its rules are kept as they were, because they were learned
from real reviews there:

- Each distinct URL is fetched once, with a browser user agent, a timeout and a per-host throttle,
  so a live site is not mistaken for a dead one.
- Three verdicts. OK is a 200. Broken is a 404, a 410 or a redirect loop. Everything else - 403,
  429, timeouts, TLS errors - is Unverified: usually a server refusing bots, not a page that is
  gone, and never repaired or called broken.
- A Broken URL is retried with two respellings before giving up: a percent-encoded comma as a
  literal comma, then the trailing slash toggled. The URL is never blanket-decoded, since most
  percent-encoded URLs work as given.

Agent-cited URLs are untrusted, and this fetches them automatically from a machine that is often
on the RMI VPN. So a request may only reach a public internet address: every hop, redirects
included, is resolved and refused if any address is private, loopback, link-local (which includes
cloud metadata at 169.254.169.254) or otherwise non-global. The connection is then made to the
address that was checked, never re-resolved, so a hostile DNS answer cannot swap in an internal
address between the check and the connect. A refused URL is Unverified and is never fetched.

One difference from RDP-63: the fetching happens in the SEARCH phase, which needs the network
anyway, and the verdicts are kept in `link_check.json` in the run directory. Arbitration only reads
that file, so `arbitrate` stays offline as the CLI promises.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


OK = "OK"
BROKEN = "Broken"
UNVERIFIED = "Unverified"

LINK_CHECK_FILE = "link_check.json"

BROKEN_STATUS_CODES = {404, 410}
TIMEOUT_SECONDS = 15
MAX_WORKERS = 16
PER_HOST_DELAY_SECONDS = 1.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125 Safari/537.36"
)


@dataclass
class LinkResult:
    url: str
    verdict: str
    status: str
    resolved_url: str


class _HostThrottle:
    """Space out requests per host so a live site is not read as a dead one."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            earliest = self._next_allowed.get(host, 0.0)
            self._next_allowed[host] = max(now, earliest) + self._delay
        delay = earliest - now
        if delay > 0:
            time.sleep(delay)


def url_variants(url: str) -> list[str]:
    """The URL as given, then the encoding and slash forms worth trying."""
    variants = [url]
    decoded = url.replace("%2C", ",").replace("%2c", ",")
    if decoded != url:
        variants.append(decoded)
    try:
        parsed = urlparse(url)
    except ValueError:
        return variants
    if not parsed.query and not parsed.fragment:
        toggled = url.rstrip("/") if url.endswith("/") else url + "/"
        if toggled != url:
            variants.append(toggled)
    return variants


def classify(status: Any) -> str:
    if isinstance(status, int):
        if status == 200:
            return OK
        return BROKEN if status in BROKEN_STATUS_CODES else UNVERIFIED
    return BROKEN if status == "TooManyRedirects" else UNVERIFIED


class BlockedDestination(OSError):
    """The URL resolves to an address the link checker must not reach."""


def _public_address(host: str, port: int) -> str:
    """Resolve `host` and return an address to connect to, or raise if ANY address is non-public.

    All addresses are checked, not just the first, so a name that resolves to one public and one
    internal address cannot be used to reach the internal one.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise BlockedDestination(f"{host} does not resolve") from exc
    addresses = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        mapped = getattr(address, "ipv4_mapped", None)
        if not (mapped or address).is_global:
            raise BlockedDestination(f"{host} resolves to non-public address {address}")
        addresses.append(str(address))
    if not addresses:
        raise BlockedDestination(f"{host} has no address")
    return addresses[0]


def _pinned_adapter_class():
    """An HTTPAdapter that connects only to an address it has checked, and never re-resolves.

    Built lazily so importing this module does not import requests. For HTTPS the TLS handshake
    still uses the real hostname (SNI) and the certificate is verified against it, so pinning the
    IP changes where the socket goes, not what is trusted. Redirects pass through `send` hop by
    hop, so every hop is checked the same way.
    """
    import requests

    class PinnedAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, *args, **kwargs):
            try:
                netloc = urlparse(request.url).netloc
            except ValueError as exc:
                raise requests.exceptions.InvalidURL(str(exc)) from exc
            request.headers["Host"] = netloc.rpartition("@")[2]
            return super().send(request, *args, **kwargs)

        def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
            parsed = urlparse(request.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise requests.exceptions.InvalidURL(request.url)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            address = _public_address(parsed.hostname, port)
            _params, pool_kwargs = self.build_connection_pool_key_attributes(request, verify, cert)
            pool_kwargs = dict(pool_kwargs)
            if parsed.scheme == "https":
                pool_kwargs["server_hostname"] = parsed.hostname
                pool_kwargs["assert_hostname"] = parsed.hostname
            return self.poolmanager.connection_from_host(
                address, port=port, scheme=parsed.scheme, pool_kwargs=pool_kwargs
            )

    return PinnedAdapter


def _fetch(session: Any, url: str, throttle: _HostThrottle) -> Any:
    import requests

    try:
        host = urlparse(url).netloc
    except ValueError:
        # One malformed citation must not abort the whole end-of-search pass.
        return "InvalidURL"
    throttle.wait(host)
    try:
        response = session.get(url, timeout=TIMEOUT_SECONDS, allow_redirects=True, stream=True)
        status = response.status_code
        response.close()
        return status
    except requests.TooManyRedirects:
        return "TooManyRedirects"
    except BlockedDestination:
        return "BlockedDestination"
    except requests.RequestException as exc:
        # urllib3 may wrap the guard's error inside a connection error; it is still a refusal.
        if isinstance(getattr(exc, "__context__", None), BlockedDestination) or "BlockedDestination" in repr(exc):
            return "BlockedDestination"
        return type(exc).__name__
    except ValueError:
        return "InvalidURL"


def check_url(session: Any, url: str, throttle: _HostThrottle, fetch=None) -> LinkResult:
    fetch = fetch or _fetch
    status = fetch(session, url, throttle)
    verdict = classify(status)
    if verdict != BROKEN:
        # OK needs no repair, and Unverified is left strictly alone.
        return LinkResult(url=url, verdict=verdict, status=str(status), resolved_url=url)
    for candidate in url_variants(url)[1:]:
        candidate_status = fetch(session, candidate, throttle)
        if classify(candidate_status) == OK:
            return LinkResult(url=url, verdict=OK, status=str(candidate_status), resolved_url=candidate)
    return LinkResult(url=url, verdict=BROKEN, status=str(status), resolved_url=url)


def load_results(path: Path) -> dict[str, LinkResult]:
    """Read a run's verdicts. Missing or unreadable means no verdicts, never an error."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {url: LinkResult(**record) for url, record in payload.items()}


def save_results(path: Path, results: dict[str, LinkResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {url: asdict(result) for url, result in sorted(results.items())}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def check_urls(
    urls: Iterable[str],
    *,
    cache_path: Path | None = None,
    max_workers: int = MAX_WORKERS,
    fetch=None,
) -> dict[str, LinkResult]:
    """Check each distinct URL once, reusing anything already in the sidecar.

    `fetch` defaults to the real HTTP fetch, looked up at call time so tests can replace it.
    """
    fetch = fetch or _fetch
    results = load_results(cache_path) if cache_path else {}
    pending = [url for url in dict.fromkeys(urls) if url and url not in results]
    if pending:
        import requests

        throttle = _HostThrottle(PER_HOST_DELAY_SECONDS)
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        adapter = _pinned_adapter_class()(pool_connections=max_workers, pool_maxsize=max_workers)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        print(f"Checking {len(pending)} source URL(s)")
        try:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="link-check") as executor:
                for result in executor.map(lambda url: check_url(session, url, throttle, fetch), pending):
                    results[result.url] = result
        finally:
            session.close()
    if cache_path:
        save_results(cache_path, results)
    return results
