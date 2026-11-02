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

`check-links --retry-unverified` fetches a URL once more, with longer to answer, where it was left
Unverified for a reason that may pass: a timeout, a failed connection, a name that didn't resolve,
a 429 or a server error. Never a 403: that site refuses automated readers on purpose.

A page that loads is also read, and its text kept in page_text/ (page_text.py), so arbitration can
check that a source's quote and stated location are really on it. The text costs no extra request:
it is the same response the verdict comes from. A page read before text was kept, or whose text file
has gone, is read again by the next check; nothing else is fetched twice.
"""
from __future__ import annotations

import ipaddress
import json
import re
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
    little_text,
    save_page_text,
)


OK = "OK"
BROKEN = "Broken"
UNVERIFIED = "Unverified"

LINK_CHECK_FILE = "link_check.json"

BROKEN_STATUS_CODES = {404, 410}
TIMEOUT_SECONDS = 15
# How long a URL fetched again (retryable) has to answer. Of the 32 pages the October 2026 model
# tests couldn't read, 5 had timed out at TIMEOUT_SECONDS and 2 had answered 500.
RETRY_TIMEOUT_SECONDS = 45
# The failures that may pass, besides a 429 and a 5xx. A TLS error, or a malformed or internal
# address, won't.
RETRYABLE_ERRORS = {"ConnectTimeout", "ReadTimeout", "Timeout", "ConnectionError", "DoesNotResolve"}
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


def retryable(result: LinkResult) -> bool:
    """Whether an Unverified URL failed for a reason that may pass, and is worth fetching again."""
    if result.verdict != UNVERIFIED:
        return False
    if result.status.isdigit():
        return int(result.status) == 429 or 500 <= int(result.status) <= 599
    return result.status in RETRYABLE_ERRORS


class BlockedDestination(OSError):
    """The URL resolves to an address the link checker must not reach."""


class DoesNotResolve(OSError):
    """The URL's host name doesn't resolve, so there is nowhere to connect to. Unlike a blocked
    address, it may pass: a name server can fail for a while."""


def _public_address(host: str, port: int) -> str:
    """Resolve `host` and return an address to connect to, or raise if ANY address is non-public.

    All addresses are checked, not just the first, so a name that resolves to one public and one
    internal address cannot be used to reach the internal one.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise DoesNotResolve(f"{host} does not resolve") from exc
    addresses = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        mapped = getattr(address, "ipv4_mapped", None)
        if not (mapped or address).is_global:
            raise BlockedDestination(f"{host} resolves to non-public address {address}")
        addresses.append(str(address))
    if not addresses:
        raise DoesNotResolve(f"{host} has no address")
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


def _fetch(
    session: Any, url: str, throttle: _HostThrottle, keep_text: bool = False, timeout: float = TIMEOUT_SECONDS
) -> Any:
    """The status of one GET, or, with keep_text and a page that loaded, the page as Fetched."""
    import requests

    try:
        host = urlparse(url).netloc
    except ValueError:
        # One malformed citation must not abort the whole end-of-search pass.
        return "InvalidURL"
    throttle.wait(host)
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True, stream=True)
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
    except (BlockedDestination, DoesNotResolve) as exc:
        return type(exc).__name__
    except requests.RequestException as exc:
        # urllib3 may wrap the guard's error inside a connection error; it is still the guard's.
        for guard in (BlockedDestination, DoesNotResolve):
            if isinstance(getattr(exc, "__context__", None), guard) or guard.__name__ in repr(exc):
                return guard.__name__
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


def check_url(
    session: Any, url: str, throttle: _HostThrottle, fetch=None, text_dir: Path | None = None, timeout: float | None = None
) -> LinkResult:
    """One URL's verdict, and its text where it loads. `timeout` gives a URL fetched again longer."""
    fetch = fetch or _fetch
    reads = text_dir is not None and getattr(fetch, "keeps_text", False)
    options: dict[str, Any] = {"keep_text": True} if reads else {}
    if timeout is not None:
        options["timeout"] = timeout

    def get(target: str) -> tuple[Any, Fetched | None]:
        fetched = fetch(session, target, throttle, **options)
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


# Why the link check couldn't read a page, as Site_Review's link_status says it (describe_link).
UNREAD = {
    "401": "the page needs a login (401)",
    "403": "the site refuses automated readers (403)",
    "429": "the site turned away too many requests (429)",
    "404": "the page is gone (404)",
    "410": "the page is gone (410)",
    "TooManyRedirects": "the page redirects in a loop",
    "ConnectTimeout": "the site timed out",
    "ReadTimeout": "the site timed out",
    "Timeout": "the site timed out",
    "ConnectionError": "the connection to the site failed",
    "SSLError": "the site's secure connection failed",
    "DoesNotResolve": "the address doesn't resolve",
    # True too of a run checked before DoesNotResolve, when a name that didn't resolve was blocked.
    "BlockedDestination": "the site isn't on the public internet",
    "InvalidURL": "the link is malformed",
    "MissingSchema": "the link is malformed",
    "InvalidSchema": "the link is malformed",
}
NOT_CHECKED = "so quotes weren't checked"
# What the note on a page that gave no text says, without its "not read" (page_text.extract_text).
_NOT_READ = re.compile(r"^(?:could )?not (?:be )?read\W*|\W+$")


def describe_link(result: LinkResult | None, menu_only: bool = False) -> str:
    """Site_Review's link_status: the link check's verdict, and where it couldn't read the page, why,
    so an SME knows the page's quotes and location weren't checked against it. "" where the link
    wasn't checked. `menu_only` says the text kept of a page that loaded is only its menu
    (on_page.menu_only)."""
    if result is None:
        return ""
    status = result.status
    if result.verdict != OK:
        if status in UNREAD:
            why = UNREAD[status]
        elif status.isdigit():
            why = f"the site had a server error ({status})" if status.startswith("5") else f"the site answered {status}"
        else:
            why = f"the check failed ({status})"
        return f"{result.verdict}: {why}, {NOT_CHECKED}"
    note = result.text_note
    if menu_only:
        # A quote in the menu, such as the page's title, is still found.
        return "OK, but its text is only the site's menu, so a quote missing from it isn't flagged"
    if little_text(note):
        why = "it has almost no text: it may be built by JavaScript"
    elif result.text_file:
        return OK
    elif "scanned" in note:
        why = "it's a scanned PDF"
    elif not note or note == "not read":
        why = "its text wasn't kept"
    else:
        why = f"its text couldn't be read ({_NOT_READ.sub('', note) or note})"
    return f"OK, but {why}, {NOT_CHECKED}"


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
    retry_unverified: bool = False,
) -> dict[str, LinkResult]:
    """Check each distinct URL once, reusing anything already in the sidecar. With `text_dir`, a
    page that loads also has its text kept there, and one checked before without it is read again.
    With `retry_unverified`, a URL left Unverified for a reason that may pass (retryable) is fetched
    once more, with RETRY_TIMEOUT_SECONDS to answer, and its new verdict replaces the old.

    `fetch` defaults to the real HTTP fetch, looked up at call time so tests can replace it.
    """
    fetch = fetch or _fetch
    results = load_results(cache_path) if cache_path else {}
    urls = [url for url in dict.fromkeys(urls) if url]
    retrying = {url for url in urls if retry_unverified and url in results and retryable(results[url])}
    pending = [
        url for url in urls
        if url not in results or url in retrying or (text_dir is not None and wants_text(results[url], text_dir))
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
        again = f", {len(retrying)} of them again with {RETRY_TIMEOUT_SECONDS} s to answer" if retrying else ""
        print(f"Checking {len(pending)} source URL(s){again}")
        try:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="link-check") as executor:
                checks = executor.map(
                    lambda url: check_url(
                        session, url, throttle, fetch, text_dir, RETRY_TIMEOUT_SECONDS if url in retrying else None
                    ),
                    pending,
                )
                for result in checks:
                    previous = results.get(result.url)
                    # A page read again for its text keeps its verdict; a URL fetched again gets a new one.
                    results[result.url] = with_new_text(previous, result) if previous and result.url not in retrying else result
        finally:
            session.close()
            # Saved even when the check is interrupted, so the verdicts it reached are kept.
            if cache_path:
                save_results(cache_path, results)
        if retrying:
            print(f"Fetched {len(retrying)} unverified URL(s) again: {sum(1 for url in retrying if results[url].verdict == OK)} now load.")
        return results
    if cache_path:
        save_results(cache_path, results)
    return results
