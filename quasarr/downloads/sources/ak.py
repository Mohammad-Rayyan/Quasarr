# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# akwam.ss download twin.
#
# Search stores the episode page. This module reopens it, follows the
# /download/ link, and returns the fresh downet.net mp4. That URL is
# minted when the download page is rendered, so it cannot be cached at
# search time.

import re
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from quasarr.constants import DOWNLOAD_REQUEST_TIMEOUT_SECONDS
from quasarr.downloads.sources.helpers.abstract_source import AbstractDownloadSource
from quasarr.providers.log import debug

RESOLUTION_PATTERN = re.compile(r"(\d{3,4})p", re.I)


class Source(AbstractDownloadSource):
    initials = "ak"

    def get_download_links(self, shared_state, url, mirrors, title, password):
        files = _mp4s(shared_state, url)
        if not files:
            debug(f"[{self.initials}] no mp4 at {url}")
            return {"links": []}
        wanted = _resolution_of(title)
        if wanted:
            matching = [item for item in files if wanted in item.lower()]
            files = matching or files
        # One direct file is enough. Extra copies of the same mp4 just
        # make JDownloader fight itself.
        unique = list(dict.fromkeys(files))
        return {
            "links": [[unique[0], "akwam"]],
            "title": title,
            "password": password or "",
        }


def _ascii_url(url: str) -> str:
    parts = urlsplit(url)
    path = quote(unquote(parts.path), safe="/")
    query = quote(unquote(parts.query), safe="=&")
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _mp4s(shared_state, episode_url: str) -> list:
    headers = {"User-Agent": shared_state.values["user_agent"]}
    episode_url = _ascii_url(episode_url)
    opened = requests.get(
        episode_url, headers=headers, timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS
    )
    if opened.status_code != 200:
        return []
    page = BeautifulSoup(opened.content, "html.parser")
    download = ""
    for anchor in page.find_all("a", href=True):
        if "/download/" in anchor["href"]:
            download = _ascii_url(urljoin(episode_url, anchor["href"]))
            break
    if not download:
        return []
    response = requests.get(
        download,
        headers={**headers, "Referer": episode_url},
        timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        debug(f"[ak] download page {response.status_code}")
        return []
    return re.findall(r"https://[^\"']+\.mp4", response.text)


def _resolution_of(title: str) -> str:
    match = RESOLUTION_PATTERN.search(title or "")
    return f"{match.group(1)}p" if match else ""
