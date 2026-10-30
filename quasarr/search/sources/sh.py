# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# shahiid-anime - Arabic-subtitled anime, direct download links.
#
# Subbed content only. The site keeps Arabic-dubbed releases in a parallel tree
# (/seriesDubbed, /seasonsDubbed, /episodesDubbed) and tags them مدبلج; both the
# URL and the label are checked so dubs never reach Sonarr.
#
# Verified page contract (live captures, 2026-08-02):
#   search    GET /?s=<query>        -> div.one-poster cards linking to
#                                       /seasons/<slug>/ (series) or
#                                       /anime/<slug>/ (movies)
#   season    /seasons/<slug>/       -> ~20 newest /episodes/ links, NO
#                                       pagination, so older episodes are not
#                                       reachable by walking the page
#   episode   /episodes/<slug>/      -> a.btn-download-eps -> /?download=<id>
#   download  /?download=<id>        -> a.btnsdownserver, quality carried in the
#                                       anchor text as FHD / HD / SD
#
# Because the season page is capped and the site's search does not index episode
# posts at all, arbitrary episodes are reached by rewriting the episode number
# into a slug pattern observed on the season page and verifying the result with
# a real request before emitting anything.

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote_plus, unquote, urljoin

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
from quasarr.search.sources.helpers.a4u_hints import (
    hint_episode_url,
    hint_titles,
    publish_site,
    relabel_releases,
    sonarr_titles,
)
from quasarr.search.sources.helpers.search_release import SearchRelease
from quasarr.search.sources.helpers.search_source import AbstractSearchSource

AR_EPISODE = "الحلقة"  # "episode"
AR_DUBBED = "مدبلج"  # "dubbed" - excluded
AR_MOVIE = "فيلم"  # "film"

EPISODE_IN_TEXT = re.compile(re.escape(AR_EPISODE) + r"\s*(\d+)")
DIGITS_IN_SLUG = re.compile(r"(\d+)")
YEAR_PATTERN = re.compile(r"(19|20)\d{2}")

# Quality tokens as they appear in the download anchor text. FHD is tested
# before HD because the substring would otherwise match first.
QUALITY_TOKENS = (
    ("fhd", "1080p"),
    ("1080", "1080p"),
    ("hd", "720p"),
    ("720", "720p"),
    ("sd", "480p"),
    ("480", "480p"),
)

MAX_SEARCH_CANDIDATES = 4
# Each query costs a search page plus up to MAX_SEARCH_CANDIDATES entry pages;
# Sonarr's indexer timeout bounds how many fallbacks fit.
MAX_TITLE_QUERIES = 3
MAX_FEED_ITEMS = 12
FEED_FETCH_WORKERS = 6

RELEASE_GROUP = "SHAH"


class Source(AbstractSearchSource):
    initials = "sh"
    language = "en"
    # No IMDb data on the site; resolve the ID to a title instead of letting
    # Quasarr skip this source for ID-based searches (see a4.py for the same).
    supports_imdb = True
    supports_phrase = True
    supports_absolute_numbering = True
    supports_date_numbering = False
    # Shows only. Verified 2026-08-02: /anime/<slug>/ movie pages carry no
    # a.btn-download-eps and no episode links, so there is no download path for
    # movies on this site - advertising SEARCH_CAT_MOVIES would only produce
    # releases Radarr could never fetch.
    supported_categories = [SEARCH_CAT_SHOWS, SEARCH_CAT_SHOWS_ANIME]

    # ------------------------------------------------------------------ #

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

        want_movie = get_base_search_category_id(search_category) == SEARCH_CAT_MOVIES

        # See a4.py: the site is absolute-numbered, so answering Sonarr's
        # season-relative search shape would return a real but wrong episode
        # (S17E41 -> episode 41 instead of absolute 407). Absolute only.
        # Truthiness, matching Quasarr's own dispatch (`if episode and not
        # season`): an absolute search can arrive with season as 0 or "".
        if season:
            debug(
                f"[{self.initials}] skipping season-relative search "
                f"S{season}E{episode} - source is absolute-numbered"
            )
            return []

        publish_site(shared_state, self.initials, host)
        imdb_id = is_imdb_id(search_string)
        if imdb_id:
            localized = get_localized_title(
                shared_state, imdb_id, self.language, search_category
            )
            queries = _dedupe(
                hint_titles(shared_state, imdb_id, self.initials)
                + ([localized] if localized else [])
                + sonarr_titles(shared_state, imdb_id)
            )[:MAX_TITLE_QUERIES]
            if not queries:
                debug(f"[{self.initials}] no title for {imdb_id}")
                return []
        else:
            queries = [search_string]

        try:
            releases = []
            if imdb_id and episode is not None:
                releases = self._hinted_episode(
                    shared_state, host, imdb_id, episode, queries[0]
                )
            for query in queries:
                if releases:
                    break
                releases = self._search_one(
                    shared_state, host, query, want_movie, episode
                )
            if not releases:
                return []
            clear_hostname_issue(self.initials)
            if imdb_id:
                releases = relabel_releases(
                    shared_state, releases, imdb_id, self.initials
                )
            return releases
        except Exception as exc:
            mark_hostname_issue(self.initials, "search", str(exc))
            return []

    def _hinted_episode(self, shared_state, host, imdb_id, episode, label):
        url = hint_episode_url(shared_state, imdb_id, self.initials, episode, host)
        if not url:
            return []
        return self._releases_from_episode(
            shared_state,
            url,
            _series_title(label),
            episode,
            None,
            False,
            SEARCH_REQUEST_TIMEOUT_SECONDS,
        )

    def _search_one(self, shared_state, host, query, want_movie, episode):
        url = f"https://{host}/?s={quote_plus(query)}"
        soup = _fetch(shared_state, url, SEARCH_REQUEST_TIMEOUT_SECONDS)
        if soup is None:
            mark_hostname_issue(self.initials, "search", f"no response from {url}")
            return []

        cards = [c for c in _parse_posters(soup) if not _is_dubbed(c["url"], c["text"])]
        wanted = [c for c in cards if c["is_movie"] == want_movie]
        matches = [c for c in wanted if _title_matches(query, c["text"])]
        # Ranking matters more than filtering here: a query like "One Piece"
        # returns specials, arcs and movies that all legitimately contain the
        # query tokens, and the real series is not first. Prefer /seasons/
        # (the verified series container) and the fewest extra title tokens.
        ordered = sorted(
            matches or wanted, key=lambda c: _rank(query, c)
        )[:MAX_SEARCH_CANDIDATES]

        releases: list[SearchRelease] = []
        for card in ordered:
            releases.extend(
                self._releases_for_entry(
                    shared_state,
                    card,
                    want_movie=want_movie,
                    episode=episode,
                    timeout=SEARCH_REQUEST_TIMEOUT_SECONDS,
                )
            )

        # Several season entries can match one query (main series, arc
        # re-cuts), each yielding an identical release title.
        return _dedupe_by_title(releases)

    def feed(
        self,
        shared_state,
        start_time: float,
        search_category: str,
    ) -> list[SearchRelease]:
        host = _host(shared_state)
        if not host:
            return []
        if get_base_search_category_id(search_category) == SEARCH_CAT_MOVIES:
            return []

        try:
            soup = _fetch(shared_state, f"https://{host}/", FEED_REQUEST_TIMEOUT_SECONDS)
            if soup is None:
                mark_hostname_issue(self.initials, "feed", "no response from front page")
                return []

            recent = []
            for card in _parse_posters(soup):
                if _is_dubbed(card["url"], card["text"]) or "/episodes/" not in card["url"]:
                    continue
                number = _episode_number(card["text"], card["url"])
                if number is None:
                    continue
                recent.append((card, number))
                if len(recent) >= MAX_FEED_ITEMS:
                    break
            if not recent:
                return []

            releases: list[SearchRelease] = []
            with ThreadPoolExecutor(max_workers=FEED_FETCH_WORKERS) as pool:
                futures = {
                    pool.submit(
                        self._releases_from_episode,
                        shared_state,
                        card["url"],
                        _series_title(card["text"]),
                        number,
                        None,
                        False,
                        FEED_REQUEST_TIMEOUT_SECONDS,
                    ): card
                    for card, number in recent
                }
                for future in futures:
                    if time.time() - start_time > FEED_REQUEST_TIMEOUT_SECONDS:
                        debug(f"[{self.initials}] feed budget exhausted")
                        break
                    try:
                        releases.extend(future.result() or [])
                    except Exception:
                        continue

            if releases:
                clear_hostname_issue(self.initials)
            return releases
        except Exception as exc:
            mark_hostname_issue(self.initials, "feed", str(exc))
            return []

    # ------------------------------------------------------------------ #

    def _releases_for_entry(
        self, shared_state, card: dict, want_movie: bool, episode: int, timeout: int
    ):
        if want_movie:
            # Unreachable: the source does not advertise SEARCH_CAT_MOVIES.
            return []

        page = _fetch(shared_state, card["url"], timeout)
        if page is None:
            return []

        title = _series_title(_page_title(page) or card["text"])
        year = _year(page)

        target = _resolve_episode_url(shared_state, card["url"], page, episode, timeout)
        if not target:
            return []
        number = episode if episode is not None else _episode_number("", target)
        return self._releases_from_episode(
            shared_state, target, title, number, year, False, timeout
        )

    def _releases_from_episode(
        self,
        shared_state,
        episode_url: str,
        series_title: str,
        episode_number,
        year,
        as_movie: bool,
        timeout: int,
    ):
        page = _fetch(shared_state, episode_url, timeout)
        if page is None:
            return []

        download_url = _download_page_url(page, episode_url)
        if not download_url:
            return []
        download_page = _fetch(shared_state, download_url, timeout)
        if download_page is None:
            return []

        resolutions = {
            r["resolution"] for r in _download_rows(download_page) if r["resolution"]
        }
        if not resolutions or not series_title:
            return []
        if not as_movie and episode_number is None:
            return []

        now = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")
        releases = []
        for resolution in sorted(resolutions, key=_res_key, reverse=True):
            if as_movie:
                title = _movie_title(series_title, year, resolution)
            else:
                title = _episode_title(series_title, int(episode_number), resolution)
            if "|" in title:
                continue
            # The download twin needs the download page, not the episode page.
            link = generate_download_link(
                shared_state, title, download_url, 0, "", None, self.initials
            )
            releases.append(
                {
                    "details": {
                        "title": title,
                        "hostname": self.initials,
                        "imdb_id": None,
                        "link": link,
                        "size": 0,
                        "date": now,
                        "source": download_url,
                    },
                    "type": "protected",
                }
            )
        return releases


# ---------------------------------------------------------------------- #
# module-private helpers
# ---------------------------------------------------------------------- #


def _host(shared_state):
    return shared_state.values["config"]("Hostnames").get(Source.initials)


def _fetch(shared_state, url: str, timeout: int):
    r = requests.get(
        url,
        headers={"User-Agent": shared_state.values["user_agent"]},
        timeout=timeout,
    )
    if r.status_code != 200:
        return None
    return BeautifulSoup(r.content, "html.parser")


def _dedupe(items):
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _is_dubbed(url: str, text: str) -> bool:
    return "Dubbed" in (url or "") or AR_DUBBED in (text or "")


def _parse_posters(soup) -> list[dict]:
    cards = []
    for poster in soup.select(".one-poster"):
        a = poster.select_one("a[href]")
        if not a:
            continue
        href = a["href"]
        if not any(p in href for p in ("/seasons/", "/anime/", "/episodes/")):
            continue
        if href.rstrip("/").split("/")[-1] in ("seasons", "anime", "episodes"):
            continue
        text = poster.get_text(" ", strip=True)
        cards.append(
            {
                "url": href,
                "text": text,
                "is_movie": "/anime/" in href or AR_MOVIE in text,
            }
        )
    return cards


def _normalize(text: str) -> str:
    # Drop Arabic so romaji/English tokens drive the comparison.
    ascii_only = re.sub(r"[^\x00-\x7F]+", " ", text or "")
    return re.sub(r"[^a-z0-9]+", " ", ascii_only.lower()).strip()


def _title_matches(query: str, candidate: str) -> bool:
    q, c = _normalize(query), _normalize(candidate)
    if not q or not c:
        return False
    if q in c or c in q:
        return True
    q_tokens = set(q.split())
    return bool(q_tokens) and q_tokens.issubset(set(c.split()))


def _dedupe_by_title(releases: list) -> list:
    seen, out = set(), []
    for release in releases:
        title = release["details"]["title"]
        if title in seen:
            continue
        seen.add(title)
        out.append(release)
    return out


def _rank(query: str, card: dict) -> tuple:
    """Sort key for search candidates - lower is better."""
    q_tokens = set(_normalize(query).split())
    c_tokens = set(_normalize(card["text"]).split())
    extra = len(c_tokens - q_tokens)
    # /seasons/ is the series container; /anime/ holds movies and one-offs.
    not_a_season = 0 if "/seasons/" in unquote(card["url"]) else 1
    return (not_a_season, extra)


def _series_title(text: str) -> str:
    """Cards mix Arabic and romaji; keep the romaji run and drop the chrome."""
    ascii_only = re.sub(r"[^\x00-\x7F]+", " ", text or "")
    ascii_only = re.sub(
        r"\b(TV|OVA|ONA|Special|Movie|\d+\.\d+|مترجمة?|اونلاين)\b", " ", ascii_only
    )
    ascii_only = re.sub(r"\b\d{1,4}\b\s*$", " ", ascii_only)
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", ascii_only)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Card labels leave dangling separators behind once the Arabic is stripped
    # ("One Piece +"), and Sonarr will not match a series title with one.
    return re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9)\]]+$", "", cleaned).strip()


def _page_title(page) -> str:
    return page.h1.get_text(strip=True) if page.h1 else ""


def _year(page) -> str:
    m = YEAR_PATTERN.search(page.get_text(" ", strip=True)[:4000])
    return m.group(0) if m else ""


def _episode_number(text: str, url: str):
    m = EPISODE_IN_TEXT.search(text or "")
    if m:
        return int(m.group(1))
    slug = unquote(url or "").rstrip("/").split("/")[-1]
    m2 = EPISODE_IN_TEXT.search(slug)
    if m2:
        return int(m2.group(1))
    nums = DIGITS_IN_SLUG.findall(slug)
    return int(nums[-1]) if nums else None


def _season_episode_links(page) -> list:
    out = []
    for a in page.find_all("a", href=True):
        href = a["href"]
        if "/episodes/" not in href:
            continue
        if href.rstrip("/").split("/")[-1] == "episodes":
            continue
        text = a.get_text(" ", strip=True)
        if _is_dubbed(href, text):
            continue
        out.append((_episode_number(text, href), href))
    return out


def _romaji_root(url: str) -> str:
    """Longest latin hyphenated run in a slug.

    Series slugs interleave Arabic and romaji
    ("<arabic>-detective-conan-<arabic>"); the romaji run is what episode slugs
    are built from.
    """
    slug = unquote(url or "").rstrip("/").split("/")[-1].lower()
    runs = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
    return max(runs, key=len) if runs else ""


def _candidate_urls(series_url: str, sample_url: str, episode: int) -> list[str]:
    """Build candidate episode URLs for an episode the listing does not show.

    Slug shapes are inconsistent across series, so try the verified romaji form
    first, then fall back to rewriting the number inside an observed slug. Every
    candidate is fetched and validated before use.
    """
    candidates = []

    # Verified form: "<romaji-root>-<number>" (e.g. detective-conan-1209).
    root = _romaji_root(series_url) or _romaji_root(sample_url)
    if root:
        base = unquote(sample_url or "").rstrip("/").rpartition("/")[0]
        for suffix in (f"{episode}", f"{episode:02d}", f"ep-{episode}"):
            candidates.append(f"{base}/{root}-{suffix}")

    # Fallback: rewrite the number after the Arabic episode marker, keeping the
    # zero padding width the sample used.
    decoded = unquote(sample_url or "").rstrip("/")
    base, _, slug = decoded.rpartition("/")
    m = EPISODE_IN_TEXT.search(slug) if slug else None
    if m:
        for width in (len(m.group(1)), 0):
            padded = str(episode).zfill(width) if width else str(episode)
            candidates.append(f"{base}/{slug[: m.start(1)]}{padded}{slug[m.end(1):]}")

    return _dedupe([c + "/" for c in candidates])


def _resolve_episode_url(shared_state, series_url, page, episode, timeout):
    links = _season_episode_links(page)
    if not links:
        return None

    if episode is None:
        numbered = [l for l in links if l[0] is not None]
        return (max(numbered, key=lambda x: x[0])[1] if numbered else links[0][1])

    for number, href in links:
        if number == episode:
            return href

    # Not in the capped listing - rewrite a known slug and verify by fetching.
    sample = links[0][1]
    for candidate in _candidate_urls(series_url, sample, episode):
        probe = _fetch(shared_state, candidate, timeout)
        if probe is not None and _download_page_url(probe, candidate):
            debug(f"[sh] resolved episode {episode} via slug rewrite")
            return candidate
    return None


def _download_page_url(page, page_url: str):
    btn = page.select_one("a.btn-download-eps") or page.select_one(".btn-download-eps")
    if btn is None:
        return None
    href = btn.get("href")
    if not href:
        inner = btn.find("a", href=True) or btn.find_parent("a", href=True)
        href = inner["href"] if inner else None
    return urljoin(page_url, href) if href else None


def _download_rows(page) -> list[dict]:
    rows, seen = [], set()
    for a in page.select("a.btnsdownserver, .btnsdownserver a[href]"):
        href = (a.get("href") or "").strip()
        if not href.lower().startswith("http"):
            continue
        text = a.get_text(" ", strip=True)
        rows.append(
            {
                "url": href,
                "mirror": _mirror_name(text, href),
                "resolution": _resolution_of(text),
            }
        )
    return [r for r in rows if not (r["url"] in seen or seen.add(r["url"]))]


def _mirror_name(text: str, href: str) -> str:
    ascii_only = re.sub(r"[^\x00-\x7F]+", " ", text or "").strip()
    token = re.split(r"[\s\-]+", ascii_only)[0].lower() if ascii_only else ""
    if token:
        return token
    from urllib.parse import urlparse

    return urlparse(href).netloc.lower().replace("www.", "")


def _resolution_of(text: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    for token, resolution in QUALITY_TOKENS:
        if token in lowered:
            return resolution
    return ""


def _res_key(resolution: str) -> int:
    m = re.search(r"(\d{3,4})", resolution)
    return int(m.group(1)) if m else 0


def _clean(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\\/:*?\"<>|]+", " ", title or "")).strip()


def _episode_title(series: str, episode: int, resolution: str) -> str:
    # WEB-DL keeps Sonarr from parsing these as HDTV and upgrading them away.
    return (
        f"[{RELEASE_GROUP}] {_clean(series)} - {episode:03d} "
        f"[{resolution}][WEB-DL][x264]"
    )


def _movie_title(title: str, year: str, resolution: str) -> str:
    parts = [_clean(title).replace(" ", ".")]
    if year:
        parts.append(year)
    parts.extend([resolution, "WEB-DL", "x264"])
    return ".".join(parts) + f"-{RELEASE_GROUP}"
