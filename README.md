# gomunime-api — metadata scraper → SQLite → REST API

> **Scope:** metadata only. This project intentionally does **not** collect, proxy, re-host, or expose video streams, subtitle files, or download URLs. Use licensed/authorized playback sources for video.

This build keeps the original architecture but hardens the failure and concurrency paths:

- persistent frontier with **atomic leases**
- crashed jobs can have their leases reclaimed
- failed pages are re-queued with backoff instead of disappearing
- crawl state is marked successful **only after parsing succeeds**
- typed fields are included in entity hashes, not just `raw`
- genre relations are reconciled instead of accumulating stale values
- 404 streaks reset correctly after non-404 failures
- static JSON files are written atomically
- old SQLite databases receive a small forward migration for frontier leases
- configured listing routes are actually used
- per-worker HTTP sessions avoid sharing a `requests.Session` between threads
- logging handlers are reset to prevent duplicate log lines
- GitHub Actions commits data only when the scrape exits cleanly

## 1. Project structure

```text
gomunime-api/
├── .github/workflows/scrape.yml
├── api/
│   ├── __init__.py
│   └── app.py
├── config/
│   └── site.yaml
├── data/
├── scraper/
│   ├── __init__.py
│   ├── settings.py
│   ├── normalize.py
│   ├── models.py
│   ├── classify.py
│   ├── http.py
│   ├── extract.py
│   ├── parsers.py
│   ├── validate.py
│   ├── store.py
│   ├── queries.py
│   ├── export.py
│   ├── crawler.py
│   └── cli.py
├── tests/test_safety.py
├── Dockerfile
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## 2. Run locally on Android / Termux

Install Python and Git first, then:

```bash
git clone <YOUR_REPO_URL>
cd gomunime-api
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` if needed. The defaults are deliberately conservative.

### Calibrate the site

Do **not** assume the route regexes are correct forever. Probe a real public page:

```bash
python -m scraper.cli probe https://gomunime.top
```

If the site's URL structure/HTML differs, edit only `config/site.yaml` first.

### Run tests

```bash
python -m unittest discover -s tests -v
```

### First crawl

```bash
python -m scraper.cli scrape --verbose
```

For a smaller test run:

```bash
python -m scraper.cli scrape --max-pages 50 --verbose
```

### Inspect stats

```bash
python -m scraper.cli stats
```

### Export static JSON

```bash
python -m scraper.cli export --out data/public
```

### Start API

```bash
python -m scraper.cli serve --host 0.0.0.0 --port 8000
```

Then open:

```text
http://127.0.0.1:8000/docs
```

## 3. GitHub Actions deployment — recommended zero-cost setup

The included workflow runs every 6 hours and can also be started manually.

### Step A — create the repository

Create an empty GitHub repository and upload this project.

```bash
git init
git add .
git commit -m "initial metadata scraper"
git branch -M main
git remote add origin <YOUR_REPO_URL>
git push -u origin main
```

### Step B — check Actions permissions

In the repository:

**Settings → Actions → General → Workflow permissions**

Enable:

```text
Read and write permissions
```

The workflow already requests:

```yaml
permissions:
  contents: write
```

### Step C — test manually

Open:

**Actions → Scrape → Run workflow**

For the first test, set:

```text
max_pages = 50
full = false
```

Watch the log. A clean run should finish with `run_status: ok`.

### Step D — normal schedule

The workflow uses:

```text
17 */6 * * *
```

That means approximately every 6 hours, deliberately not exactly on the hour.

The workflow has a concurrency group so scheduled runs don't overlap.

## 4. GitHub Pages as a static API

After a successful scrape, the workflow exports:

```text
data/public/anime.json
data/public/genres.json
data/public/ongoing.json
data/public/completed.json
data/public/latest.json
data/public/popular.json
data/public/stats.json
data/public/anime/<id>.json
```

### Enable Pages

In GitHub:

**Settings → Pages**

Choose:

```text
Build and deployment: Deploy from a branch
Branch: main
Folder: / (root)
```

Because the files are under `data/public`, the resulting URL is normally:

```text
https://<username>.github.io/<repository>/data/public/anime.json
```

For a frontend, fetch that JSON directly. GitHub Pages is useful here because it requires no VPS and no database server.

## 5. REST API deployment

GitHub Pages only serves static JSON. If you need dynamic endpoints such as search/filter/pagination, run the FastAPI app on a Python-capable host or locally.

### Docker

Build:

```bash
docker build -t gomunime-api .
```

Run with persistent data:

```bash
docker run --rm -p 8000:8000 \
  -v "$(pwd)/data:/app/data" \
  gomunime-api
```

Open:

```text
http://127.0.0.1:8000/docs
```

The API is read-only and serves the SQLite dataset. It does not serve video/stream content.

## 6. API endpoints

```text
GET /api/anime
GET /api/anime/{id}
GET /api/anime/{id}/episodes
GET /api/episode/{id}
GET /api/ongoing
GET /api/completed
GET /api/latest
GET /api/popular
GET /api/search?q=naruto
GET /api/genres
GET /api/genre/{genre}
GET /api/types
GET /api/stats
```

Example:

```bash
curl "http://127.0.0.1:8000/api/anime?status=ongoing&genre=action&sort=last_changed&order=desc&per_page=5"
```

## 7. How the race-condition fix works

Old flow:

```text
SELECT frontier row
        ↓
DELETE row
        ↓
fetch / parse
```

A crash between DELETE and parse lost the URL permanently.

New flow:

```text
SELECT + claim lease atomically
        ↓
fetch / parse
        ↓
success ─────────────→ DELETE claimed row
        │
        └─ failure ───→ release lease + retry later

process crash
        ↓
lease expires
        ↓
next run reclaims URL
```

The lease is stored in SQLite, so it survives process boundaries. The GitHub Actions concurrency group is still useful, but it is no longer the only protection.

## 8. Incremental crawling

Each URL has crawl state containing:

- HTTP status
- content hash
- ETag
- Last-Modified
- last crawl timestamp
- consecutive failure count
- consecutive 404 count

Successful 304 responses are recorded as successful HTTP state so TTL logic doesn't immediately force another request.

The crawler also keeps page-type TTLs:

```text
listing: 6h
anime:   14d
episode: 3d
genre:   30d
```

A `--full` run bypasses TTL gating.

## 9. Existing database migration

If you already have an older `data/gomunime.db`, **don't delete it** just because the schema changed.

On startup, `Store` checks the frontier table and adds these columns when missing:

```text
not_before
lease_token
lease_until
```

Existing frontier rows are made immediately eligible. Entity data is preserved.

Always make a backup before a major schema change:

```bash
cp data/gomunime.db data/gomunime.db.backup
```

## 10. Troubleshooting

### `database is locked`

Don't run two writers against the same SQLite file. The included GitHub workflow uses an Actions concurrency group, and the crawler itself serializes Store operations through a lock. For a separate deployment, keep one writer or move the database to a server database designed for multi-writer workloads.

### No pages are fetched

Check:

```bash
python -m scraper.cli probe https://gomunime.top
```

Then inspect `config/site.yaml` and `robots.txt`. The fetcher intentionally refuses to crawl when it cannot obtain `robots.txt`; this is a fail-closed safety choice.

### Anime pages are classified as `other`

Update the `routes.anime` regex in `config/site.yaml`.

### Episodes are not discovered

Update `routes.episode` and run `probe` against an actual episode page.

### Metadata is missing

Update selectors under:

```text
selectors.anime
selectors.episode
```

The generic extractors also look at JSON-LD, Open Graph, label/value infoboxes, images, and links.

### Data changed but API still shows old values

Run:

```bash
python -m scraper.cli scrape --full --verbose
python -m scraper.cli export --out data/public
```

If the HTML changed structurally, recalibrate `site.yaml` first.

## 11. Operational recommendations

For a small public metadata API:

```text
GitHub Actions
    ↓
scraper
    ↓
SQLite
    ↓
static JSON → GitHub Pages
```

is the simplest setup.

If you need high request volume, authentication, multiple writers, or many API instances, keep the crawler architecture but replace SQLite with PostgreSQL and put the API behind a proper application host.

Keep request rate conservative and respect the target site's robots policy and applicable terms. This project intentionally stays on public metadata and does not bypass authentication, CAPTCHA, DRM, or other access controls.
