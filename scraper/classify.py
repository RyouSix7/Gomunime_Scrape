"""Page-type classification from URL shape. Route regexes live in site.yaml."""
import re
from urllib.parse import urlsplit

ANIME = "anime"
EPISODE = "episode"
GENRE = "genre"
LISTING = "listing"
OTHER = "other"

_BLOCKED_EXT = (
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".css", ".js", ".ico",
    ".xml", ".zip", ".rar", ".7z", ".mp4", ".mkv", ".mp3", ".pdf",
    ".woff", ".woff2", ".ttf", ".eot",
)


class PageClassifier:
    def __init__(self, routes: dict):
        self.anime_re = re.compile(routes.get("anime") or r"^/anime/(?P<slug>[^/]+)/?$")
        episodes = routes.get("episode") or []
        episodes = episodes if isinstance(episodes, list) else [episodes]
        self.episode_res = [re.compile(p) for p in episodes if p]
        self.genre_re = re.compile(routes.get("genre") or r"^/genre/(?P<slug>[^/]+)/?$")
        self.search_re = re.compile(routes.get("search") or r"/\?s=")
        self.listings = {(l.rstrip("/") or "/") for l in (routes.get("listings") or ["/"])}

    def classify(self, url: str):
        """Returns (page_type, payload_dict)."""
        parts = urlsplit(url)
        path = parts.path or "/"
        target = path + ("?" + parts.query if parts.query else "")
        if self.search_re.search(target):
            return LISTING, {}
        if (path.rstrip("/") or "/") in self.listings:
            return LISTING, {}
        for rx in self.episode_res:
            match = rx.fullmatch(path)
            if match:
                return EPISODE, {"anime": match.group("anime"), "num": int(match.group("num"))}
        match = self.anime_re.fullmatch(path)
        if match:
            return ANIME, {"slug": match.group("slug")}
        match = self.genre_re.fullmatch(path)
        if match:
            return GENRE, {"slug": match.group("slug")}
        if re.search(r"/page/\d+/?$", path):
            return LISTING, {}
        return OTHER, {}

    def priority(self, page_type: str) -> int:
        return {LISTING: 0, ANIME: 1, EPISODE: 2, GENRE: 3, OTHER: 99}.get(page_type, 99)

    def is_excluded(self, url: str, extra_excludes) -> bool:
        parts = urlsplit(url)
        path = parts.path.lower()
        if path.endswith(_BLOCKED_EXT):
            return True
        blob = path + "?" + (parts.query or "").lower()
        return any(x in blob for x in extra_excludes)
