"""Polite HTTP layer: global rate limiting, retries with backoff,
robots.txt compliance, and conditional GETs for incremental crawling."""
import logging
import threading
import time
from dataclasses import dataclass
from urllib import robotparser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("scraper.http")


@dataclass
class FetchResult:
    url: str
    status: int          # 0 = network error, -1 = robots-disallowed
    html: str | None
    final_url: str
    etag: str
    last_modified: str
    fetched_at: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error


class PoliteFetcher:
    def __init__(self, base_url, user_agent, delay, timeout, retries, concurrency):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.delay = max(0.1, delay)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._local = threading.local()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent, "Accept-Language": "en,id"})
        retry = Retry(
            total=retries, connect=retries, read=retries, backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
        )
        self._retry = retry
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=max(2, concurrency))
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        self.robots, self.sitemap_urls = self._load_robots(base_url)

    def _throttle(self):
        with self._lock:
            now = time.monotonic()
            if self._next_at > now:
                time.sleep(self._next_at - now)
            self._next_at = max(self._next_at, time.monotonic()) + self.delay

    def _load_robots(self, base_url):
        robots, sitemaps = robotparser.RobotFileParser(), []
        try:
            resp = self._session.get(base_url.rstrip("/") + "/robots.txt", timeout=self.timeout)
            if resp.status_code == 200 and resp.text:
                robots.parse(resp.text.splitlines())
                for line in resp.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        sitemaps.append(line.split(":", 1)[1].strip())
        except requests.RequestException as exc:
            log.warning("robots.txt unavailable (%s); refusing crawl until robots can be read", exc)
            return None, sitemaps
        sitemaps.extend([base_url.rstrip("/") + s for s in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")])
        return robots, sitemaps

    def allowed(self, url: str) -> bool:
        if self.robots is None:
            return False
        try:
            return self.robots.can_fetch(self.user_agent, url)
        except Exception:
            return False

    def get(self, url: str, etag: str = "", last_modified: str = "") -> FetchResult:
        """Conditional GET. 304 means unchanged; html is None."""
        self._throttle()
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            session = getattr(self._local, "session", None)
            if session is None:
                session = requests.Session()
                session.headers.update({"User-Agent": self.user_agent, "Accept-Language": "en,id"})
                adapter = HTTPAdapter(max_retries=self._retry)
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                self._local.session = session
            resp = session.get(url, timeout=self.timeout, headers=headers)
            html = resp.text if 200 <= resp.status_code < 300 else None
            return FetchResult(url, resp.status_code, html, resp.url,
                               resp.headers.get("ETag", ""),
                               resp.headers.get("Last-Modified", ""), stamp)
        except requests.RequestException as exc:
            log.warning("fetch failed %s: %s", url, exc)
            return FetchResult(url, 0, None, url, "", "", stamp, error=str(exc))
