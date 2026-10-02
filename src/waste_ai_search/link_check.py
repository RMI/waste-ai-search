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

One difference from RDP-63: the fetching happens in the SEARCH phase, which needs the network
anyway, and the verdicts are kept in `link_check.json` in the run directory. Arbitration only reads
that file, so `arbitrate` stays offline as the CLI promises.
"""
from __future__ import annotations

import json
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
    parsed = urlparse(url)
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


def _fetch(session: Any, url: str, throttle: _HostThrottle) -> Any:
    import requests

    throttle.wait(urlparse(url).netloc)
    try:
        response = session.get(url, timeout=TIMEOUT_SECONDS, allow_redirects=True, stream=True)
        status = response.status_code
        response.close()
        return status
    except requests.TooManyRedirects:
        return "TooManyRedirects"
    except requests.RequestException as exc:
        return type(exc).__name__


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
        adapter = requests.adapters.HTTPAdapter(pool_connections=max_workers, pool_maxsize=max_workers)
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
