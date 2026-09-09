"""SQLite storage: schema, upserts with change detection, dedupe via stable
IDs, crawl state, persistent frontier, change log, run log."""
import json
import logging
import sqlite3
import threading
import uuid

from .models import Anime, Episode
from .normalize import now_iso

log = logging.getLogger("scraper.store")


def _j(value):
    return json.dumps(value, ensure_ascii=False)


def _uj(text, default):
    try:
        return json.loads(text) if text else default
    except (json.JSONDecodeError, TypeError):
        return default


SCHEMA = """
CREATE TABLE IF NOT EXISTS anime (
  id TEXT PRIMARY KEY, slug TEXT UNIQUE, url TEXT, title TEXT,
  alt_titles TEXT, genres TEXT, type TEXT, status TEXT, season TEXT,
  studio TEXT, year INTEGER, score REAL, duration TEXT,
  episode_count INTEGER, synopsis TEXT, images TEXT, raw TEXT,
  availability TEXT DEFAULT 'ok', content_hash TEXT,
  first_seen TEXT, last_seen TEXT, last_changed TEXT
);
CREATE INDEX IF NOT EXISTS idx_anime_status ON anime(status);
CREATE INDEX IF NOT EXISTS idx_anime_type ON anime(type);
CREATE INDEX IF NOT EXISTS idx_anime_year ON anime(year);

CREATE TABLE IF NOT EXISTS episode (
  id TEXT PRIMARY KEY, anime_id TEXT REFERENCES anime(id),
  number REAL, title TEXT, url TEXT, air_date TEXT, images TEXT, raw TEXT,
  availability TEXT DEFAULT 'ok', content_hash TEXT,
  first_seen TEXT, last_seen TEXT, last_changed TEXT
);
CREATE INDEX IF NOT EXISTS idx_episode_anime ON episode(anime_id);
CREATE INDEX IF NOT EXISTS idx_episode_number ON episode(anime_id, number);

CREATE TABLE IF NOT EXISTS anime_genre (
  anime_id TEXT, genre TEXT, PRIMARY KEY (anime_id, genre)
);

CREATE TABLE IF NOT EXISTS crawl_state (
  url TEXT PRIMARY KEY, page_type TEXT, last_status INTEGER,
  content_hash TEXT, etag TEXT, last_modified TEXT, last_crawled TEXT,
  consecutive_failures INTEGER DEFAULT 0, consecutive_404 INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS frontier (
  url TEXT PRIMARY KEY, page_type TEXT, priority INTEGER,
  discovered TEXT, reason TEXT, not_before TEXT, lease_token TEXT, lease_until TEXT
);


CREATE TABLE IF NOT EXISTS change_log (
  ts TEXT, entity TEXT, entity_id TEXT, action TEXT, detail TEXT
);

CREATE TABLE IF NOT EXISTS run_log (
  run_id TEXT PRIMARY KEY, started TEXT, finished TEXT,
  pages_fetched INTEGER, pages_not_modified INTEGER, errors INTEGER,
  anime_created INTEGER, anime_updated INTEGER, anime_unchanged INTEGER,
  episode_created INTEGER, episode_updated INTEGER, episode_unchanged INTEGER,
  status TEXT, notes TEXT
);
"""

STAT_KEYS = ("fetched", "not_modified", "robots_skipped", "skipped_fresh",
             "errors", "parse_rejects", "removed", "discovered",
             "anime_created", "anime_updated", "anime_unchanged",
             "episode_created", "episode_updated", "episode_unchanged")


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_frontier_order ON frontier(priority, not_before, discovered)")
        self.db.commit()

    def _migrate(self):
        """Small forward-only schema migration for databases created by older builds."""
        cols = {row[1] for row in self.db.execute("PRAGMA table_info(frontier)").fetchall()}
        for name, ddl in (("not_before", "TEXT"), ("lease_token", "TEXT"), ("lease_until", "TEXT")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE frontier ADD COLUMN {name} {ddl}")
        # Old rows are immediately eligible for processing.
        self.db.execute("UPDATE frontier SET not_before=COALESCE(not_before, ?) WHERE not_before IS NULL", (now_iso(),))

    # ---------------- crawl state ----------------

    def get_state(self, url):
        with self._lock:
            row = self.db.execute("SELECT * FROM crawl_state WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None

    def set_state(self, url, page_type, status, content_hash, etag, last_modified):
        with self._lock:
            self.db.execute("""
                INSERT INTO crawl_state (url, page_type, last_status, content_hash,
                    etag, last_modified, last_crawled, consecutive_failures, consecutive_404)
                VALUES (?,?,?,?,?,?,?,0,0)
                ON CONFLICT(url) DO UPDATE SET page_type=excluded.page_type,
                    last_status=excluded.last_status, content_hash=excluded.content_hash,
                    etag=excluded.etag, last_modified=excluded.last_modified,
                    last_crawled=excluded.last_crawled,
                    consecutive_failures=0, consecutive_404=0
            """, (url, page_type, status, content_hash, etag, last_modified, now_iso()))
            self.db.commit()

    def bump_failure(self, url, status, is_404):
        col = "consecutive_404" if is_404 else "consecutive_failures"
        other = "consecutive_failures" if is_404 else "consecutive_404"
        with self._lock:
            self.db.execute(f"""
                INSERT INTO crawl_state (url, last_status, last_crawled, {col}, {other})
                VALUES (?,?,?,?,0)
                ON CONFLICT(url) DO UPDATE SET last_status=excluded.last_status,
                    last_crawled=excluded.last_crawled, {col}=crawl_state.{col}+1, {other}=0
            """, (url, status, now_iso(), 1))
            row = self.db.execute(f"SELECT {col} FROM crawl_state WHERE url=?", (url,)).fetchone()
            self.db.commit()
        return row[col] if row else 1

    # ---------------- frontier ----------------

    def push_frontier(self, entries, ttl_by_type=None, force=False):
        """Queue URLs idempotently. Fresh successful pages are skipped unless forced."""
        ttl_by_type = ttl_by_type or {}
        from datetime import datetime, timedelta, timezone
        stamp = now_iso()
        with self._lock:
            for url, page_type, priority, reason in entries:
                if force:
                    self.db.execute("""
                        INSERT INTO frontier(url,page_type,priority,discovered,reason,not_before,lease_token,lease_until)
                        VALUES (?,?,?,?,?,?,NULL,NULL)
                        ON CONFLICT(url) DO UPDATE SET page_type=excluded.page_type,
                          priority=MIN(frontier.priority, excluded.priority), reason=excluded.reason,
                          not_before=excluded.not_before, lease_token=NULL, lease_until=NULL
                    """, (url, page_type, priority, stamp, reason, stamp))
                    continue
                ttl = ttl_by_type.get(page_type, ttl_by_type.get("anime", 1209600))
                cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")
                self.db.execute("""
                    INSERT INTO frontier(url,page_type,priority,discovered,reason,not_before,lease_token,lease_until)
                    SELECT ?,?,?,?,?,?,NULL,NULL WHERE NOT EXISTS (
                        SELECT 1 FROM crawl_state WHERE url=? AND last_status=200 AND last_crawled > ?)
                    ON CONFLICT(url) DO NOTHING
                """, (url, page_type, priority, stamp, reason, stamp, url, cutoff))
            self.db.commit()

    def claim_frontier(self, limit, lease_seconds=900):
        """Atomically claim work. Leases make crashes recoverable and prevent duplicate claims."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        now_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        lease_until = (now + timedelta(seconds=max(30, lease_seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")
        token = uuid.uuid4().hex
        with self._lock:
            self.db.execute("""
                UPDATE frontier SET lease_token=?, lease_until=?
                WHERE url IN (
                    SELECT url FROM frontier
                    WHERE (lease_until IS NULL OR lease_until <= ?)
                      AND (not_before IS NULL OR not_before <= ?)
                    ORDER BY priority, discovered LIMIT ?
                )
            """, (token, lease_until, now_s, now_s, limit))
            rows = self.db.execute("""
                SELECT f.url, f.page_type, f.priority, s.last_status, s.etag,
                       s.last_modified, s.last_crawled, s.content_hash, s.consecutive_404,
                       f.lease_token, f.lease_until
                FROM frontier f LEFT JOIN crawl_state s ON s.url=f.url
                WHERE f.lease_token=? ORDER BY f.priority, f.discovered
            """, (token,)).fetchall()
            self.db.commit()
        return [dict(r) for r in rows]

    def complete_frontier(self, url, token, success=True, retry_seconds=0):
        """Delete successful work; otherwise release it with a bounded retry delay."""
        from datetime import datetime, timedelta, timezone
        with self._lock:
            if success:
                self.db.execute("DELETE FROM frontier WHERE url=? AND lease_token=?", (url, token))
            else:
                retry_at = (datetime.now(timezone.utc) + timedelta(seconds=max(1, retry_seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")
                self.db.execute("""
                    UPDATE frontier SET lease_token=NULL, lease_until=NULL, not_before=?
                    WHERE url=? AND lease_token=?
                """, (retry_at, url, token))
            self.db.commit()

    def release_expired_leases(self):
        from datetime import datetime, timezone
        now_s = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            cur = self.db.execute("""
                UPDATE frontier SET lease_token=NULL, lease_until=NULL
                WHERE lease_until IS NOT NULL AND lease_until <= ?
            """, (now_s,))
            self.db.commit()
            return cur.rowcount

    def frontier_size(self):
        with self._lock:
            return self.db.execute("SELECT COUNT(*) c FROM frontier").fetchone()["c"]

    # ---------------- entities ----------------

    def upsert_anime(self, anime: Anime) -> str:
        stamp = now_iso()
        with self._lock:
            row = self.db.execute(
                "SELECT content_hash, raw, availability FROM anime WHERE id=?",
                (anime.id,)).fetchone()
            params = (anime.id, anime.slug, anime.url, anime.title, _j(anime.alt_titles),
                      _j(anime.genres), anime.type, anime.status, anime.season, anime.studio,
                      anime.year, anime.score, anime.duration, anime.episode_count,
                      anime.synopsis, _j(anime.images), _j(anime.raw), anime.availability,
                      anime.content_hash, anime.first_seen or stamp, stamp, stamp)
            if row is None:
                self.db.execute(f"INSERT INTO anime VALUES ({','.join('?' * 22)})", params)
                self._link_genres(anime)
                self._change("anime", anime.id, "created", anime.title)
                action = "created"
            elif row["content_hash"] != anime.content_hash:
                self.db.execute("""
                    UPDATE anime SET slug=?, url=?, title=?, alt_titles=?, genres=?, type=?,
                        status=?, season=?, studio=?, year=?, score=?, duration=?,
                        episode_count=?, synopsis=?, images=?, raw=?, availability='ok',
                        content_hash=?, last_seen=?, last_changed=? WHERE id=?
                """, params[1:18] + (stamp, stamp, anime.id))
                self._link_genres(anime)
                old_raw = _uj(row["raw"], {})
                changed = [k for k in set(old_raw) | set(anime.raw)
                           if old_raw.get(k) != anime.raw.get(k)]
                self._change("anime", anime.id, "updated",
                             ", ".join(sorted(changed))[:500] or "typed fields")
                action = "updated"
            else:
                self.db.execute(
                    "UPDATE anime SET availability='ok', last_seen=? WHERE id=?",
                    (stamp, anime.id))
                action = "unchanged"
            self.db.commit()
        return action

    def upsert_episode(self, episode: Episode, stub=False) -> str:
        stamp = now_iso()
        with self._lock:
            row = self.db.execute(
                "SELECT content_hash, availability FROM episode WHERE id=?",
                (episode.id,)).fetchone()
            params = (episode.id, episode.anime_id, episode.number, episode.title,
                      episode.url, episode.air_date, _j(episode.images), _j(episode.raw),
                      episode.availability, episode.content_hash,
                      episode.first_seen or stamp, stamp, stamp)
            if row is None:
                self.db.execute(f"INSERT INTO episode VALUES ({','.join('?' * 13)})", params)
                self._change("episode", episode.id, "created",
                             f"ep {episode.number} of {episode.anime_id}")
                action = "created"
            elif row["availability"] == "ok" and episode.availability == "stub":
                # Never downgrade a fully-parsed episode back to stub data.
                self.db.execute("UPDATE episode SET last_seen=? WHERE id=?",
                                (stamp, episode.id))
                action = "unchanged"
            elif row["content_hash"] != episode.content_hash:
                self.db.execute("""
                    UPDATE episode SET anime_id=?, number=?, title=?, url=?, air_date=?,
                        images=?, raw=?, availability=?, content_hash=?, last_seen=?,
                        last_changed=? WHERE id=?
                """, params[1:10] + (stamp, stamp, episode.id))
                self._change("episode", episode.id, "updated",
                             f"ep {episode.number} of {episode.anime_id}")
                action = "updated"
            else:
                self.db.execute("UPDATE episode SET availability=?, last_seen=? WHERE id=?",
                                (episode.availability, stamp, episode.id))
                action = "unchanged"
            self.db.commit()
        return action

    def ensure_anime_stub(self, anime_id, slug, title, image):
        """Minimal parent row for episodes discovered before their anime page."""
        stamp = now_iso()
        with self._lock:
            self.db.execute("""
                INSERT OR IGNORE INTO anime (id, slug, url, title, images, availability,
                    first_seen, last_seen)
                VALUES (?,?,?,?,?, 'stub', ?, ?)
            """, (anime_id, slug, "", title[:500], _j([image] if image else []),
                  stamp, stamp))
            self.db.commit()

    def mark_unavailable(self, url, availability="removed"):
        stamp = now_iso()
        with self._lock:
            cur = self.db.execute("UPDATE anime SET availability=?, last_changed=? WHERE url=?",
                                  (availability, stamp, url))
            if cur.rowcount:
                self._change("anime", url, availability, url)
            cur = self.db.execute("UPDATE episode SET availability=?, last_changed=? WHERE url=?",
                                  (availability, stamp, url))
            if cur.rowcount:
                self._change("episode", url, availability, url)
            self.db.commit()

    # ---------------- runs / logs ----------------

    def start_run(self) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self._lock:
            self.db.execute("INSERT INTO run_log (run_id, started, status) VALUES (?,?, 'running')",
                            (run_id, now_iso()))
            self.db.commit()
        return run_id

    def finish_run(self, run_id, stats: dict):
        status = "ok" if stats.get("fetched", 0) > 0 and stats.get("errors", 0) < stats.get("fetched", 0) \
            else ("partial" if stats.get("fetched", 0) > 0 else "failed")
        with self._lock:
            self.db.execute("""
                UPDATE run_log SET finished=?, pages_fetched=?, pages_not_modified=?,
                    errors=?, anime_created=?, anime_updated=?, anime_unchanged=?,
                    episode_created=?, episode_updated=?, episode_unchanged=?,
                    status=?, notes=?
                WHERE run_id=?
            """, (now_iso(), stats.get("fetched", 0), stats.get("not_modified", 0),
                  stats.get("errors", 0), stats.get("anime_created", 0),
                  stats.get("anime_updated", 0), stats.get("anime_unchanged", 0),
                  stats.get("episode_created", 0), stats.get("episode_updated", 0),
                  stats.get("episode_unchanged", 0), status,
                  json.dumps({k: stats.get(k, 0) for k in STAT_KEYS}), run_id))
            self.db.commit()

    def _link_genres(self, anime: Anime):
        self.db.execute("DELETE FROM anime_genre WHERE anime_id=?", (anime.id,))
        self.db.executemany("INSERT OR IGNORE INTO anime_genre VALUES (?,?)",
                            [(anime.id, g) for g in anime.genres if g])

    def _change(self, entity, entity_id, action, detail):
        self.db.execute("INSERT INTO change_log VALUES (?,?,?,?,?)",
                        (now_iso(), entity, entity_id, action, str(detail)[:500]))

    def close(self):
        with self._lock:
            self.db.commit()
            self.db.close()
