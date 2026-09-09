"""Read-only query layer shared by the API and the JSON exporter."""
import json
import sqlite3
from pathlib import Path

ANIME_LIST_COLS = ("id, slug, url, title, alt_titles, genres, type, status, season, "
                   "studio, year, score, duration, episode_count, images, availability, "
                   "first_seen, last_seen, last_changed")
EPISODE_COLS = ("id, anime_id, number, title, url, air_date, images, availability, "
                "first_seen, last_seen, last_changed")

SORT_SQL = {
    "title": "title COLLATE NOCASE {dir}",
    "slug": "slug {dir}",
    "year": "(year IS NULL) {dir}, year {dir}",
    "score": "(score IS NULL) {dir}, score {dir}",
    "last_changed": "last_changed {dir}",
    "last_seen": "last_seen {dir}",
    "first_seen": "first_seen {dir}",
}


def connect(db_path):
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path} — run the scraper first")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _decode_anime(row, synopsis=False, raw=False, episodes=None):
    d = dict(row)
    for key in ("alt_titles", "genres", "images"):
        d[key] = json.loads(d.pop(key, None) or "[]")
    if not synopsis:
        d.pop("synopsis", None)
    if not raw:
        d.pop("raw", None)
    if episodes is not None:
        d["episodes"] = episodes
    return d


def _decode_episode(row):
    d = dict(row)
    d["images"] = json.loads(d.pop("images", None) or "[]")
    return d


def list_anime(conn, page=1, per_page=50, type_=None, status=None, genre=None,
               year=None, season=None, q=None, sort="title", order="asc",
               include_removed=False):
    where, params = [], []
    if type_:
        where.append("type = ?")
        params.append(type_)
    if status:
        where.append("status = ?")
        params.append(status)
    if year:
        where.append("year = ?")
        params.append(year)
    if season:
        where.append("season = ?")
        params.append(season)
    if genre:
        where.append("id IN (SELECT anime_id FROM anime_genre WHERE genre = ? COLLATE NOCASE)")
        params.append(genre)
    if q:
        where.append("(title LIKE ? OR alt_titles LIKE ? OR slug LIKE ?)")
        wildcard = f"%{q}%"
        params.extend([wildcard, wildcard, wildcard])
    if not include_removed:
        where.append("availability != 'removed'")
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    order_sql = SORT_SQL.get(sort, SORT_SQL["title"]).format(dir=direction)
    total = conn.execute(f"SELECT COUNT(*) c FROM anime{wsql}", params).fetchone()["c"]
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT {ANIME_LIST_COLS} FROM anime{wsql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        (*params, per_page, offset)).fetchall()
    return {
        "items": [_decode_anime(r) for r in rows],
        "page": page, "per_page": per_page, "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    }


def get_anime(conn, key):
    row = conn.execute("SELECT * FROM anime WHERE id=? OR slug=?", (key, key)).fetchone()
    if not row:
        return None
    return _decode_anime(row, synopsis=True, raw=True, episodes=episodes_of(conn, row["id"]))


def episodes_of(conn, anime_id):
    rows = conn.execute(
        f"SELECT {EPISODE_COLS} FROM episode WHERE anime_id=? "
        "ORDER BY (number IS NULL), number, id", (anime_id,)).fetchall()
    return [_decode_episode(r) for r in rows]


def get_episode(conn, key):
    row = conn.execute("SELECT * FROM episode WHERE id=?", (key,)).fetchone()
    if not row:
        return None
    episode = _decode_episode(row)
    episode["raw"] = json.loads(row["raw"] or "{}")
    anime = conn.execute(
        "SELECT id, slug, url, title, type, status FROM anime WHERE id=?",
        (row["anime_id"],)).fetchone()
    episode["anime"] = dict(anime) if anime else None
    return episode


def latest_anime(conn, page=1, per_page=50, include_removed=False):
    """Anime ordered by the most recently seen episode (new releases first)."""
    where = "" if include_removed else " WHERE a.availability != 'removed'"
    total = conn.execute(
        f"SELECT COUNT(*) c FROM anime a JOIN (SELECT anime_id, MAX(last_seen) m "
        f"FROM episode GROUP BY anime_id) e ON e.anime_id = a.id{where}").fetchone()["c"]
    per_page = max(1, min(per_page, 100))
    offset = (max(1, page) - 1) * per_page
    rows = conn.execute(
        f"SELECT {', '.join('a.' + c.strip() for c in ANIME_LIST_COLS.split(','))}, e.m AS latest_episode_seen "
        f"FROM anime a JOIN (SELECT anime_id, MAX(last_seen) m FROM episode GROUP BY anime_id) e "
        f"ON e.anime_id = a.id{where} ORDER BY e.m DESC LIMIT ? OFFSET ?",
        (per_page, offset)).fetchall()
    items = []
    for r in rows:
        d = _decode_anime(r)
        d["latest_episode_seen"] = r["latest_episode_seen"]
        items.append(d)
    return {"items": items, "page": max(1, page), "per_page": per_page, "total": total,
            "total_pages": (total + per_page - 1) // per_page}


def genres(conn):
    rows = conn.execute(
        "SELECT genre, COUNT(*) count FROM anime_genre GROUP BY genre "
        "ORDER BY count DESC, genre").fetchall()
    return [{"genre": r["genre"], "count": r["count"]} for r in rows]


def stats(conn):
    def one(sql):
        return conn.execute(sql).fetchone()[0]

    last = conn.execute(
        "SELECT * FROM run_log WHERE finished IS NOT NULL "
        "ORDER BY finished DESC LIMIT 1").fetchone()
    return {
        "anime_total": one("SELECT COUNT(*) FROM anime"),
        "anime_ok": one("SELECT COUNT(*) FROM anime WHERE availability='ok'"),
        "anime_stub": one("SELECT COUNT(*) FROM anime WHERE availability='stub'"),
        "anime_removed": one("SELECT COUNT(*) FROM anime WHERE availability='removed'"),
        "episodes_total": one("SELECT COUNT(*) FROM episode"),
        "genres": one("SELECT COUNT(DISTINCT genre) FROM anime_genre"),
        "last_successful_run": dict(last) if last else None,
        "generated_at": __import__("scraper.normalize", fromlist=["now_iso"]).now_iso(),
    }
