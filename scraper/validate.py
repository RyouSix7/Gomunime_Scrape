"""Pre-write validation and sanitization. Reject bad records, cap runaway data."""
from urllib.parse import urlsplit

MAX_TITLE = 500
MAX_SYNOPSIS = 20000


def validate_anime(anime):
    if not anime.title or not anime.title.strip():
        return False, "missing title"
    if not anime.url or urlsplit(anime.url).scheme not in ("http", "https"):
        return False, "missing or invalid url"
    if not anime.slug:
        return False, "missing slug"
    if len(anime.title) > MAX_TITLE:
        anime.title = anime.title[:MAX_TITLE]
    if len(anime.synopsis) > MAX_SYNOPSIS:
        anime.synopsis = anime.synopsis[:MAX_SYNOPSIS]
    anime.genres = [g[:100] for g in anime.genres if g][:32]
    anime.alt_titles = [t[:300] for t in anime.alt_titles if t][:16]
    anime.images = anime.images[:64]
    _cap_raw(anime.raw)
    return True, ""


def validate_episode(episode):
    if not episode.url or urlsplit(episode.url).scheme not in ("http", "https"):
        return False, "missing or invalid url"
    if not episode.anime_id:
        return False, "missing anime link"
    if episode.number is None and not episode.title.strip():
        return False, "no number and no title"
    if episode.number is not None and episode.number < 0:
        return False, "invalid episode number"
    if len(episode.title) > MAX_TITLE:
        episode.title = episode.title[:MAX_TITLE]
    episode.images = episode.images[:16]
    _cap_raw(episode.raw)
    return True, ""


def _cap_raw(raw, max_str=10000, max_list=200, max_dict=200):
    if not isinstance(raw, dict):
        return
    for key, value in list(raw.items()):
        if value in (None, "", [], {}):
            raw.pop(key)
        elif isinstance(value, str) and len(value) > max_str:
            raw[key] = value[:max_str]
        elif isinstance(value, list) and len(value) > max_list:
            raw[key] = value[:max_list]
        elif isinstance(value, dict) and len(value) > max_dict:
            raw[key] = dict(list(value.items())[:max_dict])
