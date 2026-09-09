"""FastAPI application. Read-only access to data/gomunime.db."""
import os
import sqlite3
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from scraper import queries

DB_PATH = Path(os.environ.get("DB_PATH", "data/gomunime.db"))

app = FastAPI(
    title="gomunime metadata API",
    description="REST API over publicly scraped anime metadata. Metadata only — "
                "no video/stream content is served or stored.",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"],
                   allow_headers=["*"])


def get_db():
    if not DB_PATH.exists():
        raise HTTPException(status_code=503,
                            detail="database not built yet — run the scraper first")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _not_found(what: str):
    raise HTTPException(status_code=404, detail=f"{what} not found")


@app.get("/")
def root():
    return {
        "name": "gomunime metadata API",
        "endpoints": [
            "/api/anime", "/api/anime/{id}", "/api/anime/{id}/episodes",
            "/api/episode/{id}", "/api/ongoing", "/api/completed", "/api/latest",
            "/api/popular", "/api/search?q=", "/api/genres", "/api/genre/{genre}",
            "/api/types", "/api/stats",
        ],
    }


def _list_params(type_: str = Query(None, alias="type"),
                 status: str = Query(None),
                 genre: str = Query(None),
                 year: int = Query(None),
                 season: str = Query(None),
                 q: str = Query(None, min_length=1),
                 sort: str = Query("title"),
                 order: str = Query("asc", pattern="^(asc|desc)$"),
                 page: int = Query(1, ge=1),
                 per_page: int = Query(50, ge=1, le=100)):
    return dict(type_=type_, status=status, genre=genre, year=year, season=season,
                q=q, sort=sort, order=order, page=page, per_page=per_page)


@app.get("/api/anime")
def list_anime(params: dict = Depends(_list_params), db=Depends(get_db)):
    return queries.list_anime(db, **params)


@app.get("/api/anime/{anime_id}")
def anime_detail(anime_id: str, db=Depends(get_db)):
    anime = queries.get_anime(db, anime_id)
    if not anime:
        _not_found("anime")
    return anime


@app.get("/api/anime/{anime_id}/episodes")
def anime_episodes(anime_id: str, db=Depends(get_db)):
    anime = queries.get_anime(db, anime_id)
    if not anime:
        _not_found("anime")
    return {"anime": {"id": anime["id"], "slug": anime["slug"], "title": anime["title"]},
            "count": len(anime["episodes"]), "episodes": anime["episodes"]}


@app.get("/api/episode/{episode_id}")
def episode_detail(episode_id: str, db=Depends(get_db)):
    episode = queries.get_episode(db, episode_id)
    if not episode:
        _not_found("episode")
    return episode


@app.get("/api/ongoing")
def ongoing(page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
            db=Depends(get_db)):
    return queries.list_anime(db, page=page, per_page=per_page, status="ongoing",
                              sort="last_changed", order="desc")


@app.get("/api/completed")
def completed(page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
              db=Depends(get_db)):
    return queries.list_anime(db, page=page, per_page=per_page, status="completed",
                              sort="last_changed", order="desc")


@app.get("/api/latest")
def latest(page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
           db=Depends(get_db)):
    return queries.latest_anime(db, page=page, per_page=per_page)


@app.get("/api/popular")
def popular(page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
            db=Depends(get_db)):
    return queries.list_anime(db, page=page, per_page=per_page, sort="score",
                              order="desc")


@app.get("/api/search")
def search(q: str = Query(..., min_length=1),
           page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
           db=Depends(get_db)):
    return queries.list_anime(db, page=page, per_page=per_page, q=q,
                              sort="title", order="asc")


@app.get("/api/genres")
def genre_list(db=Depends(get_db)):
    return {"count": 0, "genres": queries.genres(db)} if not (genres := queries.genres(db)) \
        else {"count": len(genres), "genres": genres}


@app.get("/api/genre/{genre}")
def genre_anime(genre: str, page: int = Query(1, ge=1),
                per_page: int = Query(50, ge=1, le=100), db=Depends(get_db)):
    return queries.list_anime(db, page=page, per_page=per_page, genre=genre)


@app.get("/api/types")
def type_list(db=Depends(get_db)):
    rows = db.execute(
        "SELECT type, COUNT(*) count FROM anime WHERE type != '' "
        "GROUP BY type ORDER BY count DESC").fetchall()
    return {"types": [dict(r) for r in rows]}


@app.get("/api/stats")
def dataset_stats(db=Depends(get_db)):
    return queries.stats(db)
