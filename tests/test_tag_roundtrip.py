"""Regression tests for multi-value ID3 tag write round-trips.

Guards against the write side of ``musiq edit``/``retag`` re-serializing a
multi-artist value as a single literal string. A multi-artist TPE1 frame must
stay multi-value on disk (null-separated in one frame, the on-disk layout the
spotdl/mutagen download pipeline produces) across a full read->write-back
cycle, and a previously flattened comma-joined value must be split back out on
write.
"""

import struct
import tempfile
import unittest
from pathlib import Path

import eyed3
from eyed3.id3 import frames

from musiq.tagger import (
    read_id3_tags,
    rewrite_id3_tags,
    tag_file,
    _split_multi_value,
    _write_text_frame,
)

ARTIST_FID = frames.ARTIST_FID
ALBUM_ARTIST_FID = frames.ALBUM_ARTIST_FID
COMPOSER_FID = frames.COMPOSER_FID


def synth_mp3(path: Path, frames_n: int = 24) -> None:
    """Write a tiny valid MPEG1 Layer3 silent file that eyeD3 can load."""
    header = 0xFFFB9000  # MPEG1 L3, 128kbps, 44100Hz, stereo
    frame = struct.pack(">I", header) + b"\x00" * (417 - 4)
    with open(path, "wb") as fh:
        for _ in range(frames_n):
            fh.write(frame)


def raw_frame_values(path: Path, fid: bytes) -> list[str]:
    audio = eyed3.load(str(path))
    assert audio is not None and audio.tag is not None
    return [f.text for f in (audio.tag.frame_set.get(fid) or [])]


class TagRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="musiq_test_"))
        self.mp3 = self.tmp / "track.mp3"
        synth_mp3(self.mp3)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_multi_artist_survives_read_write_unchanged(self):
        tag_file(
            self.mp3, "Ice Melts", artist="Drake", album="X", track_number=1,
            year="2016", genre="Hip-Hop", artists=["Drake", "Young Thug"],
        )
        self.assertEqual(raw_frame_values(self.mp3, ARTIST_FID), ["Drake\x00Young Thug"])

        tags = read_id3_tags(self.mp3)
        self.assertEqual(tags["artists"], ["Drake", "Young Thug"])

        rewrite_id3_tags(self.mp3, tags)
        values = raw_frame_values(self.mp3, ARTIST_FID)
        self.assertEqual(len(values), 1, "must stay a single multi-value frame")
        self.assertEqual(values, ["Drake\x00Young Thug"], "must not flatten to one string")

    def test_comma_flattened_value_is_split_back_on_write(self):
        audio = eyed3.load(str(self.mp3))
        audio.initTag()
        # Simulate the OLD writer: raw comma-literal on disk (bypass _write_text_frame)
        audio.tag.frame_set[ARTIST_FID] = frames.TextFrame(ARTIST_FID, "Drake, Young Thug")
        audio.tag.save(encoding="utf-8")

        tags = read_id3_tags(self.mp3)
        self.assertEqual(tags["artists"], ["Drake", "Young Thug"],
                         "read of a flattened value must expose both artists")

        rewrite_id3_tags(self.mp3, tags)
        self.assertEqual(
            raw_frame_values(self.mp3, ARTIST_FID),
            ["Drake\x00Young Thug"],
            "unchanged write-back must repair the flattened frame",
        )

    def test_null_separated_frame_written_by_edit_path_survives(self):
        audio = eyed3.load(str(self.mp3))
        audio.initTag()
        _write_text_frame(audio.tag, ARTIST_FID, ["Drake", "Kanye West"])
        audio.tag.save(encoding="utf-8")

        tags = read_id3_tags(self.mp3)
        rewrite_id3_tags(self.mp3, tags)  # what musiq edit runs on accept

        self.assertEqual(raw_frame_values(self.mp3, ARTIST_FID), ["Drake\x00Kanye West"])

    def test_album_artist_and_composer_multi_values_survive(self):
        audio = eyed3.load(str(self.mp3))
        audio.initTag()
        _write_text_frame(audio.tag, ALBUM_ARTIST_FID, ["Various", "Artists"])
        _write_text_frame(audio.tag, COMPOSER_FID, "Producer One, Producer Two")
        audio.tag.save(encoding="utf-8")

        tags = read_id3_tags(self.mp3)
        self.assertEqual(tags["album_artist"], "Various, Artists")
        self.assertEqual(tags["composer"], "Producer One, Producer Two")

        rewrite_id3_tags(self.mp3, tags)
        self.assertEqual(raw_frame_values(self.mp3, ALBUM_ARTIST_FID), ["Various\x00Artists"])
        self.assertEqual(raw_frame_values(self.mp3, COMPOSER_FID), ["Producer One\x00Producer Two"])

    def test_split_multi_value_handles_structured_and_joined_inputs(self):
        self.assertEqual(_split_multi_value(["Drake", "Young Thug"]), ["Drake", "Young Thug"])
        self.assertEqual(_split_multi_value("Drake, Young Thug"), ["Drake", "Young Thug"])
        self.assertEqual(_split_multi_value("Drake\x00Young Thug"), ["Drake", "Young Thug"])
        self.assertEqual(_split_multi_value("Drake/Young Thug"), ["Drake", "Young Thug"])
        self.assertEqual(_split_multi_value("Travis Scott/Kacy Hill"), ["Travis Scott", "Kacy Hill"])
        self.assertEqual(_split_multi_value("Travis Scott / Kacy Hill"), ["Travis Scott", "Kacy Hill"])
        self.assertEqual(_split_multi_value(["Drake, Young Thug"]), ["Drake", "Young Thug"])
        self.assertEqual(_split_multi_value(None), [])
        self.assertEqual(_split_multi_value(""), [])

    def test_slash_flattened_artists_are_split_and_repaired_on_write(self):
        audio = eyed3.load(str(self.mp3))
        audio.initTag()
        # Simulate the OLD writer: raw slash-literal on disk (bypass _write_text_frame)
        audio.tag.frame_set[ARTIST_FID] = frames.TextFrame(ARTIST_FID, "Travis Scott/Kacy Hill")
        audio.tag.save(encoding="utf-8")

        tags = read_id3_tags(self.mp3)
        self.assertEqual(tags["artists"], ["Travis Scott", "Kacy Hill"])
        self.assertEqual(tags["artist"], "Travis Scott, Kacy Hill")
        self.assertTrue(tags["artist_repair"], "slash-packed frame must be flagged for repair")

        rewrite_id3_tags(self.mp3, tags)
        self.assertEqual(raw_frame_values(self.mp3, ARTIST_FID), ["Travis Scott\x00Kacy Hill"])

    def test_null_separated_multi_artist_is_not_flagged_for_repair(self):
        tag_file(
            self.mp3, "Ice Melts", artist="Drake", album="X", track_number=1,
            year="2016", genre="Hip-Hop", artists=["Drake", "Young Thug"],
        )
        tags = read_id3_tags(self.mp3)
        self.assertFalse(tags["artist_repair"])


if __name__ == "__main__":
    unittest.main()