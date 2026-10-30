# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# witanime.site - Arabic-hardsub anime. The blogger host witanime.net is only a
# landing page; the catalog and the download gate live on witanime.site.
#
# Verified page contract (live captures, 2026-10-03):
#   search   GET /search?q=<query>              -> a[data-preview] cards,
#                                                 img[alt] is the romaji title
#   anime    /anime/<slug>                      -> a[href="/watch/<slug>/<n>"]
#   watch    /watch/<slug>/<n>                  -> meta csrf-token
#   sources  POST /watch/<slug>/<n>/sources     -> {"downloads": {"FHD": [
#                                                 {"token", "label", "version"}]}}
#   file     POST /watch/download-source/<tok>  -> {"ok": true}
#            GET  /watch/download-gate/<tok>    -> 302 to mediafire/workupload/mp4upload
#
# Episode numbers on a series page are local to that page (a split season is
# its own /anime/<slug>, numbered from 1). Season 2+ searches are ignored:
# answering S06E02 with local episode 2 of the first cour is worse than
# returning nothing. Season 1 is answered, because that is how Sonarr asks
# for a single-cour show typed as a standard series.

import re
import time
from datetime import datetime
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from quasarr.constants import (
    FEED_REQUEST_TIMEOUT_SECONDS,
    SEARCH_CAT_MOVIES,
    SEARCH_CAT_SHOWS,
    SEARCH_CAT_SHOWS_ANIME,
    SEARCH_REQUEST_TIMEOUT_SECONDS,
)
from quasarr.providers import sonarr_api
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

AR_DUBBED = "مدبلج"
RELEASE_GROUP = "WITA"
MAX_SEARCH_CANDIDATES = 4
MAX_FEED_ITEMS = 6

# Manifest quality key -> the token Sonarr's parser expects.
RES_FOR_QUALITY = {
    "4K": "2160p",
    "FHD": "1080p",
    "HD": "720p",
    "SD": "480p",
}


class Source(AbstractSearchSource):
    initials = "wt"
    language = "en"
    supports_imdb = True
    supports_phrase = True
    supports_absolute_numbering = True
    supports_date_numbering = False
    supported_categories = [SEARCH_CAT_SHOWS, SEARCH_CAT_SHOWS_ANIME]

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

        # Local episode numbers. S02+ would answer with that number from the
        # first cour (JoJo S06E02 -> episode 2 of Phantom Blood). Season 1 is
        # safe: for a single-cour show S01E12 is episode 12, and Sonarr sends
        # exactly that for series typed "standard" (Akane-banashi).
        season_num = _season_number(season)
        if season_num not in (None, 1):
            debug(
                f"[{self.initials}] skipping season-relative search "
                f"S{season}E{episode}"
            )
            return []

        if get_base_search_category_id(search_category) == SEARCH_CAT_MOVIES:
            return []

        imdb_id = is_imdb_id(search_string)
        if imdb_id:
            queries = _title_candidates(shared_state, imdb_id, self.language, search_category)
            # Scene-absolute mapping only applies to absolute searches. A
            # season-1 request is already the local episode number.
            if episode is not None and season_num is None:
                episode = _canonical_absolute_episode(shared_state, imdb_id, episode)
        else:
            queries = [search_string]
        queries = _with_spelling_variants(queries)
        if not queries:
            debug(f"[{self.initials}] could not resolve a title for {search_string}")
            return []

        try:
            for query in queries:
                releases = self._search_one(
                    shared_state, host, query, episode, imdb_id if imdb_id else None
                )
                if releases:
                    clear_hostname_issue(self.initials)
                    return releases
            return []
        except Exception as exc:
            mark_hostname_issue(self.initials, "search", str(exc))
            return []

    def feed(self, shared_state, start_time: float, search_category: str) -> list[SearchRelease]:
        host = _host(shared_state)
        if not host:
            return []
        try:
            soup = _fetch(shared_state, f"https://{host}/", FEED_REQUEST_TIMEOUT_SECONDS)
            if soup is None:
                mark_hostname_issue(self.initials, "feed", "no response from front page")
                return []
            watches = []
            seen = set()
            for anchor in soup.find_all("a", href=True):
                number, slug = _watch_path(anchor["href"])
                if number is None or slug in seen:
                    continue
                # Homepage repeats a series; keep the first (newest) only.
                seen.add(slug)
                watches.append(urljoin(f"https://{host}/", anchor["href"]))
                if len(watches) >= MAX_FEED_ITEMS:
                    break
            releases = []
            for watch_url in watches:
                if time.time() - start_time > FEED_REQUEST_TIMEOUT_SECONDS:
                    debug(f"[{self.initials}] feed budget exhausted")
                    break
                page = _fetch(shared_state, watch_url, FEED_REQUEST_TIMEOUT_SECONDS)
                if page is None:
                    continue
                title = _series_title(page)
                number, _slug = _watch_path(watch_url)
                if not title or number is None:
                    continue
                resolutions = _resolutions(shared_state, watch_url)
                releases.extend(
                    self._releases(
                        shared_state, title, number, watch_url, resolutions, None
                    )
                )
            if releases:
                clear_hostname_issue(self.initials)
            return releases
        except Exception as exc:
            mark_hostname_issue(self.initials, "feed", str(exc))
            return []

    def _search_one(self, shared_state, host, query, episode, imdb_id):
        url = f"https://{host}/search?q={quote_plus(query)}"
        soup = _fetch(shared_state, url, SEARCH_REQUEST_TIMEOUT_SECONDS)
        if soup is None:
            mark_hostname_issue(self.initials, "search", f"no response from {url}")
            return []

        cards = [c for c in _parse_cards(soup) if not _is_dubbed(c["title"], c["url"])]
        matches = [c for c in cards if _title_matches(query, c["title"])]
        ordered = sorted(matches or [], key=lambda c: _rank(query, c))[:MAX_SEARCH_CANDIDATES]
        if not ordered:
            return []

        for card in ordered:
            page = _fetch(shared_state, card["url"], SEARCH_REQUEST_TIMEOUT_SECONDS)
            if page is None:
                continue
            slug = _anime_slug(card["url"])
            entries = _watch_entries(page, slug, host)
            if not entries:
                continue
            if episode is None:
                number, watch_url = max(entries, key=lambda item: item[0])
            else:
                found = [item for item in entries if item[0] == int(episode)]
                if not found:
                    continue
                number, watch_url = found[0]
            watch_page = _fetch(shared_state, watch_url, SEARCH_REQUEST_TIMEOUT_SECONDS)
            if watch_page is None:
                continue
            title = _series_title(watch_page) or card["title"]
            resolutions = _resolutions(shared_state, watch_url)
            releases = self._releases(
                shared_state, title, number, watch_url, resolutions, imdb_id
            )
            if releases:
                return releases
        return []

    def _releases(self, shared_state, title, number, watch_url, resolutions, imdb_id):
        if not title or not resolutions:
            return []
        now = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")
        releases = []
        for resolution in sorted(resolutions, key=_res_key, reverse=True):
            release_title = _episode_release_title(title, int(number), resolution)
            if "|" in release_title:
                continue
            link = generate_download_link(
                shared_state,
                release_title,
                watch_url,
                0,
                "",
                imdb_id,
                self.initials,
            )
            releases.append(
                {
                    "details": {
                        "title": release_title,
                        "hostname": self.initials,
                        "imdb_id": imdb_id,
                        "link": link,
                        "size": 0,
                        "date": now,
                        "source": watch_url,
                    },
                    "type": "protected",
                }
            )
        return releases


def _season_number(season):
    if season in (None, "", 0, "0"):
        return None
    try:
        return int(season)
    except (TypeError, ValueError):
        return None


def _host(shared_state):
    return shared_state.values["config"]("Hostnames").get(Source.initials)


def _fetch(shared_state, url: str, timeout: int):
    response = requests.get(
        url,
        headers={"User-Agent": shared_state.values["user_agent"]},
        timeout=timeout,
    )
    if response.status_code != 200:
        return None
    return BeautifulSoup(response.content, "html.parser")


def _session(shared_state):
    session = requests.Session()
    session.headers["User-Agent"] = shared_state.values["user_agent"]
    return session


def _csrf(page) -> str:
    tag = page.select_one('meta[name="csrf-token"]')
    return (tag.get("content") or "").strip() if tag else ""


def _resolutions(shared_state, watch_url: str) -> set:
    """Qualities that actually have a subtitled download, not just a player.

    The CSRF token is bound to the session cookie, so the GET and the POST
    have to share one session. A token parsed from a different response 419s.
    """
    session = _session(shared_state)
    opened = session.get(watch_url, timeout=SEARCH_REQUEST_TIMEOUT_SECONDS)
    if opened.status_code != 200:
        return set()
    csrf = _csrf(BeautifulSoup(opened.content, "html.parser"))
    if not csrf:
        return set()
    response = session.post(
        watch_url.rstrip("/") + "/sources",
        headers={
            "Accept": "application/json",
            "X-CSRF-TOKEN": csrf,
            "Referer": watch_url,
        },
        timeout=SEARCH_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        debug(f"[wt] sources {response.status_code} for {watch_url}")
        return set()
    try:
        payload = response.json()
    except Exception:
        return set()
    found = set()
    for quality, items in (payload.get("downloads") or {}).items():
        resolution = RES_FOR_QUALITY.get(quality)
        if not resolution:
            continue
        for item in items or []:
            if (item.get("version") or "sub") != "sub":
                continue
            found.add(resolution)
            break
    return found


def _parse_cards(soup) -> list[dict]:
    cards = []
    seen = set()
    for anchor in soup.select("a[data-preview]"):
        href = (anchor.get("href") or "").strip()
        if "/anime/" not in href:
            continue
        image = anchor.find("img")
        title = (image.get("alt") if image else "") or anchor.get_text(" ", strip=True)
        title = re.sub(r"\s+", " ", title).strip()
        if not title or href in seen:
            continue
        seen.add(href)
        cards.append({"title": title, "url": href})
    return cards


def _watch_path(href: str):
    path = urlparse(href).path.rstrip("/")
    match = re.search(r"/watch/([a-z0-9-]+)/(\d+)$", path)
    if not match:
        return None, None
    return int(match.group(2)), match.group(1)


def _anime_slug(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2 and parts[-2] == "anime":
        return parts[-1]
    if parts and parts[0] == "anime" and len(parts) >= 2:
        return parts[1]
    return ""


def _watch_entries(page, slug: str, host: str) -> list:
    if not slug:
        return []
    found = {}
    for anchor in page.find_all("a", href=True):
        number, link_slug = _watch_path(anchor["href"])
        if number is None or link_slug != slug or number in found:
            continue
        found[number] = urljoin(f"https://{host}/", anchor["href"])
    return list(found.items())


def _series_title(page) -> str:
    for anchor in page.find_all("a", href=True):
        if "/anime/" not in anchor["href"] or "/watch/" in anchor["href"]:
            continue
        text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        if text and AR_DUBBED not in text:
            return text
    if page.title and page.title.string:
        return re.sub(r"\s+", " ", page.title.string).strip()
    return ""


def _is_dubbed(title: str, url: str) -> bool:
    blob = f"{title} {url}".lower()
    return AR_DUBBED in (title or "") or "dubbed" in blob or "-dub" in blob or "/dub" in blob


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
    return bool(q_tokens) and q_tokens.issubset(set(c.split()))


def _rank(query: str, card: dict) -> tuple:
    q = _normalize(query)
    c = _normalize(card["title"])
    extra = len(set(c.split()) - set(q.split()))
    exact = 0 if q == c else 1
    return (exact, extra, len(c))


def _with_spelling_variants(queries: list) -> list:
    """Sonarr spells 二十世紀 'Nijuuseiki'; the site indexes 'Nijusseiki'."""
    seen = set()
    out = []
    for query in queries:
        variants = [query]
        if query and re.search(r"nijuuseiki", query, re.I) and not re.search(r"nijusseiki", query, re.I):
            variants.append(re.sub(r"nijuuseiki", "Nijusseiki", query, flags=re.I))
        for variant in variants:
            key = _normalize(variant)
            if variant and key and key not in seen:
                seen.add(key)
                out.append(variant)
    return out


def _title_candidates(shared_state, imdb_id, language, search_category):
    candidates = []
    localized = get_localized_title(shared_state, imdb_id, language, search_category)
    if localized:
        candidates.append(localized)
    try:
        client = sonarr_api.get_client(shared_state)
        library = client._get("/series") if client else None
        for record in library or []:
            if record.get("imdbId") != imdb_id:
                continue
            candidates.append(record.get("title"))
            candidates.append(record.get("originalTitle"))
            for alternate in record.get("alternateTitles") or []:
                if isinstance(alternate, dict):
                    candidates.append(alternate.get("title"))
                elif isinstance(alternate, str):
                    candidates.append(alternate)
            break
    except Exception as exc:
        debug(f"[wt] alternate title lookup failed: {exc}")
    return [c.strip() for c in candidates if (c or "").strip()]


def _canonical_absolute_episode(shared_state, imdb_id, requested_episode):
    """Scene-absolute (Sonarr's q=) -> the canonical absolute Sonarr stores.

    Copied from the anime4up adapter: with useSceneNumbering, Sonarr searches
    the scene absolute, which for a new cour is the cour-local number.
    """
    try:
        client = sonarr_api.get_client(shared_state)
        if not client:
            return requested_episode
        series = next(
            (
                record
                for record in client._get("/series") or []
                if record.get("imdbId") == imdb_id
            ),
            None,
        )
        if not series or not series.get("useSceneNumbering") or not series.get("id"):
            return requested_episode
        episodes = client._get("/episode", params={"seriesId": series["id"]}) or []
        matches = [
            ep
            for ep in episodes
            if ep.get("sceneAbsoluteEpisodeNumber") == requested_episode
            and ep.get("absoluteEpisodeNumber")
        ]
        if not matches:
            return requested_episode
        matches.sort(key=lambda ep: ep.get("lastSearchTime") or "", reverse=True)
        absolute = matches[0]["absoluteEpisodeNumber"]
        if absolute != requested_episode:
            debug(f"[wt] scene absolute {requested_episode} -> canonical {absolute}")
        return absolute
    except Exception as exc:
        debug(f"[wt] absolute-number lookup failed: {exc}")
        return requested_episode


def _episode_release_title(series: str, episode: int, resolution: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", series or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return f"[{RELEASE_GROUP}] {cleaned} - {episode:03d} [{resolution}][WEB-DL][x264]"


def _res_key(resolution: str) -> int:
    match = re.search(r"(\d{3,4})", resolution or "")
    return int(match.group(1)) if match else 0
