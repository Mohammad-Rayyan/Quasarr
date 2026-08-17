# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace

from bs4 import BeautifulSoup

from quasarr.search.sources import a4
from quasarr.search.sources.a4 import (
    EPISODE_NUM_PATTERN,
    Source,
    _canonical_absolute_episode,
    _episode_entries,
    _franchise_chain,
    _max_episode_number,
)

HOST = "source.invalid"


def make_shared_state(host=HOST):
    def config(section):
        if section == "Hostnames":
            return {"a4": host}
        return {}

    return SimpleNamespace(values={"user_agent": "UA/1.0", "config": config})


def soup(html):
    return BeautifulSoup(html, "html.parser")


def _card(ep_num_text, slug):
    """A listing card: number lives in .ep_num, slug is deliberately unreliable."""
    num = (
        f'<div class="ep_num">{ep_num_text}</div>' if ep_num_text is not None else ""
    )
    return (
        '<div class="anime-card-container">'
        f"{num}"
        f'<div class="anime-card-poster"><a href="https://{HOST}/episode/{slug}/">'
        "watch</a></div>"
        "</div>"
    )


def _anime_page(cards, atype="TV", year="2020", extra=""):
    return (
        "<html><body>"
        f'<div class="anime-info">نوع الأنمي : {atype}</div>'
        f'<div class="anime-info">بداية العرض : {year}</div>'
        f"{extra}"
        f'<div class="anime-list-content">{"".join(cards)}</div>'
        "</body></html>"
    )


def _episode_page(resolutions):
    rows = "".join(
        f'<tr><td class="td-quality">{r}</td>'
        f'<td class="td-server"><span class="server-name">mega.nz</span></td>'
        f'<td class="td-link"><a href="https://mega.nz/x{i}">dl</a></td></tr>'
        for i, r in enumerate(resolutions)
    )
    return (
        "<html><body>"
        '<div class="episode-download-container"><table>'
        f"{rows}"
        "</table></div></body></html>"
    )


class EpisodeNumberPatternTests(unittest.TestCase):
    def test_clean_number_matches(self):
        self.assertEqual(EPISODE_NUM_PATTERN.search("الحلقة 330").group(1), "330")
        self.assertEqual(EPISODE_NUM_PATTERN.search("الحلقة-366-kiyhh").group(1), "366")

    def test_junk_suffix_rejected(self):
        # "44pss" must not read as episode 44 - the alnum boundary blocks it.
        self.assertIsNone(EPISODE_NUM_PATTERN.search("الحلقة-44pss"))

    def test_slug_without_number(self):
        self.assertIsNone(EPISODE_NUM_PATTERN.search("الحلقة-smfpi"))


class EpisodeEntriesTests(unittest.TestCase):
    def test_card_text_wins_over_lying_slug(self):
        page = soup(_anime_page([_card("الحلقة 330", "bleach-الحلقة-44pss")]))
        entries = _episode_entries(page)
        self.assertEqual([n for n, _ in entries], [330])

    def test_card_text_when_slug_has_no_number(self):
        page = soup(_anime_page([_card("الحلقة 365", "bleach-الحلقة-smfpi")]))
        self.assertEqual([n for n, _ in _episode_entries(page)], [365])

    def test_slug_fallback_when_no_ep_num(self):
        page = soup(_anime_page([_card(None, "show-الحلقة-7")]))
        self.assertEqual([n for n, _ in _episode_entries(page)], [7])

    def test_sidebar_anime_links_ignored(self):
        extra = f'<aside><a href="https://{HOST}/anime/other/">rel</a></aside>'
        page = soup(_anime_page([_card("الحلقة 5", "show-الحلقة-5")], extra=extra))
        self.assertEqual(len(_episode_entries(page)), 1)

    def test_max_episode_number_from_page_one(self):
        cards = [_card(f"الحلقة {n}", f"show-الحلقة-{n}") for n in (12, 11, 10)]
        self.assertEqual(_max_episode_number(soup(_anime_page(cards))), 12)


class ReleaseTitleTests(unittest.TestCase):
    def setUp(self):
        self._orig = a4.generate_download_link
        a4.generate_download_link = (
            lambda ss, title, url, size, pw, imdb, ini: f"stub://{url}"
        )

    def tearDown(self):
        a4.generate_download_link = self._orig

    def test_title_uses_resolved_number_not_slug(self):
        page = soup(_episode_page(["FHD 1080p", "HD 720p"]))
        src = Source()
        releases = src._releases_from_episode_page(
            make_shared_state(),
            page,
            f"https://{HOST}/episode/bleach-الحلقة-44pss/",
            series_title="Bleach",
            episode_number=330,
            year="",
            as_movie=False,
        )
        titles = sorted(r["details"]["title"] for r in releases)
        self.assertEqual(
            titles,
            [
                "[A4U] Bleach - 330 [1080p][WEB-DL][x264]",
                "[A4U] Bleach - 330 [720p][WEB-DL][x264]",
            ],
        )


class ArcMappingTests(unittest.TestCase):
    """Absolute-number resolution across split-arc franchise entries."""

    def setUp(self):
        self._orig_link = a4.generate_download_link
        a4.generate_download_link = (
            lambda ss, title, url, size, pw, imdb, ini: f"stub://{url}"
        )

        # main run 1..5 (2020), arc-b 1..3 (2021), arc-c 1..2 (2022).
        # Absolute chain: [1-5] main, [6-8] arc-b, [9-10] arc-c.
        main = f"https://{HOST}/anime/show/"
        arcb = f"https://{HOST}/anime/show-arc-b/"
        arcc = f"https://{HOST}/anime/show-arc-c/"
        dub = f"https://{HOST}/anime/show-arc-b-مدبلج/"

        def anime(url_base, count, year):
            cards = [
                _card(f"الحلقة {n}", f"{url_base}-الحلقة-{n}") for n in range(count, 0, -1)
            ]
            return _anime_page(cards, year=year)

        self._pages = {
            main: anime("show", 5, "2020"),
            arcb: anime("show-arc-b", 3, "2021"),
            arcc: anime("show-arc-c", 2, "2022"),
            dub: anime("show-arc-b-dub", 3, "2021"),
        }
        # Episode pages: main eps 480p only, arcs carry 1080p.
        for base, count, res in (
            ("show", 5, ["SD 480p"]),
            ("show-arc-b", 3, ["FHD 1080p", "HD 720p"]),
            ("show-arc-c", 2, ["FHD 1080p"]),
        ):
            for n in range(1, count + 1):
                self._pages[f"https://{HOST}/episode/{base}-الحلقة-{n}/"] = (
                    _episode_page(res)
                )

        self._candidates = [
            {"title": "Show", "url": main, "type": ""},
            {"title": "Show Arc B", "url": arcb, "type": ""},
            {"title": "Show Arc B مدبلج", "url": dub, "type": ""},
            {"title": "Show Arc C", "url": arcc, "type": ""},
        ]

        def fake_fetch(shared_state, url, timeout):
            html = self._pages.get(url)
            return soup(html) if html is not None else None

        self._orig_fetch = a4._fetch
        a4._fetch = fake_fetch

    def tearDown(self):
        a4.generate_download_link = self._orig_link
        a4._fetch = self._orig_fetch

    def test_chain_orders_oldest_first_and_excludes_dub(self):
        chain = _franchise_chain(make_shared_state(), self._candidates, 5)
        self.assertEqual(
            [(e["title"], e["count"]) for e in chain],
            [("Show", 5), ("Show Arc B", 3), ("Show Arc C", 2)],
        )

    def test_absolute_in_main_run(self):
        src = Source()
        rel = src._resolve_tv_episode(make_shared_state(), self._candidates, 3, 5)
        self.assertEqual({r["details"]["title"] for r in rel}, {
            "[A4U] Show - 003 [480p][WEB-DL][x264]"
        })

    def test_absolute_maps_into_arc_with_1080p(self):
        # Absolute 7 -> arc-b local 2. Label keeps the absolute number so Sonarr
        # still matches it, series title stays the franchise root "Show".
        src = Source()
        rel = src._resolve_tv_episode(make_shared_state(), self._candidates, 7, 5)
        titles = {r["details"]["title"] for r in rel}
        self.assertIn("[A4U] Show - 007 [1080p][WEB-DL][x264]", titles)
        self.assertIn("[A4U] Show - 007 [720p][WEB-DL][x264]", titles)
        src_url = rel[0]["details"]["source"]
        self.assertIn("show-arc-b", src_url)

    def test_absolute_beyond_total_returns_nothing(self):
        src = Source()
        self.assertEqual(
            src._resolve_tv_episode(make_shared_state(), self._candidates, 99, 5), []
        )


class SeasonRelativeGuardTests(unittest.TestCase):
    def test_season_relative_search_skipped(self):
        # a4 is absolute-only; a truthy season must yield no results.
        src = Source()
        result = src.search(
            make_shared_state(),
            0.0,
            a4.SEARCH_CAT_SHOWS,
            search_string="Bleach",
            season=17,
            episode=44,
        )
        self.assertEqual(result, [])


class SceneAbsoluteMappingTests(unittest.TestCase):
    class FakeSonarrClient:
        def _get(self, path, params=None):
            if path == "/series":
                return [
                    {
                        "id": 113,
                        "imdbId": "tt0434665",
                        "useSceneNumbering": True,
                    }
                ]
            if path == "/episode" and params == {"seriesId": 113}:
                return [
                    {
                        "absoluteEpisodeNumber": 44,
                        "sceneAbsoluteEpisodeNumber": 44,
                        "lastSearchTime": "2026-08-11T06:50:58Z",
                    },
                    {
                        "absoluteEpisodeNumber": 410,
                        "sceneAbsoluteEpisodeNumber": 44,
                        "lastSearchTime": "2026-08-17T06:52:41Z",
                    },
                ]
            return []

    def test_latest_sonarr_search_disambiguates_scene_number(self):
        state = make_shared_state()
        state.values["sonarr_client"] = self.FakeSonarrClient()
        self.assertEqual(
            _canonical_absolute_episode(state, "tt0434665", 44),
            410,
        )

    def test_unknown_scene_number_stays_unchanged(self):
        state = make_shared_state()
        state.values["sonarr_client"] = self.FakeSonarrClient()
        self.assertEqual(
            _canonical_absolute_episode(state, "tt0434665", 999),
            999,
        )


if __name__ == "__main__":
    unittest.main()
