# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# takkiadrama.asia download twin.
#
# Search stores the episode page. This module opens <episode>/downloads/ and
# returns the a.dw hoster URLs for the resolution in the release title.
# send.now / upzur / hgcloud are handed to JDownloader first. Stream embeds
# and captcha hosts are dropped.

import re
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from quasarr.constants import DOWNLOAD_REQUEST_TIMEOUT_SECONDS
from quasarr.downloads.sources.helpers.abstract_source import AbstractDownloadSource
from quasarr.providers.log import debug

RESOLUTION_PATTERN = re.compile(r"(\d{3,4})p", re.I)
PREFERRED_HOSTS = ("send.now", "upzur.com", "hgcloud.to", "morencius.com")
CAPTCHA_GATED_HOSTS = (
    "dsvplay",
    "71stream",
    "ult4vid",
    "dood",
    "mixdrop",
    "streamtape",
    "vidmoly",
    "filemoon",
    "uqload",
    "voe",
    "streamruby",
    "vkvideo",
    "ok.ru",
)


class Source(AbstractDownloadSource):
    initials = "tk"

    def get_download_links(self, shared_state, url, mirrors, title, password):
        rows = _rows(shared_state, url)
        if not rows:
            debug(f"[{self.initials}] no download rows at {url}")
            return {"links": []}
        wanted = _resolution_of(title)
        if wanted:
            matching = [row for row in rows if row["resolution"] == wanted]
            rows = matching or rows
        usable = [row for row in rows if not _gated(row["url"]) and "/watch" not in row["url"]]
        if not usable:
            debug(f"[{self.initials}] only unusable mirrors at {url}")
            return {"links": []}
        preferred = [row for row in usable if _preferred(row["url"])]
        other = [row for row in usable if not _preferred(row["url"])]
        chosen = (preferred + other)[:5]
        return {
            "links": [[row["url"], row["label"]] for row in chosen],
            "title": title,
            "password": password or "",
        }


def _ascii_url(url: str) -> str:
    parts = urlsplit(url)
    path = quote(unquote(parts.path), safe="/")
    query = quote(unquote(parts.query), safe="=&")
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _rows(shared_state, episode_url: str) -> list:
    episode_url = _ascii_url(episode_url)
    target = episode_url.rstrip("/") + "/downloads/"
    response = requests.get(
        target,
        headers={
            "User-Agent": shared_state.values["user_agent"],
            "Referer": episode_url,
        },
        timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        return []
    soup = BeautifulSoup(response.content, "html.parser")
    rows = []
    seen = set()
    for anchor in soup.select("a.dw"):
        href = (anchor.get("href") or "").strip()
        if not href.lower().startswith("http") or href in seen:
            continue
        seen.add(href)
        badge = anchor.select_one(".quality-badge")
        label_el = anchor.find("span")
        text = badge.get_text(" ", strip=True) if badge else anchor.get_text(" ", strip=True)
        match = RESOLUTION_PATTERN.search(text)
        rows.append(
            {
                "url": href,
                "label": (label_el.get_text(strip=True) if label_el else "") or urlparse(href).netloc,
                "resolution": f"{match.group(1)}p" if match else "",
            }
        )
    return rows


def _resolution_of(title: str) -> str:
    match = RESOLUTION_PATTERN.search(title or "")
    return f"{match.group(1)}p" if match else ""


def _gated(url: str) -> bool:
    blob = (url or "").lower()
    return any(host in blob for host in CAPTCHA_GATED_HOSTS)


def _preferred(url: str) -> bool:
    blob = (url or "").lower()
    return any(host in blob for host in PREFERRED_HOSTS)
