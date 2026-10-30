# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# Read side of the scout contract. claude-cron's episode scout browses the
# Arabic DDL sites offline and records what it found in <config>/a4u_hints.json;
# a4/sh/tc consult it at search time. Quasarr publishes its current hostnames to
# <config>/a4u_sites.json because Quasarr.ini stores them encrypted.

import json
import os
import re
import threading
import time
from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit

from quasarr.providers import sonarr_api
from quasarr.providers.log import debug
from quasarr.providers.utils import generate_download_link

HINTS_FILE = "a4u_hints.json"
SITES_FILE = "a4u_sites.json"
PUBLISHED_SITES = ("a4", "sh", "tc", "ak", "tk", "wt")
PARSE_CACHE_SECONDS = 6 * 60 * 60
LIBRARY_CACHE_SECONDS = 10 * 60
RELABEL_MIN_SIMILARITY = 0.85

BRACKETED_SHAPE = re.compile(
    r"^(?P<head>\[[^\]]+\]\s+)(?P<name>.+?)(?P<tail>\s+-\s+\d{2,4}\s+\[.*)$"
)
SCENE_SHAPE = re.compile(
    r"^(?P<head>)(?P<name>.+?)(?P<tail>\.S\d{2}(?:E\d{2,3})?\..+)$"
)

_lock = threading.Lock()
_hints_cache = {"path": None, "mtime": None, "data": {}}
_parse_cache: dict = {}
_library_cache = {"at": 0.0, "records": None}
_published: dict = {}


def _config_dir(shared_state):
    configfile = shared_state.values.get("configfile") or ""
    return os.path.dirname(configfile) if configfile else ""


def load_hints(shared_state) -> dict:
    """Return ``{"series": {imdb: {...}}}``; never raises, empty on any problem."""
    directory = _config_dir(shared_state)
    if not directory:
        return {}
    path = os.path.join(directory, HINTS_FILE)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _lock:
        if _hints_cache["path"] == path and _hints_cache["mtime"] == mtime:
            return _hints_cache["data"]
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("series"), dict):
            data = {}
    except Exception as exc:
        debug(f"[hints] ignoring unreadable {path}: {exc}")
        data = {}
    with _lock:
        _hints_cache.update(path=path, mtime=mtime, data=data)
    return data


def series_hint(shared_state, imdb_id) -> dict:
    if not imdb_id:
        return {}
    entry = load_hints(shared_state).get("series", {}).get(imdb_id)
    return entry if isinstance(entry, dict) else {}


def hint_titles(shared_state, imdb_id, initials) -> list:
    titles = series_hint(shared_state, imdb_id).get("titles") or {}
    out = []
    for key in (initials, "*"):
        for title in titles.get(key) or []:
            if isinstance(title, str) and title.strip() and title.strip() not in out:
                out.append(title.strip())
    return out


def hint_episode_url(shared_state, imdb_id, initials, key, host):
    """Hinted episode URL for ``key`` ("192" or "S04E08") rebased onto ``host``."""
    episodes = (series_hint(shared_state, imdb_id).get("episodes") or {}).get(
        initials
    ) or {}
    url = episodes.get(str(key))
    if not isinstance(url, str) or not url.strip() or not host:
        return None
    return rebase_url(url.strip(), host)


def rebase_url(url: str, host: str) -> str:
    parts = urlsplit(url if "//" in url else f"https://{host}/{url.lstrip('/')}")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def normalize_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").casefold())


def season_map_entries(shared_state, initials, imdb_id=None) -> list:
    """Season-map entries for a source; all hinted series when ``imdb_id`` is None."""
    series = load_hints(shared_state).get("series", {})
    pool = [series.get(imdb_id) or {}] if imdb_id else list(series.values())
    entries = []
    for entry in pool:
        if not isinstance(entry, dict):
            continue
        for item in (entry.get("seasons") or {}).get(initials) or []:
            try:
                entries.append(
                    {
                        "siteTitle": normalize_name(item.get("siteTitle") or ""),
                        "siteSeason": int(item["siteSeason"]),
                        "season": int(item["season"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
    # Most specific siteTitle first: "Shiguang Dailiren Yingdu Pian" must win
    # over "Shiguang Dailiren" for the arc's own cards.
    entries.sort(key=lambda e: len(e["siteTitle"]), reverse=True)
    return entries


def mapped_season(entries, name, site_season):
    """TVDB season for a site card, or ``site_season`` when nothing maps it."""
    if site_season is None:
        return site_season
    key = normalize_name(name)
    for entry in entries:
        if entry["siteSeason"] != int(site_season):
            continue
        if entry["siteTitle"] and entry["siteTitle"] != key:
            continue
        return entry["season"]
    return site_season


def publish_site(shared_state, initials, host):
    """Keep <config>/a4u_sites.json in step with the configured hostnames."""
    if not host or _published.get(initials) == host:
        return
    directory = _config_dir(shared_state)
    if not directory:
        return
    path = os.path.join(directory, SITES_FILE)
    with _lock:
        try:
            with open(path, encoding="utf-8") as handle:
                current = json.load(handle)
            if not isinstance(current, dict):
                current = {}
        except Exception:
            current = {}
        if current.get(initials) != host:
            current[initials] = host
            tmp = f"{path}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(current, handle, indent=2, sort_keys=True)
                os.replace(tmp, path)
            except OSError as exc:
                debug(f"[hints] could not write {path}: {exc}")
                return
        _published[initials] = host


def _library(client):
    now = time.time()
    with _lock:
        if (
            _library_cache["records"] is not None
            and now - _library_cache["at"] < LIBRARY_CACHE_SECONDS
        ):
            return _library_cache["records"]
    records = client._get("/series") or []
    with _lock:
        _library_cache.update(at=now, records=records)
    return records


def sonarr_titles(shared_state, imdb_id) -> list:
    """Sonarr title, original title and alternate titles for an IMDb ID."""
    client = sonarr_api.get_client(shared_state)
    if not client or not imdb_id:
        return []
    out = []
    for record in _library(client):
        if record.get("imdbId") != imdb_id:
            continue
        for title in [record.get("title"), record.get("originalTitle")] + [
            (alt.get("title") if isinstance(alt, dict) else alt)
            for alt in record.get("alternateTitles") or []
        ]:
            if isinstance(title, str) and title.strip() and title.strip() not in out:
                out.append(title.strip())
        break
    return out


def _parses_to(client, title, imdb_id):
    """True/False from Sonarr /parse, or None when Sonarr could not answer."""
    now = time.time()
    with _lock:
        cached = _parse_cache.get(title)
    if cached and now - cached[0] < PARSE_CACHE_SECONDS:
        return cached[1] == imdb_id
    result = client._get("/parse", params={"title": title})
    if result is None:
        return None
    parsed_imdb = (result.get("series") or {}).get("imdbId")
    with _lock:
        _parse_cache[title] = (now, parsed_imdb)
    return parsed_imdb == imdb_id


def _safe(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", name or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def site_name(title: str):
    for shape in (BRACKETED_SHAPE, SCENE_SHAPE):
        match = shape.match(title)
        if match:
            return match["name"].replace(".", " ")
    return None


def resembles(name: str, known: list) -> bool:
    """Whether a site series name is a spelling variant of a known title.

    Gate for relabelling: sources fall back to unmatched search results, and an
    unrelated show must never be renamed into the searched one.
    """
    key = normalize_name(name)
    if not key:
        return False
    for title in known:
        other = normalize_name(title)
        if (
            other
            and SequenceMatcher(None, key, other).ratio() >= RELABEL_MIN_SIMILARITY
        ):
            return True
    return False


def relabel_title(title: str, canonical: str):
    """Swap the series-name segment for ``canonical``; None if the shape is unknown."""
    match = BRACKETED_SHAPE.match(title)
    if match:
        return f"{match['head']}{_safe(canonical)}{match['tail']}"
    match = SCENE_SHAPE.match(title)
    if match:
        dotted = re.sub(r"[\s.]+", ".", _safe(canonical)).strip(".")
        return f"{dotted}{match['tail']}"
    return None


def relabel_releases(shared_state, releases, imdb_id, initials):
    """Rename releases Sonarr cannot map to ``imdb_id`` onto its own series title.

    Titles Sonarr already resolves stay as they are: the site title can carry
    scene-season meaning ("Dr. Stone Science Future - 002" is S4E2, while
    "Dr. STONE - 002" would be S1E2).
    """
    client = sonarr_api.get_client(shared_state)
    if not releases or not imdb_id or not client:
        return releases
    hint = series_hint(shared_state, imdb_id)
    known = sonarr_titles(shared_state, imdb_id)
    canonical = hint.get("title") or (known[0] if known else None)
    if not canonical:
        return releases
    for titles in (hint.get("titles") or {}).values():
        known.extend(t for t in titles or [] if isinstance(t, str))

    out = []
    for release in releases:
        details = release.get("details") or {}
        title = details.get("title") or ""
        ok = _parses_to(client, title, imdb_id) if title else True
        if ok is not False or not resembles(site_name(title) or "", known):
            out.append(release)
            continue
        new_title = relabel_title(title, canonical)
        if not new_title or "|" in new_title or new_title == title:
            out.append(release)
            continue
        size_mb = int((details.get("size") or 0) / (1024 * 1024))
        link = generate_download_link(
            shared_state,
            new_title,
            details.get("source"),
            size_mb,
            "",
            details.get("imdb_id"),
            initials,
        )
        debug(f"[{initials}] relabelled '{title}' -> '{new_title}'")
        out.append(
            {**release, "details": {**details, "title": new_title, "link": link}}
        )
    return out
