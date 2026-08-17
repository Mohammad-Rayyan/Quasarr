# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# shahiid-anime download twin.
#
# The search side stores the /?download=<id> page as the payload source, so this
# module reopens it directly and picks the hoster links matching the resolution
# encoded in the release title. Links are plain hoster URLs with no CAPTCHA or
# link protection, so JDownloader resolves them natively.

import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from quasarr.constants import DOWNLOAD_REQUEST_TIMEOUT_SECONDS
from quasarr.downloads.sources.helpers.abstract_source import AbstractDownloadSource
from quasarr.downloads.sources.helpers.download_release import DownloadRelease
from quasarr.providers.log import debug

RESOLUTION_PATTERN = re.compile(r"(\d{3,4})p")

# See a4.py: CAPTCHA-gated streaming hosts leave the JDownloader package short
# of 100%, so Sonarr never sees the download finish and never imports it.
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
    "freedl",
    "frdl",
    "freeldl",
    "nitroflare",
    "exe.io",
    "ex-load",
    "exload",
    "up-4ever",
    "up4ever",
    "file-upload",
)

# FHD must be tested before HD, otherwise the substring matches first.
QUALITY_TOKENS = (
    ("fhd", "1080p"),
    ("1080", "1080p"),
    ("hd", "720p"),
    ("720", "720p"),
    ("sd", "480p"),
    ("480", "480p"),
)


class Source(AbstractDownloadSource):
    initials = "sh"

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

        wanted = _resolution_of_title(title)
        if wanted:
            exact = [r for r in rows if r["resolution"] == wanted]
            # Multi-quality entries carry every resolution in one package, so
            # they stay usable when no exact-resolution mirror is listed.
            multi = [r for r in rows if not r["resolution"]]
            rows = exact or multi or rows

        usable = [r for r in rows if not _is_captcha_gated(r)]
        if not usable:
            debug(f"[{self.initials}] only captcha-gated mirrors at {url}")
        rows = usable or rows

        rows = _apply_mirror_preference(rows, mirrors)
        return {
            "links": [[r["url"], r["mirror"]] for r in rows],
            "title": title,
            "password": password or "",
        }


def _download_rows(shared_state, url: str) -> list[dict]:
    response = requests.get(
        url,
        headers={"User-Agent": shared_state.values["user_agent"]},
        timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    rows, seen = [], set()
    for anchor in soup.select("a.btnsdownserver, .btnsdownserver a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href.lower().startswith("http") or href in seen:
            continue
        seen.add(href)
        text = anchor.get_text(" ", strip=True)
        rows.append(
            {
                "url": href,
                "mirror": _mirror_name(text, href),
                "resolution": _resolution_of_label(text),
            }
        )
    return rows


def _mirror_name(text: str, href: str) -> str:
    ascii_only = re.sub(r"[^\x00-\x7F]+", " ", text or "").strip()
    token = re.split(r"[\s\-]+", ascii_only)[0].lower() if ascii_only else ""
    return token or urlparse(href).netloc.lower().replace("www.", "")


def _resolution_of_label(text: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "", (text or "").lower())
    for token, resolution in QUALITY_TOKENS:
        if token in lowered:
            return resolution
    return ""


def _is_captcha_gated(row: dict) -> bool:
    haystack = f"{row.get('mirror', '')} {row.get('url', '')}".lower()
    return any(host in haystack for host in CAPTCHA_GATED_HOSTS)


def _resolution_of_title(title: str) -> str:
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
