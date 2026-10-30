# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# witanime.site download twin.
#
# The search side stores the watch-page URL. Tokens in the sources manifest
# are session-bound, so this module reopens the page, POSTs /sources, then
# POSTs /watch/download-source/<token> and reads the 302 from
# /watch/download-gate/<token>. That location is a plain hoster URL
# (mediafire, workupload, mp4upload) JDownloader can fetch on its own.

import re
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from quasarr.constants import DOWNLOAD_REQUEST_TIMEOUT_SECONDS
from quasarr.downloads.sources.helpers.abstract_source import AbstractDownloadSource
from quasarr.downloads.sources.helpers.download_release import DownloadRelease
from quasarr.providers.log import debug

RESOLUTION_PATTERN = re.compile(r"(\d{3,4})p", re.I)
RES_FOR_QUALITY = {
    "4K": "2160p",
    "FHD": "1080p",
    "HD": "720p",
    "SD": "480p",
}
# Hosts JD finishes unattended, best first. Everything else is kept as a
# fallback unless it is in CAPTCHA_GATED_HOSTS.
PREFERRED_HOSTS = (
    "mediafire",
    "mp4upload",
    "workupload",
    "mega",
)
CAPTCHA_GATED_HOSTS = (
    "videa",
    "mixdrop",
    "dood",
    "streamtape",
    "streamruby",
    "voe",
    "uqload",
    "vkvideo",
    "vk.com",
    "filemoon",
    "vidmoly",
    "ok.ru",
)


class Source(AbstractDownloadSource):
    initials = "wt"

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
            matching = [row for row in rows if row["resolution"] == wanted]
            rows = matching or rows

        usable = [row for row in rows if not _is_captcha_gated(row)]
        if not usable:
            debug(f"[{self.initials}] only captcha-gated mirrors at {url}")
            return {"links": []}
        rows = usable

        preferred = [row for row in rows if _is_preferred(row)]
        other = [row for row in rows if not _is_preferred(row)]
        # A handful of working mirrors beats handing JD ten dead ones.
        rows = (preferred + other)[:4]

        links = []
        for row in rows:
            hoster = _resolve_gate(row["session"], row["token"], row["csrf"], url)
            if not hoster or _is_captcha_gated({"label": hoster, "url": hoster}):
                continue
            links.append([hoster, row["label"] or "witanime"])
        if not links:
            debug(f"[{self.initials}] download gate did not yield a hoster for {url}")
        return {"links": links, "title": title, "password": password or ""}


def _download_rows(shared_state, watch_url: str) -> list[dict]:
    session = requests.Session()
    session.headers["User-Agent"] = shared_state.values["user_agent"]
    opened = session.get(watch_url, timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS)
    if opened.status_code != 200:
        return []
    tag = BeautifulSoup(opened.content, "html.parser").select_one('meta[name="csrf-token"]')
    csrf = (tag.get("content") or "").strip() if tag else ""
    if not csrf:
        return []
    response = session.post(
        watch_url.rstrip("/") + "/sources",
        headers={
            "Accept": "application/json",
            "X-CSRF-TOKEN": csrf,
            "Referer": watch_url,
        },
        timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        debug(f"[wt] sources {response.status_code} for {watch_url}")
        return []
    try:
        payload = response.json()
    except Exception:
        return []

    rows = []
    for quality, items in (payload.get("downloads") or {}).items():
        resolution = RES_FOR_QUALITY.get(quality)
        if not resolution:
            continue
        for item in items or []:
            if (item.get("version") or "sub") != "sub":
                continue
            token = item.get("token") or ""
            if not re.fullmatch(r"[a-f0-9]{64}", token):
                continue
            rows.append(
                {
                    "session": session,
                    "token": token,
                    "label": (item.get("label") or "").strip(),
                    "resolution": resolution,
                    "csrf": csrf,
                }
            )
    return rows


def _resolve_gate(session, token: str, csrf: str, watch_url: str) -> str:
    parsed = urlparse(watch_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "Accept": "application/json",
        "Referer": watch_url,
        "X-CSRF-TOKEN": csrf,
    }
    for cookie in session.cookies:
        if cookie.name == "XSRF-TOKEN":
            headers["X-XSRF-TOKEN"] = unquote(cookie.value)
    source = session.post(
        f"{origin}/watch/download-source/{token}",
        headers=headers,
        timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
    )
    if source.status_code != 200:
        debug(f"[wt] download-source {source.status_code} for {token[:8]}")
        return ""
    gate = session.get(
        f"{origin}/watch/download-gate/{token}",
        headers={"Referer": watch_url},
        allow_redirects=False,
        timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
    )
    location = gate.headers.get("Location") or ""
    if location.startswith("/"):
        location = urljoin(origin + "/", location)
    if not location.lower().startswith("http"):
        return ""
    return location


def _resolution_of(title: str) -> str:
    # Prefer the bracketed quality in our own release title ("[1080p]").
    match = RESOLUTION_PATTERN.search(title or "")
    if not match:
        return ""
    return f"{match.group(1)}p"


def _is_captcha_gated(row: dict) -> bool:
    blob = f"{row.get('label') or ''} {row.get('url') or ''}".lower()
    return any(host in blob for host in CAPTCHA_GATED_HOSTS)


def _is_preferred(row: dict) -> bool:
    blob = (row.get("label") or "").lower()
    return any(host in blob for host in PREFERRED_HOSTS)
