# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# TopCinema-style Arabic DDL - movies, Western/Asian series, and anime.
#
# Titles on the site are English/romaji; only the chrome is Arabic. Season packs
# ("one link for the whole season") are first-class: episode pages link to a
# /series/<slug>/download/ pack when one exists. Per-episode and movie pages
# expose hoster links under the same /download/ suffix.
#
# Verified page contract (live captures, 2026-08-05):
#   search       GET /?s=<query>              -> div.Small--Box cards
#   episode      /<arabic-slug>/              -> a[href*="/series/"][href*="/download/"]
#                                                 for completed seasons; a.download
#                                                 -> /download/ for the episode
#   season pack  /series/<arabic-slug>/       -> a.downloadFullSeason -> /download/
#   movie        /<arabic-slug>/              -> a.download -> /download/
#   download     <page>/download/             -> a.downloadsLink (hoster + NxNp text)
#   Season/episode numbers sit after the Arabic words for "season"/"episode";
#   ordinal words (الاول..العاشر, …) are used for seasons, digits for episodes.
#   Cards carry a quality badge like "1080p WEB-DL".

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from quasarr.constants import (
    FEED_REQUEST_TIMEOUT_SECONDS,
    SEARCH_CAT_MOVIES,
    SEARCH_CAT_SHOWS,
    SEARCH_CAT_SHOWS_ANIME,
    SEARCH_REQUEST_TIMEOUT_SECONDS,
)
from quasarr.providers.hostname_issues import clear_hostname_issue, mark_hostname_issue
from quasarr.providers.imdb_metadata import get_localized_title
from quasarr.providers.log import debug
from quasarr.providers.utils import (
    generate_download_link,
    get_base_search_category_id,
    is_imdb_id,
)
from quasarr.search.sources.helpers.search_release import SearchRelease
from quasarr.search.sources.helpers.search_source import AbstractSearchSource

AR_SERIES = "مسلسل"
AR_ANIME = "انمي"
AR_FILM = "فيلم"
AR_SEASON = "الموسم"
AR_EPISODE = "الحلقة"

# Ordinal season words as they appear after AR_SEASON. Digits are also accepted.
AR_SEASON_ORDINALS = {
    "الاول": 1,
    "الأول": 1,
    "الثاني": 2,
    "الثالث": 3,
    "الرابع": 4,
    "الخامس": 5,
    "السادس": 6,
    "السابع": 7,
    "الثامن": 8,
    "التاسع": 9,
    "العاشر": 10,
    "الحادي عشر": 11,
    "الثاني عشر": 12,
    "الثالث عشر": 13,
    "الرابع عشر": 14,
    "الخامس عشر": 15,
    "السادس عشر": 16,
    "السابع عشر": 17,
    "الثامن عشر": 18,
    "التاسع عشر": 19,
    "العشرون": 20,
}

SEASON_ORDINAL_ALT = "|".join(
    sorted((re.escape(k) for k in AR_SEASON_ORDINALS), key=len, reverse=True)
)
SEASON_PATTERN = re.compile(
    re.escape(AR_SEASON) + r"\s+(" + SEASON_ORDINAL_ALT + r"|\d+)"
)
EPISODE_PATTERN = re.compile(re.escape(AR_EPISODE) + r"\s+(\d+)")
YEAR_PATTERN = re.compile(r"(19|20)\d{2}")
RESOLUTION_PATTERN = re.compile(r"(\d{3,4})p", re.I)

MAX_SEARCH_CANDIDATES = 12
MAX_FEED_ITEMS = 12
FEED_FETCH_WORKERS = 6

# Not "TC" — Radarr/Sonarr parse a trailing "-TC" as Telecine quality and then
# refuse imports as a downgrade from WEBDL/BluRay. Keep the host identity without
# colliding with quality tokens.
RELEASE_GROUP = "TCinema"


class Source(AbstractSearchSource):
    initials = "tc"
    language = "en"
    # No native IMDb index - resolve IDs to English titles before searching.
    supports_imdb = True
    supports_phrase = True
    supports_absolute_numbering = False
    supports_date_numbering = False
    supported_categories = [
        SEARCH_CAT_MOVIES,
        SEARCH_CAT_SHOWS,
        SEARCH_CAT_SHOWS_ANIME,
    ]

    def search(
        self,
        shared_state,
        start_time: float,
        search_category: str,
        search_string: str = "",
        season: int = None,
        episode: int = None,
    ) -> list[SearchRelease]:
        host = _host(shared_state)
        if not host or not search_string:
            return []

        base_category = get_base_search_category_id(search_category)
        want_movie = base_category == SEARCH_CAT_MOVIES

        imdb_id = is_imdb_id(search_string)
        if imdb_id:
            query = get_localized_title(
                shared_state, imdb_id, self.language, search_category
            )
            if not query:
                debug(f"[{self.initials}] could not resolve a title for {imdb_id}")
                return []
        else:
            query = search_string
            imdb_id = None

        try:
            releases = self._search_one(
                shared_state,
                host,
                query,
                want_movie=want_movie,
                season=season,
                episode=episode,
                imdb_id=imdb_id,
            )
            if releases:
                clear_hostname_issue(self.initials)
            return releases
        except Exception as exc:
            mark_hostname_issue(self.initials, "search", str(exc))
            return []

    def feed(
        self,
        shared_state,
        start_time: float,
        search_category: str,
    ) -> list[SearchRelease]:
        host = _host(shared_state)
        if not host:
            return []

        base_category = get_base_search_category_id(search_category)
        want_movie = base_category == SEARCH_CAT_MOVIES

        try:
            soup = _fetch(
                shared_state, f"https://{host}/", FEED_REQUEST_TIMEOUT_SECONDS
            )
            if soup is None:
                mark_hostname_issue(self.initials, "feed", "no response from front page")
                return []

            cards = [
                c
                for c in _parse_cards(soup)
                if c["kind"] == ("movie" if want_movie else "episode")
            ][:MAX_FEED_ITEMS]
            if not cards:
                return []

            releases: list[SearchRelease] = []
            with ThreadPoolExecutor(max_workers=FEED_FETCH_WORKERS) as pool:
                futures = {
                    pool.submit(
                        self._release_from_card,
                        shared_state,
                        card,
                        imdb_id=None,
                        as_season_pack=False,
                    ): card
                    for card in cards
                }
                for future in futures:
                    if time.time() - start_time > FEED_REQUEST_TIMEOUT_SECONDS:
                        debug(f"[{self.initials}] feed budget exhausted, stopping")
                        break
                    try:
                        release = future.result()
                    except Exception:
                        continue
                    if release:
                        releases.append(release)

            if releases:
                clear_hostname_issue(self.initials)
            return releases
        except Exception as exc:
            mark_hostname_issue(self.initials, "feed", str(exc))
            return []

    def _search_one(
        self,
        shared_state,
        host: str,
        query: str,
        want_movie: bool,
        season,
        episode,
        imdb_id,
    ) -> list[SearchRelease]:
        url = f"https://{host}/?s={quote_plus(query)}"
        soup = _fetch(shared_state, url, SEARCH_REQUEST_TIMEOUT_SECONDS)
        if soup is None:
            mark_hostname_issue(self.initials, "search", f"no response from {url}")
            return []

        cards = _parse_cards(soup)
        if not cards:
            return []

        matched = [c for c in cards if _title_matches(query, c["name"])]
        ordered = matched or cards

        if want_movie:
            movies = [c for c in ordered if c["kind"] == "movie"][:MAX_SEARCH_CANDIDATES]
            releases = []
            for card in movies:
                release = self._release_from_card(
                    shared_state, card, imdb_id=imdb_id, as_season_pack=False
                )
                if release:
                    releases.append(release)
            return releases

        # Season-only request (Sonarr season pack / missing-season search).
        # Truthiness matches Quasarr's dispatch: season may arrive as 0/"".
        want_pack = bool(season) and not episode
        if want_pack:
            return self._season_pack_releases(
                shared_state, ordered, season=int(season), imdb_id=imdb_id
            )

        episodes = [c for c in ordered if c["kind"] == "episode"]
        if season:
            episodes = [c for c in episodes if c["season"] == int(season)]
        if episode:
            episodes = [c for c in episodes if c["episode"] == int(episode)]

        releases = []
        for card in episodes[:MAX_SEARCH_CANDIDATES]:
            release = self._release_from_card(
                shared_state, card, imdb_id=imdb_id, as_season_pack=False
            )
            if release:
                releases.append(release)
        return releases

    def _season_pack_releases(
        self, shared_state, cards: list[dict], season: int, imdb_id
    ) -> list[SearchRelease]:
        """Resolve a one-link season pack via any episode page of that season."""
        candidates = [
            c
            for c in cards
            if c["kind"] == "episode" and c["season"] == season
        ] or [c for c in cards if c["kind"] == "episode"]
        if not candidates:
            return []

        for card in candidates[:MAX_SEARCH_CANDIDATES]:
            page = _fetch(shared_state, card["url"], SEARCH_REQUEST_TIMEOUT_SECONDS)
            if page is None:
                continue
            pack_url = _season_pack_url(page, season)
            if not pack_url:
                continue
            # Confirm the pack actually exposes download mirrors before emitting.
            dl_page = _fetch(
                shared_state,
                _download_url(pack_url),
                SEARCH_REQUEST_TIMEOUT_SECONDS,
            )
            if dl_page is None or not dl_page.select("a.downloadsLink"):
                debug(
                    f"[{self.initials}] season pack has no download mirrors yet: "
                    f"{pack_url}"
                )
                continue

            resolutions = _resolutions_from_download_page(dl_page)
            if not resolutions and card["resolution"]:
                resolutions = {card["resolution"]}
            if not resolutions:
                resolutions = {"1080p"}

            name = card["name"]
            source_token = card["source_token"] or "WEB-DL"
            return [
                self._build_release(
                    shared_state,
                    title=_season_release_title(name, season, resolution, source_token),
                    page_url=pack_url,
                    imdb_id=imdb_id,
                )
                for resolution in sorted(resolutions, key=_resolution_sort_key, reverse=True)
                if "|" not in _season_release_title(name, season, resolution, source_token)
            ]
        return []

    def _release_from_card(
        self, shared_state, card: dict, imdb_id, as_season_pack: bool
    ):
        if as_season_pack:
            return None

        resolution = card["resolution"] or "1080p"
        source_token = card["source_token"] or "WEB-DL"
        if card["kind"] == "movie":
            title = _movie_release_title(
                card["name"], card["year"], resolution, source_token
            )
        elif card["kind"] == "episode" and card["season"] and card["episode"]:
            title = _episode_release_title(
                card["name"],
                card["season"],
                card["episode"],
                resolution,
                source_token,
            )
        else:
            return None

        if "|" in title:
            return None
        return self._build_release(
            shared_state, title=title, page_url=card["url"], imdb_id=imdb_id
        )

    def _build_release(self, shared_state, title: str, page_url: str, imdb_id):
        link = generate_download_link(
            shared_state,
            title,
            page_url,
            0,
            "",
            imdb_id,
            self.initials,
        )
        return {
            "details": {
                "title": title,
                "hostname": self.initials,
                "imdb_id": imdb_id,
                "link": link,
                "size": 0,
                "date": datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000"),
                "source": page_url,
            },
            "type": "protected",
        }


# ---------------------------------------------------------------------- #
# module-private helpers
# ---------------------------------------------------------------------- #


def _host(shared_state):
    return shared_state.values["config"]("Hostnames").get(Source.initials)


def _fetch(shared_state, url: str, timeout: int):
    headers = {"User-Agent": shared_state.values["user_agent"]}
    response = requests.get(url, headers=headers, timeout=timeout)
    if response.status_code != 200:
        return None
    return BeautifulSoup(response.content, "html.parser")


def _parse_cards(soup) -> list[dict]:
    cards = []
    for card in soup.select(".Small--Box"):
        anchor = card.select_one("a[href]")
        title_el = card.select_one("h3.title") or card.select_one("h3")
        if not anchor or not title_el:
            continue
        href = anchor["href"].strip()
        if not href:
            continue
        raw_title = title_el.get_text(strip=True)
        badges = [
            li.get_text(" ", strip=True) for li in card.select("ul.liList li")
        ]
        resolution, source_token = _quality_from_badges(badges)
        kind, name, season, episode, year = _classify_title(raw_title, href)
        if not kind or not name:
            continue
        cards.append(
            {
                "title": raw_title,
                "name": name,
                "url": href,
                "kind": kind,
                "season": season,
                "episode": episode,
                "year": year,
                "resolution": resolution,
                "source_token": source_token,
            }
        )
    return cards


def _classify_title(raw_title: str, href: str):
    """Return (kind, english_name, season, episode, year)."""
    text = raw_title or ""
    href_l = (href or "").lower()

    if AR_FILM in text or f"/{AR_FILM}" in href or AR_FILM in href:
        name = _strip_prefix(text, (AR_FILM,))
        year_match = YEAR_PATTERN.search(name) or YEAR_PATTERN.search(text)
        year = year_match.group(0) if year_match else ""
        name = YEAR_PATTERN.sub("", name)
        name = re.sub(r"(مترجم|مترجمة|اون لاين).*$", "", name).strip(" -–—")
        return "movie", _clean_name(name), None, None, year

    if "/series/" in href_l and AR_EPISODE not in text:
        # Season-pack listing cards (no episode number).
        name, season, _ = _parse_series_fields(text)
        return ("season" if season else None), name, season, None, ""

    if AR_EPISODE in text or AR_SERIES in text or AR_ANIME in text:
        name, season, episode = _parse_series_fields(text)
        if episode:
            return "episode", name, season, episode, ""
        if season:
            return "season", name, season, None, ""

    return None, "", None, None, ""


def _parse_series_fields(text: str):
    name = _strip_prefix(text, (AR_SERIES, AR_ANIME, AR_FILM))
    season_match = SEASON_PATTERN.search(text)
    episode_match = EPISODE_PATTERN.search(text)
    season = _season_number(season_match.group(1)) if season_match else None
    episode = int(episode_match.group(1)) if episode_match else None

    if season_match:
        name = text[: season_match.start()]
        name = _strip_prefix(name, (AR_SERIES, AR_ANIME, AR_FILM))
    elif episode_match:
        name = text[: episode_match.start()]
        name = _strip_prefix(name, (AR_SERIES, AR_ANIME, AR_FILM))
    else:
        name = re.sub(r"(مترجم|مترجمة).*$", "", name)

    return _clean_name(name), season, episode


def _season_number(token: str):
    token = (token or "").strip()
    if token.isdigit():
        return int(token)
    return AR_SEASON_ORDINALS.get(token)


def _strip_prefix(text: str, prefixes) -> str:
    out = (text or "").strip()
    for prefix in prefixes:
        if out.startswith(prefix):
            out = out[len(prefix) :].strip()
    return out


def _clean_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", (name or "")).strip(" -–—:")
    return cleaned


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _title_matches(query: str, candidate: str) -> bool:
    q = _normalize(query)
    c = _normalize(candidate)
    if not q or not c:
        return False
    if q == c or q in c or c in q:
        return True
    q_tokens = set(q.split())
    c_tokens = set(c.split())
    if not q_tokens:
        return False
    return q_tokens.issubset(c_tokens)


def _quality_from_badges(badges: list[str]):
    resolution = ""
    source_token = ""
    for badge in badges:
        if not resolution:
            match = RESOLUTION_PATTERN.search(badge)
            if match:
                resolution = f"{match.group(1)}p"
        compact = re.sub(r"[^A-Z0-9]+", "", badge.upper())
        if "WEBDL" in compact or "WEB-DL".replace("-", "") in compact:
            source_token = "WEB-DL"
        elif "WEBRIP" in compact:
            source_token = "WEBRip"
        elif "BLURAY" in compact:
            source_token = "BluRay"
        elif "HDTV" in compact:
            source_token = "HDTV"
        elif "HDTS" in compact:
            source_token = "HDTS"
        elif "HDCAM" in compact:
            source_token = "HDCAM"
    return resolution, source_token


def _season_pack_url(page, season: int) -> str:
    """Pick the /series/… season-pack URL for the requested season.

    Prefer a link that already points at /download/, then fall back to the plain
    season page. Ignore the series hub (no الموسم token).
    """
    download_hit = ""
    page_hit = ""
    for anchor in page.select('a[href*="/series/"]'):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        season_match = SEASON_PATTERN.search(href) or SEASON_PATTERN.search(
            anchor.get_text(" ", strip=True)
        )
        if not season_match:
            continue
        if _season_number(season_match.group(1)) != season:
            continue
        if "/download" in href:
            download_hit = href.split("/download")[0].rstrip("/") + "/"
            break
        if not page_hit:
            page_hit = href.rstrip("/") + "/"
    return download_hit or page_hit


def _download_url(page_url: str) -> str:
    base = page_url.rstrip("/")
    if base.endswith("/download"):
        return base + "/"
    return base + "/download/"


def _resolutions_from_download_page(page) -> set:
    resolutions = set()
    for anchor in page.select("a.downloadsLink"):
        match = RESOLUTION_PATTERN.search(anchor.get_text(" ", strip=True))
        if match:
            resolutions.add(f"{match.group(1)}p")
    for heading in page.select("h2.download-title, .DownloadBlock .TitleBox h2"):
        match = RESOLUTION_PATTERN.search(heading.get_text(" ", strip=True))
        if match:
            resolutions.add(f"{match.group(1)}p")
    return resolutions


def _resolution_sort_key(resolution: str) -> int:
    match = RESOLUTION_PATTERN.search(resolution or "")
    return int(match.group(1)) if match else 0


def _clean_title_token(title: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _dots(title: str) -> str:
    return re.sub(r"\s+", ".", _clean_title_token(title)).strip(".")


def _episode_release_title(name, season, episode, resolution, source_token) -> str:
    # WEB-DL keeps Sonarr from treating these as HDTV and upgrading them away.
    return (
        f"{_dots(name)}.S{int(season):02d}E{int(episode):02d}."
        f"{resolution}.{source_token}.x264-{RELEASE_GROUP}"
    )


def _season_release_title(name, season, resolution, source_token) -> str:
    return (
        f"{_dots(name)}.S{int(season):02d}."
        f"{resolution}.{source_token}.x264-{RELEASE_GROUP}"
    )


def _movie_release_title(name, year, resolution, source_token) -> str:
    parts = [_dots(name)]
    if year:
        parts.append(year)
    parts.extend([resolution, source_token, "x264"])
    return ".".join(parts) + f"-{RELEASE_GROUP}"
