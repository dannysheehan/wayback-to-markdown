# Wayback To Markdown

Download archived HTML pages for a domain from the Wayback Machine and convert them into a Markdown static-site layout.

The tool is designed for long-running archive jobs where the Wayback Machine may throttle, return transient errors, or have individual replay URLs that fail even though CDX lists them as successful captures.

## Features

- Queries the Wayback CDX API for unique archived HTML URLs.
- Downloads archived replay pages one at a time with configurable pacing.
- Converts page bodies into Markdown with YAML front matter.
- Saves original raw HTML alongside generated Markdown.
- Writes a manifest after every URL so interrupted runs can resume.
- Retries HTTP 429, transient 5xx responses, timeouts, and network errors.
- Tries alternate captures when a replay URL returns 404 or another transient replay failure.
- Can rebuild Markdown from saved raw HTML without re-downloading pages.

## Requirements

Python 3.10+ and the dependencies declared in `pyproject.toml` (BeautifulSoup 4.12+ is required).

Install with:

```bash
uv sync
```

## Development

Install development dependencies:

```bash
uv sync --extra dev --locked
```

Run the local checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -v
```

The GitHub Actions workflow runs the same lint, format, and unit test checks on Python 3.10, 3.11, and 3.12.

## Usage

```bash
./wayback_to_markdown.py www.example.com
```

Output is written to:

```text
sites/<domain>/
├── content/       # Markdown pages with YAML front matter
├── raw-html/      # Original archived HTML responses
└── metadata/      # CDX response and resumable download manifest
```

Example:

```bash
./wayback_to_markdown.py www.ftmon.org --output sites
```

## Recommended Settings

For most domains, start with the defaults:

```bash
./wayback_to_markdown.py www.example.com
```

The defaults are intentionally conservative:

```text
--delay 2
--retries 5
--timeout 60
--alternate-capture-limit 10
--resume enabled
```

Recommended settings by situation:

```bash
# Small test run before committing to a large site.
./wayback_to_markdown.py www.example.com --limit 10 --output /tmp/wayback-test

# Normal full-domain run with explicit safe defaults.
./wayback_to_markdown.py www.example.com --delay 2 --retries 5 --timeout 60

# Large or throttle-prone domains.
./wayback_to_markdown.py www.example.com --delay 3 --retries 6 --timeout 90

# Resume a previous run without re-querying CDX.
./wayback_to_markdown.py www.example.com --use-existing-cdx

# Rebuild Markdown after improving extraction rules, using saved raw HTML.
./wayback_to_markdown.py www.example.com --use-existing-cdx --reconvert-existing

# Include subdomains such as blog.example.com and www.example.com.
./wayback_to_markdown.py example.com --include-subdomains
```

Do not set `--delay 0` for full runs. The Wayback Machine is a shared public service, and fast request loops tend to produce 429s or connection failures.

## How Resume Works

The script writes `metadata/manifest.json` after every URL. Successful URLs are skipped on the next run. Failed URLs are retried because network loss, throttling, and replay errors can be temporary.

To resume safely:

```bash
./wayback_to_markdown.py www.example.com --output sites --use-existing-cdx
```

## Output Front Matter

Each Markdown page includes:

```yaml
---
title: "Page Title"
original_url: "http://www.example.com/page/"
archive_url: "https://web.archive.org/web/<timestamp>id_/http://www.example.com/page/"
wayback_timestamp: "<timestamp>"
mimetype: "text/html"
---
```

## Notes

- The generated Markdown is best-effort. Old WordPress themes, plugins, comments, sidebars, and archive pages vary widely.
- The raw HTML is kept so extraction rules can be improved later without re-downloading.
- Query-string variants are preserved as separate pages and given stable hash suffixes when they would otherwise collide.
