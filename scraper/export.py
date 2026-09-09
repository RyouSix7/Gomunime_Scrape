"""Static JSON export for zero-cost hosting (GitHub Pages / any CDN)."""
import json
import logging
from pathlib import Path

from . import queries
from .normalize import now_iso

log = logging.getLogger("scraper.export")


def run(db_path, out_dir):
    conn = queries.connect(db_path)
    out = Path(out_dir)
    (out / "anime").mkdir(parents=True, exist_ok=True)

    def write(name, payload):
        path = out / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        return path.stat().st_size

    summaries = [_decode_summary(r) for r in conn.execute(
        f"SELECT {queries.ANIME_LIST_COLS} FROM anime ORDER BY title COLLATE NOCASE")]
    write("anime.json", {"generated_at": now_iso(), "count": len(summaries),
                         "items": summaries})
    for row in conn.execute(f"SELECT {queries.ANIME_LIST_COLS} FROM anime"):
        detail = queries.get_anime(conn, row["id"])
        if detail:
            path = out / "anime" / f"{row['id']}.json"
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)

    write("genres.json", queries.genres(conn))
    write("stats.json", queries.stats(conn))
    write("ongoing.json", queries.list_anime(conn, per_page=100, status="ongoing",
                                             sort="last_changed", order="desc"))
    write("completed.json", queries.list_anime(conn, per_page=100, status="completed",
                                               sort="last_changed", order="desc"))
    write("latest.json", queries.latest_anime(conn, per_page=100))
    write("popular.json", queries.list_anime(conn, per_page=100, sort="score",
                                             order="desc"))
    write("index.json", {
        "generated_at": now_iso(),
        "endpoints": {
            "anime": "anime.json",
            "anime_detail": "anime/{id}.json",
            "genres": "genres.json",
            "ongoing": "ongoing.json",
            "completed": "completed.json",
            "latest": "latest.json",
            "popular": "popular.json",
            "stats": "stats.json",
        },
    })
    log.info("exported %d anime to %s", len(summaries), out)
    return len(summaries)


def _decode_summary(row):
    d = dict(row)
    for key in ("alt_titles", "genres", "images"):
        d[key] = json.loads(d.pop(key, None) or "[]")
    return d
