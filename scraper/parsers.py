"""Page-specific parsers. Typed extraction layered on the generic extractors;
anything unrecognized lands in `raw` instead of being dropped."""
import logging

from . import extract
from .classify import ANIME, EPISODE
from .models import Anime, Episode, EpisodeStub
from .normalize import (as_date, as_float, as_int, clean_text, json_hash,
                        norm_status, now_iso, slugify, stable_id)

log = logging.getLogger("scraper.parsers")

# Bilingual (EN/ID) key mapping used by anime listing themes.
KEYMAP = {
    "alternative_title": "alt_titles", "alternative_titles": "alt_titles",
    "judul_alternatif": "alt_titles", "judul_alternative": "alt_titles",
    "alternative": "alt_titles", "synonym": "alt_titles", "synonyms": "alt_titles",
    "english": "alt_titles", "english_title": "alt_titles", "japanese": "alt_titles",
    "japanese_title": "alt_titles", "romaji": "alt_titles",
    "type": "type", "tipe": "type", "format": "type",
    "status": "status", "genre": "genres", "genres": "genres",
    "season": "season", "musim": "season",
    "studio": "studio", "studios": "studio",
    "producer": "producers", "producers": "producers", "produksi": "producers",
    "director": "directors", "directors": "directors", "sutradara": "directors",
    "author": "authors", "authors": "authors", "creator": "authors",
    "pencipta": "authors", "mangaka": "authors",
    "duration": "duration", "durasi": "duration", "duration_per_episode": "duration",
    "total_episode": "episode_count", "total_episodes": "episode_count",
    "episodes": "episode_count", "jumlah_episode": "episode_count",
    "rating": "score", "score": "score", "skor": "score", "rating_score": "score",
    "age_rating": "age_rating", "rated": "age_rating", "klasifikasi": "age_rating",
    "country": "country", "negara": "country", "origin": "country",
    "network": "network", "networks": "network",
    "aired": "aired", "date_aired": "aired", "rilis": "aired", "tanggal_rilis": "aired",
    "year": "year", "tahun": "year", "release_year": "year",
}

_LIST_FIELDS = {"alt_titles", "genres", "producers", "directors", "authors"}


def _apply_keymap(infobox: dict, anime: Anime):
    for key, values in infobox.items():
        target = KEYMAP.get(key)
        if not target:
            continue
        if target in _LIST_FIELDS:
            bucket = getattr(anime, target)
            for value in values:
                value = clean_text(value).strip(" ,")
                if value and value not in bucket and len(value) < 300:
                    bucket.append(value)
        elif target == "status":
            anime.status = norm_status(values[0]) or anime.status
        elif target == "episode_count":
            anime.episode_count = as_int(values[0]) or anime.episode_count
        elif target == "score":
            parsed = as_float(values[0])
            if parsed is not None and anime.score is None:
                anime.score = parsed
        elif target == "year":
            anime.year = as_int(values[0]) or anime.year
        elif not getattr(anime, target):
            setattr(anime, target, clean_text(values[0])[:300])


def _apply_jsonld(blocks: list, anime: Anime):
    for block in blocks:
        if not isinstance(block, dict) or not block.get("name"):
            continue
        if not anime.title:
            anime.title = clean_text(block["name"])
        alt = block.get("alternateName")
        for item in (alt if isinstance(alt, list) else [alt] if alt else []):
            item = clean_text(item)
            if item and item not in anime.alt_titles:
                anime.alt_titles.append(item)
        if block.get("numberOfEpisodes") and not anime.episode_count:
            anime.episode_count = as_int(block["numberOfEpisodes"])
        genres = block.get("genre")
        for genre in (genres if isinstance(genres, list) else [genres] if genres else []):
            genre = clean_text(genre)
            if genre and genre not in anime.genres:
                anime.genres.append(genre)
        if block.get("datePublished") and not anime.year:
            anime.year = as_int(block["datePublished"])
        if block.get("description") and not anime.synopsis:
            anime.synopsis = clean_text(block["description"])[:20000]
        images = block.get("image")
        for image in (images if isinstance(images, list) else [images] if images else []):
            url = image if isinstance(image, str) else (image or {}).get("url", "")
            if url and url not in anime.images:
                anime.images.append(url)
        rating = block.get("aggregateRating") or {}
        if rating.get("ratingValue") and anime.score is None:
            anime.score = as_float(rating["ratingValue"])
        anime.raw.setdefault("jsonld", []).append(block)


def parse_anime_page(url: str, soup, ctx: dict):
    """Returns (Anime, [EpisodeStub]) or None when nothing usable was found."""
    selectors = ctx["selectors"].get("anime", {})
    meta = extract.meta_tags(soup)
    infobox = extract.infobox_pairs(soup, selectors.get("infobox", []))

    title = ""
    for selector in selectors.get("title", []):
        el = soup.select_one(selector)
        if el and clean_text(el.get_text()):
            title = clean_text(el.get_text())
            break
    if not title and meta.get("og_title"):
        title = meta["og_title"][0]

    synopsis = ""
    for selector in selectors.get("synopsis", []):
        el = soup.select_one(selector)
        if el:
            synopsis = clean_text(el.get_text())
            break
    if not synopsis:
        synopsis = (meta.get("og_description") or meta.get("description") or [""])[0]

    _, payload = ctx["classifier"].classify(url)
    slug = payload.get("slug") or url.rstrip("/").rsplit("/", 1)[-1]
    anime = Anime(id=stable_id("anime", slug), slug=slug, url=url,
                  title=title[:500], synopsis=synopsis[:20000])
    _apply_keymap(infobox, anime)
    _apply_jsonld(extract.jsonld_blocks(soup), anime)

    for anchor in soup.select(selectors.get("genre_link", 'a[href*="/genre/"]')):
        genre = clean_text(anchor.get_text())
        if genre and genre.lower() not in ("genre", "genres", "action") or (
                genre.lower() == "action" and "action" not in [g.lower() for g in anime.genres]):
            if genre not in anime.genres:
                anime.genres.append(genre)

    if meta.get("og_image"):
        anime.images = extract.images_on(soup, url, extra=[meta["og_image"][0]])
    else:
        anime.images = extract.images_on(soup, url)
    if meta.get("og_title") and meta["og_title"][0] not in anime.alt_titles:
        anime.alt_titles.append(meta["og_title"][0])

    if not anime.title:
        log.debug("no title extracted on %s", url)
        return None

    stubs, related = [], set()
    for link_url, text, image in extract.links_on(soup, url):
        ptype, link_payload = ctx["classifier"].classify(link_url)
        if ptype == EPISODE:
            stubs.append(EpisodeStub(url=link_url, number=link_payload.get("num"),
                                     title=text, image=image))
        elif ptype == ANIME and link_payload.get("slug") and link_payload["slug"] != slug:
            related.add(link_payload["slug"])
    anime.raw.update({"meta": meta, "infobox": infobox, "url": url,
                      "related": sorted(related)})
    anime.content_hash = model_hash({"typed": {"title": anime.title, "alt_titles": anime.alt_titles, "synopsis": anime.synopsis, "genres": anime.genres, "type": anime.type, "status": anime.status, "season": anime.season, "studio": anime.studio, "year": anime.year, "score": anime.score, "duration": anime.duration, "episode_count": anime.episode_count, "images": anime.images}, "raw": anime.raw})
    stamp = now_iso()
    anime.first_seen, anime.last_seen, anime.last_changed = stamp, stamp, stamp
    return anime, stubs


def parse_episode_page(url: str, soup, ctx: dict):
    """Returns an Episode or None. NOTE: video player / stream / subtitle /
    download extraction is intentionally out of scope — metadata only."""
    selectors = ctx["selectors"].get("episode", {})
    meta = extract.meta_tags(soup)
    infobox = extract.infobox_pairs(soup, selectors.get("infobox", []))
    _, payload = ctx["classifier"].classify(url)

    anime_slug = payload.get("anime") or slugify(infobox.get("series", [""])[0])
    if not anime_slug:
        return None
    number = payload.get("num")
    if number is None:
        number = as_int((infobox.get("episode") or [""])[0])

    title = ""
    for selector in selectors.get("title", []):
        el = soup.select_one(selector)
        if el:
            title = clean_text(el.get_text())
            break
    if not title and meta.get("og_title"):
        title = meta["og_title"][0]

    air_date = ""
    for selector in selectors.get("date", []):
        el = soup.select_one(selector)
        if el:
            air_date = as_date(el.get("datetime") or el.get_text()) or air_date
            break
    if not air_date:
        for key in ("release_date", "date", "aired", "tanggal_rilis", "rilis"):
            if infobox.get(key):
                air_date = as_date(infobox[key][0]) or air_date
                break
    if not air_date:
        for block in extract.jsonld_blocks(soup):
            if isinstance(block, dict) and block.get("datePublished"):
                air_date = as_date(block["datePublished"]) or ""
                break

    anime_id = stable_id("anime", anime_slug)
    key = str(number if number is not None else url)
    stamp = now_iso()
    episode = Episode(
        id=stable_id("episode", anime_id, key), anime_id=anime_id,
        number=float(number) if number is not None else None,
        title=title[:500], url=url, air_date=air_date,
        images=extract.images_on(soup, url, extra=(meta.get("og_image") or [])[:1]),
        raw={"meta": meta, "infobox": infobox, "url": url, "anime_slug": anime_slug},
        first_seen=stamp, last_seen=stamp, last_changed=stamp,
    )
    episode.content_hash = model_hash({"typed": {"anime_id": episode.anime_id, "number": episode.number, "title": episode.title, "url": episode.url, "air_date": episode.air_date, "images": episode.images}, "raw": episode.raw})
    if number is None and not title:
        return None
    return episode


def parse_genre_links(url: str, soup, ctx: dict) -> list:
    """(slug, name) for every genre anchor — used to seed the genre directory."""
    from .classify import GENRE
    out, seen = [], set()
    for link_url, text, _ in extract.links_on(soup, url):
        ptype, payload = ctx["classifier"].classify(link_url)
        if ptype == GENRE:
            slug = payload.get("slug", "")
            if slug and slug not in seen:
                seen.add(slug)
                out.append((slug, text or slug))
    return out
