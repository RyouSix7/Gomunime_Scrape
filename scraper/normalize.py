"""URL/text normalization, type coercion, stable IDs, content hashes."""
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "referrer",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def clean_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_url(base: str, href: str):
    """Absolutize and normalize; None for mailto:/javascript:/non-http junk."""
    if not href:
        return None
    href = href.strip()
    if href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    absolute = urljoin(base, href)
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if k not in _TRACKING]
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, urlencode(query), ""))


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


def norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", clean_text(key).lower()).strip("_")


def as_int(value):
    match = re.search(r"-?\d+", clean_text(value).replace(",", ""))
    return int(match.group()) if match else None


def as_float(value):
    match = re.search(r"\d+(?:\.\d+)?", clean_text(value))
    return float(match.group()) if match else None


_DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%d/%m/%Y",
                 "%m/%d/%Y", "%d %b %Y", "%b %d, %Y")


def as_date(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    text = clean_text(value)
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        text = match.group()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


_STATUS_MAP = {
    "ongoing": "ongoing", "currently airing": "ongoing", "airing": "ongoing",
    "sedang berlangsung": "ongoing", "berlangsung": "ongoing",
    "completed": "completed", "finished airing": "completed", "finished": "completed",
    "selesai": "completed", "tamat": "completed", "tammat": "completed",
    "upcoming": "upcoming", "belum rilis": "upcoming", "announce": "upcoming",
}


def norm_status(value):
    text = clean_text(value).lower()
    for needle, mapped in _STATUS_MAP.items():
        if needle in text:
            return mapped
    return text or None



def model_hash(payload: dict) -> str:
    """Stable hash of typed fields plus raw data; detects changes outside raw metadata."""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

def json_hash(obj) -> str:
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_id(*parts) -> str:
    joined = "|".join(clean_text(p) for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def parse_duration(spec: str) -> int:
    match = re.fullmatch(r"(\d+)\s*([smhd])", str(spec).strip().lower())
    if not match:
        raise ValueError(f"bad duration spec: {spec!r}")
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


def slugify(value: str) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text
