"""Tests for the per-field retag comparison and override UI.

Covers the candidate grouping shown in the comparison table, the 
near-duplicate collapse that keeps low-stakes fields compact, the letter
(upper-level) / number (per-field candidate) selection split, and the
per-field override flows. Provider dicts are synthetic so the assertions
are deterministic — no network calls.

Two known real-world disagreements are exercised:
  * "Wyclef Jean" — Young Thug: a weak iTunes album pick vs a confident
    Deezer one ("J'ouvert (Deluxe Edition)" vs "JEFFERY").
  * "You Was Right" — Lil Uzi Vert: iTunes says the album is "Luv Is Rage
    2" (2017), Deezer + Last.fm say "Lil Uzi Vert vs. The World" (2016);
    the genre values "Hip-Hop/Rap" vs "Rap/Hip Hop" are the same genre
    written differently and must collapse to one line.
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


def _providers_wyclef():
    """Weak iTunes "J'ouvert (Deluxe Edition)" vs confident Deezer "JEFFERY"."""
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
    lastfm = {"title": "", "artist": "", "album": "", "track_num": 0,
              "duration_ms": 0, "toptags": [], "mbid": "", "url": ""}
    return mb, itunes, deezer, lastfm


def _providers_you_was_right():
    """iTunes "Luv Is Rage 2" (2017) vs Deezer+Last.fm "Lil Uzi Vert vs. The World" (2016)."""
    mb = {
        "ok": False, "error": "no matching recording", "album": "", "year": "",
        "genre": "", "match": False, "reason": "", "candidates": 0,
        "rg_title": "", "release_title": "", "primary_type": "",
        "secondary_types": [], "first_release_date": "", "recording_title": "",
    }
    itunes = {
        "genre": "Hip-Hop/Rap", "year": "2017", "artwork_url": "",
        "album": "Luv Is Rage 2", "confidence": 1.0,
    }
    deezer = {
        "ok": True, "confidence": 1.0, "title": "You Was Right",
        "artist": "Lil Uzi Vert", "album": "Lil Uzi Vert vs. The World",
        "year": "2016", "genre": "Rap/Hip Hop", "track_num": 0,
        "duration_ms": 189000, "artwork_url": "", "id": "130914362",
        "url": "", "error": "",
    }
    lastfm = {
        "title": "", "artist": "", "album": "Lil Uzi Vert vs. the World",
        "track_num": 0, "duration_ms": 189000, "toptags": [],
        "mbid": "", "url": "",
    }
    return mb, itunes, deezer, lastfm


_WY = dict(TAGS, album="You Was Right - Single", title="You Was Right")


class RetagDisagreementTest(unittest.TestCase):
    def test_album_disagreement_surfaces(self):
        mb, itunes, deezer, lastfm = _providers_wyclef()
        proposed = {"album": "JEFFERY"}
        cands = _retag_field_candidates(
            _WY, proposed, "album", mb, itunes, deezer, lastfm
        )
        self.assertTrue(cands["disputed"])
        values = [g["display"] for g in cands["groups"]]
        self.assertEqual(
            set(values), {"JEFFERY", "J'ouvert (Deluxe Edition)"}
        )
        by_value = {g["display"]: g for g in cands["groups"]}
        self.assertEqual(by_value["JEFFERY"]["providers"], ["Deezer"])
        self.assertIn("weak match", by_value["J'ouvert (Deluxe Edition)"]["caveats"])
        self.assertEqual(by_value["JEFFERY"]["caveats"], [])

    def test_proposing_group_comes_first(self):
        mb, itunes, deezer, lastfm = _providers_wyclef()
        cands = _retag_field_candidates(
            _WY, {"album": "JEFFERY"}, "album", mb, itunes, deezer, lastfm
        )
        self.assertEqual(cands["proposing"], 0)
        self.assertEqual(cands["groups"][0]["display"], "JEFFERY")

    def test_you_was_right_album_and_year_disagree(self):
        mb, itunes, deezer, lastfm = _providers_you_was_right()
        proposed = _propose_metadata(
            dict(_WY), mb, itunes, deezer, lastfm, "You Was Right", "Lil Uzi Vert"
        )
        for field, expected_set in (
            ("album", {"Luv Is Rage 2", "Lil Uzi Vert vs. The World"}),
            ("year", {"2016", "2017"}),
        ):
            cands = _retag_field_candidates(
                _WY, proposed, field, mb, itunes, deezer, lastfm
            )
            self.assertTrue(cands["disputed"], field)
            self.assertEqual(
                {g["display"] for g in cands["groups"]}, expected_set
            )
        self.assertEqual(proposed["album"], "Luv Is Rage 2")

    def test_near_similar_genres_collapse(self):
        mb, itunes, deezer, lastfm = _providers_you_was_right()
        proposed = _propose_metadata(
            dict(_WY), mb, itunes, deezer, lastfm, "You Was Right", "Lil Uzi Vert"
        )
        cands = _retag_field_candidates(
            _WY, proposed, "genre", mb, itunes, deezer, lastfm
        )
        self.assertFalse(cands["disputed"])
        self.assertEqual(
            {g["display"] for g in cands["groups"]},
            {"Hip-Hop", "Rap/Hip Hop"},
        )

    def test_years_differing_is_a_real_disagreement(self):
        mb, itunes, deezer, lastfm = _providers_you_was_right()
        cands = _retag_field_candidates(
            _WY, {"year": "2016"}, "year", mb, itunes, deezer, lastfm
        )
        self.assertTrue(cands["disputed"])

    def test_agreed_field_is_compact(self):
        mb, itunes, deezer, lastfm = _providers_wyclef()
        proposed = {"album": "JEFFERY", "year": "2016", "genre": "Hip-Hop"}
        itunes.update(confidence=1.0, album="JEFFERY")
        deezer["album"] = "JEFFERY"
        mb.update(ok=True, match=True, album="JEFFERY")
        cands = _retag_field_candidates(
            TAGS, proposed, "album", mb, itunes, deezer, lastfm
        )
        self.assertFalse(cands["disputed"])
        self.assertEqual(len(cands["groups"]), 1)
        self.assertEqual(cands["groups"][0]["display"], "JEFFERY")
        self.assertEqual(
            set(cands["groups"][0]["providers"]),
            {"MusicBrainz", "iTunes", "Deezer"},
        )

    def test_table_renders_arrow_marked_breakdown_without_discogs(self):
        mb, itunes, deezer, lastfm = _providers_wyclef()
        proposed = _propose_metadata(
            dict(_WY), mb, itunes, deezer, lastfm, "You Was Right", "Lil Uzi Vert"
        )
        out = io.StringIO()
        Console(file=out, width=200).print(
            _retag_table(_WY, proposed, mb, itunes, deezer, lastfm),
            no_wrap=True,
        )
        text = out.getvalue()
        self.assertIn("J'ouvert (Deluxe Edition)", text)
        self.assertIn("[iTunes — weak match]", text)
        self.assertNotRegex(text, r"(?i)discogs")
        self.assertNotIn("1)", text)


class RetagActionTest(unittest.TestCase):
    def test_yes_fastpath_actions(self):
        for c in ("", "y", "Y", "yes", "YES"):
            self.assertEqual(_retag_action(c), "yes")
        for c in ("s", "skip", "Skip"):
            self.assertEqual(_retag_action(c), "skip")

    def test_field_selected_by_letter(self):
        letters = "abcdefg"
        fields = ["title", "artist", "album", "track_num", "year", "genre", "composer"]
        for letter, field in zip(letters, fields):
            self.assertEqual(_retag_action(letter), field)
            self.assertEqual(_retag_action(letter.upper()), field)

    def test_field_selected_by_name_or_prefix(self):
        self.assertEqual(_retag_action("album"), "album")
        self.assertEqual(_retag_action("Genre"), "genre")
        self.assertEqual(_retag_action("track"), "track_num")
        self.assertEqual(_retag_action("composer"), "composer")

    def test_numbers_are_not_field_selectors(self):
        for c in ("1", "2", "9", "0"):
            self.assertIsNone(_retag_action(c))

    def test_ambiguous_and_unknown_rejected(self):
        self.assertIsNone(_retag_action("t"))
        self.assertIsNone(_retag_action("bogus"))


class RetagOverrideTest(unittest.TestCase):
    def _prompt(self, answers):
        return mock.patch("musiq.cli._prompt", side_effect=answers)

    def _quiet(self):
        return mock.patch("musiq.cli.console.print")

    def test_pick_alternative_candidate_by_number(self):
        mb, itunes, deezer, lastfm = _providers_wyclef()
        proposed = _propose_metadata(
            dict(_WY), mb, itunes, deezer, lastfm, "Wyclef Jean", "Young Thug"
        )
        with self._prompt(["2"]), self._quiet():
            out = _retag_prompt_field(
                _WY, proposed, "album", mb, itunes, deezer, lastfm
            )
        self.assertEqual(out["album"], "J'ouvert (Deluxe Edition)")

    def test_keep_current_value(self):
        mb, itunes, deezer, lastfm = _providers_wyclef()
        proposed = _propose_metadata(
            dict(_WY), mb, itunes, deezer, lastfm, "Wyclef Jean", "Young Thug"
        )
        with self._prompt(["0"]), self._quiet():
            out = _retag_prompt_field(
                _WY, proposed, "album", mb, itunes, deezer, lastfm
            )
        self.assertEqual(out["album"], _WY["album"])

    def test_type_custom_value(self):
        mb, itunes, deezer, lastfm = _providers_wyclef()
        proposed = _propose_metadata(
            dict(_WY), mb, itunes, deezer, lastfm, "Wyclef Jean", "Young Thug"
        )
        with self._prompt(["Gangsta Rap"]), self._quiet():
            out = _retag_prompt_field(
                _WY, proposed, "genre", mb, itunes, deezer, lastfm
            )
        self.assertEqual(out["genre"], "Gangsta Rap")

    def test_year_validation_loops(self):
        mb, itunes, deezer, lastfm = _providers_you_was_right()
        proposed = _propose_metadata(
            dict(_WY), mb, itunes, deezer, lastfm, "You Was Right", "Lil Uzi Vert"
        )
        with self._prompt(["nope", "2017"]), self._quiet():
            out = _retag_prompt_field(
                _WY, proposed, "year", mb, itunes, deezer, lastfm
            )
        self.assertEqual(out["year"], "2017")

    def test_artist_override_syncs_artists(self):
        mb, itunes, deezer, lastfm = _providers_wyclef()
        proposed = _propose_metadata(
            dict(_WY), mb, itunes, deezer, lastfm, "Wyclef Jean", "Young Thug"
        )
        with self._prompt(["Bob, Alice"]), self._quiet():
            out = _retag_prompt_field(
                _WY, proposed, "artist", mb, itunes, deezer, lastfm
            )
        self.assertEqual(out["artist"], "Bob, Alice")
        self.assertEqual(out["artists"], ["Bob, Alice"])

    def test_empty_entry_keeps_proposal(self):
        mb, itunes, deezer, lastfm = _providers_wyclef()
        proposed = _propose_metadata(
            dict(_WY), mb, itunes, deezer, lastfm, "Wyclef Jean", "Young Thug"
        )
        original = dict(proposed)
        with self._prompt([""]), self._quiet():
            out = _retag_prompt_field(
                _WY, proposed, "album", mb, itunes, deezer, lastfm
            )
        self.assertEqual(out["album"], original["album"])


if __name__ == "__main__":
    unittest.main()