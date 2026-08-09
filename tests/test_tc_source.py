# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup

from quasarr.constants import SEARCH_CAT_MOVIES, SEARCH_CAT_SHOWS
from quasarr.downloads.sources.tc import Source as DownloadSource
from quasarr.downloads.sources.tc import _download_url, _resolution_of_label
from quasarr.search.sources.tc import (
    Source as SearchSource,
)
from quasarr.search.sources.tc import (
    _classify_title,
    _parse_cards,
    _quality_from_badges,
    _season_pack_url,
    _title_matches,
)


def make_shared_state(host="source.invalid"):
    def config(section):
        if section == "Hostnames":
            return {"tc": host}
        return {}

    return SimpleNamespace(
        values={
            "user_agent": "UA/1.0",
            "internal_address": "http://localhost:8080",
            "config": config,
        }
    )


SEARCH_HTML = """
<html><body>
<ul class="Posts--List">
  <div class="Small--Box">
    <a href="https://source.invalid/series-ep-s3e12/">
      <h3 class="title">مسلسل Example Show الموسم الثالث الحلقة 12 مترجمة</h3>
      <ul class="liList"><li>دراما</li><li>1080p WEB-DL</li><li>8.1</li></ul>
    </a>
  </div>
  <div class="Small--Box">
    <a href="https://source.invalid/series-ep-s3e11/">
      <h3 class="title">مسلسل Example Show الموسم الثالث الحلقة 11 مترجمة</h3>
      <ul class="liList"><li>دراما</li><li>720p WEB-DL</li></ul>
    </a>
  </div>
  <div class="Small--Box">
    <a href="https://source.invalid/movie-x/">
      <h3 class="title">فيلم Example Movie 2024 مترجم اون لاين</h3>
      <ul class="liList"><li>اكشن</li><li>1080p BluRay</li></ul>
    </a>
  </div>
</ul>
</body></html>
"""

EPISODE_PAGE_HTML = """
<html><body>
<a href="https://source.invalid/series/example-show-s3/">مسلسل Example Show الموسم الثالث مترجم</a>
<a href="https://source.invalid/series/example-show-s3/download/">تحميل الموسم 3 كاملا برابط واحد</a>
<a href="https://source.invalid/series/example-show/">مسلسل Example Show مترجم</a>
</body></html>
"""

PACK_DOWNLOAD_HTML = """
<html><body>
<div class="DownloadBox">
  <div class="DownloadBlock">
    <h2 class="download-title">تحميل 1080p</h2>
    <ul class="download-items">
      <li><a class="downloadsLink green" href="https://hoster.invalid/a">UpDown 1080p</a></li>
      <li><a class="downloadsLink" href="https://hoster.invalid/b">Rapidgator 1080p</a></li>
      <li><a class="downloadsLink" href="https://vidtube.invalid/c">VideoTube 1080p - 720p</a></li>
    </ul>
  </div>
</div>
</body></html>
"""

EPISODE_DOWNLOAD_HTML = """
<html><body>
<div class="DownloadBox">
  <div class="DownloadBlock">
    <h2 class="download-title">تحميل 1080p</h2>
    <a class="downloadsLink" href="https://hoster.invalid/ep1080">UpDown 1080p</a>
  </div>
  <div class="DownloadBlock">
    <h2 class="download-title">تحميل 720p</h2>
    <a class="downloadsLink" href="https://hoster.invalid/ep720">UpDown 720p</a>
  </div>
</div>
</body></html>
"""


class FakeResponse:
    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code


class TitleParsingTests(unittest.TestCase):
    def test_missing_season_defaults_to_one_on_cards(self):
        html = """
        <html><body><div class="Small--Box">
          <a href="https://source.invalid/ep1/">
            <h3 class="title">مسلسل Ms. Incognito الحلقة 1 مترجمة</h3>
            <ul class="liList"><li>1080p WEB-DL</li></ul>
          </a>
        </div></body></html>
        """
        cards = _parse_cards(BeautifulSoup(html, "html.parser"))
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["episode"], 1)
        self.assertEqual(cards[0]["season"], 1)

    def test_episode_and_movie_classification(self):
        kind, name, season, episode, year = _classify_title(
            "مسلسل Example Show الموسم الثالث الحلقة 12 مترجمة",
            "https://source.invalid/x/",
        )
        self.assertEqual(kind, "episode")
        self.assertEqual(name, "Example Show")
        self.assertEqual(season, 3)
        self.assertEqual(episode, 12)

        kind, name, season, episode, year = _classify_title(
            "فيلم Example Movie 2024 مترجم اون لاين",
            "https://source.invalid/film/",
        )
        self.assertEqual(kind, "movie")
        self.assertEqual(name, "Example Movie")
        self.assertEqual(year, "2024")

    def test_title_match_and_quality_badges(self):
        self.assertTrue(_title_matches("Example Show", "Example Show"))
        self.assertTrue(_title_matches("Example", "Example Show"))
        self.assertFalse(_title_matches("Other Show", "Example Show"))
        resolution, source = _quality_from_badges(["دراما", "1080p WEB-DL", "8.1"])
        self.assertEqual(resolution, "1080p")
        self.assertEqual(source, "WEB-DL")

    def test_season_pack_url_prefers_download_link(self):
        page = BeautifulSoup(EPISODE_PAGE_HTML, "html.parser")
        url = _season_pack_url(page, 3)
        self.assertEqual(url, "https://source.invalid/series/example-show-s3/")


def fake_gen_link(shared_state, title, url, size_mb, password, imdb_id, source_key):
    return f"http://localhost:8080/download/?t={title}"


class SearchSourceTests(unittest.TestCase):
    def _run_search(self, fake_fetch, **kwargs):
        shared = make_shared_state()
        source = SearchSource()
        with (
            patch("quasarr.search.sources.tc._fetch", side_effect=fake_fetch),
            patch("quasarr.search.sources.tc.generate_download_link", fake_gen_link),
            patch("quasarr.search.sources.tc.mark_hostname_issue"),
            patch("quasarr.search.sources.tc.clear_hostname_issue"),
        ):
            return source.search(shared, 0.0, **kwargs)

    def test_episode_search(self):
        def fake_fetch(_shared, url, _timeout):
            if "?s=" in url:
                return BeautifulSoup(SEARCH_HTML, "html.parser")
            return None

        releases = self._run_search(
            fake_fetch,
            search_category=str(SEARCH_CAT_SHOWS),
            search_string="Example Show",
            season=3,
            episode=12,
        )

        self.assertEqual(len(releases), 1)
        details = releases[0]["details"]
        self.assertIn("S03E12", details["title"])
        self.assertIn("1080p", details["title"])
        self.assertIn("WEB-DL", details["title"])
        self.assertTrue(details["source"].endswith("/series-ep-s3e12/"))

    def test_season_pack_search(self):
        def fake_fetch(_shared, url, _timeout):
            if "?s=" in url:
                return BeautifulSoup(SEARCH_HTML, "html.parser")
            if url.endswith("/series-ep-s3e12/") or url.endswith("/series-ep-s3e11/"):
                return BeautifulSoup(EPISODE_PAGE_HTML, "html.parser")
            if url.rstrip("/").endswith("/download"):
                return BeautifulSoup(PACK_DOWNLOAD_HTML, "html.parser")
            return None

        releases = self._run_search(
            fake_fetch,
            search_category=str(SEARCH_CAT_SHOWS),
            search_string="Example Show",
            season=3,
            episode=None,
        )

        self.assertTrue(releases)
        details = releases[0]["details"]
        self.assertRegex(details["title"], r"\.S03\.\d{3,4}p\.")
        self.assertNotRegex(details["title"], r"\.S03E\d+")
        self.assertEqual(details["source"], "https://source.invalid/series/example-show-s3/")

    def test_season_search_falls_back_to_episodes_when_pack_has_no_mirrors(self):
        empty_pack = BeautifulSoup("<html><body></body></html>", "html.parser")

        def fake_fetch(_shared, url, _timeout):
            if "?s=" in url:
                return BeautifulSoup(SEARCH_HTML, "html.parser")
            if url.endswith("/series-ep-s3e12/") or url.endswith("/series-ep-s3e11/"):
                return BeautifulSoup(EPISODE_PAGE_HTML, "html.parser")
            if url.rstrip("/").endswith("/download"):
                return empty_pack
            return None

        releases = self._run_search(
            fake_fetch,
            search_category=str(SEARCH_CAT_SHOWS),
            search_string="Example Show",
            season=3,
            episode=None,
        )

        self.assertTrue(releases)
        titles = [r["details"]["title"] for r in releases]
        self.assertTrue(any(".S03E12." in t for t in titles))
        self.assertTrue(any(".S03E11." in t for t in titles))
        self.assertFalse(any(t.endswith("S03.1080p.WEB-DL.x264-TCinema") for t in titles))

    def test_movie_search(self):
        def fake_fetch(_shared, url, _timeout):
            if "?s=" in url:
                return BeautifulSoup(SEARCH_HTML, "html.parser")
            return None

        releases = self._run_search(
            fake_fetch,
            search_category=str(SEARCH_CAT_MOVIES),
            search_string="Example Movie",
        )

        self.assertEqual(len(releases), 1)
        self.assertIn("2024", releases[0]["details"]["title"])
        self.assertIn("BluRay", releases[0]["details"]["title"])

    def test_parse_cards(self):
        cards = _parse_cards(BeautifulSoup(SEARCH_HTML, "html.parser"))
        self.assertEqual(len(cards), 3)
        self.assertEqual(cards[0]["kind"], "episode")
        self.assertEqual(cards[2]["kind"], "movie")


class DownloadSourceTests(unittest.TestCase):
    def test_download_url_helper(self):
        self.assertEqual(
            _download_url("https://source.invalid/ep/"),
            "https://source.invalid/ep/download/",
        )
        self.assertEqual(
            _download_url("https://source.invalid/ep/download/"),
            "https://source.invalid/ep/download/",
        )

    def test_multi_quality_label_is_agnostic(self):
        self.assertEqual(_resolution_of_label("UpDown 1080p"), "1080p")
        self.assertEqual(_resolution_of_label("VideoTube 1080p - 720p - 480p"), "")

    def test_get_download_links_filters_resolution_and_captcha_hosts(self):
        shared = make_shared_state()
        source = DownloadSource()

        with patch(
            "quasarr.downloads.sources.tc.requests.get",
            return_value=FakeResponse(PACK_DOWNLOAD_HTML.encode()),
        ):
            result = source.get_download_links(
                shared,
                "https://source.invalid/series/example-show-s3/",
                mirrors=[],
                title="Example.Show.S03.1080p.WEB-DL.x264-TCinema",
                password="",
            )

        links = result["links"]
        hosts = [link[0] for link in links]
        self.assertIn("https://hoster.invalid/a", hosts)
        self.assertIn("https://hoster.invalid/b", hosts)
        self.assertTrue(all("vidtube" not in url for url in hosts))

    def test_episode_resolution_filter(self):
        shared = make_shared_state()
        source = DownloadSource()

        with patch(
            "quasarr.downloads.sources.tc.requests.get",
            return_value=FakeResponse(EPISODE_DOWNLOAD_HTML.encode()),
        ):
            result = source.get_download_links(
                shared,
                "https://source.invalid/series-ep-s3e12/",
                mirrors=[],
                title="Example.Show.S03E12.720p.WEB-DL.x264-TCinema",
                password="",
            )

        self.assertEqual(result["links"], [["https://hoster.invalid/ep720", "updown"]])


if __name__ == "__main__":
    unittest.main()
