"""Tests for the `musiq organize` command and `musiq.folders` module."""

import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console

from musiq import cli
import musiq.folders
from musiq.folders import FolderIssue, _read_id3_tags, rename_folder, scan_library


class ScanLibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.music = Path(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _make_album(self, artist, album, files=None, album_tag=None, title=None):
        """Build an artist/album folder structure and optionally write tags."""
        artist_dir = self.music / artist / album
        artist_dir.mkdir(parents=True, exist_ok=True)
        created = []
        for fname in (files or ["01 - Track.mp3"]):
            p = artist_dir / fname
            p.write_bytes(b"\x00" * 128)
            created.append(p)
        if album_tag is not None:
            self._write_tags(created[0], album=album_tag, title=title or album_tag)
        return created

    def _write_tags(self, path: Path, album: str, title: str = ""):
        """Write ID3 album + title tags to an MP3 file using mutagen."""
        from mutagen.id3 import ID3, TALB, TIT2

        path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00" + b"\x00" * 1024)
        audio = ID3(str(path))
        audio.add(TALB(encoding=3, text=album))
        if title:
            audio.add(TIT2(encoding=3, text=title))
        audio.save()

    def test_read_id3_tags_returns_album_and_title(self):
        """_read_id3_tags returns both album and title."""
        files = self._make_album("Art", "Album", album_tag="Album", title="My Song")
        album, title = _read_id3_tags(files[0])
        self.assertEqual(album, "Album")
        self.assertEqual(title, "My Song")

    def test_read_id3_tags_returns_empty_for_no_tag(self):
        """_read_id3_tags returns empty for untagged file."""
        f = self.music / "Art" / "Album"
        f.mkdir(parents=True)
        p = f / "track.mp3"
        p.write_bytes(b"\x00" * 128)
        album, title = _read_id3_tags(p)
        self.assertEqual(album, "")
        self.assertEqual(title, "")

    def test_scan_finds_mismatches(self):
        files = self._make_album("TestArtist", "WrongName", title="My Song")
        self._write_tags(files[0], album="CorrectName", title="My Song")

        issues = scan_library(self.music)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].category, "rename")
        self.assertEqual(issues[0].artist_folder, "TestArtist")
        self.assertEqual(issues[0].album_folder, "WrongName")
        self.assertEqual(issues[0].tag_album, "CorrectName")

    def test_scan_skips_correct_folders(self):
        """Folder name already matches sanitize(tag_album) — no issue."""
        self._make_album("Artist", "OK Album", title="Track")
        issues = scan_library(self.music)
        self.assertEqual(len(issues), 0)

    def test_scan_skips_symbol_substitutes(self):
        """Tag has '/' but folder has '_' — already correct."""
        self._make_album("Artist", "Fetish _ Valley Serpent", title="Track")
        issues = scan_library(self.music)
        self.assertEqual(len(issues), 0)

    def test_scan_classifies_retagged_single_as_rename(self):
        """Folder 'X - Single', tag = real album (different from title) → rename."""
        files = self._make_album("Gunna", "kills - Single",
                                 album_tag="Dedication", title="kills")
        issues = scan_library(self.music)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].category, "rename")
        self.assertEqual(issues[0].tag_album, "Dedication")

    def test_scan_flags_single_with_tag_equal_to_title(self):
        """Folder 'X - Single', tag == title → still a mismatch, renamed."""
        self._make_album("Gunna", "kills - Single",
                         album_tag="kills", title="kills")
        issues = scan_library(self.music)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].category, "rename")
        self.assertEqual(issues[0].target_folder, "kills")

    def test_scan_flags_single_case_insensitive(self):
        """Folder 'X - single' (lowercase) never matches the tag → mismatch."""
        self._make_album("Gunna", "kills - single",
                         album_tag="kills", title="kills")
        issues = scan_library(self.music)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].category, "rename")
        self.assertEqual(issues[0].target_folder, "kills")

    def test_scan_flags_single_when_tag_matches_sanitized_name(self):
        """'All Summer Long - Single' with tag 'All Summer Long' → rename."""
        self._make_album("Ally Nicholas", "All Summer Long - Single",
                         album_tag="All Summer Long", title="All Summer Long")
        issues = scan_library(self.music)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].category, "rename")
        self.assertEqual(issues[0].target_folder, "All Summer Long")

    def test_scan_classifies_merge_when_folder_exists_without_file(self):
        """Target folder exists but same-named file absent → merge."""
        self._make_album("Gunna", "kills - Single",
                         album_tag="Gift", files=["01 - kills.mp3"],
                         title="kills")
        self._make_album("Gunna", "Gift", files=["01 - other.mp3"])
        issues = scan_library(self.music)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].category, "merge")

    def test_scan_detects_collision(self):
        """Target folder already contains a same-named file → collision."""
        self._make_album("Gunna", "kills - Single",
                         album_tag="Gift", files=["01 - kils.mp3"], title="kills")
        # target already contains a file with the same name → file-level collision
        self._make_album("Gunna", "Gift", files=["01 - kils.mp3"])
        issues = scan_library(self.music)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].category, "collision")

    def test_scan_merge_distinct_filenames(self):
        """Target exists with different file name → merge."""
        self._make_album("Gunna", "kills - Single",
                         album_tag="Gift", files=["01 - kills.mp3"], title="kills")
        self._make_album("Gunna", "Gift", files=["02 - other.mp3"])
        issues = scan_library(self.music)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].category, "merge")

    def test_scan_skips_nested_layouts(self):
        """Files nested deeper than artist/album/file are skipped."""
        nested = self.music / "Audiobook" / "Chapter 1" / "Part 1"
        nested.mkdir(parents=True)
        nested_file = nested / "track.mp3"
        nested_file.write_bytes(b"\x00" * 128)
        self._write_tags(nested_file, album="Some Album", title="Chapter 1")
        self.assertEqual(len(scan_library(self.music)), 0)

    def test_scan_handles_mixed_tags(self):
        """Two files in the same folder disagree on album tag."""
        artist_dir = self.music / "Mixed" / "Shared"
        artist_dir.mkdir(parents=True)
        f1 = artist_dir / "track1.mp3"
        f2 = artist_dir / "track2.mp3"
        f1.write_bytes(b"\x00" * 128)
        f2.write_bytes(b"\x00" * 128)
        self._write_tags(f1, album="Album A", title="Song A")
        self._write_tags(f2, album="Album B", title="Song B")
        issues = scan_library(self.music)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].category, "mixed")

    def test_scan_skips_empty_dirs(self):
        """Empty album folders produce no issues."""
        (self.music / "Artist" / "Empty Album").mkdir(parents=True)
        self.assertEqual(len(scan_library(self.music)), 0)

    def test_scan_skips_non_audio_files(self):
        """Non-audio files are ignored."""
        artist_dir = self.music / "Artist" / "Album"
        artist_dir.mkdir(parents=True)
        (artist_dir / "cover.jpg").write_bytes(b"\xff")
        (artist_dir / "info.txt").write_text("hello")
        self.assertEqual(len(scan_library(self.music)), 0)

    def test_scan_retagged_single_not_unfixed_when_title_differs(self):
        """A retagged single where tag != title gets classified as rename."""
        self._make_album("2Pac", "Do For Love - Single",
                         album_tag="R U Still Down? [Remember Me]",
                         title="Do For Love")
        issues = scan_library(self.music)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].category, "rename")
        self.assertEqual(issues[0].target_folder, "R U Still Down_ [Remember Me]")


class RenameFolderTest(unittest.TestCase):
    def test_dry_run_returns_pending(self):
        issue = FolderIssue(
            artist_folder="Test", album_folder="Wrong",
            tag_album="Right", target_folder="Right",
            files=[], category="rename",
        )
        self.assertEqual(rename_folder(issue, dry_run=True), "pending")

    def test_apply_renames_folder(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            music = tmpdir / "Music"
            (music / "TestArtist" / "WrongName").mkdir(parents=True)
            (music / "TestArtist" / "WrongName" / "track.mp3").write_bytes(b"test")

            issue = FolderIssue(
                artist_folder="TestArtist", album_folder="WrongName",
                tag_album="RightName", target_folder="RightName",
                files=[], category="rename",
            )
            with mock.patch("musiq.folders.Path.home", return_value=tmpdir):
                result = rename_folder(issue, dry_run=False)
            self.assertEqual(result, "renamed")
            self.assertTrue((music / "TestArtist" / "RightName").is_dir())
            self.assertFalse((music / "TestArtist" / "WrongName").is_dir())
        finally:
            shutil.rmtree(tmpdir)

    def test_apply_merges_files_into_existing_folder(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            music = tmpdir / "Music"
            (music / "Test" / "Source").mkdir(parents=True)
            (music / "Test" / "Target").mkdir(parents=True)
            src_file = music / "Test" / "Source" / "01 - kills.mp3"
            src_file.write_bytes(b"data")
            tgt_file = music / "Test" / "Target" / "02 - other.mp3"
            tgt_file.write_bytes(b"data")

            issue = FolderIssue(
                artist_folder="Test", album_folder="Source",
                tag_album="Target", target_folder="Target",
                files=[src_file], category="merge",
            )
            with mock.patch("musiq.folders.Path.home", return_value=tmpdir):
                result = rename_folder(issue, dry_run=False)
            self.assertEqual(result, "merged")
            self.assertTrue((music / "Test" / "Target" / "01 - kills.mp3").is_file())
            self.assertFalse((music / "Test" / "Source").is_dir())
        finally:
            shutil.rmtree(tmpdir)

    def test_apply_collision_when_file_exists_returns_collision(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            music = tmpdir / "Music"
            (music / "Test" / "Source").mkdir(parents=True)
            (music / "Test" / "Target").mkdir(parents=True)
            src_file = music / "Test" / "Source" / "01 - same.mp3"
            src_file.write_bytes(b"data")
            (music / "Test" / "Target" / "01 - same.mp3").write_bytes(b"data2")

            issue = FolderIssue(
                artist_folder="Test", album_folder="Source",
                tag_album="Target", target_folder="Target",
                files=[src_file], category="merge",
            )
            with mock.patch("musiq.folders.Path.home", return_value=tmpdir):
                result = rename_folder(issue, dry_run=False)
            self.assertEqual(result, "collision")
            self.assertTrue((music / "Test" / "Source").is_dir())
            self.assertEqual(
                (music / "Test" / "Target" / "01 - same.mp3").read_bytes(), b"data2"
            )
        finally:
            shutil.rmtree(tmpdir)

    def test_apply_collision_skips_blocking_file_only(self):
        """A colliding file is skipped, the rest of the folder still moves."""
        tmpdir = Path(tempfile.mkdtemp())
        try:
            music = tmpdir / "Music"
            (music / "Test" / "Source").mkdir(parents=True)
            (music / "Test" / "Target").mkdir(parents=True)
            blocked = music / "Test" / "Source" / "01 - same.mp3"
            blocked.write_bytes(b"data")
            moves = music / "Test" / "Source" / "02 - other.mp3"
            moves.write_bytes(b"data")
            (music / "Test" / "Target" / "01 - same.mp3").write_bytes(b"data2")

            issue = FolderIssue(
                artist_folder="Test", album_folder="Source",
                tag_album="Target", target_folder="Target",
                files=[blocked, moves], category="merge",
            )
            with mock.patch("musiq.folders.Path.home", return_value=tmpdir):
                result = rename_folder(issue, dry_run=False)
            self.assertEqual(result, "collision")
            self.assertTrue((music / "Test" / "Target" / "02 - other.mp3").is_file())
            self.assertEqual(
                (music / "Test" / "Target" / "01 - same.mp3").read_bytes(), b"data2"
            )
            self.assertTrue((music / "Test" / "Source" / "01 - same.mp3").is_file())
        finally:
            shutil.rmtree(tmpdir)

    def test_apply_collision_category_still_moves_non_blocking(self):
        """'collision' issues are applied: blocking file skipped, rest moved."""
        tmpdir = Path(tempfile.mkdtemp())
        try:
            music = tmpdir / "Music"
            (music / "Test" / "Source").mkdir(parents=True)
            (music / "Test" / "Target").mkdir(parents=True)
            blocked = music / "Test" / "Source" / "01 - same.mp3"
            blocked.write_bytes(b"data")
            moves = music / "Test" / "Source" / "02 - other.mp3"
            moves.write_bytes(b"data")
            (music / "Test" / "Target" / "01 - same.mp3").write_bytes(b"data2")

            issue = FolderIssue(
                artist_folder="Test", album_folder="Source",
                tag_album="Target", target_folder="Target",
                files=[blocked, moves], category="collision",
            )
            with mock.patch("musiq.folders.Path.home", return_value=tmpdir):
                result = rename_folder(issue, dry_run=False)
            self.assertEqual(result, "collision")
            self.assertTrue((music / "Test" / "Target" / "02 - other.mp3").is_file())
        finally:
            shutil.rmtree(tmpdir)

    def test_apply_source_not_found_returns_failed(self):
        issue = FolderIssue(
            artist_folder="Test", album_folder="Ghost",
            tag_album="Nope", target_folder="Nope",
            files=[], category="rename",
        )
        with mock.patch("musiq.folders.Path.home", return_value=Path(tempfile.mkdtemp())):
            result = rename_folder(issue, dry_run=False)
        self.assertEqual(result, "failed")

    def test_non_rename_category_returns_skipped(self):
        issue = FolderIssue(
            artist_folder="Test", album_folder="Folder",
            tag_album="Tag", target_folder="Tag",
            files=[], category="mixed",
        )
        self.assertEqual(rename_folder(issue, dry_run=False), "skipped")


class OrganizeCommandTest(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.patch_console = mock.patch.object(
            cli, "console", Console(file=self.out, width=120)
        )
        self.patch_console.start()
        self.addCleanup(self.patch_console.stop)

    def test_renders_tables_with_issues(self):
        issues = [
            FolderIssue("Artist", "Wrong", "Right", "Right", [Path("/x")], "rename"),
        ]
        with mock.patch.object(musiq.folders, "scan_library", return_value=issues), \
             mock.patch.object(musiq.folders, "rename_folder", return_value="skipped"), \
             mock.patch("musiq.cli.click.confirm", return_value=False):
            cli.organize.callback(do_apply=False, no_navidrome=True)

        text = self.out.getvalue()
        self.assertIn("Album-folder mismatches", text)
        self.assertIn("Wrong", text)
        self.assertIn("Right", text)

    def test_single_suffix_mismatch_flagged_in_table(self):
        """A '- Single' folder whose name differs from the tag appears in the table."""
        issues = [
            FolderIssue("Gunna", "kills - Single", "kills",
                        "kills", [Path("/x")], "rename"),
        ]
        with mock.patch.object(musiq.folders, "scan_library", return_value=issues):
            cli.organize.callback(do_apply=False, no_navidrome=True)

        text = self.out.getvalue()
        self.assertIn("Album-folder mismatches", text)
        self.assertIn("kills - Single", text)

    def test_empty_scan_prints_no_mismatches(self):
        with mock.patch.object(musiq.folders, "scan_library", return_value=[]):
            cli.organize.callback(do_apply=False, no_navidrome=True)

        text = self.out.getvalue()
        self.assertIn("No album-folder mismatches found", text)

    def test_apply_processes_all_applicable_issues(self):
        issues = [
            FolderIssue("A", "Wrong", "Right", "Right", [], "rename"),
            FolderIssue("A", "Source", "Target", "Target", [Path("/x")], "merge"),
            FolderIssue("A", "Other", "Existing", "Existing", [Path("/x")], "collision"),
        ]
        with mock.patch.object(musiq.folders, "scan_library", return_value=issues), \
             mock.patch.object(musiq.folders, "rename_folder",
                               return_value="renamed") as mock_rename, \
             mock.patch("musiq.cli.click.confirm", return_value=True):
            cli.organize.callback(do_apply=True, no_navidrome=True)

        self.assertEqual(mock_rename.call_count, 3)
        text = self.out.getvalue()
        self.assertIn("Done:", text)
        self.assertIn("3 renamed, 0 merged", text)

    def test_apply_confirms_each_move(self):
        issues = [
            FolderIssue("A", "One", "T1", "T1", [], "rename"),
            FolderIssue("A", "Two", "T2", "T2", [], "rename"),
        ]
        with mock.patch.object(musiq.folders, "scan_library", return_value=issues), \
             mock.patch.object(musiq.folders, "rename_folder",
                               return_value="renamed") as mock_rename, \
             mock.patch("musiq.cli.click.confirm",
                        side_effect=[False, True]) as mock_confirm:
            cli.organize.callback(do_apply=True, no_navidrome=True)

        self.assertEqual(mock_confirm.call_count, 2)
        self.assertEqual(mock_rename.call_count, 1)
        self.assertIn("Skipped: A / One", self.out.getvalue())

    def test_apply_reports_collision_result(self):
        issues = [
            FolderIssue("A", "Other", "Existing", "Existing", [Path("/x")], "collision"),
        ]
        with mock.patch.object(musiq.folders, "scan_library", return_value=issues), \
             mock.patch.object(musiq.folders, "rename_folder",
                               return_value="collision") as mock_rename, \
             mock.patch("musiq.cli.click.confirm", return_value=True):
            cli.organize.callback(do_apply=True, no_navidrome=True)

        self.assertEqual(mock_rename.call_count, 1)
        self.assertIn("collided during apply", self.out.getvalue())

    def test_organise_alias_registered(self):
        self.assertIn("organise", cli.cli.commands)

    def test_mixed_only_reports_nothing_to_move(self):
        issues = [
            FolderIssue("A", "Shared", "Album A", "Album A", [Path("/x")], "mixed"),
        ]
        with mock.patch.object(musiq.folders, "scan_library", return_value=issues), \
             mock.patch.object(musiq.folders, "rename_folder",
                               return_value="renamed") as mock_rename:
            cli.organize.callback(do_apply=True, no_navidrome=True)

        self.assertEqual(mock_rename.call_count, 0)
        text = self.out.getvalue()
        self.assertIn("mixed tags need manual review", text)

    def test_apply_merges_reported_in_summary(self):
        issues = [
            FolderIssue("A", "Source", "Target", "Target", [Path("/x")], "merge"),
        ]
        with mock.patch.object(musiq.folders, "scan_library", return_value=issues), \
             mock.patch.object(musiq.folders, "rename_folder",
                               return_value="merged") as mock_rename, \
             mock.patch("musiq.cli.click.confirm", return_value=True):
            cli.organize.callback(do_apply=True, no_navidrome=True)

        self.assertEqual(mock_rename.call_count, 1)
        text = self.out.getvalue()
        self.assertIn("Done:", text)
        self.assertIn("0 renamed, 1 merged", text)


if __name__ == "__main__":
    sys.exit(unittest.main())
