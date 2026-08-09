# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# anime4up download twin.
#
# The search side stores the episode page URL as the payload source; this module
# reopens that page and extracts the hoster links for the resolution encoded in
# the release title. Links are plain hoster URLs - the site applies no CAPTCHA
# or link protection - so JDownloader resolves them natively.

import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from quasarr.constants import DOWNLOAD_REQUEST_TIMEOUT_SECONDS
from quasarr.downloads.sources.helpers.abstract_source import AbstractDownloadSource
from quasarr.downloads.sources.helpers.download_release import DownloadRelease
from quasarr.providers.log import debug

RESOLUTION_PATTERN = re.compile(r"(\d{3,4})p")

# Streaming-first hosts that gate downloads behind a CAPTCHA. JDownloader parks
# those links as "Skipped - Captcha is required", which keeps the whole package
# short of 100% even after a good mirror has finished - and Sonarr then never
# sees the download complete, so the release is never imported. Excluding them
# costs nothing: they are always duplicates of a mirror that works unattended.
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
)


class Source(AbstractDownloadSource):
    initials = "a4"

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
            matching = [r for r in rows if r["resolution"] == wanted]
            # A missing resolution means the site changed its offering since the
            # search ran; fall back rather than returning an empty package.
            rows = matching or rows

        usable = [r for r in rows if not _is_captcha_gated(r)]
        if not usable:
            debug(f"[{self.initials}] only captcha-gated mirrors at {url}")
        rows = usable or rows

        rows = _apply_mirror_preference(rows, mirrors)

        links = [[row["url"], row["mirror"]] for row in rows]
        return {"links": links, "title": title, "password": password or ""}


def _download_rows(shared_state, url: str) -> list[dict]:
    headers = {"User-Agent": shared_state.values["user_agent"]}
    response = requests.get(
        url, headers=headers, timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS
    )
    if response.status_code != 200:
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    container = soup.select_one(".episode-download-container")
    if not container:
        return []

    rows = []
    seen = set()
    # Real <table> markup: <tr> carries no class, the cells do
    # (.td-link / .td-server / .td-quality), so anchor the walk on <tr>.
    for row in container.select("table tr"):
        anchor = row.find("a", href=True)
        if not anchor:
            continue

        href = anchor["href"].strip()
        if not href.lower().startswith("http"):
            continue

        server_el = row.select_one(".server-name") or row.select_one(".td-server")
        quality_el = row.select_one(".td-quality")

        mirror = _normalize_mirror(
            server_el.get_text(" ", strip=True)
            if server_el
            else urlparse(href).netloc
        )

        resolution = ""
        if quality_el:
            match = RESOLUTION_PATTERN.search(quality_el.get_text(" ", strip=True))
            if match:
                resolution = f"{match.group(1)}p"

        key = (href, resolution)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"url": href, "mirror": mirror, "resolution": resolution})

    return rows


def _is_captcha_gated(row: dict) -> bool:
    haystack = f"{row.get('mirror', '')} {row.get('url', '')}".lower()
    return any(host in haystack for host in CAPTCHA_GATED_HOSTS)


def _normalize_mirror(name: str) -> str:
    """Reduce "mega.nz", "mega.nz 2", "www.mp4upload.com" to a stable key."""
    cleaned = re.sub(r"\s*\d+\s*$", "", (name or "").strip().lower())
    cleaned = re.sub(r"^www\.", "", cleaned)
    return cleaned.split("/")[0]


def _resolution_of(title: str) -> str:
    match = RESOLUTION_PATTERN.search(title or "")
    return f"{match.group(1)}p" if match else ""


def _apply_mirror_preference(rows: list[dict], mirrors: list[str]) -> list[dict]:
    if not mirrors:
        return rows

    wanted = [_normalize_mirror(m) for m in mirrors if m]
    if not wanted:
        return rows

    preferred = [r for r in rows if any(w in r["mirror"] for w in wanted)]
    return preferred or rows
