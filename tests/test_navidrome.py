"""Tests for the Navidrome unplayed-songs helper."""

import unittest
from unittest import mock

from musiq import navidrome


def _song(sid, title, artist, album="Album", play_count=None):
    return {
        "id": sid,
        "title": title,
        "artist": artist,
        "album": album,
        "playCount": play_count,
        "played": None if play_count is None else "2026-01-01",
    }


class GetUnplayedSongsTest(unittest.TestCase):
    def _run(self, pages):
        """pages: list of lists returned by successive _api_call invocations."""
        calls = []

        def fake_api_call(base_url, user, password, endpoint, **extra):
            calls.append(extra)
            page = pages[len(calls) - 1] if len(calls) <= len(pages) else []
            return {"searchResult3": {"song": page}}

        with mock.patch.object(navidrome, "_api_call", side_effect=fake_api_call):
            result = navidrome.get_unplayed_songs("http://nd", "u", "p", page_size=2)
        return result, calls

    def test_keeps_only_zero_or_missing_playcount(self):
        page = [
            _song("1", "Never", "Alpha", play_count=0),
            _song("2", "Absent", "Beta", play_count=None),
            _song("3", "Played", "Gamma", play_count=5),
        ]
        result, _ = self._run([page])
        self.assertEqual([s["id"] for s in result], ["1", "2"])

    def test_pagination_stops_on_short_page(self):
        p1 = [_song("1", "A", "A", play_count=0), _song("2", "B", "B", play_count=0)]
        p2 = [_song("3", "C", "C", play_count=0)]
        result, calls = self._run([p1, p2])
        self.assertEqual([s["id"] for s in result], ["1", "2", "3"])
        self.assertEqual([c["songOffset"] for c in calls], [0, 2])
        self.assertTrue(all(c["query"] == "" for c in calls))

    def test_sorted_by_artist_album_title_case_insensitive(self):
        page = [
            _song("1", "Zebra", "beta", album="X", play_count=0),
            _song("2", "apple", "Alpha", album="Y", play_count=0),
            _song("3", "Banana", "Alpha", album="Y", play_count=0),
        ]
        result, _ = self._run([page])
        self.assertEqual([s["id"] for s in result], ["2", "3", "1"])

    def test_empty_library(self):
        result, calls = self._run([[]])
        self.assertEqual(result, [])
        self.assertEqual(len(calls), 1)

    def test_full_page_then_empty_terminates(self):
        full = [_song(str(i), f"t{i}", "A", play_count=0) for i in range(2)]
        result, calls = self._run([full, []])
        self.assertEqual(len(result), 2)
        self.assertEqual([c["songOffset"] for c in calls], [0, 2])


if __name__ == "__main__":
    unittest.main()