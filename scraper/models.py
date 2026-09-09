"""Data models. Typed fields are queryable; `raw` preserves every other
discovered attribute so unknown fields are never lost."""
from dataclasses import dataclass, field


@dataclass
class Anime:
    id: str
    slug: str
    url: str
    title: str
    alt_titles: list = field(default_factory=list)
    synopsis: str = ""
    genres: list = field(default_factory=list)
    type: str = ""
    status: str = ""
    season: str = ""
    studio: str = ""
    year: int = None
    score: float = None
    duration: str = ""
    episode_count: int = None
    images: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    availability: str = "ok"          # ok | stub | removed
    content_hash: str = ""
    first_seen: str = ""
    last_seen: str = ""
    last_changed: str = ""


@dataclass
class Episode:
    id: str
    anime_id: str
    number: float = None
    title: str = ""
    url: str = ""
    air_date: str = ""
    images: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    availability: str = "ok"
    content_hash: str = ""
    first_seen: str = ""
    last_seen: str = ""
    last_changed: str = ""


@dataclass
class EpisodeStub:
    url: str
    number: int = None
    title: str = ""
    image: str = ""
