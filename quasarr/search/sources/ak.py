# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# akwam.ss - Arabic-hardsub series, direct mp4 on downet.net.
#
# Verified page contract (live captures, 2026-10-05):
#   search   GET /search?q=<query>          -> a.box > img[alt]
#                                            "A Shop for Killers الموسم الاول"
#   series   /series/<id>/<slug>/           -> a[href*="/episode/"]
#   episode  /episode/<id>/<slug>/الحلقة-N  -> a[href*="/download/"]
#   file     /download/<dl>/<ep>/...        -> href to *.downet.net/.../*.mp4
#
# The mp4 URL is minted per request, so search stores the episode page and
# the download twin resolves a fresh file at grab time.

import re
from datetime import datetime
from urllib.parse import quote, quote_plus, unquote, urljoin, urlsplit, urlunsplit

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
RELEASE_GROUP = "AKWAM"

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
    initials = "ak"
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
        url = f"https://{host}/search?q={quote_plus(query)}"
        soup = _fetch(shared_state, url)
        if soup is None:
            mark_hostname_issue(self.initials, "search", f"no response from {url}")
            return []
        card = _pick_card(soup, host, query, season)
        if card is None:
            return []
        series = _fetch(shared_state, card["url"])
        if series is None:
            return []
        episode_url = _episode_url(series, host, episode)
        if not episode_url:
            return []
        try:
            resolution = _resolution_of_episode(shared_state, episode_url) or "720p"
        except Exception as exc:
            debug(f"[{self.initials}] resolution lookup failed: {exc}")
            resolution = "720p"
        title = _episode_title(card["name"], season, episode, resolution, card["source"])
        if "|" in title:
            return []
        now = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")
        link = generate_download_link(
            shared_state, title, episode_url, 0, "", imdb_id, self.initials
        )
        return [
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
        ]


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


def _pick_card(soup, host: str, query: str, season: int):
    for anchor in soup.select("a.box"):
        href = urljoin(f"https://{host}/", (anchor.get("href") or "").strip())
        if "/series/" not in href:
            continue
        image = anchor.find("img")
        raw = (image.get("alt") if image else "") or anchor.get("title") or ""
        if AR_DUBBED in raw or AR_DUBBED in unquote(href):
            continue
        name = _english_name(raw) or _english_name(unquote(href))
        card_season = _season_of(raw) or _season_of(unquote(href)) or 1
        if not name or not _same_title(query, name) or card_season != season:
            continue
        box = anchor.find_parent(class_="entry-box")
        quality = ""
        if box:
            label = box.select_one("span.label.quality")
            quality = label.get_text(strip=True) if label else ""
        return {
            "name": name,
            "url": href,
            "season": card_season,
            "source": quality or "WEB-DL",
        }
    return None


def _episode_url(page, host: str, episode: int):
    for anchor in page.find_all("a", href=True):
        href = urljoin(f"https://{host}/", anchor["href"])
        if "/episode/" not in href:
            continue
        if _episode_of(unquote(href)) == episode or _episode_of(anchor.get_text(" ", strip=True)) == episode:
            return href.split("#")[0]
    return None


def _ascii_url(url: str) -> str:
    # HTTP headers are latin-1. These paths contain Arabic, so unquote then
    # quote: a raw Arabic path gets encoded, an already-encoded one stays once.
    parts = urlsplit(url)
    path = quote(unquote(parts.path), safe="/")
    query = quote(unquote(parts.query), safe="=&")
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _resolution_of_episode(shared_state, episode_url: str) -> str:
    page = _fetch(shared_state, episode_url)
    if page is None:
        return ""
    download = ""
    for anchor in page.find_all("a", href=True):
        if "/download/" in anchor["href"]:
            download = urljoin(episode_url, anchor["href"])
            break
    if not download:
        return ""
    response = requests.get(
        _ascii_url(download),
        headers={
            "User-Agent": shared_state.values["user_agent"],
            "Referer": _ascii_url(episode_url),
        },
        timeout=SEARCH_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        return ""
    # The page also contains junk like 120p and 999p in CSS. Only trust the
    # resolution baked into the mp4 filename.
    for file_url in re.findall(r"https://[^\"']+\.mp4", response.text):
        match = re.search(r"(480|720|1080|2160)p", file_url, re.I)
        if match:
            return f"{match.group(1)}p"
    return ""


def _english_name(text: str) -> str:
    cut = re.split(AR_SEASON, unquote(text or ""), maxsplit=1)[0]
    cut = re.sub(r"^مسلسل\s*", "", cut).strip(" -/")
    runs = re.findall(r"[A-Za-z0-9]+(?:[ '&:+.-][A-Za-z0-9]+)*", cut)
    if not runs:
        return ""
    return re.sub(r"\s+", " ", max(runs, key=len)).strip()


def _season_of(text: str):
    decoded = unquote(text or "")
    match = re.search(re.escape(AR_SEASON) + r"[\s\-]*(\d+)", decoded)
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
    left = re.sub(r"[^a-z0-9]+", " ", (query or "").lower()).strip()
    right = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    return bool(left) and left == right


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _episode_title(name, season, episode, resolution, source_token) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", name or "")
    dotted = re.sub(r"\s+", ".", cleaned).strip(".")
    token = re.sub(r"[^A-Za-z0-9-]+", "", source_token or "") or "WEB-DL"
    return (
        f"{dotted}.S{int(season):02d}E{int(episode):02d}."
        f"{resolution}.{token}.x264-{RELEASE_GROUP}"
    )
