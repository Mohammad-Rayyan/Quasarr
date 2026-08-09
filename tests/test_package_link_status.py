# -*- coding: utf-8 -*-

import unittest

from quasarr.downloads.packages import get_links_status, is_not_downloadable

PACKAGE = {"uuid": 1, "name": "Synthetic.Release.Example"}


def make_link(
    uuid,
    url,
    availability="ONLINE",
    status="",
    finished=False,
    status_icon="",
    bytes_loaded=0,
    bytes_total=0,
):
    return {
        "uuid": uuid,
        "packageUUID": 1,
        "name": f"file-{uuid}.mkv",
        "url": url,
        "availability": availability,
        "status": status,
        "finished": finished,
        "statusIconKey": status_icon,
        "bytesLoaded": bytes_loaded,
        "bytesTotal": bytes_total,
    }


class IsNotDownloadableTests(unittest.TestCase):
    def test_matches_status_case_insensitively(self):
        self.assertTrue(is_not_downloadable("Not downloadable!"))
        self.assertTrue(is_not_downloadable("NOT DOWNLOADABLE! (Premium needed)"))

    def test_ignores_other_or_missing_status(self):
        self.assertFalse(is_not_downloadable("Extraction OK"))
        self.assertFalse(is_not_downloadable(""))
        self.assertFalse(is_not_downloadable(None))


class GetLinksStatusNotDownloadableTests(unittest.TestCase):
    def test_all_mirrors_not_downloadable_sets_error(self):
        links = [
            make_link(11, "http://mirror-a.invalid/f1", status="Not downloadable!"),
            make_link(12, "http://mirror-a.invalid/f2", status="Not downloadable!"),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertEqual(result["error"], "Links not downloadable for all mirrors")
        self.assertFalse(result["all_finished"])

    def test_not_downloadable_mirror_does_not_count_as_online(self):
        links = [
            make_link(11, "http://mirror-a.invalid/f1", availability="OFFLINE"),
            make_link(21, "http://mirror-b.invalid/f1", status="Not downloadable!"),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertIn("for all mirrors", result["error"])
        self.assertEqual(result["offline_mirror_linkids"], [])

    def test_online_mirror_suppresses_not_downloadable_error(self):
        links = [
            make_link(11, "http://mirror-a.invalid/f1", finished=True),
            make_link(21, "http://mirror-b.invalid/f1", status="Not downloadable!"),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertIsNone(result["error"])
        self.assertFalse(result["all_finished"])
        # A not-downloadable link keeps availability "online", so it must be
        # queued for removal by id (not via the offline-only cleanup), otherwise
        # it lingers and keeps the package from ever finishing.
        self.assertEqual(result["not_downloadable_linkids"], [21])
        self.assertEqual(result["offline_mirror_linkids"], [])

    def test_offline_links_collected_for_cleanup_with_online_mirror(self):
        links = [
            make_link(11, "http://mirror-a.invalid/f1", finished=True),
            make_link(21, "http://mirror-b.invalid/f1", availability="OFFLINE"),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertIsNone(result["error"])
        self.assertEqual(result["offline_mirror_linkids"], [21])
        self.assertEqual(result["not_downloadable_linkids"], [])

    def test_download_list_mirror_healthy_without_availability(self):
        # Links already in JD's download list have no "availability" field. A
        # healthy mirror there (empty availability) must still count as online so
        # a not-downloadable sibling is collected for removal instead of taking
        # the all-mirrors error path.
        links = [
            make_link(11, "http://mirror-a.invalid/f1", availability="", finished=True),
            make_link(
                21,
                "http://mirror-b.invalid/f1",
                availability="",
                status="Not downloadable!",
            ),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertIsNone(result["error"])
        self.assertEqual(result["not_downloadable_linkids"], [21])
        self.assertEqual(result["offline_mirror_linkids"], [])

    def test_file_error_link_removed_when_mirror_healthy(self):
        # In the download list, JD reports a dead link via statusIconKey "false"
        # (no "availability"/offline). With a healthy mirror it must be removed,
        # not fail the whole package.
        links = [
            make_link(11, "http://mirror-a.invalid/f1", availability="", finished=True),
            make_link(
                21, "http://mirror-b.invalid/f1", availability="", status_icon="false"
            ),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertIsNone(result["error"])
        self.assertEqual(result["file_error_linkids"], [21])

    def test_file_error_link_fails_package_without_mirror(self):
        # Only mirror has a file error -> no healthy fallback -> package fails.
        links = [
            make_link(
                11, "http://mirror-a.invalid/f1", availability="", status_icon="false"
            ),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertEqual(result["error"], "File error in package")
        self.assertEqual(result["file_error_linkids"], [])

    def test_file_already_exists_complete_is_success(self):
        # JD marks "File already exists" with statusIconKey "false" even when
        # the file is fully on disk. That must finish the package, not fail it.
        links = [
            make_link(
                11,
                "http://mirror-a.invalid/f1",
                availability="",
                status="File already exists",
                status_icon="false",
                bytes_loaded=266668172,
                bytes_total=266668172,
            ),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertIsNone(result["error"])
        self.assertTrue(result["all_finished"])
        self.assertEqual(result["file_error_linkids"], [])

    def test_file_already_exists_keeps_dead_mirror_for_cleanup(self):
        # Complete "already exists" counts as a healthy mirror so the dead
        # sibling is queued for removal instead of failing the package.
        links = [
            make_link(
                11,
                "http://mirror-a.invalid/f1",
                availability="",
                status="File already exists",
                status_icon="false",
                bytes_loaded=266668172,
                bytes_total=266668172,
            ),
            make_link(
                21,
                "http://mirror-b.invalid/f1",
                availability="",
                status="Try restarting this link (plugin outdated?)",
                status_icon="false",
            ),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertIsNone(result["error"])
        self.assertEqual(result["file_error_linkids"], [21])

    def test_file_already_exists_incomplete_still_fails_without_mirror(self):
        links = [
            make_link(
                11,
                "http://mirror-a.invalid/f1",
                availability="",
                status="File already exists",
                status_icon="false",
                bytes_loaded=100,
                bytes_total=1000,
            ),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertEqual(result["error"], "File error in package")


if __name__ == "__main__":
    unittest.main()
