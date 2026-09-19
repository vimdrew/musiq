"""Tests for Deezer enrichment in the download pipeline.

Covers the pure metadata resolution (Deezer preferred when confident,
otherwise existing iTunes logic unchanged) and the album-batch caching /
``--no-deezer`` wiring in ``handle_url``.
"""

import unittest
from unittest import mock

from musiq import deezer
from musiq.downloader import _resolve_metadata, handle_url


def _deezer(**over) -> dict:
    base = {
        "ok": True,
        "confidence": 0.95,
        "title": "Song",
        "artist": "Artist",
        "album": "Deezer Album",
        "year": "2019",
        "genre": "Deezer Genre",
        "artwork_url": "https://cdn.deezer.com/cover.jpg",
    }
    base.update(over)
    return base


def _itunes(**over) -> dict:
    base = {
        "genre": "iTunes Genre",
        "year": "2001",
        "artwork_url": "https://itunes.apple.com/cover.jpg",
        "album": "iTunes Album",
        "confidence": 0.9,
    }
    base.update(over)
    return base


class ResolveMetadataTest(unittest.TestCase):
    def test_deezer_confident_wins(self):
        meta = {"album": "Song - Single", "cover_url": "spotify.jpg", "year": ""}
        album, cover, genre, year = _resolve_metadata(meta, _itunes(), _deezer(), "Song")
        self.assertEqual(album, "Deezer Album")
        self.assertEqual(cover, "spotify.jpg")
        self.assertEqual(genre, "Deezer Genre")
        self.assertEqual(year, "2019")

    def test_spotify_year_beats_deezer_year(self):
        meta = {"album": "Album", "cover_url": None, "year": "2024"}
        _, _, _, year = _resolve_metadata(meta, _itunes(), _deezer(year="2019"), "Song")
        self.assertEqual(year, "2024")

    def test_deezer_cover_used_when_spotify_missing(self):
        meta = {"album": "Album", "cover_url": None, "year": "2024"}
        _, cover, _, _ = _resolve_metadata(meta, _itunes(), _deezer(), "Song")
        self.assertEqual(cover, "https://cdn.deezer.com/cover.jpg")

    def test_itunes_cover_used_when_deezer_not_confident(self):
        meta = {"album": "Album", "cover_url": None, "year": "2024"}
        _, cover, _, _ = _resolve_metadata(meta, _itunes(), _deezer(confidence=0.5), "Song")
        self.assertEqual(cover, "https://itunes.apple.com/cover.jpg")

    def test_weak_deezer_falls_back_to_itunes(self):
        meta = {"album": "Song - Single", "cover_url": "spotify.jpg", "year": ""}
        album, _, genre, year = _resolve_metadata(meta, _itunes(), _deezer(confidence=0.8), "Song")
        self.assertEqual(album, "iTunes Album")
        self.assertEqual(genre, "iTunes Genre")
        self.assertEqual(year, "2001")

    def test_no_deezer_hit_preserves_itunes_logic(self):
        meta = {"album": "Song - Single", "cover_url": "spotify.jpg", "year": ""}
        album, _, genre, year = _resolve_metadata(meta, _itunes(), {"ok": False}, "Song")
        self.assertEqual(album, "iTunes Album")
        self.assertEqual(genre, "iTunes Genre")
        self.assertEqual(year, "2001")

    def test_low_confidence_itunes_uses_resolved_fallbacks(self):
        meta = {
            "album": "",
            "cover_url": None,
            "year": "",
            "resolved_genre": "Canonical Genre",
            "resolved_year": "1999",
        }
        album, _, genre, year = _resolve_metadata(meta, _itunes(confidence=0.4, album=""), {}, "Song")
        self.assertEqual(album, "Song - Single")
        self.assertEqual(genre, "Canonical Genre")
        self.assertEqual(year, "1999")


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class AlbumDetailCacheTest(unittest.TestCase):
    def setUp(self):
        with deezer._ALBUM_CACHE_LOCK:
            deezer._ALBUM_DETAIL_CACHE.clear()

    def tearDown(self):
        with deezer._ALBUM_CACHE_LOCK:
            deezer._ALBUM_DETAIL_CACHE.clear()

    def test_album_detail_fetched_once_per_album(self):
        track = {
            "id": 1,
            "title": "Song",
            "artist": {"name": "Artist"},
            "album": {"id": 123, "title": "Album", "cover_xl": "cover.jpg"},
            "track_position": 3,
            "duration": 200,
        }
        album = {"title": "Album", "release_date": "2019-05-01", "genres": {"data": [{"name": "Rock"}]}}
        album_calls = []

        def fake_get(url, params=None, timeout=None):
            if "/search" in url:
                return _Resp({"data": [track]})
            if "/album/123" in url:
                album_calls.append(url)
                return _Resp(album)
            return _Resp({})

        with mock.patch.object(deezer.requests, "get", side_effect=fake_get):
            first = deezer.fetch_track_metadata("Artist", "Song")
            second = deezer.fetch_track_metadata("Artist", "Song")

        self.assertTrue(first["ok"])
        self.assertEqual(first["genre"], "Rock")
        self.assertEqual(first["year"], "2019")
        self.assertEqual(second["album"], "Album")
        self.assertEqual(len(album_calls), 1)


class HandleUrlAlbumBatchTest(unittest.TestCase):
    def _tracks(self):
        return [
            {
                "id": "t1", "title": "One", "artist": "Artist", "artists": ["Artist"],
                "album": "Album", "year": "2020", "_source_type": "album",
            },
            {
                "id": "t2", "title": "Two", "artist": "Artist", "artists": ["Artist"],
                "album": "Album", "year": "2020", "_source_type": "album",
            },
        ]

    def test_album_batch_populates_deezer_cache(self):
        tracks = self._tracks()
        with (
            mock.patch("musiq.downloader.resolve_entity", return_value=tracks),
            mock.patch("musiq.downloader.itunes_search", return_value=_itunes()),
            mock.patch("musiq.downloader.deezer_search", return_value=_deezer()) as dz,
            mock.patch("musiq.downloader.process_track", return_value={"status": "downloaded"}),
        ):
            handle_url("https://open.spotify.com/album/abc")
        self.assertEqual(dz.call_count, 2)
        self.assertEqual(tracks[0]["_deezer_cache"]["genre"], "Deezer Genre")
        self.assertEqual(tracks[1]["_deezer_cache"]["year"], "2019")

    def test_no_deezer_skips_batch_prefetch(self):
        tracks = self._tracks()
        with (
            mock.patch("musiq.downloader.resolve_entity", return_value=tracks),
            mock.patch("musiq.downloader.itunes_search", return_value=_itunes()),
            mock.patch("musiq.downloader.deezer_search", return_value=_deezer()) as dz,
            mock.patch("musiq.downloader.process_track", return_value={"status": "downloaded"}),
        ):
            handle_url("https://open.spotify.com/album/abc", no_deezer=True)
        self.assertEqual(dz.call_count, 0)
        self.assertNotIn("_deezer_cache", tracks[0])


if __name__ == "__main__":
    unittest.main()
