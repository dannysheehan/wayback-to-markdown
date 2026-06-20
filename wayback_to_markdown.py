#!/usr/bin/env python3
"""Download Wayback Machine HTML pages for a domain and convert them to Markdown."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    from bs4 import BeautifulSoup, Comment, NavigableString, Tag
except ImportError:  # pragma: no cover - fallback is exercised only without bs4
    BeautifulSoup = None
    Comment = str
    NavigableString = str
    Tag = object


USER_AGENT = "wayback-to-markdown/1.0 (+local archival use)"


class RateLimitedClient:
    """Small urllib wrapper that keeps all Wayback requests polite and resumable.

    The Internet Archive will return 429s or transient 5xx responses if a run is
    too aggressive. Keeping the rate limiting in one client makes CDX lookups,
    replay downloads, and alternate-capture lookups follow the same rules.
    """

    def __init__(self, delay: float, retries: int, timeout: float) -> None:
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.last_request_at = 0.0

    def fetch(self, url: str) -> tuple[bytes, dict[str, str]]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._sleep_before_request()
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    body = response.read()
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    return body, headers
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.retries:
                    self._backoff(attempt, exc.headers.get("Retry-After"))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt < self.retries:
                    self._backoff(attempt, None)
                    continue
                raise
        raise RuntimeError(f"request failed: {last_error}")

    def _sleep_before_request(self) -> None:
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_request_at = time.monotonic()

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        wait = None
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = None
        if wait is None:
            wait = max(self.delay, 2.0) * (2**attempt)
        print(f"rate/backoff: sleeping {wait:.1f}s", file=sys.stderr)
        time.sleep(wait)


def normalize_domain(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = "http://" + value
    parsed = urllib.parse.urlparse(value)
    if not parsed.netloc:
        raise ValueError(f"invalid domain: {value}")
    return parsed.netloc.lower().rstrip("/")


def cdx_url(domain: str, include_subdomains: bool, limit: int | None) -> str:
    host = domain
    query_host = f"*.{host}" if include_subdomains and not host.startswith("*.") else host
    # collapse=urlkey asks CDX for one representative capture per URL. Without
    # it, large WordPress sites can return thousands of snapshots for the same
    # page before we ever get to distinct articles.
    params = {
        "url": f"{query_host}/*",
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest",
        "filter": ["statuscode:200", "mimetype:text/html"],
        "collapse": "urlkey",
    }
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if isinstance(value, list):
            pairs.extend((key, item) for item in value)
        else:
            pairs.append((key, value))
    if limit:
        pairs.append(("limit", str(limit)))
    return "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(pairs)


def cdx_captures_url(original_url: str, limit: int) -> str:
    # A CDX record can claim statuscode:200 while the replay endpoint later
    # returns 404 or 5xx. When that happens, query captures for the exact URL and
    # try recent digest-distinct alternatives before declaring the page failed.
    pairs = [
        ("url", original_url),
        ("output", "json"),
        ("fl", "timestamp,original,statuscode,mimetype,digest"),
        ("filter", "statuscode:200"),
        ("filter", "mimetype:text/html"),
        ("collapse", "digest"),
        ("sort", "reverse"),
        ("limit", str(limit)),
    ]
    return "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(pairs)


def record_host(record: dict[str, str]) -> str:
    return urllib.parse.urlparse(record["original"]).netloc.lower().split("@")[-1].split(":")[0]


def load_cdx(
    client: RateLimitedClient,
    domain: str,
    include_subdomains: bool,
    limit: int | None,
) -> list[dict[str, str]]:
    body, _headers = client.fetch(cdx_url(domain, include_subdomains, limit))
    data = json.loads(body.decode("utf-8"))
    if not data:
        return []
    header = data[0]
    records = [dict(zip(header, row, strict=False)) for row in data[1:]]
    # CDX matching can be broader than expected. Exact-host mode filters records
    # back to the requested host so www.example.com does not silently pull in
    # www2.example.com or other neighboring hosts.
    if include_subdomains:
        bare = domain.removeprefix("www.")
        return [
            record
            for record in records
            if record_host(record) == bare or record_host(record).endswith("." + bare)
        ]
    return [record for record in records if record_host(record) == domain]


def load_alternate_captures(
    client: RateLimitedClient,
    original_url: str,
    limit: int,
) -> list[dict[str, str]]:
    body, _headers = client.fetch(cdx_captures_url(original_url, limit))
    data = json.loads(body.decode("utf-8"))
    if not data:
        return []
    header = data[0]
    return [dict(zip(header, row, strict=False)) for row in data[1:]]


def safe_slug(value: str, fallback: str) -> str:
    value = html.unescape(value).strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:90] or fallback


def output_relative_path(original: str, title: str, used: set[Path]) -> Path:
    parsed = urllib.parse.urlparse(original)
    path = urllib.parse.unquote(parsed.path or "/").strip("/")
    if not path:
        path = "index"
    if path.endswith("/"):
        path = path.rstrip("/")
    parts = [safe_slug(part, "page") for part in path.split("/") if part]
    if not parts:
        parts = ["index"]
    if parts[-1] in {"index", "index-html", "index-php"} and title:
        parts[-1] = safe_slug(title, parts[-1])
    rel = Path(*parts).with_suffix(".md")
    # Query-string variants and duplicate titles often map to the same filename.
    # Add a stable hash suffix rather than overwriting an earlier capture.
    if rel in used:
        suffix = hashlib.sha1(original.encode("utf-8")).hexdigest()[:8]
        rel = rel.with_name(f"{rel.stem}-{suffix}.md")
    used.add(rel)
    return rel


def raw_html_path(original: str, used: set[Path]) -> Path:
    parsed = urllib.parse.urlparse(original)
    path = urllib.parse.unquote(parsed.path or "/").strip("/")
    if not path or path.endswith("/"):
        path = (path.rstrip("/") + "/index.html").lstrip("/")
    rel = Path(*[safe_slug(part, "page") for part in path.split("/") if part])
    if rel.suffix.lower() not in {".html", ".htm", ".php"}:
        rel = rel.with_suffix(".html")
    if rel in used:
        suffix = hashlib.sha1(original.encode("utf-8")).hexdigest()[:8]
        rel = rel.with_name(f"{rel.stem}-{suffix}{rel.suffix}")
    used.add(rel)
    return rel


def soup_title(soup: Any) -> str:
    title_tag = soup.select_one("div.post h2.storytitle a")
    if not title_tag:
        title_tag = soup.select_one("div.post h2.storytitle")
    if not title_tag:
        title_tag = soup.select_one(
            ".post h1.title, article h1.title, h1.entry-title, .entry-title",
        )
    if not title_tag:
        title_tag = soup.find("title")
    return clean(title_tag.get_text(" ", strip=True)) if title_tag else ""


def soup_main_content(soup: Any) -> Any:
    # Remove common WordPress/plugin chrome before extracting text. These nodes
    # are useful on the original site, but become noisy in a Markdown archive.
    for selector in [
        "script",
        "style",
        "nav",
        "form",
        "aside",
        ".shareaholic-like-buttonset",
        ".shr-publisher-8",
        ".su-linkbox",
        "#post-author",
        "#connect",
        "#comments",
        ".comments",
        ".related-posts",
        ".woo-sc-related-posts",
        ".post-more",
        ".post-meta",
        ".breadcrumb",
        ".breadcrumbs",
    ]:
        for node in soup.select(selector):
            node.decompose()
    return (
        # Older WordPress themes commonly use storycontent; newer ones tend to
        # use entry/entry-content. Prefer article body containers before falling
        # back to broader main/body content.
        soup.select_one("div.post div.storycontent")
        or soup.select_one(".post .entry")
        or soup.select_one("article .entry")
        or soup.select_one(".entry-content")
        or soup.select_one(".post-content")
        or soup.select_one("#main")
        or soup.select_one("article")
        or soup.select_one("main")
        or soup.select_one("body")
        or soup
    )


def clean(text: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", text.replace("\xa0", " ")).strip()


def inline_md(node: Any) -> str:
    if BeautifulSoup is not None and isinstance(node, Comment):
        return ""
    if BeautifulSoup is not None and isinstance(node, NavigableString):
        return str(node).replace("\xa0", " ")
    if BeautifulSoup is None or not isinstance(node, Tag):
        return str(node).replace("\xa0", " ") if isinstance(node, str) else ""
    name = node.name.lower()
    text = "".join(inline_md(c) for c in node.children)
    text = re.sub(r"\s+", " ", text)
    if name == "a":
        label = clean(text)
        href = node.get("href", "").strip()
        return f"[{label}]({href})" if href and label else label
    if name in {"strong", "b"}:
        return f"**{clean(text)}**"
    if name in {"em", "i"}:
        return f"*{clean(text)}*"
    if name == "code":
        return f"`{clean(text)}`"
    if name == "br":
        return "\n"
    return text


def block_md(node: Any, depth: int = 0) -> str:
    if BeautifulSoup is not None and isinstance(node, Comment):
        return ""
    if BeautifulSoup is not None and isinstance(node, NavigableString):
        return clean(str(node))
    if BeautifulSoup is None or not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name in {"script", "style", "nav", "form", "aside"}:
        return ""
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(name[1])
        return f"{'#' * level} {clean(inline_md(node))}\n"
    if name == "p":
        text = clean(inline_md(node))
        return f"{text}\n" if text else ""
    if name in {"ul", "ol"}:
        lines = []
        for idx, li in enumerate(node.find_all("li", recursive=False), 1):
            body = clean(" ".join(block_md(c, depth + 1).strip() for c in li.children))
            marker = f"{idx}." if name == "ol" else "-"
            if body:
                lines.append(f"{'  ' * depth}{marker} {body}")
        return "\n".join(lines) + ("\n" if lines else "")
    if name == "blockquote":
        block_text = "".join(block_md(c, depth) for c in node.children)
        body = "\n".join(line for line in block_text.splitlines() if line.strip())
        return "\n".join("> " + line for line in body.splitlines()) + "\n" if body else ""
    if name == "pre":
        return "```\n" + node.get_text().strip("\n") + "\n```\n"
    if name == "hr":
        return "---\n"
    parts = [block_md(c, depth).rstrip() for c in node.children]
    return "\n".join(p for p in parts if p.strip()) + ("\n" if parts else "")


class BasicHTMLText(HTMLParser):
    """Dependency-free fallback used when BeautifulSoup is not installed."""

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.in_title = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag in {"p", "br", "h1", "h2", "h3", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in {"p", "h1", "h2", "h3", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        text = clean(data)
        if not text:
            return
        if self.in_title:
            self.title += " " + text
        self.parts.append(text + " ")


def html_to_markdown(html_text: str) -> tuple[str, str]:
    if BeautifulSoup is None:
        parser = BasicHTMLText()
        parser.feed(html_text)
        fallback_body = "\n".join(line.strip() for line in "".join(parser.parts).splitlines())
        body = re.sub(r"\n{3,}", "\n\n", fallback_body).strip()
        return clean(parser.title), body
    soup = BeautifulSoup(html_text, "html.parser")
    title = soup_title(soup)
    content = soup_main_content(soup)
    body = block_md(content).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    return title, body


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_markdown(
    path: Path,
    title: str,
    body: str,
    record: dict[str, str],
    archive_url: str,
) -> None:
    if not title:
        title = (
            Path(urllib.parse.urlparse(record["original"]).path).stem.replace("-", " ").title()
            or "Archived Page"
        )
    frontmatter = [
        "---",
        f"title: {yaml_string(title)}",
        f"original_url: {yaml_string(record['original'])}",
        f"archive_url: {yaml_string(archive_url)}",
        f"wayback_timestamp: {yaml_string(record['timestamp'])}",
        f"mimetype: {yaml_string(record.get('mimetype', ''))}",
        "---",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(frontmatter) + body.strip() + "\n", encoding="utf-8")


def archive_url_for(record: dict[str, str]) -> str:
    return f"https://web.archive.org/web/{record['timestamp']}id_/{record['original']}"


def fetch_archived_page(
    client: RateLimitedClient,
    record: dict[str, str],
    alternate_capture_limit: int,
) -> tuple[bytes, dict[str, str], dict[str, str], str, list[dict[str, str]]]:
    tried: list[dict[str, str]] = []
    candidates = [record]
    loaded_alternates = False
    last_error: Exception | None = None

    while candidates:
        candidate = candidates.pop(0)
        archive_url = archive_url_for(candidate)
        tried.append({"timestamp": candidate["timestamp"], "archive_url": archive_url})
        try:
            body, headers = client.fetch(archive_url)
            return body, headers, candidate, archive_url, tried
        except urllib.error.HTTPError as exc:
            last_error = exc
            # Only load alternate captures once. That keeps bad URLs bounded:
            # primary replay -> one CDX alternate query -> up to N alternates.
            if (
                exc.code in {404, 500, 502, 503, 504}
                and alternate_capture_limit > 0
                and not loaded_alternates
            ):
                loaded_alternates = True
                alternates = load_alternate_captures(
                    client,
                    record["original"],
                    alternate_capture_limit,
                )
                seen = {record["timestamp"]}
                candidates.extend(
                    candidate for candidate in alternates if candidate["timestamp"] not in seen
                )
                seen.update(candidate["timestamp"] for candidate in candidates)
                continue
            raise
        except Exception as exc:
            last_error = exc
            if alternate_capture_limit > 0 and not loaded_alternates:
                loaded_alternates = True
                alternates = load_alternate_captures(
                    client,
                    record["original"],
                    alternate_capture_limit,
                )
                seen = {record["timestamp"]}
                candidates.extend(
                    candidate for candidate in alternates if candidate["timestamp"] not in seen
                )
                seen.update(candidate["timestamp"] for candidate in candidates)
                continue
            raise
    raise RuntimeError(f"all captures failed for {record['original']}: {last_error}")


def write_index(content_dir: Path, pages: list[dict[str, str]]) -> None:
    lines = [
        "---",
        'title: "Archived Pages"',
        "---",
        "",
        "# Archived Pages",
        "",
    ]
    for page in sorted(pages, key=lambda item: item["markdown_file"]):
        title = page.get("title") or page["original"]
        rel = Path(page["markdown_file"]).relative_to(content_dir)
        lines.append(f"- [{title}]({rel.as_posix()})")
    (content_dir / "_index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def reconvert_existing(content_dir: Path, page: dict[str, str]) -> None:
    # Useful after improving extraction rules: rebuild Markdown from saved raw
    # HTML without spending more Wayback requests.
    raw_file = page.get("raw_html_file")
    markdown_file = page.get("markdown_file")
    if not raw_file or not markdown_file:
        return
    raw_path = Path(raw_file)
    md_path = Path(markdown_file)
    if not raw_path.exists():
        return
    title, markdown_body = html_to_markdown(raw_path.read_text(encoding="utf-8", errors="replace"))
    archive_url = page.get("archive_url", "")
    write_markdown(md_path, title, markdown_body, page, archive_url)
    page["title"] = title


def run(args: argparse.Namespace) -> int:
    domain = normalize_domain(args.domain)
    out = Path(args.output).resolve() / domain
    raw_dir = out / "raw-html"
    content_dir = out / "content"
    meta_dir = out / "metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    content_dir.mkdir(parents=True, exist_ok=True)

    client = RateLimitedClient(args.delay, args.retries, args.timeout)
    cdx_path = meta_dir / "cdx.json"
    if args.use_existing_cdx and cdx_path.exists():
        records = json.loads(cdx_path.read_text(encoding="utf-8"))
    else:
        records = load_cdx(client, domain, args.include_subdomains, args.limit)
        cdx_path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    print(f"found {len(records)} archived HTML pages for {domain}", flush=True)

    existing: list[dict[str, str]] = []
    manifest_path = meta_dir / "manifest.json"
    if args.resume and manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    done = {item["original"] for item in existing if item.get("status") == "ok"}
    pages = [item for item in existing if item.get("status") == "ok"]
    # Track paths already emitted by resumed runs so new duplicate URLs still get
    # deterministic hash suffixes rather than clobbering old files.
    used_md = {
        Path(item["markdown_file"]).relative_to(content_dir)
        for item in pages
        if item.get("markdown_file")
    }
    used_raw = {
        Path(item["raw_html_file"]).relative_to(raw_dir)
        for item in pages
        if item.get("raw_html_file")
    }

    for idx, record in enumerate(records, 1):
        original = record["original"]
        if original in done:
            if args.reconvert_existing:
                for page in pages:
                    if page.get("original") == original and page.get("status") == "ok":
                        reconvert_existing(content_dir, page)
                        break
            print(f"{idx}/{len(records)} skip {original}", flush=True)
            continue
        archive_url = archive_url_for(record)
        item = {**record, "archive_url": archive_url, "status": "ok", "error": ""}
        try:
            pages = [page for page in pages if page.get("original") != original]
            body, headers, successful_record, archive_url, tried = fetch_archived_page(
                client,
                record,
                args.alternate_capture_limit,
            )
            text = body.decode("utf-8", errors="replace")
            title, markdown_body = html_to_markdown(text)
            raw_rel = raw_html_path(original, used_raw)
            md_rel = output_relative_path(original, title, used_md)
            raw_path = raw_dir / raw_rel
            md_path = content_dir / md_rel
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(body)
            write_markdown(md_path, title, markdown_body, successful_record, archive_url)
            if successful_record["timestamp"] != record["timestamp"]:
                print(
                    f"{idx}/{len(records)} alternate {original}: "
                    f"{record['timestamp']} -> {successful_record['timestamp']}",
                    flush=True,
                )
            item.update(
                {
                    **successful_record,
                    "title": title,
                    "archive_url": archive_url,
                    "raw_html_file": str(raw_path),
                    "markdown_file": str(md_path),
                    "content_type": headers.get("content-type", ""),
                    "captures_tried": tried,
                }
            )
            pages.append(item)
            print(f"{idx}/{len(records)} ok {original} -> {md_path.relative_to(out)}", flush=True)
        except Exception as exc:  # noqa: BLE001 - persist failure and continue.
            pages = [page for page in pages if page.get("original") != original]
            item.update({"status": "error", "error": repr(exc)})
            pages.append(item)
            print(f"{idx}/{len(records)} error {original}: {exc}", file=sys.stderr, flush=True)
        # Write after every URL so Ctrl-C, laptop sleep, or network outages can
        # resume without losing a long run.
        manifest_path.write_text(json.dumps(pages, indent=2) + "\n", encoding="utf-8")

    ok_pages = [item for item in pages if item.get("status") == "ok"]
    write_index(content_dir, ok_pages)
    print(f"wrote {len(ok_pages)} markdown pages to {content_dir}", flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain", help="Domain or host to archive, for example www.ftmon.org")
    parser.add_argument("--output", default="sites", help="Output root. Default: sites")
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Minimum seconds between Wayback requests. Default: 2",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="Retries for 429/5xx/timeouts. Default: 5",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Per-request timeout in seconds. Default: 60",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit CDX results for testing")
    parser.add_argument(
        "--include-subdomains",
        action="store_true",
        help="Use *.domain in the CDX query",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Skip already downloaded pages from manifest. Default: on",
    )
    parser.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Ignore existing manifest",
    )
    parser.add_argument(
        "--use-existing-cdx",
        action="store_true",
        help="Reuse metadata/cdx.json instead of querying CDX",
    )
    parser.add_argument(
        "--reconvert-existing",
        action="store_true",
        help="Rebuild Markdown from existing raw HTML for skipped pages",
    )
    parser.add_argument(
        "--alternate-capture-limit",
        type=int,
        default=10,
        help=(
            "When a replay fails, try up to this many alternate captures for "
            "the same URL. Default: 10"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
