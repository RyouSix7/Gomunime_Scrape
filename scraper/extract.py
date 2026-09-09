"""Generic HTML extraction: JSON-LD, meta tags, infobox label/value pairs,
links, images. Field-agnostic by design — this is what keeps the schema open."""
import json
import logging

from bs4 import BeautifulSoup

from .normalize import clean_text, norm_key, normalize_url

log = logging.getLogger("scraper.extract")


def soup_from(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def jsonld_blocks(soup) -> list:
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def meta_tags(soup) -> dict:
    out = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name") or tag.get("itemprop")
        content = tag.get("content")
        if key and content:
            out.setdefault(norm_key(key), []).append(clean_text(content))
    return out


def infobox_pairs(soup, selector_groups) -> dict:
    """Label/value extraction. Handles 'Key: Value' text, <dl> pairs, and
    <table> rows. Unknown keys are preserved — no predefined whitelist."""
    out = {}

    def add(key, value):
        k, v = norm_key(key), clean_text(value)
        if k and v:
            out.setdefault(k, []).append(v)

    for selector in selector_groups or []:
        for el in soup.select(selector):
            text = clean_text(el.get_text(" ", strip=True))
            if ":" in text and len(text) < 400:
                key, _, value = text.partition(":")
                if 0 < len(clean_text(key)) < 60 and not clean_text(key).lower().startswith("http"):
                    add(key, value)
    for dl in soup.find_all("dl"):
        for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
            add(dt.get_text(), dd.get_text())
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) == 2:
            add(cells[0].get_text(), cells[1].get_text())
    return out


def _img_sources(tag) -> list:
    sources = []
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        value = tag.get(attr)
        if value and not value.startswith("data:"):
            sources.append(value)
    srcset = tag.get("srcset") or tag.get("data-srcset")
    if srcset:
        for part in srcset.split(","):
            url = part.strip().split(" ")[0]
            if url and not url.startswith("data:"):
                sources.append(url)
    return sources


def images_on(soup, base: str, extra=()) -> list:
    found = list(extra)
    for tag in soup.find_all("img"):
        found.extend(_img_sources(tag))
    for tag in soup.find_all("link"):
        rels = tag.get("rel") or []
        if "image_src" in rels and tag.get("href"):
            found.append(tag["href"])
    out, seen = [], set()
    for src in found:
        url = normalize_url(base, src) if src else None
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def links_on(soup, base: str) -> list:
    """[(url, text, image)] for every http(s) <a> on the page."""
    out, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        url = normalize_url(base, anchor["href"])
        if not url:
            continue
        text = clean_text(anchor.get_text(" ", strip=True))
        image = ""
        img = anchor.find("img")
        if img:
            for src in _img_sources(img):
                image = normalize_url(base, src) or ""
                if image:
                    break
        if url not in seen:
            seen.add(url)
            out.append((url, text, image))
    return out
