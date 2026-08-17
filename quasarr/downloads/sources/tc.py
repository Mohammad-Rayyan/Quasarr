# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# TopCinema-style download twin.
#
# The search side stores the content page URL (episode, movie, or /series/
# season pack). This module appends /download/, scrapes a.downloadsLink hoster
# rows, and filters by the resolution encoded in the release title. Links are
# plain hoster URLs - no CAPTCHA or link protection - so JDownloader resolves
# them natively.

import re
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from quasarr.constants import DOWNLOAD_REQUEST_TIMEOUT_SECONDS
from quasarr.downloads.sources.helpers.abstract_source import AbstractDownloadSource
from quasarr.downloads.sources.helpers.download_release import DownloadRelease
from quasarr.providers.log import debug

RESOLUTION_PATTERN = re.compile(r"(\d{3,4})p", re.I)

# Streaming-first / captcha / account-gated / JD-broken hosts.
CAPTCHA_GATED_HOSTS = (
    "streamruby",
    "rubyvidhub",
    "voe",
    "uqload",
    "dsvplay",
    "videa",
    "vkvideo",
    "mixdrop",
    "ok.ru",
    "vidtube",
    # premium-or-fail on free JD (no account configured)
    "nitroflare",
    # chronically dead / plugin-outdated on this stack
    "exe.io",
    "ex-load",
    "exload",
    "up-4ever",
    "up4ever",
    "file-upload",
    # freel captcha plugin submits wrong answers with no MyJD prompt
    "freedl",
    "frdl",
    "freeldl",
    # JD has no plugin; we resolve these to DirectHTTP below when possible
    "mdiaload",
)

# XFileSharing free-wait hosts JD cannot handle natively — resolve to CDN.
XFS_RESOLVE_HOSTS = (
    "updown",
)

# Prefer resolved directs / hosts JD can use unattended.
PREFERRED_FREE_HOSTS = (
    "directhttp",
    "ddownload",
    "1fichier",
    "savefiles",
    "cloudfile",
    "gofile",
    "mega",
    "mp4upload",
    "rapidgator",
)


class Source(AbstractDownloadSource):
    initials = "tc"

    def get_download_links(
        self,
        shared_state,
        url: str,
        mirrors: list[str],
        title: str,
        password: str,
    ) -> DownloadRelease:
        rows = _download_rows(shared_state, url)
        if not rows:
            debug(f"[{self.initials}] no download rows at {url}")
            return {"links": []}

        wanted = _resolution_of(title)
        if wanted:
            exact = [r for r in rows if r["resolution"] == wanted]
            # Multi-quality entries (e.g. "1080p - 720p - 480p") carry every
            # resolution in one package; keep them when no exact match exists.
            multi = [r for r in rows if not r["resolution"]]
            rows = exact or multi or rows

        usable = [r for r in rows if not _is_captcha_gated(r)]
        if not usable:
            debug(f"[{self.initials}] only captcha-gated mirrors at {url}")
        rows = usable or rows

        rows = [_resolve_row(shared_state, r) for r in rows]
        rows = [r for r in rows if r.get("url")]

        # Prefer free/reliable hosts first, but keep other non-captcha mirrors
        # as fallback — exclusive filtering left packages with only dead links.
        preferred = [r for r in rows if _is_preferred_free(r)]
        other = [r for r in rows if not _is_preferred_free(r)]
        rows = preferred + other

        rows = _apply_mirror_preference(rows, mirrors)
        return {
            "links": [[row["url"], row["mirror"]] for row in rows],
            "title": title,
            "password": password or "",
        }


def _download_url(page_url: str) -> str:
    base = (page_url or "").rstrip("/")
    if not base:
        return page_url
    if base.endswith("/download"):
        return base + "/"
    return base + "/download/"


def _download_rows(shared_state, url: str) -> list[dict]:
    target = _download_url(url)
    response = requests.get(
        target,
        headers={"User-Agent": shared_state.values["user_agent"]},
        timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    rows = []
    seen = set()
    for anchor in soup.select("a.downloadsLink"):
        href = (anchor.get("href") or "").strip()
        if not href.lower().startswith("http"):
            continue
        text = anchor.get_text(" ", strip=True)
        resolution = _resolution_of_label(text)
        key = (href, resolution)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "url": href,
                "mirror": _normalize_mirror(text, href),
                "resolution": resolution,
            }
        )
    return rows


def _resolution_of_label(text: str) -> str:
    """Single resolution from label text; empty when multiple are listed."""
    matches = RESOLUTION_PATTERN.findall(text or "")
    if len(matches) == 1:
        return f"{matches[0]}p"
    return ""


def _normalize_mirror(text: str, href: str) -> str:
    ascii_only = re.sub(r"[^\x00-\x7F]+", " ", text or "").strip()
    token = re.split(r"[\s\-]+", ascii_only)[0].lower() if ascii_only else ""
    if token:
        return token
    return urlparse(href).netloc.lower().replace("www.", "")


def _is_captcha_gated(row: dict) -> bool:
    haystack = f"{row.get('mirror', '')} {row.get('url', '')}".lower()
    return any(host in haystack for host in CAPTCHA_GATED_HOSTS)


def _is_preferred_free(row: dict) -> bool:
    haystack = f"{row.get('mirror', '')} {row.get('url', '')}".lower()
    return any(host in haystack for host in PREFERRED_FREE_HOSTS)


def _needs_xfs_resolve(row: dict) -> bool:
    haystack = f"{row.get('mirror', '')} {row.get('url', '')}".lower()
    return any(host in haystack for host in XFS_RESOLVE_HOSTS)


def _resolve_row(shared_state, row: dict) -> dict:
    """Resolve XFS free-wait pages (e.g. updown.cam) to DirectHTTP CDN URLs.

    JDownloader has no updown.cam plugin, so raw links are silently dropped.
    After the free countdown the host 302s to a gamescdn.online MP4 that JD
    can fetch via DirectHTTP without captcha.
    """
    if not _needs_xfs_resolve(row):
        return row
    direct = _resolve_xfs_direct(shared_state, row["url"])
    if not direct:
        debug(f"[tc] XFS resolve failed for {row['url']}")
        return {"url": "", "mirror": row.get("mirror", ""), "resolution": row.get("resolution")}
    return {
        "url": direct,
        "mirror": "directhttp",
        "resolution": row.get("resolution"),
    }


def _resolve_xfs_direct(shared_state, page_url: str) -> str:
    session = requests.Session()
    session.headers["User-Agent"] = shared_state.values["user_agent"]
    try:
        first = session.get(page_url, timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS)
        if first.status_code != 200:
            return ""
        soup = BeautifulSoup(first.content, "html.parser")
        data = {
            inp.get("name"): (inp.get("value") or "")
            for inp in soup.find_all("input")
            if inp.get("name")
        }
        if not data.get("op"):
            return ""
        second = session.post(
            page_url, data=data, timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS
        )
        soup2 = BeautifulSoup(second.content, "html.parser")
        data2 = {
            inp.get("name"): (inp.get("value") or "")
            for inp in soup2.find_all("input")
            if inp.get("name")
        }
        # Free countdown is usually ~10–20s; wait a bit over the common 15s.
        time.sleep(16)
        third = session.post(
            page_url,
            data=data2,
            timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
        )
        location = third.headers.get("Location") or ""
        third.close()
        if location.lower().startswith("http"):
            return location
    except Exception as exc:
        debug(f"[tc] XFS resolve exception for {page_url}: {exc}")
    return ""


def _resolution_of(title: str) -> str:
    match = RESOLUTION_PATTERN.search(title or "")
    return f"{match.group(1)}p" if match else ""


def _apply_mirror_preference(rows: list[dict], mirrors: list[str]) -> list[dict]:
    if not mirrors:
        return rows
    wanted = [m.lower().replace("www.", "").split(".")[0] for m in mirrors if m]
    if not wanted:
        return rows
    preferred = [r for r in rows if any(w in r["mirror"] for w in wanted)]
    return preferred or rows
