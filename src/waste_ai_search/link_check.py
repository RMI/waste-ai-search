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

A page that loads is also read, and its text kept in page_text/ (page_text.py), so arbitration can
check that a source's quote and stated location are really on it. The text costs no extra request:
it is the same response the verdict comes from. A page read before text was kept, or whose text file
has gone, is read again by the next check; nothing else is fetched twice.
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

from .page_text import (
    CUT_BY_ERROR,
    CUT_BY_SIZE,
    CUT_BY_TIME,
    MAX_PAGE_BYTES,
    READ_SECONDS,
    RETRIED_NOTES,
    extract_text,
    save_page_text,
)


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
    # The page's text, where the check read it: the file in page_text/ it is kept in, what kind of
    # page it was, how many characters it gave, and why it gave few or none.
    text_file: str = ""
    text_kind: str = ""
    text_chars: int = 0
    text_note: str = ""


@dataclass
class Fetched:
    """A page that was read, not just its status: what keeping its text needs. `cut` says why its
    body was not read to the end, if it wasn't: its size, the time it took, or a broken connection."""

    status: Any
    body: bytes
    content_type: str = ""
    cut: str = ""


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


def _read_body(response: Any) -> tuple[bytes, str]:
    """A response's body, up to MAX_PAGE_BYTES and READ_SECONDS, and why it was cut short, if it was.

    Read a network read at a time, so the deadline is checked after each one: a server dripping a
    byte at a time can't hold a worker past it, as waiting for whole chunks would.
    """
    raw = response.raw
    read = getattr(raw, "read1", None) or raw.read
    chunks, size, started = [], 0, time.monotonic()
    try:
        while True:
            chunk = read(64 * 1024, decode_content=True)
            if not chunk:
                return b"".join(chunks), ""
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_PAGE_BYTES:
                return b"".join(chunks)[:MAX_PAGE_BYTES], CUT_BY_SIZE
            if time.monotonic() - started > READ_SECONDS:
                return b"".join(chunks), CUT_BY_TIME
    except Exception:  # noqa: BLE001 - the page loaded, so its verdict stands
        # Whatever arrived before the break is kept.
        return b"".join(chunks), CUT_BY_ERROR


def _fetch(session: Any, url: str, throttle: _HostThrottle, keep_text: bool = False) -> Any:
    """The status of one GET, or, with keep_text and a page that loaded, the page as Fetched."""
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
        try:
            if keep_text and classify(status) == OK:
                body, cut = _read_body(response)
                return Fetched(status, body, response.headers.get("Content-Type", ""), cut)
            return status
        finally:
            response.close()
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


# The real fetch can read a page as well as check it; a stand-in a test supplies may only check.
_fetch.keeps_text = True


def _with_text(result: LinkResult, page: Fetched | None, text_dir: Path) -> LinkResult:
    """Keep the text of a page that loaded. Reading it never changes the verdict."""
    if page is None:
        result.text_note = "not read"
        return result
    try:
        read = extract_text(page.body, page.content_type, result.resolved_url, page.cut)
        # Filed under the spelling that loaded, which evidence rows carry once a URL is repaired.
        result.text_file = save_page_text(text_dir, result.resolved_url, read.text)
        result.text_kind, result.text_chars, result.text_note = read.kind, len(read.text), read.note
    except Exception as exc:  # noqa: BLE001 - a page that can't be kept is noted, never fatal
        result.text_note = f"could not be kept ({type(exc).__name__})"
    return result


def check_url(session: Any, url: str, throttle: _HostThrottle, fetch=None, text_dir: Path | None = None) -> LinkResult:
    fetch = fetch or _fetch
    reads = text_dir is not None and getattr(fetch, "keeps_text", False)

    def get(target: str) -> tuple[Any, Fetched | None]:
        fetched = fetch(session, target, throttle, keep_text=True) if reads else fetch(session, target, throttle)
        return (fetched.status, fetched) if isinstance(fetched, Fetched) else (fetched, None)

    status, page = get(url)
    verdict = classify(status)
    if verdict == OK:
        result = LinkResult(url=url, verdict=OK, status=str(status), resolved_url=url)
        return _with_text(result, page, text_dir) if text_dir is not None else result
    if verdict != BROKEN:
        # Unverified is left strictly alone.
        return LinkResult(url=url, verdict=verdict, status=str(status), resolved_url=url)
    for candidate in url_variants(url)[1:]:
        candidate_status, candidate_page = get(candidate)
        if classify(candidate_status) == OK:
            result = LinkResult(url=url, verdict=OK, status=str(candidate_status), resolved_url=candidate)
            return _with_text(result, candidate_page, text_dir) if text_dir is not None else result
    return LinkResult(url=url, verdict=BROKEN, status=str(status), resolved_url=url)


def wants_text(result: LinkResult, text_dir: Path) -> bool:
    """A page that loaded but whose text was never kept: checked before text was kept, its file has
    gone since, or its read was interrupted. One that gave a lasting reason for no text, a scan say,
    is not read again."""
    if result.verdict != OK:
        return False
    if any(note in result.text_note for note in RETRIED_NOTES):
        return True
    if result.text_file:
        return not (text_dir / result.text_file).exists()
    return not result.text_note or result.text_note == "not read"


def with_new_text(previous: LinkResult, fresh: LinkResult) -> LinkResult:
    """A page read again only for its text keeps its verdict: an SME may already have reviewed the
    run on it. Only text that this read kept, or a reason for none, is taken from it."""
    if fresh.verdict == OK and (fresh.text_file or fresh.text_note):
        previous.text_file, previous.text_kind = fresh.text_file, fresh.text_kind
        previous.text_chars, previous.text_note = fresh.text_chars, fresh.text_note
    return previous


def load_results(path: Path) -> dict[str, LinkResult]:
    """Read a run's verdicts. Missing or unreadable means no verdicts, never an error."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    known = set(LinkResult.__dataclass_fields__)
    return {url: LinkResult(**{key: value for key, value in record.items() if key in known}) for url, record in payload.items()}


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
    text_dir: Path | None = None,
) -> dict[str, LinkResult]:
    """Check each distinct URL once, reusing anything already in the sidecar. With `text_dir`, a
    page that loads also has its text kept there, and one checked before without it is read again.

    `fetch` defaults to the real HTTP fetch, looked up at call time so tests can replace it.
    """
    fetch = fetch or _fetch
    results = load_results(cache_path) if cache_path else {}
    pending = [
        url for url in dict.fromkeys(urls)
        if url and (url not in results or (text_dir is not None and wants_text(results[url], text_dir)))
    ]
    if pending:
        import requests

        throttle = _HostThrottle(PER_HOST_DELAY_SECONDS)
        session = requests.Session()
        # An environment proxy would bypass the pinned adapter's address check, so it is ignored.
        session.trust_env = False
        session.headers.update({"User-Agent": USER_AGENT})
        adapter = _pinned_adapter_class()(pool_connections=max_workers, pool_maxsize=max_workers)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        print(f"Checking {len(pending)} source URL(s)")
        try:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="link-check") as executor:
                for result in executor.map(lambda url: check_url(session, url, throttle, fetch, text_dir), pending):
                    previous = results.get(result.url)
                    results[result.url] = with_new_text(previous, result) if previous else result
        finally:
            session.close()
            # Saved even when the check is interrupted, so the verdicts it reached are kept.
            if cache_path:
                save_results(cache_path, results)
        return results
    if cache_path:
        save_results(cache_path, results)
    return results
