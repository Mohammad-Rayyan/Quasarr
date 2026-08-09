# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock

from quasarr.downloads.sources.helpers.sonarr_filenames import (
    align_package_filenames_to_release_title,
    release_filename_for,
)


class ReleaseFilenameTests(unittest.TestCase):
    def test_keeps_extension_from_hoster_name(self):
        self.assertEqual(
            release_filename_for(
                "[A4U] One Piece - 1172 [1080p][WEB-DL][x264]",
                "[Anime4up.rest] OP EP 1172 FHD.mp4",
            ),
            "[A4U] One Piece - 1172 [1080p][WEB-DL][x264].mp4",
        )

    def test_defaults_to_mp4(self):
        self.assertEqual(
            release_filename_for("[A4U] One Piece - 1172 [1080p][WEB-DL][x264]"),
            "[A4U] One Piece - 1172 [1080p][WEB-DL][x264].mp4",
        )

    def test_does_not_double_extension(self):
        title = "[A4U] One Piece - 1172 [1080p][WEB-DL][x264].mp4"
        self.assertEqual(release_filename_for(title, "x.mkv"), title)

    def test_strips_illegal_basename_chars(self):
        self.assertEqual(
            release_filename_for('Foo: Bar?/Baz', "ep.mkv"),
            "Foo BarBaz.mkv",
        )


class AlignPackageFilenamesTests(unittest.TestCase):
    def test_renames_finished_hoster_filename(self):
        device = MagicMock()
        shared_state = MagicMock()
        shared_state.get_device.return_value = device

        package = {
            "name": "[A4U] One Piece - 1172 [1080p][WEB-DL][x264]",
            "uuid": 42,
        }
        links = [
            {
                "uuid": 7,
                "packageUUID": 42,
                "name": "[Anime4up.rest] OP EP 1172 FHD.mp4",
                "finished": True,
                "bytesTotal": 100,
                "bytesLoaded": 100,
                "enabled": True,
            }
        ]

        renamed = align_package_filenames_to_release_title(
            shared_state, package, links
        )

        self.assertEqual(renamed, 1)
        device.downloads.rename_link.assert_called_once_with(
            7, "[A4U] One Piece - 1172 [1080p][WEB-DL][x264].mp4"
        )

    def test_skips_when_already_aligned(self):
        device = MagicMock()
        shared_state = MagicMock()
        shared_state.get_device.return_value = device

        package = {
            "name": "[A4U] One Piece - 1172 [1080p][WEB-DL][x264]",
            "uuid": 42,
        }
        links = [
            {
                "uuid": 7,
                "packageUUID": 42,
                "name": "[A4U] One Piece - 1172 [1080p][WEB-DL][x264].mp4",
                "finished": True,
                "bytesTotal": 100,
                "bytesLoaded": 100,
                "enabled": True,
            }
        ]

        renamed = align_package_filenames_to_release_title(
            shared_state, package, links
        )

        self.assertEqual(renamed, 0)
        device.downloads.rename_link.assert_not_called()

    def test_renames_only_largest_finished_mirror(self):
        """Multiple mirrors must not share one release basename (JD FileExists)."""
        device = MagicMock()
        shared_state = MagicMock()
        shared_state.get_device.return_value = device

        package = {
            "name": "[A4U] Diamond no Ace Act II Second Season - 007 [1080p][WEB-DL][x264]",
            "uuid": 42,
        }
        links = [
            {
                "uuid": 1,
                "packageUUID": 42,
                "name": "[Anime4up.rest] DNAAISS EP 07 HD.mp4",
                "finished": True,
                "bytesTotal": 180_000_000,
                "bytesLoaded": 180_000_000,
                "enabled": True,
            },
            {
                "uuid": 2,
                "packageUUID": 42,
                "name": "[Anime4up.rest] DNAAISS EP 07 FHD.mp4",
                "finished": True,
                "bytesTotal": 310_000_000,
                "bytesLoaded": 310_000_000,
                "enabled": True,
            },
        ]

        renamed = align_package_filenames_to_release_title(
            shared_state, package, links
        )

        self.assertEqual(renamed, 1)
        device.downloads.rename_link.assert_called_once_with(
            2,
            "[A4U] Diamond no Ace Act II Second Season - 007 [1080p][WEB-DL][x264].mp4",
        )


if __name__ == "__main__":
    unittest.main()
