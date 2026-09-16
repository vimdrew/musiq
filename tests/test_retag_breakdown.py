"""Tests for the per-field retag comparison and override UI.

Covers the candidate grouping shown in the comparison table, the caveats
attached to specific candidates (weak iTunes/Deezer/short Discogs), and the
action classifier for the interactive prompt. Provider dicts are synthetic so
the assertions are deterministic — no network calls.
"""

import io
import unittest
from unittest import mock

from rich.console import Console

from musiq.cli import (
    _propose_metadata,
    _retag_action,
    _retag_field_candidates,
    _retag_prompt_field,
    _retag_table,
)

TAGS = {
    "title": "Wyclef Jean",
    "artist": "Young Thug",
    "artists": ["Young Thug"],
    "album": "JEFFERY",
    "track_num": 0,
    "year": "2016",
    "genre": "Hip-Hop",
    "composer": "",
}


def _wyclef_providers():
    """Providers for Young Thug — "Wyclef Jean".

    iTunes returns a weak (0.19) wrong album; Deezer is confident (1.0) and
    correct. Discogs/MusicBrainz/Last.fm contribute nothing.
    """
    mb = {
        "ok": False, "error": "", "album": "", "year": "", "genre": "",
        "genres": [], "match": False, "reason": "", "candidates": 0,
        "rg_title": "", "release_title": "", "primary_type": "",
        "secondary_types": [], "first_release_date": "", "recording_title": "",
    }
    itunes = {
        "genre": "Hip-Hop/Rap", "year": "2016", "artwork_url": "",
        "album": "J'ouvert (Deluxe Edition)", "confidence": 0.19,
    }
    deezer = {
        "ok": True, "confidence": 1.0, "title": "Wyclef Jean",
        "artist": "Young Thug", "album": "JEFFERY", "year": "2016",
        "genre": "Rap/Hip Hop", "track_num": 0, "duration_ms": 236000,
        "artwork_url": "", "id": "130914062", "url": "", "error": "",
    }
    lastfm = {
        "title": "", "artist": "", "album": "", "track_num": 0,
        "duration_ms": 0, "toptags": [],
    }
    discogs = {
        "ok": False, "error": "no credentials", "album": "", "year": "",
        "genre": "", "styles": [], "track_num": 0, "composer": "",
        "data_quality": "", "match": False,
    }
    return mb, itunes, deezer, lastfm, discogs


def _cands(field, proposed, **kw):
    mb, itunes, deezer, lastfm, discogs = _wyclef_providers()
    mb.update(kw)
    return _retag_field_candidates(
        TAGS, proposed, field, mb, itunes, deezer, lastfm, discogs
    )


class RetagDisagreementTest(unittest.TestCase):
    def test_disagreement_groups_both_album_candidates(self):
        proposed = {"album": "JEFFERY"}
        cands = _cands("album", proposed)
        self.assertTrue(cands["disputed"])
        values = [g["display"] for g in cands["groups"]]
        self.assertEqual(
            set(values), {"JEFFERY", "J'ouvert (Deluxe Edition)"}
        )
        by_value = {g["display"]: g for g in cands["groups"]}
        self.assertEqual(by_value["JEFFERY"]["providers"], ["Deezer"])
        self.assertEqual(
            by_value["J'ouvert (Deluxe Edition)"]["providers"], ["iTunes"]
        )

    def test_weak_itunes_candidate_carries_caveat(self):
        cands = _cands("album", {"album": "JEFFERY"})
        by_value = {g["display"]: g for g in cands["groups"]}
        self.assertIn("weak match", by_value["J'ouvert (Deluxe Edition)"]["caveats"])
        self.assertEqual(by_value["JEFFERY"]["caveats"], [])

    def test_weak_deezer_candidate_carries_caveat(self):
        proposed = {"album": "JEFFERY"}
        mb, itunes, deezer, lastfm, discogs = _wyclef_providers()
        deezer["confidence"] = 0.4
        cands = _retag_field_candidates(
            TAGS, proposed, "album", mb, itunes, deezer, lastfm, discogs
        )
        by_value = {g["display"]: g for g in cands["groups"]}
        self.assertIn("weak match", by_value["JEFFERY"]["caveats"])

    def test_discogs_data_quality_carries_caveat(self):
        proposed = {"album": "JEFFERY"}
        mb, itunes, deezer, lastfm, discogs = _wyclef_providers()
        discogs.update(
            ok=True, data_quality="Needs Vote", album="J'ouvert (Deluxe Edition)",
            year="2016", genre="Hip-Hop", track_num=1,
        )
        cands = _retag_field_candidates(
            TAGS, proposed, "album", mb, itunes, deezer, lastfm, discogs
        )
        by_value = {g["display"]: g for g in cands["groups"]}
        self.assertEqual(
            by_value["J'ouvert (Deluxe Edition)"]["caveats"],
            ["weak match", "Needs Vote"],
        )

    def test_proposing_group_comes_first(self):
        cands = _cands("album", {"album": "JEFFERY"})
        self.assertEqual(cands["proposing"], 0)
        self.assertEqual(cands["groups"][0]["display"], "JEFFERY")

    def test_proposal_from_tags_keeps_current(self):
        cands = _cands("album", {"album": "Something Else"})
        self.assertIsNone(cands["proposing"])
        self.assertIn(
            "JEFFERY", [g["display"] for g in cands["groups"]]
        )

    def test_merge_prefers_confident_deezer_over_weak_itunes(self):
        mb, itunes, deezer, lastfm, discogs = _wyclef_providers()
        proposed = _propose_metadata(
            dict(TAGS), mb, itunes, deezer, lastfm, discogs,
            "Wyclef Jean", "Young Thug",
        )
        self.assertEqual(proposed["album"], "JEFFERY")

    def test_agreed_field_is_compact(self):
        proposed = {"album": "JEFFERY", "year": "2016", "genre": "Hip-Hop"}
        mb, itunes, deezer, lastfm, discogs = _wyclef_providers()
        itunes.update(confidence=1.0, album="JEFFERY")
        deezer["album"] = "JEFFERY"
        mb.update(ok=True, match=True, album="JEFFERY")
        cands = _retag_field_candidates(
            TAGS, proposed, "album", mb, itunes, deezer, lastfm, discogs
        )
        self.assertFalse(cands["disputed"])
        self.assertEqual(len(cands["groups"]), 1)
        self.assertEqual(cands["groups"][0]["display"], "JEFFERY")
        self.assertEqual(
            set(cands["groups"][0]["providers"]),
            {"MusicBrainz", "iTunes", "Deezer"},
        )

    def test_table_renders_breakdown_and_arrow(self):
        proposed = _propose_metadata(
            dict(TAGS), *_wyclef_providers(), "Wyclef Jean", "Young Thug"
        )
        mb, itunes, deezer, lastfm, discogs = _wyclef_providers()
        out = io.StringIO()
        Console(file=out, width=200).print(
            _retag_table(TAGS, proposed, mb, itunes, deezer, lastfm, discogs),
            no_wrap=True,
        )
        text = out.getvalue()
        self.assertIn("J'ouvert (Deluxe Edition)", text)
        self.assertIn("[iTunes — weak match]", text)
        self.assertIn("→ proposing: 1", text)

    def test_yes_fastpath_actions(self):
        for c in ("", "y", "Y", "yes", "YES"):
            self.assertEqual(_retag_action(c), "yes")
        for c in ("s", "skip", "Skip"):
            self.assertEqual(_retag_action(c), "skip")

    def test_field_action_by_number_and_name(self):
        self.assertEqual(_retag_action("1"), "title")
        self.assertEqual(_retag_action("4"), "track_num")
        self.assertEqual(_retag_action("7"), "composer")
        self.assertEqual(_retag_action("album"), "album")
        self.assertEqual(_retag_action("Genre"), "genre")
        self.assertEqual(_retag_action("track"), "track_num")
        self.assertEqual(_retag_action("composer"), "composer")
        self.assertIsNone(_retag_action("9"))
        self.assertIsNone(_retag_action("t"))
        self.assertIsNone(_retag_action("bogus"))


class RetagOverrideTest(unittest.TestCase):
    def _proposed(self):
        return _propose_metadata(
            dict(TAGS), *_wyclef_providers(), "Wyclef Jean", "Young Thug"
        )

    def _prompt(self, answers):
        return mock.patch("musiq.cli._prompt", side_effect=answers)

    def _quiet(self):
        return mock.patch("musiq.cli.console.print")

    def test_pick_alternative_candidate(self):
        with self._prompt(["2"]), self._quiet():
            out = _retag_prompt_field(
                TAGS, self._proposed(), "album", *_wyclef_providers()
            )
        self.assertEqual(out["album"], "J'ouvert (Deluxe Edition)")

    def test_keep_current_value(self):
        with self._prompt(["0"]), self._quiet():
            out = _retag_prompt_field(
                TAGS, self._proposed(), "album", *_wyclef_providers()
            )
        self.assertEqual(out["album"], TAGS["album"])

    def test_type_custom_value(self):
        with self._prompt(["Gangsta Rap"]), self._quiet():
            out = _retag_prompt_field(
                TAGS, self._proposed(), "genre", *_wyclef_providers()
            )
        self.assertEqual(out["genre"], "Gangsta Rap")

    def test_year_validation_loops(self):
        with self._prompt(["nope", "2017"]), self._quiet():
            out = _retag_prompt_field(
                TAGS, self._proposed(), "year", *_wyclef_providers()
            )
        self.assertEqual(out["year"], "2017")

    def test_artist_override_syncs_artists(self):
        with self._prompt(["Bob, Alice"]), self._quiet():
            out = _retag_prompt_field(
                TAGS, self._proposed(), "artist", *_wyclef_providers()
            )
        self.assertEqual(out["artist"], "Bob, Alice")
        self.assertEqual(out["artists"], ["Bob, Alice"])

    def test_empty_entry_keeps_proposal(self):
        proposed = self._proposed()
        original = dict(proposed)
        with self._prompt([""]), self._quiet():
            out = _retag_prompt_field(TAGS, proposed, "album", *_wyclef_providers())
        self.assertEqual(out["album"], original["album"])


if __name__ == "__main__":
    unittest.main()