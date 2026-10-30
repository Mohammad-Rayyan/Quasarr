# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup

from quasarr.constants import SEARCH_CAT_SHOWS, SEARCH_CAT_SHOWS_ANIME
from quasarr.search.sources.helpers import a4u_hints
from quasarr.search.sources.sh import Source as ShSource
from quasarr.search.sources.tc import Source as TcSource

LINK_CLICK = "tt14976292"
SPARKS = "tt38939446"

HINTS = {
    "version": 1,
    "series": {
        LINK_CLICK: {
            "titles": {"tc": ["Shiguang Dailiren"]},
            "episodes": {"tc": {"S04E09": "https://old.invalid/episode/lc-s3-e9/"}},
            "seasons": {
                "tc": [
                    {"siteTitle": "Shiguang Dailiren", "siteSeason": 3, "season": 4},
                    {
                        "siteTitle": "Shiguang Dailiren Yingdu Pian",
                        "siteSeason": 2,
                        "season": 3,
                    },
                ]
            },
        },
        SPARKS: {
            "titles": {"sh": ["Nijusseiki Denki Mokuroku"]},
            "episodes": {"sh": {"9": "/episodes/nijusseiki-9/"}},
        },
    },
}

LIBRARY = [
    {
        "imdbId": SPARKS,
        "title": "Sparks of Tomorrow",
        "originalTitle": "Nijuuseiki Denki Mokuroku",
        "alternateTitles": [{"title": "Nijuuseiki Denki Mokuroku: Eureka Evrika"}],
    },
    {"imdbId": LINK_CLICK, "title": "Link Click", "alternateTitles": []},
]

TC_SEARCH_HTML = """
<html><body>
  <div class="Small--Box">
    <a href="https://tc.invalid/lc-s3-e8/">
      <h3 class="title">انمي Shiguang Dailiren الموسم الثالث الحلقة 8 مترجمة</h3>
      <ul class="liList"><li>1080p WEB-DL</li></ul>
    </a>
  </div>
  <div class="Small--Box">
    <a href="https://tc.invalid/lc-yp-s2-e8/">
      <h3 class="title">انمي Shiguang Dailiren Yingdu Pian الموسم الثاني الحلقة 8 مترجمة</h3>
      <ul class="liList"><li>1080p WEB-DL</li></ul>
    </a>
  </div>
</body></html>
"""


class FakeSonarr:
    def __init__(self, parses=None, fail=False):
        self.parses = parses or {}
        self.fail = fail
        self.calls = []

    def _get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/series":
            return LIBRARY
        if path == "/parse":
            if self.fail:
                return None
            imdb = self.parses.get(params["title"])
            return {"series": {"imdbId": imdb}} if imdb else {"series": None}
        return None


def fake_link(shared_state, title, url, size_mb, password, imdb_id, source_key):
    return f"link:{title}|{url}"


class HintsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        a4u_hints._parse_cache.clear()
        a4u_hints._published.clear()
        a4u_hints._library_cache.update(at=0.0, records=None)
        a4u_hints._hints_cache.update(path=None, mtime=None, data={})
        hosts = {"tc": "tc.invalid", "sh": "sh.invalid"}
        self.shared = SimpleNamespace(
            values={
                "configfile": os.path.join(self.tmp.name, "Quasarr.ini"),
                "user_agent": "UA/1.0",
                "internal_address": "http://localhost:8080",
                "config": lambda section: hosts if section == "Hostnames" else {},
            }
        )

    def write_hints(self, data=HINTS, raw=None):
        path = os.path.join(self.tmp.name, a4u_hints.HINTS_FILE)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(raw if raw is not None else json.dumps(data))

    def sonarr(self, client):
        return patch("quasarr.providers.sonarr_api.get_client", return_value=client)


class LoaderTests(HintsTestBase):
    def test_missing_file_is_empty(self):
        self.assertEqual(a4u_hints.load_hints(self.shared), {})
        self.assertEqual(a4u_hints.hint_titles(self.shared, LINK_CLICK, "tc"), [])

    def test_malformed_file_is_empty(self):
        self.write_hints(raw="{not json")
        self.assertEqual(a4u_hints.load_hints(self.shared), {})

    def test_episode_url_is_rebased_onto_current_host(self):
        self.write_hints()
        self.assertEqual(
            a4u_hints.hint_episode_url(
                self.shared, LINK_CLICK, "tc", "S04E09", "tc.invalid"
            ),
            "https://tc.invalid/episode/lc-s3-e9/",
        )
        self.assertEqual(
            a4u_hints.hint_episode_url(self.shared, SPARKS, "sh", 9, "sh.invalid"),
            "https://sh.invalid/episodes/nijusseiki-9/",
        )

    def test_more_specific_site_title_wins(self):
        self.write_hints()
        entries = a4u_hints.season_map_entries(self.shared, "tc", LINK_CLICK)
        self.assertEqual(
            a4u_hints.mapped_season(entries, "Shiguang Dailiren Yingdu Pian", 2), 3
        )
        self.assertEqual(a4u_hints.mapped_season(entries, "Shiguang Dailiren", 3), 4)
        self.assertEqual(a4u_hints.mapped_season(entries, "Shiguang Dailiren", 2), 2)

    def test_publish_site_writes_hostnames(self):
        a4u_hints.publish_site(self.shared, "tc", "tc.invalid")
        a4u_hints.publish_site(self.shared, "a4", "a4.invalid")
        with open(
            os.path.join(self.tmp.name, a4u_hints.SITES_FILE), encoding="utf-8"
        ) as handle:
            self.assertEqual(
                json.load(handle), {"a4": "a4.invalid", "tc": "tc.invalid"}
            )


def release(title, source="https://sh.invalid/dl/1/"):
    return {
        "details": {
            "title": title,
            "source": source,
            "size": 0,
            "imdb_id": None,
            "link": "old",
        },
        "type": "protected",
    }


class RelabelTests(HintsTestBase):
    UNKNOWN = (
        "[SHAH] Nijusseiki Denki Mokuroku Eureka Evrika - 007 [1080p][WEB-DL][x264]"
    )

    def relabel(self, releases, client, imdb=SPARKS):
        with (
            self.sonarr(client),
            patch.object(a4u_hints, "generate_download_link", fake_link),
        ):
            return a4u_hints.relabel_releases(self.shared, releases, imdb, "sh")

    def test_unknown_series_spelling_variant_is_relabelled(self):
        out = self.relabel([release(self.UNKNOWN)], FakeSonarr())
        title = out[0]["details"]["title"]
        self.assertEqual(title, "[SHAH] Sparks of Tomorrow - 007 [1080p][WEB-DL][x264]")
        self.assertEqual(
            out[0]["details"]["link"], f"link:{title}|https://sh.invalid/dl/1/"
        )

    def test_title_sonarr_resolves_is_kept(self):
        title = "[SHAH] Dr. Stone Science Future - 002 [1080p][WEB-DL][x264]"
        out = self.relabel([release(title)], FakeSonarr(parses={title: SPARKS}))
        self.assertEqual(out[0]["details"]["title"], title)
        self.assertEqual(out[0]["details"]["link"], "old")

    def test_unrelated_show_is_never_renamed(self):
        title = "[SHAH] Kingdom - 007 [1080p][WEB-DL][x264]"
        out = self.relabel([release(title)], FakeSonarr())
        self.assertEqual(out[0]["details"]["title"], title)

    def test_sonarr_unreachable_keeps_titles(self):
        out = self.relabel([release(self.UNKNOWN)], FakeSonarr(fail=True))
        self.assertEqual(out[0]["details"]["title"], self.UNKNOWN)

    def test_scene_shape_is_dotted(self):
        self.write_hints()
        title = "Shiguang.Dailiren.S04E08.1080p.WEB-DL.x264-TCinema"
        with (
            self.sonarr(FakeSonarr()),
            patch.object(a4u_hints, "generate_download_link", fake_link),
        ):
            out = a4u_hints.relabel_releases(
                self.shared, [release(title)], LINK_CLICK, "tc"
            )
        self.assertEqual(
            out[0]["details"]["title"], "Link.Click.S04E08.1080p.WEB-DL.x264-TCinema"
        )


class TcSeasonMapTests(HintsTestBase):
    def search(self, client, season, episode, fetch=None):
        def default_fetch(_shared, url, _timeout):
            return (
                BeautifulSoup(TC_SEARCH_HTML, "html.parser") if "?s=" in url else None
            )

        with (
            self.sonarr(client),
            patch(
                "quasarr.search.sources.tc._fetch", side_effect=fetch or default_fetch
            ),
            patch("quasarr.search.sources.tc.generate_download_link", fake_link),
            patch.object(a4u_hints, "generate_download_link", fake_link),
            patch(
                "quasarr.search.sources.tc.get_localized_title",
                return_value="Link Click",
            ),
            patch("quasarr.search.sources.tc.mark_hostname_issue"),
            patch("quasarr.search.sources.tc.clear_hostname_issue"),
        ):
            return TcSource().search(
                self.shared,
                0.0,
                search_category=str(SEARCH_CAT_SHOWS),
                search_string=LINK_CLICK,
                season=season,
                episode=episode,
            )

    def test_site_season_three_answers_tvdb_season_four(self):
        self.write_hints()
        title = "Shiguang.Dailiren.S04E08.1080p.WEB-DL.x264-TCinema"
        releases = self.search(FakeSonarr(parses={title: LINK_CLICK}), 4, 8)
        self.assertEqual([r["details"]["title"] for r in releases], [title])
        self.assertTrue(releases[0]["details"]["source"].endswith("/lc-s3-e8/"))

    def test_arc_title_maps_to_its_own_season(self):
        self.write_hints()
        releases = self.search(FakeSonarr(parses={}), 3, 8)
        self.assertEqual(len(releases), 1)
        self.assertTrue(releases[0]["details"]["source"].endswith("/lc-yp-s2-e8/"))

    def test_hinted_episode_skips_site_search(self):
        self.write_hints()
        fetched = []

        def fetch(_shared, url, _timeout):
            fetched.append(url)
            return None

        releases = self.search(FakeSonarr(parses={}), 4, 9, fetch=fetch)
        self.assertEqual(fetched, [])
        self.assertEqual(
            releases[0]["details"]["source"], "https://tc.invalid/episode/lc-s3-e9/"
        )

    def test_without_hints_behaviour_is_unchanged(self):
        releases = self.search(FakeSonarr(parses={}), 4, 8)
        self.assertEqual(releases, [])


class ShFallbackTests(HintsTestBase):
    def test_tries_hint_then_localized_then_sonarr_titles_capped(self):
        self.write_hints({"series": {}})
        queries = []

        def search_one(_self, _shared, _host, query, _want_movie, _episode):
            queries.append(query)
            return []

        with (
            self.sonarr(FakeSonarr()),
            patch.object(ShSource, "_search_one", search_one),
            patch(
                "quasarr.search.sources.sh.get_localized_title",
                return_value="Sparks of Tomorrow",
            ),
        ):
            ShSource().search(
                self.shared,
                0.0,
                search_category=str(SEARCH_CAT_SHOWS_ANIME),
                search_string=SPARKS,
                episode=7,
            )
        self.assertEqual(
            queries,
            [
                "Sparks of Tomorrow",
                "Nijuuseiki Denki Mokuroku",
                "Nijuuseiki Denki Mokuroku: Eureka Evrika",
            ],
        )

    def test_hinted_episode_url_is_used_directly(self):
        self.write_hints()
        seen = []

        def from_episode(
            _self, _shared, url, title, number, _year, _as_movie, _timeout
        ):
            seen.append((url, title, number))
            return [
                release(f"[SHAH] {title} - {number:03d} [1080p][WEB-DL][x264]", url)
            ]

        with (
            self.sonarr(FakeSonarr()),
            patch.object(ShSource, "_releases_from_episode", from_episode),
            patch.object(
                ShSource, "_search_one", side_effect=AssertionError("searched")
            ),
            patch.object(a4u_hints, "generate_download_link", fake_link),
            patch("quasarr.search.sources.sh.get_localized_title", return_value=None),
            patch("quasarr.search.sources.sh.mark_hostname_issue"),
            patch("quasarr.search.sources.sh.clear_hostname_issue"),
        ):
            out = ShSource().search(
                self.shared,
                0.0,
                search_category=str(SEARCH_CAT_SHOWS_ANIME),
                search_string=SPARKS,
                episode=9,
            )
        self.assertEqual(
            seen,
            [
                (
                    "https://sh.invalid/episodes/nijusseiki-9/",
                    "Nijusseiki Denki Mokuroku",
                    9,
                )
            ],
        )
        self.assertEqual(
            out[0]["details"]["title"],
            "[SHAH] Sparks of Tomorrow - 009 [1080p][WEB-DL][x264]",
        )


if __name__ == "__main__":
    unittest.main()
