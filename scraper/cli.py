"""Command line entry point: scrape | probe | export | stats | serve."""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

import yaml

from .settings import Settings


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        })


def setup_logging(settings: Settings, run_name: str):
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%SZ"
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    if root.handlers:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(console)
    plain = logging.FileHandler(settings.log_dir / f"{run_name}.log", encoding="utf-8")
    plain.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(plain)
    structured = logging.FileHandler(settings.log_dir / f"{run_name}.jsonl", encoding="utf-8")
    structured.setFormatter(JsonFormatter())
    root.addHandler(structured)


def load_site_cfg(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def build_settings(args) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "db", None):
        settings.db_path = Path(args.db)
    if getattr(args, "config", None):
        settings.config_path = Path(args.config)
    if getattr(args, "log_dir", None):
        settings.log_dir = Path(args.log_dir)
    if getattr(args, "verbose", False):
        settings.log_level = "DEBUG"
    elif getattr(args, "quiet", False):
        settings.log_level = "WARNING"
    return settings


def cmd_scrape(args):
    settings = build_settings(args)
    setup_logging(settings, f"scrape-{time.strftime('%Y%m%d-%H%M%S')}")
    site = load_site_cfg(settings.config_path)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    from .crawler import Crawler
    from .store import Store
    store = Store(settings.db_path)
    try:
        crawler = Crawler(settings, site, store)
        sections = [s.strip() for s in args.sections.split(",")] if args.sections else None
        stats = crawler.run(max_pages=args.max_pages, full=args.full, sections=sections)
    finally:
        store.close()
    print(json.dumps(stats, indent=2, default=str))
    return 0 if stats.get("run_status") == "ok" else 1


def cmd_probe(args):
    settings = build_settings(args)
    setup_logging(settings, f"probe-{time.strftime('%Y%m%d-%H%M%S')}")
    site = load_site_cfg(settings.config_path)
    from .classify import PageClassifier
    from .extract import infobox_pairs, jsonld_blocks, links_on, meta_tags, soup_from
    from .http import PoliteFetcher
    fetcher = PoliteFetcher(settings.base_url, settings.user_agent, settings.delay,
                            settings.timeout, settings.retries, 1)
    result = fetcher.get(args.url)
    print(json.dumps({"status": result.status, "final_url": result.final_url,
                      "error": result.error}, indent=2))
    if not result.ok:
        return 1
    soup = soup_from(result.html)
    classifier = PageClassifier(site.get("routes", {}))
    selectors = site.get("selectors", {}).get("anime", {})
    payload = {
        "classification": classifier.classify(args.url),
        "meta": meta_tags(soup),
        "jsonld": jsonld_blocks(soup),
        "infobox": infobox_pairs(soup, selectors.get("infobox", [])),
        "links": [{"url": url, "text": text[:80],
                   "type": classifier.classify(url)[0]}
                  for url, text, _ in links_on(soup, args.url)[:80]],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_export(args):
    settings = build_settings(args)
    setup_logging(settings, f"export-{time.strftime('%Y%m%d-%H%M%S')}")
    from .export import run
    count = run(settings.db_path, args.out)
    print(f"exported {count} anime to {args.out}")
    return 0


def cmd_stats(args):
    settings = build_settings(args)
    from . import queries
    try:
        conn = queries.connect(settings.db_path)
    except FileNotFoundError as exc:
        print(str(exc))
        return 1
    print(json.dumps(queries.stats(conn), indent=2))
    conn.close()
    return 0


def cmd_serve(args):
    import uvicorn
    uvicorn.run("api.app:app", host=args.host, port=args.port, log_level="info")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="scraper", description="gomunime.top metadata scraper and API pipeline")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--verbose", action="store_true", help="DEBUG logging")
    common.add_argument("--quiet", action="store_true", help="WARNING logging only")
    common.add_argument("--db", help="override DB path")
    common.add_argument("--config", help="override site.yaml path")
    common.add_argument("--log-dir", help="override log directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scrape = sub.add_parser("scrape", parents=[common], help="run the crawl")
    p_scrape.add_argument("--max-pages", type=int, default=0,
                          help="page budget (0 = MAX_PAGES_PER_RUN from env)")
    p_scrape.add_argument("--full", action="store_true",
                          help="ignore TTLs, re-crawl everything")
    p_scrape.add_argument("--sections", help="comma-separated listing paths to seed")
    p_scrape.set_defaults(func=cmd_scrape)

    p_probe = sub.add_parser("probe", parents=[common],
                             help="inspect one page for selector calibration")
    p_probe.add_argument("url")
    p_probe.set_defaults(func=cmd_probe)

    p_export = sub.add_parser("export", parents=[common],
                              help="export static JSON bundle")
    p_export.add_argument("--out", default="data/public")
    p_export.set_defaults(func=cmd_export)

    p_stats = sub.add_parser("stats", parents=[common], help="print dataset stats")
    p_stats.set_defaults(func=cmd_stats)

    p_serve = sub.add_parser("serve", parents=[common], help="serve the REST API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
