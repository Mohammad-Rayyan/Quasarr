# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# Align finished JDownloader filenames with the Quasarr release title.
#
# Anime4up (and similar hosters) leave files named like
# "[Anime4up.rest] OP EP 1172 FHD.mp4". Sonarr's anime parser cannot map that
# to an absolute episode, invents S11E72 from the digits, and leaves the grab
# stuck in importPending. The package/folder name Quasarr already sets
# ("[A4U] One Piece - 1172 [1080p][WEB-DL][x264]") parses correctly - so rename
# the downloaded file to that title before Sonarr imports.
#
# Packages often contain several hoster mirrors (and occasionally FHD+HD).
# Renaming every link to the same release title makes JDownloader throw
# FileExistsException on the second file. Only the primary (largest finished)
# video link is renamed; mirrors keep their hoster names.

from __future__ import annotations

import os
import re

from quasarr.providers.log import debug

VIDEO_EXTENSIONS = (
    ".mp4",
    ".mkv",
    ".avi",
    ".m4v",
    ".mov",
    ".wmv",
    ".ts",
    ".flv",
    ".webm",
)

# Characters Sonarr/filesystem choke on inside a basename.
_ILLEGAL_BASENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def release_filename_for(title: str, current_name: str | None = None) -> str:
    """Build `<release title><ext>` keeping the current file's extension."""
    base = _ILLEGAL_BASENAME.sub("", (title or "").strip()).rstrip(". ")
    if not base:
        raise ValueError("empty release title")

    title_ext = _extension_of(base)
    if title_ext.lower() in VIDEO_EXTENSIONS:
        return base

    ext = _extension_of(current_name) or ".mp4"
    return f"{base}{ext}"


def align_package_filenames_to_release_title(
    shared_state, package: dict, package_links: list | None
) -> int:
    """Rename the primary finished video link to match the package name.

    Only one link is renamed. Multiple mirrors targeting the same basename
    crash JDownloader with FileExistsException.

    Returns the number of links successfully renamed (0 or 1).
    """
    package_name = (package or {}).get("name") or ""
    package_uuid = (package or {}).get("uuid")
    if not package_name or package_uuid is None:
        return 0

    links = [
        link
        for link in (package_links or [])
        if link.get("packageUUID") == package_uuid
    ]
    if not links:
        return 0

    device = shared_state.get_device()
    if device is None:
        return 0

    candidates = [link for link in links if _should_rename_link(link, package_name)]
    primary = _primary_video_link(candidates)
    if primary is None:
        return 0

    link_id = primary.get("uuid")
    current_name = primary.get("name") or ""
    try:
        new_name = release_filename_for(package_name, current_name)
    except ValueError:
        return 0

    if _basename_equal(current_name, new_name):
        return 0

    try:
        device.downloads.rename_link(link_id, new_name)
    except Exception as exc:  # noqa: BLE001 - JD API is flaky; never fail the poll
        debug(
            f"Failed to rename '{current_name}' -> '{new_name}' "
            f"for package '{package_name}': {exc}"
        )
        return 0

    debug(
        f"Renamed download '{current_name}' -> '{new_name}' "
        f"to match release title '{package_name}'"
    )
    return 1


def align_linkgrabber_filenames_to_release_title(
    shared_state, package_id: str, title: str
) -> int:
    """Rename one linkgrabber link for a Quasarr package before download starts.

    Only the primary link is renamed to the release title. Renaming every
    mirror to the same basename races downloads into FileExistsException.
    """
    if not package_id or not title:
        return 0

    device = shared_state.get_device()
    if device is None:
        return 0

    try:
        links = device.linkgrabber.query_links() or []
    except Exception as exc:  # noqa: BLE001
        debug(f"Linkgrabber query for rename failed: {exc}")
        return 0

    package_links = [link for link in links if link.get("comment") == package_id]
    primary = _primary_linkgrabber_link(package_links, title)
    if primary is None:
        return 0

    current_name = primary.get("name") or ""
    try:
        new_name = release_filename_for(title, current_name)
    except ValueError:
        return 0
    if _basename_equal(current_name, new_name):
        return 0

    try:
        device.linkgrabber.rename_link(primary.get("uuid"), new_name)
    except Exception as exc:  # noqa: BLE001
        debug(
            f"Failed to rename linkgrabber '{current_name}' -> '{new_name}': {exc}"
        )
        return 0

    debug(
        f"Renamed linkgrabber '{current_name}' -> '{new_name}' "
        f"for package '{package_id}'"
    )
    return 1


def _primary_video_link(candidates: list[dict]) -> dict | None:
    """Pick the largest finished video among rename candidates."""
    if not candidates:
        return None

    def sort_key(link: dict) -> tuple[int, int]:
        loaded = int(link.get("bytesLoaded") or link.get("bytesloaded") or 0)
        total = int(link.get("bytesTotal") or 0)
        return (loaded, total)

    return max(candidates, key=sort_key)


def _primary_linkgrabber_link(links: list[dict], title: str) -> dict | None:
    """Pick one linkgrabber link to receive the release title."""
    candidates = []
    for link in links:
        if not link.get("uuid"):
            continue
        if link.get("enabled") is False:
            continue
        name = link.get("name") or ""
        try:
            aligned = release_filename_for(title, name)
        except ValueError:
            continue
        if _basename_equal(name, title) or _basename_equal(name, aligned):
            continue
        ext = _extension_of(name)
        # Prefer known video names; allow unknown/empty ext (hoster URL leaf).
        if ext and ext.lower() not in VIDEO_EXTENSIONS:
            continue
        candidates.append(link)

    if not candidates:
        return None

    def sort_key(link: dict) -> tuple[int, int]:
        loaded = int(link.get("bytesLoaded") or link.get("bytesloaded") or 0)
        total = int(link.get("bytesTotal") or 0)
        return (loaded, total)

    # Prefer the biggest known size when available; otherwise first candidate.
    return max(candidates, key=sort_key)


def _should_rename_link(link: dict, package_name: str) -> bool:
    if not link.get("uuid"):
        return False
    if link.get("enabled") is False:
        return False

    name = link.get("name") or ""
    if _basename_equal(name, package_name) or _basename_equal(
        name, release_filename_for(package_name, name)
    ):
        return False

    # Prefer links that actually carried bytes. Mirror placeholders often report
    # finished without being the on-disk file; renaming them is harmless but
    # wasteful - still attempt any finished / fully-loaded video-looking link.
    finished = bool(link.get("finished"))
    bytes_total = int(link.get("bytesTotal") or 0)
    bytes_loaded = int(link.get("bytesLoaded") or link.get("bytesloaded") or 0)
    looks_complete = finished or (bytes_total > 0 and bytes_loaded >= bytes_total)
    if not looks_complete:
        return False

    ext = _extension_of(name)
    if ext and ext.lower() not in VIDEO_EXTENSIONS:
        # Archives keep their own names; extraction produces the video later.
        return False
    return True


def _extension_of(name: str | None) -> str:
    if not name:
        return ""
    _, ext = os.path.splitext(name)
    return ext if ext else ""


def _basename_equal(left: str, right: str) -> bool:
    return os.path.basename(left).casefold() == os.path.basename(right).casefold()
