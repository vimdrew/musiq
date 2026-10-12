"""Tests for canonical album voting in album downloads.

Steps 1-3 of the album-consistency fix: an album batch votes a single
majority album name and locks it, so one track with a rogue per-track
Deezer/iTunes hit (single / EP / compilation) can't tag itself with a
different album than the rest of the album.
"""

import unittest
from unittest import mock

from musiq.downloader import (
    _canonical_album,
    _enrich_album_batch,
    _resolve_metadata,
    handle_url,
)


def _deezer(**over) -> dict:
    base = {
        "ok": True,
        "confidence": 0.95,
        "title": "Song",
        "artist": "NewDad",
        "album": "Madra",
        "year": "2024",
        "genre": "Rock",
        "artwork_url": "https://cdn.deezer.com/cover.jpg",
    }
    base.update(over)
    return base


def _itunes(**over) -> dict:
    base = {
        "genre": "Alternative",
        "year": "2024",
        "artwork_url": "https://itunes.apple.com/cover.jpg",
        "album": "Madra",
        "confidence": 0.9,
    }
    base.update(over)
    return base


def _track(tid: str, album: str = "Madra", deezer_album: str | None = None) -> dict:
    track = {
        "id": tid,
        "title": f"Song {tid}",
        "artist": "NewDad",
        "artists": ["NewDad"],
        "album": album,
        "album_artist": "NewDad",
        "cover_url": "https://spotify/c.jpg",
        "year": "2024",
        "_source_type": "album",
    }
    if deezer_album is not None:
        track["_deezer_cache"] = _deezer(album=deezer_album)
    return track


class CanonicalAlbumTest(unittest.TestCase):
    def test_majority_album_wins(self):
        tracks = [
            _track("t1", deezer_album="Madra"),
            _track("t2", deezer_album="Madra"),
            _track("t3", deezer_album="Madra"),
            _track("t4", deezer_album="Madra"),
            _track("t5", deezer_album="Madra - Single"),  # the rogue track
        ]
        self.assertEqual(_canonical_album(tracks), "Madra")

    def test_spotify_album_wins_a_tie(self):
        # Spotify gets one vote per track; a rival album can only tie it when
        # every track's Deezer hit names that rival.
        tracks = [
            _track("t1", deezer_album="Deluxe Reissue"),
            _track("t2", deezer_album="Deluxe Reissue"),
        ]
        self.assertEqual(_canonical_album(tracks), "Madra")

    def test_weak_deezer_hits_do_not_vote(self):
        tracks = [
            _track("t1"),
            _track("t2", deezer_album="Madra - Single"),
        ]
        tracks[0]["_deezer_cache"] = _deezer(confidence=0.5, album="Madra")
        self.assertEqual(_canonical_album(tracks), "Madra")

    def test_no_candidates_falls_back_to_spotify(self):
        tracks = [_track("t1"), _track("t2")]
        self.assertEqual(_canonical_album([tracks[0]]), "Madra")


class LockedAlbumTest(unittest.TestCase):
    def test_locked_album_is_not_overridden(self):
        meta = {
            "album": "Madra",
            "cover_url": "spotify.jpg",
            "year": "2024",
            "_album_locked": True,
        }
        album, cover, _, _ = _resolve_metadata(
            meta,
            _itunes(album="iTunes Other"),
            _deezer(album="Deezer Other"),
            "Boat House",
        )
        self.assertEqual(album, "Madra")

    def test_unlocked_album_still_allows_confident_deezer(self):
        meta = {"album": "Madra", "cover_url": "spotify.jpg", "year": "2024"}
        album, *_ = _resolve_metadata(meta, _itunes(), _deezer(album="Deezer Other"), "Boat House")
        self.assertEqual(album, "Deezer Other")


class EnrichAlbumBatchTest(unittest.TestCase):
    def test_all_tracks_share_canonical_album_and_get_locked(self):
        tracks = [
            _track("t1"),
            _track("t2"),
            _track("t3", deezer_album="Madra - Single"),
        ]
        with (
            mock.patch("musiq.downloader.itunes_search", return_value=_itunes()),
            mock.patch(
                "musiq.downloader.deezer_search",
                side_effect=[_deezer(), _deezer(), _deezer(album="Madra - Single")],
            ),
        ):
            _enrich_album_batch(tracks)

        for track in tracks:
            self.assertEqual(track["album"], "Madra")
            self.assertTrue(track["_album_locked"])

    def test_playlist_batches_are_not_enriched(self):
        tracks = [
            {"id": "p1", "title": "A", "album": "Album A", "_source_type": "playlist"},
            {"id": "p2", "title": "B", "album": "Album B", "_source_type": "playlist"},
        ]
        with (
            mock.patch("musiq.downloader.itunes_search") as it,
            mock.patch("musiq.downloader.deezer_search") as dz,
        ):
            _enrich_album_batch(tracks)
        it.assert_not_called()
        dz.assert_not_called()
        self.assertNotIn("_album_locked", tracks[0])

    def test_single_track_is_not_enriched(self):
        tracks = [_track("t1")]
        with mock.patch("musiq.downloader.itunes_search") as it:
            _enrich_album_batch(tracks)
        it.assert_not_called()
        self.assertNotIn("_album_locked", tracks[0])


class HandleUrlCanonicalTest(unittest.TestCase):
    def test_album_batch_rogue_deezer_does_not_split_album(self):
        tracks = [
            _track("t1", deezer_album="Madra"),
            _track("t2", deezer_album="Madra"),
            _track("t3", deezer_album="Madra - Single"),
        ]
        with (
            mock.patch("musiq.downloader.resolve_entity", return_value=tracks),
            mock.patch("musiq.downloader.itunes_search", return_value=_itunes()),
            mock.patch(
                "musiq.downloader.deezer_search",
                side_effect=[_deezer(), _deezer(), _deezer(album="Madra - Single")],
            ),
            mock.patch("musiq.downloader.process_track", return_value={"status": "downloaded"}),
        ):
            handle_url("https://open.spotify.com/album/abc")

        for track in tracks:
            self.assertEqual(track["album"], "Madra")
            self.assertTrue(track["_album_locked"])


class FallbackAlbumCanonicalTest(unittest.TestCase):
    def _entries(self):
        return [
            {
                "id": "t1", "title": "A", "artist": "NewDad", "album": "Madra",
                "album_artist": "NewDad", "track_number": 1, "duration_ms": 1000, "cover_url": "",
            },
            {
                "id": "t2", "title": "B", "artist": "NewDad", "album": "Madra",
                "album_artist": "NewDad", "track_number": 2, "duration_ms": 1000, "cover_url": "",
            },
        ]

    def test_fallback_album_gets_canonical_album(self):
        seen = []

        def fake_process(track, **kwargs):
            seen.append(track)
            return {"status": "downloaded"}

        with (
            mock.patch("musiq.downloader.resolve_entity", return_value=None),
            mock.patch("musiq.spotify.resolve_embed_track_list", return_value=self._entries()),
            mock.patch("musiq.spotify.resolve_track", return_value=None),
            mock.patch("musiq.downloader.itunes_search", return_value=_itunes()),
            mock.patch(
                "musiq.downloader.deezer_search",
                side_effect=[_deezer(), _deezer(album="Madra - Single")],
            ),
            mock.patch("musiq.downloader.process_track", side_effect=fake_process),
        ):
            handle_url("https://open.spotify.com/album/xyz")

        self.assertEqual(len(seen), 2)
        for track in seen:
            self.assertEqual(track["album"], "Madra")
            self.assertTrue(track["_album_locked"])

    def test_fallback_playlist_is_not_locked(self):
        entries = [
            {"id": "t1", "title": "A", "artist": "X", "album": "Album A",
             "album_artist": "X", "track_number": 1, "duration_ms": 1, "cover_url": ""},
            {"id": "t2", "title": "B", "artist": "Y", "album": "Album B",
             "album_artist": "Y", "track_number": 1, "duration_ms": 1, "cover_url": ""},
        ]
        seen = []

        def fake_process(track, **kwargs):
            seen.append(track)
            return {"status": "downloaded"}

        with (
            mock.patch("musiq.downloader.resolve_entity", return_value=None),
            mock.patch("musiq.spotify.resolve_embed_track_list", return_value=entries),
            mock.patch("musiq.spotify.resolve_track", return_value=None),
            mock.patch("musiq.downloader.deezer_search") as dz,
            mock.patch("musiq.downloader.process_track", side_effect=fake_process),
        ):
            handle_url("https://open.spotify.com/playlist/xyz")

        dz.assert_not_called()
        self.assertFalse(any("_album_locked" in track for track in seen))


if __name__ == "__main__":
    unittest.main()