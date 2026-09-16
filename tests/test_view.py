"""Tests for the `musiq view` command (read-only metadata table)."""

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console

from musiq import cli

_FILE = Path("/home/andrew/Music/2Pac/Do For Love - Single/01 - 2Pac - Do For Love.mp3")


def _tags(**over) -> dict:
    base = {
        "title": "Do For Love", "artist": "2Pac", "artists": ["2Pac"],
        "album_artist": "", "album": "Do For Love - Single",
        "track_num": 5, "disc_num": 1, "genre": "Rap", "composer": "",
        "year": "1997", "images": [], "artist_repair": False,
    }
    base.update(over)
    return base


class ViewCommandTest(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.patch_console = mock.patch.object(
            cli, "console", Console(file=self.out, width=120)
        )
        self.patch_console.start()
        self.addCleanup(self.patch_console.stop)

    def _run(self, tags, query="do for love"):
        with (
            mock.patch.object(cli, "_select_track", return_value=_FILE) as sel,
            mock.patch.object(cli, "read_id3_tags", return_value=tags) as read,
            mock.patch.object(
                cli, "_track_parts",
                return_value=("2Pac", "Do For Love - Single", "Do For Love"),
            ),
        ):
            cli.view.callback((query,))
        return sel, read, self.out.getvalue()

    def test_view_renders_all_metadata_fields(self):
        _, _, text = self._run(_tags())
        for label in (
            "File", "Folder", "Title", "Artist", "Album artist", "Album",
            "Track", "Disc", "Year", "Genre", "Composer", "Artwork",
        ):
            self.assertIn(label, text)
        for value in ("Do For Love", "2Pac", "Do For Love - Single", "5", "1",
                      "1997", "Rap", "Do For Love - Single"):
            self.assertIn(value, text)
        self.assertIn("Metadata for:", text)

    def test_view_resolves_via_select_track(self):
        sel, read, _ = self._run(_tags())
        sel.assert_called_once_with(mock.ANY, "do for love")
        read.assert_called_once_with(_FILE)

    def test_view_escapes_markup_in_tag_values(self):
        tags = _tags(album="R U Still Down? [Remember Me]", title="Do [For] Love")
        _, _, text = self._run(tags)
        self.assertIn("R U Still Down? [Remember Me]", text)
        self.assertIn("Do [For] Love", text)

    def test_view_placeholder_for_missing_values(self):
        tags = _tags(album="", genre="", composer="", year="", track_num=0, disc_num=0)
        _, _, text = self._run(tags)
        for fragment in ("Do For Love", "2Pac", "Album artist"):
            self.assertIn(fragment, text)
        self.assertGreaterEqual(text.count("—"), 6)

    def test_view_artwork_count(self):
        _, _, text = self._run(_tags(images=[{"mime_type": "image/jpeg"}]))
        self.assertIn("1 image", text)
        _, _, text2 = self._run(_tags())
        self.assertIn("—", text2)

    def test_view_fails_on_missing_tags(self):
        with mock.patch.object(cli, "_select_track", return_value=_FILE), \
             mock.patch.object(cli, "read_id3_tags", return_value={}):
            with self.assertRaises(SystemExit):
                cli.view.callback(("do for love",))
        self.assertIn("No ID3 tags found", self.out.getvalue())

    def test_view_returns_quietly_when_track_not_found(self):
        with mock.patch.object(cli, "_select_track", return_value=None), \
             mock.patch.object(cli, "read_id3_tags") as read:
            cli.view.callback(("nothing",))
        read.assert_not_called()


if __name__ == "__main__":
    sys.exit(unittest.main())