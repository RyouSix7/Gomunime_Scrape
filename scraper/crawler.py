"""Crawl orchestration: seeds (sections + sitemaps), persistent frontier,
bounded thread pool with a global rate limit, incremental state, removal
detection, and error isolation per page."""
import hashlib
import logging
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from . import extract, parsers
from .classify import ANIME, EPISODE, GENRE, LISTING, PageClassifier
from .http import PoliteFetcher
from .models import Anime, Episode
from .normalize import host_of, now_iso, parse_duration, parse_iso, stable_id
from .validate import validate_anime, validate_episode

log = logging.getLogger("scraper.crawler")

_DEFAULT_TTL = {"listing": "6h", "anime": "14d", "episode": "3d", "genre": "30d"}


def _local(tag: str) -> str:
    return tag.split("}")[-1]


class Crawler:
    def __init__(self, settings, site_cfg, store):
        self.settings = settings
        self.store = store
        routes = site_cfg.get("routes", {})
        self._routes_cache = routes
        self.classifier = PageClassifier(routes)
        self.excludes = [e.lower() for e in site_cfg.get("exclude_paths", [])]
        limits = site_cfg.get("limits", {})
        self.ttl = {k: parse_duration(v) for k, v in
                    (limits.get("ttl") or _DEFAULT_TTL).items()}
        self.removal_after = int(limits.get("removal_after_404", 5))
        self.ctx = {
            "base_url": settings.base_url,
            "classifier": self.classifier,
            "selectors": site_cfg.get("selectors", {}),
        }
        self.fetcher = PoliteFetcher(
            settings.base_url, settings.user_agent, settings.delay,
            settings.timeout, settings.retries, settings.concurrency)
        self.stats = dict.fromkeys(("fetched", "not_modified", "robots_skipped",
                                    "skipped_fresh", "errors", "parse_rejects", "removed",
                                    "discovered", "anime_created", "anime_updated",
                                    "anime_unchanged", "episode_created", "episode_updated",
                                    "episode_unchanged"), 0)

    # ---------------- public entry ----------------

    def run(self, max_pages=0, full=False, sections=None) -> dict:
        run_id = self.store.start_run()
        self.stats["run_id"] = run_id
        budget = max_pages or self.settings.max_pages_per_run
        log.info("run %s starting: budget=%d full=%s", run_id, budget, full)
        self.store.release_expired_leases()
        self._seed(sections, force=full)
        batch_size = min(self.settings.concurrency * 4, 64)
        lease_seconds = max(300, int(self.settings.timeout * (self.settings.retries + 2) * 4))
        try:
            with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
                while budget > 0 and self.store.frontier_size() > 0:
                    batch = self.store.claim_frontier(min(budget, batch_size), lease_seconds)
                    if not batch:
                        break
                    futures = {pool.submit(self._fetch, entry): entry for entry in batch}
                    for future in as_completed(futures):
                        entry = futures[future]
                        try:
                            result = future.result()
                            success = self._handle(result, entry, full)
                            self.store.complete_frontier(
                                entry["url"], entry["lease_token"], success=success,
                                retry_seconds=self._retry_delay(entry))
                        except Exception:
                            log.exception("handler crashed on %s", entry["url"])
                            self.stats["errors"] += 1
                            self.store.complete_frontier(
                                entry["url"], entry["lease_token"], success=False,
                                retry_seconds=self._retry_delay(entry))
                        budget -= 1
        finally:
            self.stats["frontier_remaining"] = self.store.frontier_size()
            self.stats["run_status"] = ("ok" if self.stats["errors"] == 0 and self.stats["parse_rejects"] == 0 and self.stats["robots_skipped"] == 0 else "partial")
            self.store.finish_run(run_id, self.stats)
            log.info("run %s finished: %s", run_id,
                     {k: v for k, v in self.stats.items() if k != "run_id"})
        return self.stats

    def _retry_delay(self, entry):
        failures = int(entry.get("consecutive_404") or 0)
        # Network/parse failures use exponential backoff, capped at 1 hour.
        return min(3600, 30 * (2 ** min(failures, 6)))

    # ---------------- seeding ----------------

    def _seed(self, sections, force=False):
        base = self.settings.base_url
        listings = sections or (self.ctx and self.site_listings())
        entries = [(base + (p if p.startswith("/") else "/" + p), LISTING, 0, "seed")
                   for p in listings]
        self.store.push_frontier(entries, ttl_by_type=self.ttl, force=force)
        sitemap_entries = []
        for url in self._sitemaps():
            ptype, _ = self.classifier.classify(url)
            if ptype == "other" or self.classifier.is_excluded(url, self.excludes):
                continue
            sitemap_entries.append((url, ptype, self.classifier.priority(ptype), "sitemap"))
        sitemap_entries = sitemap_entries[:30000]
        self.store.push_frontier(sitemap_entries, ttl_by_type=self.ttl, force=force)
        log.info("seeded %d listing + %d sitemap URLs", len(entries), len(sitemap_entries))

    def site_listings(self):
        routes = self._routes()
        return routes.get("listings") or ["/"]

    def _routes(self):
        # Kept separate for testability; config was passed via constructor ctx.
        return getattr(self, "_routes_cache", None) or {"listings": ["/"]}

    def _sitemaps(self, max_urls=30000):
        """Discover sitemaps via robots.txt + common WordPress paths."""
        found, queue, seen = [], list(self.fetcher.sitemap_urls), set()
        while queue and len(found) < max_urls:
            sitemap = queue.pop(0)
            if sitemap in seen:
                continue
            seen.add(sitemap)
            if host_of(sitemap) != host_of(self.settings.base_url):
                continue
            if not self.fetcher.allowed(sitemap):
                continue
            result = self.fetcher.get(sitemap)
            if not result.ok or not result.html:
                continue
            try:
                root = ET.fromstring(result.html.encode("utf-8"))
            except ET.ParseError:
                log.warning("unparseable sitemap %s", sitemap)
                continue
            if _local(root.tag) == "sitemapindex":
                for child in root:
                    if _local(child.tag) == "sitemap":
                        for sub in child:
                            if _local(sub.tag) == "loc" and sub.text:
                                queue.append(sub.text.strip())
            else:
                for entry in root:
                    if _local(entry.tag) != "url":
                        continue
                    for child in entry:
                        if _local(child.tag) == "loc" and child.text:
                            found.append(child.text.strip())
        log.info("sitemap discovery: %d URLs", len(found))
        return found

    # ---------------- fetch / handle ----------------

    def _fetch(self, entry):
        url = entry["url"]
        if not self.fetcher.allowed(url):
            return None
        return self.fetcher.get(url, etag=entry.get("etag") or "",
                                last_modified=entry.get("last_modified") or "")

    def _handle(self, result, entry, full=False):
        url, ptype = entry["url"], entry["page_type"]
        if result is None:
            self.stats["robots_skipped"] += 1
            # No state update: robots decisions are not crawl success.
            return True
        if self._is_fresh(entry, ptype) and not full and ptype != LISTING and entry.get("last_status") == 200:
            self.stats["skipped_fresh"] += 1
            return True
        self.stats["fetched"] += 1
        if result.status == 304:
            self.stats["not_modified"] += 1
            self.store.set_state(url, ptype, 200, entry.get("content_hash") or "", result.etag, result.last_modified)
            return True
        if result.status in (404, 410):
            hits = self.store.bump_failure(url, result.status, True)
            if hits >= self.removal_after:
                self.store.mark_unavailable(url, "removed")
                self.stats["removed"] += 1
            # Keep URL queued so transient deletions can recover and the streak is observable.
            return False
        if result.error or result.status == 0 or result.status >= 500:
            self.store.bump_failure(url, result.status, False)
            self.stats["errors"] += 1
            return False
        if not (200 <= result.status < 300) or result.html is None:
            self.stats["errors"] += 1
            return False
        page_hash = hashlib.sha256(result.html.encode("utf-8", "ignore")).hexdigest()
        if page_hash == entry.get("content_hash") and ptype != LISTING:
            self.stats["not_modified"] += 1
            self.store.set_state(url, ptype, 200, page_hash, result.etag, result.last_modified)
            return True
        try:
            soup = extract.soup_from(result.html)
            self._dispatch(url, ptype, soup)
        except Exception:
            self.stats["errors"] += 1
            self.store.bump_failure(url, result.status, False)
            log.exception("parse dispatch failed: %s", url)
            return False
        try:
            self._discover(url, soup)
        except Exception:
            # Parsing succeeded; retrying the whole page is preferable to losing it,
            # but don't mark the page failed because its own record is valid.
            log.exception("link discovery failed: %s", url)
        # Only now is the crawl state marked successful. A crash before this point
        # leaves the leased frontier item reclaimable on the next run.
        self.store.set_state(url, ptype, 200, page_hash, result.etag, result.last_modified)
        return True

    def _is_fresh(self, entry, ptype):
        if entry.get("last_status") != 200 or not entry.get("last_crawled"):
            return False
        last = parse_iso(entry["last_crawled"])
        if not last:
            return False
        ttl = self.ttl.get(ptype, self.ttl.get("anime", 1209600))
        return (datetime.now(timezone.utc) - last).total_seconds() < ttl

    # ---------------- dispatch ----------------

    def _dispatch(self, url, ptype, soup):
        if ptype == ANIME:
            parsed = parsers.parse_anime_page(url, soup, self.ctx)
            if parsed is None:
                self.stats["parse_rejects"] += 1
                return
            anime, stubs = parsed
            ok, reason = validate_anime(anime)
            if not ok:
                log.warning("invalid anime %s: %s", url, reason)
                self.stats["parse_rejects"] += 1
                return
            action = self.store.upsert_anime(anime)
            self.stats[f"anime_{action}"] += 1
            for stub in stubs:
                self._store_episode_stub(anime, stub)
        elif ptype == EPISODE:
            episode = parsers.parse_episode_page(url, soup, self.ctx)
            if episode is None:
                self.stats["parse_rejects"] += 1
                return
            ok, reason = validate_episode(episode)
            if not ok:
                log.warning("invalid episode %s: %s", url, reason)
                self.stats["parse_rejects"] += 1
                return
            self.store.ensure_anime_stub(episode.anime_id,
                                         (episode.raw.get("anime_slug") or ""),
                                         episode.title, episode.images[0] if episode.images else "")
            action = self.store.upsert_episode(episode)
            self.stats[f"episode_{action}"] += 1
        elif ptype == GENRE:
            # Genre pages are also listings of anime; discovery handles links,
            # and we record the genre directory if present.
            for slug, name in parsers.parse_genre_links(url, soup, self.ctx):
                log.debug("genre directory entry: %s (%s)", name, slug)
        # LISTING pages: no dedicated parsing — link discovery does the work.

    def _store_episode_stub(self, anime: Anime, stub):
        stamp = now_iso()
        key = str(stub.number if stub.number is not None else stub.url)
        episode = Episode(
            id=stable_id("episode", anime.id, key), anime_id=anime.id,
            number=float(stub.number) if stub.number is not None else None,
            title=stub.title[:500], url=stub.url,
            images=[stub.image] if stub.image else [],
            raw={"stub": True, "source_page": anime.url},
            availability="stub", first_seen=stamp, last_seen=stamp, last_changed=stamp,
        )
        episode.content_hash = "stub:" + episode.id
        action = self.store.upsert_episode(episode, stub=True)
        self.stats[f"episode_{action}"] += 1

    # ---------------- discovery ----------------

    def _discover(self, source_url, soup):
        base_host = host_of(self.settings.base_url)
        entries = []
        for url, _, _ in extract.links_on(soup, source_url):
            if host_of(url) != base_host:
                continue
            if self.classifier.is_excluded(url, self.excludes):
                continue
            ptype, _ = self.classifier.classify(url)
            if ptype == "other":
                continue
            entries.append((url, ptype, self.classifier.priority(ptype), source_url))
        if entries:
            self.store.push_frontier(entries, ttl_by_type=self.ttl)
            self.stats["discovered"] += len(entries)
