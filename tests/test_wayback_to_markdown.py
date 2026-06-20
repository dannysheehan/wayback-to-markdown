from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory

import wayback_to_markdown as wtm


class FakeClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def fetch(self, url: str) -> tuple[bytes, dict[str, str]]:
        self.urls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response, {"content-type": "text/html"}


def http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", hdrs=None, fp=None)


class WaybackToMarkdownTests(unittest.TestCase):
    def test_cdx_url_uses_representative_html_captures(self) -> None:
        url = wtm.cdx_url("www.example.com", include_subdomains=False, limit=5)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

        self.assertEqual(query["url"], ["www.example.com/*"])
        self.assertEqual(query["output"], ["json"])
        self.assertIn("statuscode:200", query["filter"])
        self.assertIn("mimetype:text/html", query["filter"])
        self.assertEqual(query["collapse"], ["urlkey"])
        self.assertEqual(query["limit"], ["5"])

    def test_load_cdx_filters_exact_host(self) -> None:
        cdx = [
            ["timestamp", "original", "statuscode", "mimetype", "digest"],
            ["1", "http://www.example.com/page/", "200", "text/html", "a"],
            ["2", "http://www2.example.com/page/", "200", "text/html", "b"],
        ]
        client = FakeClient({wtm.cdx_url("www.example.com", False, None): json.dumps(cdx).encode()})

        records = wtm.load_cdx(client, "www.example.com", include_subdomains=False, limit=None)

        self.assertEqual(
            [record["original"] for record in records],
            ["http://www.example.com/page/"],
        )

    def test_load_cdx_can_include_subdomains(self) -> None:
        cdx = [
            ["timestamp", "original", "statuscode", "mimetype", "digest"],
            ["1", "http://example.com/", "200", "text/html", "a"],
            ["2", "http://blog.example.com/page/", "200", "text/html", "b"],
            ["3", "http://notexample.com/page/", "200", "text/html", "c"],
        ]
        client = FakeClient({wtm.cdx_url("example.com", True, None): json.dumps(cdx).encode()})

        records = wtm.load_cdx(client, "example.com", include_subdomains=True, limit=None)

        self.assertEqual(
            [record["original"] for record in records],
            ["http://example.com/", "http://blog.example.com/page/"],
        )

    def test_output_relative_path_uses_title_for_index_and_hash_for_collisions(self) -> None:
        used: set[Path] = set()

        first = wtm.output_relative_path("http://www.example.com/", "Home Page", used)
        second = wtm.output_relative_path("http://www.example.com/?ref=rss", "Home Page", used)

        self.assertEqual(first, Path("home-page.md"))
        self.assertEqual(second.parent, Path("."))
        self.assertTrue(second.name.startswith("home-page-"))
        self.assertEqual(second.suffix, ".md")

    def test_html_to_markdown_prefers_wordpress_entry_content(self) -> None:
        if wtm.BeautifulSoup is None:
            self.skipTest("BeautifulSoup is not installed")
        html = """
        <html>
          <head><title>Fallback Title</title></head>
          <body>
            <nav>Navigation should disappear</nav>
            <div class="post">
              <h1 class="title">Real Title</h1>
              <div class="post-meta">meta should disappear</div>
              <div class="entry">
                <!-- plugin comment should disappear -->
                <p>Hello <strong>world</strong>.</p>
                <ul><li>First step</li><li>Second step</li></ul>
              </div>
            </div>
            <aside>Sidebar should disappear</aside>
          </body>
        </html>
        """

        title, body = wtm.html_to_markdown(html)

        self.assertEqual(title, "Real Title")
        self.assertIn("Hello **world**.", body)
        self.assertIn("- First step", body)
        self.assertNotIn("Navigation", body)
        self.assertNotIn("plugin comment", body)
        self.assertNotIn("meta should disappear", body)

    def test_fetch_archived_page_tries_alternate_capture_after_404(self) -> None:
        primary = {
            "timestamp": "20200101000000",
            "original": "http://www.example.com/page/",
            "statuscode": "200",
            "mimetype": "text/html",
            "digest": "a",
        }
        alternate = {
            "timestamp": "20210101000000",
            "original": "http://www.example.com/page/",
            "statuscode": "200",
            "mimetype": "text/html",
            "digest": "b",
        }
        alternate_cdx = [
            ["timestamp", "original", "statuscode", "mimetype", "digest"],
            list(alternate.values()),
        ]
        primary_archive = wtm.archive_url_for(primary)
        alternate_archive = wtm.archive_url_for(alternate)
        captures_url = wtm.cdx_captures_url(primary["original"], 10)
        client = FakeClient(
            {
                primary_archive: http_error(primary_archive, 404),
                captures_url: json.dumps(alternate_cdx).encode(),
                alternate_archive: b"<html><body><p>ok</p></body></html>",
            },
        )

        body, _headers, record, archive_url, tried = wtm.fetch_archived_page(client, primary, 10)

        self.assertEqual(body, b"<html><body><p>ok</p></body></html>")
        self.assertEqual(record["timestamp"], alternate["timestamp"])
        self.assertEqual(archive_url, alternate_archive)
        self.assertEqual(
            [item["timestamp"] for item in tried],
            ["20200101000000", "20210101000000"],
        )

    def test_write_markdown_includes_front_matter(self) -> None:
        record = {
            "timestamp": "20200101000000",
            "original": "http://www.example.com/page/",
            "mimetype": "text/html",
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.md"
            wtm.write_markdown(
                path,
                "Example Page",
                "Body text.",
                record,
                "https://archive.example",
            )

            text = path.read_text()

        self.assertIn('title: "Example Page"', text)
        self.assertIn('original_url: "http://www.example.com/page/"', text)
        self.assertTrue(text.endswith("Body text.\n"))


if __name__ == "__main__":
    unittest.main()
