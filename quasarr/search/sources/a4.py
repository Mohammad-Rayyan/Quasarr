# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# anime4up - Arabic-subtitled anime, direct download links (no CAPTCHA).
#
# The site publishes romaji/English titles (only the UI chrome is Arabic), so
# `language` is "en" and titles need no localization pass. Subtitles are burned
# into the video by the fansub groups, which is the whole point of using it.
#
# Verified page contract (live captures, 2026-08-02):
#   search      GET /?s=<query>          -> div.anime-card-container cards
#   anime page  /anime/<slug>/           -> h1 title, div.anime-info rows,
#                                           /episode/ links, WP pagination at
#                                           /anime/<slug>/page/<n>/
#   episode     /episode/<slug>/         -> div.episode-download-container
#                                           > div.download-list rows with
#                                           .server-name / .td-quality / a[href]
#   Episode slugs embed the number after the Arabic word for "episode";
#   movie slugs use the Arabic word for "film" instead and carry no number.

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

# Arabic markers used as structural anchors on the site.
AR_EPISODE = "الحلقة"  # "episode", precedes the number in episode slugs
AR_FILM = "فيلم"  # "film", marks a movie's single entry page
AR_DUBBED = "مدبلج"  # "dubbed" - marks Arabic-dub duplicates of a subbed arc
AR_TYPE_LABEL = "نوع الأنمي"  # "anime type" -> TV / Movie / OVA / ONA / Special
AR_YEAR_LABEL = "بداية العرض"  # "airing start" -> year

# The trailing boundary is not optional. Episode slugs on the site carry SEO
# junk suffixes ("الحلقة-44pss" is really episode 330) and newer slugs drop the
# number entirely ("الحلقة-smfpi"), so a number is trustworthy only when it is
# immediately followed by a non-alphanumeric character. The real number always
# lives in the listing card's .ep_num text instead - see _episode_entries.
EPISODE_NUM_PATTERN = re.compile(re.escape(AR_EPISODE) + r"[-\s]+(\d+)(?![0-9A-Za-z])")
YEAR_PATTERN = re.compile(r"(19|20)\d{2}")

# Quality cell text looks like "FHD 1080p" / "HD 720p" / "SD 480p".
RESOLUTION_PATTERN = re.compile(r"(\d{3,4})p")

# Bounded so a single search cannot walk a 25-page series to exhaustion.
MAX_EPISODE_PAGES = 30
MAX_SEARCH_CANDIDATES = 4
# Arc resolution has to consider every franchise entry (a split series can have
# a main run plus several arc pages), so it uses its own, wider cap.
MAX_FRANCHISE_CANDIDATES = 12
MAX_FEED_ITEMS = 12
FEED_FETCH_WORKERS = 6

RELEASE_GROUP = "A4U"


class Source(AbstractSearchSource):
    initials = "a4"
    language = "en"
    # The site has no IMDb data of its own, but Sonarr/Radarr search by IMDb ID
    # whenever they hold one. Declaring False makes Quasarr skip this source for
    # those queries entirely, so instead resolve the ID to a title first - the
    # documented pattern for sources without native IMDb search.
    supports_imdb = True
    supports_phrase = True
    supports_absolute_numbering = True
    supports_date_numbering = False
    supported_categories = [
        SEARCH_CAT_MOVIES,
        SEARCH_CAT_SHOWS,
        SEARCH_CAT_SHOWS_ANIME,
    ]

    # ------------------------------------------------------------------ #
    # public API
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

        base_category = get_base_search_category_id(search_category)
        want_movie = base_category == SEARCH_CAT_MOVIES

        # The site numbers episodes absolutely, with no season concept. Sonarr
        # issues two shapes for anime: an absolute search (season=None) and a
        # season-relative one. Serving the season-relative shape is actively
        # harmful - a request for Bleach S17E41 (absolute 407) would be answered
        # with episode 41, a real but completely different episode. Only the
        # absolute form is answerable here.
        # Truthiness, not `is not None`: Quasarr's dispatch treats season the
        # same way (`if episode and not season`), and an absolute search can
        # arrive with season as 0 or "" rather than None. Testing identity here
        # silently discarded valid absolute searches.
        if not want_movie and season:
            debug(
                f"[{self.initials}] skipping season-relative search "
                f"S{season}E{episode} - source is absolute-numbered"
            )
            return []

        # Sonarr/Radarr send the IMDb ID when they have one; the site only
        # indexes romaji/English titles, so trade the ID for a title first.
        imdb_id = is_imdb_id(search_string)
        if imdb_id:
            queries = _title_candidates(
                shared_state, imdb_id, self.language, search_category, want_movie
            )
            if not queries:
                debug(f"[{self.initials}] could not resolve a title for {imdb_id}")
                return []
            if not want_movie and episode is not None:
                episode = _canonical_absolute_episode(
                    shared_state, imdb_id, episode
                )
        else:
            queries = [search_string]

        try:
            for query in queries:
                releases = self._search_one(
                    shared_state, host, query, want_movie, episode
                )
                if releases:
                    clear_hostname_issue(self.initials)
                    return releases
            return []
        except Exception as exc:
            mark_hostname_issue(self.initials, "search", str(exc))
            return []

    def _search_one(self, shared_state, host, query, want_movie, episode):
        url = f"https://{host}/?s={quote_plus(query)}"
        soup = _fetch(shared_state, url, SEARCH_REQUEST_TIMEOUT_SECONDS)
        if soup is None:
            mark_hostname_issue(self.initials, "search", f"no response from {url}")
            return []

        candidates = _parse_cards(soup)
        if not candidates:
            return []

        matches = [c for c in candidates if _title_matches(query, c["title"])]
        # Fall back to raw order when nothing survives strict matching; the
        # site's own relevance ranking is usually right for exact queries.
        franchise = matches or candidates

        # A specific TV episode is answered by absolute-number resolution across
        # the whole franchise (main run + split arcs), not by taking the first
        # title match. See _resolve_tv_episode for the offset mapping.
        if not want_movie and episode is not None:
            return self._resolve_tv_episode(
                shared_state,
                franchise[:MAX_FRANCHISE_CANDIDATES],
                episode,
                SEARCH_REQUEST_TIMEOUT_SECONDS,
            )

        ordered = franchise[:MAX_SEARCH_CANDIDATES]
        releases: list[SearchRelease] = []
        for candidate in ordered:
            releases.extend(
                self._releases_for_anime(
                    shared_state,
                    host,
                    candidate,
                    want_movie=want_movie,
                    episode=episode,
                    timeout=SEARCH_REQUEST_TIMEOUT_SECONDS,
                )
            )
        return releases

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
        if base_category == SEARCH_CAT_MOVIES:
            # The front page only surfaces newly released episodes; there is no
            # movie-specific discovery feed to read.
            return []

        try:
            soup = _fetch(
                shared_state, f"https://{host}/", FEED_REQUEST_TIMEOUT_SECONDS
            )
            if soup is None:
                mark_hostname_issue(self.initials, "feed", "no response from front page")
                return []

            episode_urls = _dedupe(
                [
                    a["href"]
                    for a in soup.find_all("a", href=True)
                    if "/episode/" in a["href"]
                ]
            )[:MAX_FEED_ITEMS]
            if not episode_urls:
                return []

            releases: list[SearchRelease] = []
            with ThreadPoolExecutor(max_workers=FEED_FETCH_WORKERS) as pool:
                futures = {
                    pool.submit(
                        _fetch, shared_state, url, FEED_REQUEST_TIMEOUT_SECONDS
                    ): url
                    for url in episode_urls
                }
                for future, url in futures.items():
                    if time.time() - start_time > FEED_REQUEST_TIMEOUT_SECONDS:
                        debug(f"[{self.initials}] feed budget exhausted, stopping")
                        break
                    try:
                        page = future.result()
                    except Exception:
                        continue
                    if page is None:
                        continue
                    releases.extend(
                        self._releases_from_episode_page(
                            shared_state,
                            page,
                            url,
                            series_title=_series_title_from_episode(page),
                            episode_number=_episode_number(url),
                            year=None,
                            as_movie=False,
                        )
                    )

            if releases:
                clear_hostname_issue(self.initials)
            return releases
        except Exception as exc:
            mark_hostname_issue(self.initials, "feed", str(exc))
            return []

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _releases_for_anime(
        self,
        shared_state,
        host: str,
        candidate: dict,
        want_movie: bool,
        episode: int,
        timeout: int,
    ) -> list[SearchRelease]:
        page = _fetch(shared_state, candidate["url"], timeout)
        if page is None:
            return []

        anime_type = _anime_type(page)
        is_movie = anime_type.lower() in ("movie", "film")
        # Keep Radarr and Sonarr results disjoint.
        if want_movie != is_movie:
            return []

        title = _page_title(page) or candidate["title"]
        year = _anime_year(page)

        if is_movie:
            entry_urls = _episode_links(page)
            if not entry_urls:
                return []
            entry = entry_urls[0]
            entry_page = _fetch(shared_state, entry, timeout)
            if entry_page is None:
                return []
            return self._releases_from_episode_page(
                shared_state,
                entry_page,
                entry,
                series_title=title,
                episode_number=None,
                year=year,
                as_movie=True,
            )

        target, number = _locate_episode(
            shared_state, candidate["url"], page, episode, timeout
        )
        if not target:
            return []

        entry_page = _fetch(shared_state, target, timeout)
        if entry_page is None:
            return []
        return self._releases_from_episode_page(
            shared_state,
            entry_page,
            target,
            series_title=title,
            episode_number=number,
            year=year,
            as_movie=False,
        )

    # ------------------------------------------------------------------ #
    # absolute-number arc resolution
    # ------------------------------------------------------------------ #

    def _resolve_tv_episode(
        self,
        shared_state,
        candidates: list[dict],
        episode: int,
        timeout: int,
    ) -> list[SearchRelease]:
        """Map an absolute episode number onto the franchise's arc entries.

        The site splits long franchises into separate ``/anime/<arc>/`` pages,
        each numbered locally from 1 (Bleach: main run 1-366, then TYBW arcs
        1-13, 1-13, 1-14, 1-4). Sonarr searches with the absolute number, so we
        order the arcs oldest-first and walk cumulative local episode counts
        until the requested absolute number falls inside one arc, then fetch
        that arc's local episode. Labelling keeps the absolute number so Sonarr
        still recognises the release for the episode it asked for.
        """
        chain = _franchise_chain(shared_state, candidates, timeout)
        if not chain:
            return []

        base_title = chain[0]["title"]
        offset = 0
        for entry in chain:
            count = entry["count"]
            if count <= 0:
                continue
            if offset < episode <= offset + count:
                local = episode - offset
                target, _ = _locate_episode(
                    shared_state, entry["url"], entry["page"], local, timeout
                )
                if not target:
                    debug(
                        f"[{self.initials}] arc '{entry['title']}' should hold "
                        f"local {local} (absolute {episode}) but no link found"
                    )
                    return []
                entry_page = _fetch(shared_state, target, timeout)
                if entry_page is None:
                    return []
                debug(
                    f"[{self.initials}] absolute {episode} -> '{entry['title']}' "
                    f"local {local}"
                )
                return self._releases_from_episode_page(
                    shared_state,
                    entry_page,
                    target,
                    series_title=base_title,
                    episode_number=episode,
                    year=entry.get("year") or "",
                    as_movie=False,
                )
            offset += count

        debug(
            f"[{self.initials}] absolute {episode} exceeds franchise total "
            f"{offset} across {len(chain)} arc(s)"
        )
        return []

    def _releases_from_episode_page(
        self,
        shared_state,
        page: BeautifulSoup,
        page_url: str,
        series_title: str,
        episode_number: int,
        year: str,
        as_movie: bool,
    ) -> list[SearchRelease]:
        if not series_title:
            return []

        resolutions = _available_resolutions(page)
        if not resolutions:
            return []

        now = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")
        releases: list[SearchRelease] = []

        for resolution in sorted(resolutions, key=_resolution_sort_key, reverse=True):
            if as_movie:
                title = _movie_release_title(series_title, year, resolution)
            else:
                if episode_number is None:
                    continue
                title = _episode_release_title(
                    series_title, episode_number, resolution
                )

            # Pipe is the payload field separator - never let it reach the link.
            if "|" in title:
                continue

            link = generate_download_link(
                shared_state,
                title,
                page_url,
                0,  # the site publishes no file sizes
                "",
                None,
                self.initials,
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
                        "source": page_url,
                    },
                    "type": "protected",
                }
            )

        return releases


# ---------------------------------------------------------------------- #
# module-private helpers
# ---------------------------------------------------------------------- #


def _title_candidates(shared_state, imdb_id, language, search_category, want_movie):
    """Ordered query titles to try for an IMDb ID.

    IMDb's English title is tried first, then the original/alternate titles from
    Sonarr. This matters because the site indexes anime under romaji only:
    "Daemons of the Shadow Realm" returns nothing while "Yomi no Tsugai" - the
    same series - returns results.
    """
    candidates = []

    localized = get_localized_title(shared_state, imdb_id, language, search_category)
    if localized:
        candidates.append(localized)

    if not want_movie:
        try:
            client = sonarr_api.get_client(shared_state)
            # Deliberately the stored library list, not series_lookup_imdb():
            # Sonarr's /series/lookup endpoint returns alternateTitles=[] and
            # originalTitle=None, while the stored record carries the romaji
            # title this site actually indexes under. Only reached when the
            # localized title found nothing, so the extra call is rare.
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
            # Alternate titles are an enhancement; never fail the search on them.
            debug(f"[{Source.initials}] alternate title lookup failed: {exc}")

    seen, out = set(), []
    for candidate in candidates:
        candidate = (candidate or "").strip()
        key = _normalize(candidate)
        if not candidate or not key or key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _canonical_absolute_episode(shared_state, imdb_id, requested_episode):
    """Translate Sonarr scene-absolute numbering to canonical absolute numbering.

    For anime with ``useSceneNumbering``, Sonarr sends the scene absolute value
    in the Newznab ``q`` parameter. Bleach S17E44 therefore arrives as 44 even
    though its canonical absolute number is 410. Multiple episodes can share a
    scene absolute value; the episode Sonarr is currently searching has the
    newest ``lastSearchTime``, so that timestamp disambiguates the mapping.
    """
    try:
        requested = int(requested_episode)
    except (TypeError, ValueError):
        return requested_episode

    try:
        client = sonarr_api.get_client(shared_state)
        if client is None:
            return requested

        series = next(
            (
                record
                for record in client._get("/series") or []
                if record.get("imdbId") == imdb_id
            ),
            None,
        )
        if not series or not series.get("useSceneNumbering") or not series.get("id"):
            return requested

        matches = []
        for record in client._get(
            "/episode", params={"seriesId": series["id"]}
        ) or []:
            if record.get("sceneAbsoluteEpisodeNumber") != requested:
                continue
            absolute = record.get("absoluteEpisodeNumber")
            if absolute is None:
                continue
            matches.append(record)

        if not matches:
            return requested

        chosen = max(matches, key=lambda record: record.get("lastSearchTime") or "")
        absolute = int(chosen["absoluteEpisodeNumber"])
        if absolute != requested:
            debug(
                f"[{Source.initials}] Sonarr scene absolute {requested} -> "
                f"canonical absolute {absolute}"
            )
        return absolute
    except Exception as exc:
        debug(f"[{Source.initials}] Sonarr absolute-number lookup failed: {exc}")
        return requested


def _host(shared_state):
    return shared_state.values["config"]("Hostnames").get(Source.initials)


def _fetch(shared_state, url: str, timeout: int):
    headers = {"User-Agent": shared_state.values["user_agent"]}
    response = requests.get(url, headers=headers, timeout=timeout)
    if response.status_code != 200:
        return None
    return BeautifulSoup(response.content, "html.parser")


def _dedupe(items: list) -> list:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _parse_cards(soup) -> list[dict]:
    cards = []
    for card in soup.select(".anime-card-container"):
        title_el = card.select_one(".anime-card-title")
        link_el = card.select_one("a[href]")
        if not title_el or not link_el:
            continue
        href = link_el["href"]
        if "/anime/" not in href:
            continue
        cards.append(
            {
                "title": title_el.get_text(strip=True),
                "url": href,
                "type": (
                    card.select_one(".anime-card-type").get_text(" ", strip=True)
                    if card.select_one(".anime-card-type")
                    else ""
                ),
            }
        )
    return cards


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
    # Every query token must appear; guards against "One Piece" matching
    # unrelated shows while still allowing site-side title suffixes.
    return q_tokens.issubset(c_tokens)


def _page_title(page) -> str:
    if page.h1:
        return page.h1.get_text(strip=True)
    el = page.select_one(".anime-card-title")
    return el.get_text(strip=True) if el else ""


def _info_rows(page) -> list[str]:
    return [el.get_text(" ", strip=True) for el in page.select(".anime-info")]


def _anime_type(page) -> str:
    for row in _info_rows(page):
        if AR_TYPE_LABEL in row:
            return row.split(":", 1)[-1].strip()
    return ""


def _anime_year(page) -> str:
    for row in _info_rows(page):
        if AR_YEAR_LABEL in row:
            match = YEAR_PATTERN.search(row)
            if match:
                return match.group(0)
    return ""


def _episode_links(page) -> list[str]:
    """Every ``/episode/`` href on the page.

    Kept only for the movie edge (a film has a single entry page linked from
    the anime page). Episode-number matching must use _episode_entries instead,
    which reads the number from the listing card rather than the URL slug.
    """
    return _dedupe(
        [a["href"] for a in page.find_all("a", href=True) if "/episode/" in a["href"]]
    )


def _episode_number(url: str):
    match = EPISODE_NUM_PATTERN.search(unquote(url))
    return int(match.group(1)) if match else None


def _is_movie_entry(url: str) -> bool:
    return AR_FILM in unquote(url)


def _episode_entries(page) -> list[tuple]:
    """(number, url) pairs from the listing cards on an anime page.

    The card's ``.ep_num`` text ("الحلقة 330") carries the real episode number;
    the slug does not (it may lie - "الحلقة-44pss" is episode 330 - or omit the
    number entirely). Only cards that actually link to an episode are returned,
    which also drops sidebar / related-anime widgets that link to ``/anime/``.
    """
    entries = []
    for card in page.select(".anime-card-container"):
        link = card.select_one('a[href*="/episode/"]')
        if not link:
            continue
        url = link["href"]
        number = None
        num_el = card.select_one(".ep_num")
        if num_el:
            match = EPISODE_NUM_PATTERN.search(num_el.get_text(" ", strip=True))
            if match:
                number = int(match.group(1))
        if number is None:
            # Last resort: a well-formed number in the slug (rare, but harmless
            # now that the pattern rejects junk suffixes).
            number = _episode_number(url)
        entries.append((number, url))
    return entries


def _max_episode_number(page) -> int:
    """Highest local episode number visible on the first listing page.

    Listings run newest-first, so page 1 always carries the latest (highest)
    episode, which for a contiguous 1..N run equals the arc's episode count.
    """
    numbers = [
        number
        for number, url in _episode_entries(page)
        if number is not None and not _is_movie_entry(url)
    ]
    return max(numbers) if numbers else 0


def _locate_episode(shared_state, anime_url: str, first_page, episode, timeout):
    """Return ``(url, number)`` for the requested episode, or ``(None, None)``.

    ``episode`` is a local number here (or None for "newest"). Walks the series
    pagination newest-first; the requested episode is usually on page 1 for
    ongoing series and deeper for back-catalogue requests.
    """
    pages = [first_page]
    known = _page_numbers(first_page)

    for page_index in range(1, MAX_EPISODE_PAGES + 1):
        page = pages[-1]
        for number, url in _episode_entries(page):
            if _is_movie_entry(url):
                continue
            if episode is None:
                if number is not None:
                    return url, number
            elif number == episode:
                return url, number

        if page_index >= known:
            break
        next_url = urljoin(anime_url, f"page/{page_index + 1}/")
        next_page = _fetch(shared_state, next_url, timeout)
        if next_page is None:
            break
        pages.append(next_page)

    return None, None


def _franchise_chain(shared_state, candidates: list[dict], timeout) -> list[dict]:
    """Ordered arc chain for absolute-number resolution.

    Keeps only subtitled TV entries (movies, specials and Arabic-dub duplicates
    are excluded so they cannot inflate the cumulative offset), then orders them
    oldest-first. Each entry carries its fetched page and local episode count.
    Search-result order breaks ties within a year so the mapping stays stable.
    """
    chain = []
    for index, candidate in enumerate(candidates):
        title = candidate.get("title") or ""
        url = candidate.get("url") or ""
        if AR_DUBBED in title or AR_DUBBED in unquote(url):
            continue

        page = _fetch(shared_state, url, timeout)
        if page is None:
            continue
        if _anime_type(page).lower() != "tv":
            continue

        count = _max_episode_number(page)
        if count <= 0:
            continue

        year = _anime_year(page)
        chain.append(
            {
                "title": _page_title(page) or title,
                "url": url,
                "page": page,
                "count": count,
                "year": year,
                "year_key": int(year) if year.isdigit() else 9999,
                "order": index,
            }
        )

    chain.sort(key=lambda e: (e["year_key"], e["count"], e["order"]))
    return chain


def _page_numbers(page) -> int:
    holder = page.select_one(".episodes-pagination-holder")
    if not holder:
        return 1
    numbers = [
        int(el.get_text(strip=True))
        for el in holder.select(".page-numbers")
        if el.get_text(strip=True).isdigit()
    ]
    return max(numbers) if numbers else 1


def _series_title_from_episode(page) -> str:
    """Episode pages link back to their series; that anchor carries the title."""
    for a in page.find_all("a", href=True):
        if "/anime/" in a["href"]:
            text = a.get_text(strip=True)
            if text:
                return text
    return _page_title(page)


def _available_resolutions(page) -> set:
    resolutions = set()
    container = page.select_one(".episode-download-container")
    if not container:
        return resolutions
    for cell in container.select(".td-quality"):
        match = RESOLUTION_PATTERN.search(cell.get_text(" ", strip=True))
        if match:
            resolutions.add(f"{match.group(1)}p")
    return resolutions


def _resolution_sort_key(resolution: str) -> int:
    match = RESOLUTION_PATTERN.search(resolution)
    return int(match.group(1)) if match else 0


def _clean_title(title: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _episode_release_title(series: str, episode: int, resolution: str) -> str:
    """Absolute-numbered anime form that Sonarr's parser understands natively:
    "[Group] Series Title - 005 [1080p][WEB-DL][x264]".

    The WEB-DL token is not decoration. Without a source token Sonarr parses the
    release as HDTV-1080p - the lowest rung of the 1080p tier - so any WEBDL or
    Bluray 1080p from another indexer counts as an upgrade and silently replaces
    the Arabic-subtitled file. The site serves web-sourced encodes, so WEB-DL is
    both accurate and keeps these releases from being upgraded away.
    """
    return (
        f"[{RELEASE_GROUP}] {_clean_title(series)} - {episode:03d} "
        f"[{resolution}][WEB-DL][x264]"
    )


def _movie_release_title(title: str, year: str, resolution: str) -> str:
    parts = [_clean_title(title).replace(" ", ".")]
    if year:
        parts.append(year)
    parts.extend([resolution, "WEB-DL", "x264"])
    return ".".join(parts) + f"-{RELEASE_GROUP}"
