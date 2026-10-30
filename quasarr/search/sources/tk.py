# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# takkiadrama.asia - Arabic-hardsub Korean dramas, file-host downloads.
#
# Verified page contract (live captures, 2026-10-05):
#   search   GET /?s=<query>                 -> a.drama-card, title attr is
#                                              "مسلسل <arabic> <English> الموسم N ..."
#   series   /series/<slug>/                 -> onclick episode URLs
#   episode  /<slug-with-الموسم-N-الحلقة-M>/ -> a[href$="/downloads/"]
#   files    <episode>/downloads/            -> a.dw hoster links, quality badge
#                                              "1080P" / "720P"

import re
from datetime import datetime
from urllib.parse import quote_plus, unquote

import requests
from bs4 import BeautifulSoup

from quasarr.constants import (
    SEARCH_CAT_MOVIES,
    SEARCH_CAT_SHOWS,
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
from quasarr.search.sources.helpers.search_source import AbstractSearchSource

AR_DUBBED = "مدبلج"
AR_SEASON = "الموسم"
AR_EPISODE = "الحلقة"
RELEASE_GROUP = "Takia"
SOURCE_TOKEN = "WEB-DL"

ORDINALS = {
    "الاول": 1,
    "الأول": 1,
    "الاولى": 1,
    "الأولى": 1,
    "الثاني": 2,
    "الثانية": 2,
    "الثالث": 3,
    "الثالثة": 3,
    "الرابع": 4,
    "الرابعة": 4,
    "الخامس": 5,
    "الخامسة": 5,
    "السادس": 6,
    "السادسة": 6,
    "السابع": 7,
    "السابعة": 7,
    "الثامن": 8,
    "الثامنة": 8,
    "التاسع": 9,
    "التاسعة": 9,
    "العاشر": 10,
    "العاشرة": 10,
}


class Source(AbstractSearchSource):
    initials = "tk"
    language = "en"
    supports_imdb = True
    supports_phrase = True
    supports_absolute_numbering = False
    supports_date_numbering = False
    supported_categories = [SEARCH_CAT_SHOWS]

    def search(
        self,
        shared_state,
        start_time: float,
        search_category: str,
        search_string: str = "",
        season: int = None,
        episode: int = None,
    ) -> list:
        host = _host(shared_state)
        if not host or not search_string:
            return []
        if get_base_search_category_id(search_category) == SEARCH_CAT_MOVIES:
            return []
        season_num = _as_int(season)
        episode_num = _as_int(episode)
        if not season_num or not episode_num:
            return []

        if is_imdb_id(search_string):
            query = get_localized_title(
                shared_state, search_string, self.language, search_category
            )
            imdb_id = search_string
        else:
            query = search_string
            imdb_id = None
        if not query:
            debug(f"[{self.initials}] no title for {search_string}")
            return []

        try:
            releases = self._search_one(
                shared_state, host, query, season_num, episode_num, imdb_id
            )
            if releases:
                clear_hostname_issue(self.initials)
            return releases
        except Exception as exc:
            mark_hostname_issue(self.initials, "search", str(exc))
            return []

    def feed(self, shared_state, start_time: float, search_category: str) -> list:
        return []

    def _search_one(self, shared_state, host, query, season, episode, imdb_id):
        url = f"https://{host}/?s={quote_plus(query)}"
        soup = _fetch(shared_state, url)
        if soup is None:
            mark_hostname_issue(self.initials, "search", f"no response from {url}")
            return []

        card = _pick_card(soup, query, season)
        if card is None:
            return []
        series = _fetch(shared_state, card["url"])
        if series is None:
            return []
        episode_url = _episode_url(series, season, episode)
        if not episode_url:
            return []
        files = _fetch(shared_state, episode_url.rstrip("/") + "/downloads/")
        resolutions = _resolutions(files) if files is not None else {"1080p"}
        if not resolutions:
            resolutions = {"1080p"}

        now = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")
        releases = []
        for resolution in sorted(resolutions, key=_res_key, reverse=True):
            title = _episode_title(card["name"], season, episode, resolution)
            if "|" in title:
                continue
            link = generate_download_link(
                shared_state, title, episode_url, 0, "", imdb_id, self.initials
            )
            releases.append(
                {
                    "details": {
                        "title": title,
                        "hostname": self.initials,
                        "imdb_id": imdb_id,
                        "link": link,
                        "size": 0,
                        "date": now,
                        "source": episode_url,
                    },
                    "type": "protected",
                }
            )
        return releases


def _host(shared_state):
    return shared_state.values["config"]("Hostnames").get(Source.initials)


def _fetch(shared_state, url: str):
    response = requests.get(
        url,
        headers={"User-Agent": shared_state.values["user_agent"]},
        timeout=SEARCH_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        return None
    return BeautifulSoup(response.content, "html.parser")


def _pick_card(soup, query: str, season: int):
    for anchor in soup.select("a.drama-card"):
        href = (anchor.get("href") or "").strip()
        raw = anchor.get("title") or anchor.get_text(" ", strip=True)
        if "/series/" not in href or AR_DUBBED in raw:
            continue
        name = _english_name(raw) or _english_name(unquote(href))
        card_season = _season_of(raw) or _season_of(unquote(href))
        if not name or not _same_title(query, name):
            continue
        if card_season != season:
            continue
        return {"name": name, "url": href, "season": card_season}
    return None


def _episode_url(page, season: int, episode: int):
    seen = set()
    for raw in re.findall(r"https://takkiadrama\.asia/[^'\"\s>]+", str(page)):
        decoded = unquote(raw)
        if (decoded, episode) in seen:
            continue
        got_season = _season_of(decoded)
        got_episode = _episode_of(decoded)
        if got_episode != episode:
            continue
        if got_season not in (None, season):
            continue
        seen.add((decoded, episode))
        return decoded.split("?")[0]
    return None


def _resolutions(page) -> set:
    found = set()
    for anchor in page.select("a.dw"):
        badge = anchor.select_one(".quality-badge")
        text = badge.get_text(" ", strip=True) if badge else anchor.get_text(" ", strip=True)
        match = re.search(r"(\d{3,4})\s*p", text, re.I)
        if match and anchor.get("href"):
            found.add(f"{match.group(1)}p")
    return found


def _english_name(text: str) -> str:
    cut = re.split(AR_SEASON, text or "", maxsplit=1)[0]
    cut = re.split(r"مترجم", cut, maxsplit=1)[0]
    cut = re.sub(r"^مسلسل\s*", "", cut).strip()
    runs = re.findall(r"[A-Za-z0-9]+(?:[ '&:+.-][A-Za-z0-9]+)*", cut)
    if not runs:
        runs = re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", unquote(text or ""))
    if not runs:
        return ""
    name = max(runs, key=len).replace("-", " ")
    return re.sub(r"\s+", " ", name).strip()


def _season_of(text: str):
    decoded = unquote(text or "")
    match = re.search(re.escape(AR_SEASON) + r"[\s\-]*(\d+)", decoded)
    if match:
        return int(match.group(1))
    match = re.search(r"season-(\d+)", decoded, re.I)
    if match:
        return int(match.group(1))
    ordinal = "|".join(sorted((re.escape(k) for k in ORDINALS), key=len, reverse=True))
    match = re.search(re.escape(AR_SEASON) + r"[\s\-]+(" + ordinal + ")", decoded)
    if match:
        return ORDINALS.get(match.group(1))
    return None


def _episode_of(text: str):
    match = re.search(re.escape(AR_EPISODE) + r"[\s\-]*(\d+)", unquote(text or ""))
    return int(match.group(1)) if match else None


def _same_title(query: str, name: str) -> bool:
    return _norm(query) == _norm(name) and bool(_norm(query))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _res_key(resolution: str) -> int:
    match = re.search(r"(\d{3,4})", resolution or "")
    return int(match.group(1)) if match else 0


def _episode_title(name: str, season: int, episode: int, resolution: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", name or "")
    dotted = re.sub(r"\s+", ".", cleaned).strip(".")
    return (
        f"{dotted}.S{int(season):02d}E{int(episode):02d}."
        f"{resolution}.{SOURCE_TOKEN}.x264-{RELEASE_GROUP}"
    )
