import os

import requests

from musiq.display import console
from musiq.utils import artist_match as artist_match_confidence


def _score_result(result: dict, title: str, artist: str) -> int:
    score = 0
    collection = result.get("collectionName", "")
    track = result.get("trackName", "")

    if collection.endswith("- Single"):
        score -= 10
    if collection.endswith("- EP"):
        score -= 5

    for word in ["Live", "Remix", "Karaoke", "Tribute", "Cover"]:
        if word.lower() in collection.lower():
            score -= 8

    if track.lower() == title.lower():
        score += 5

    if result.get("artistName", "").lower() == artist.lower():
        score += 3

    return score


def _itunes_country() -> str:
    return os.environ.get("MUSIQ_ITUNES_COUNTRY", "US")


def fetch_metadata(artist: str, title: str) -> dict:
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term": f"{artist} {title}",
                "entity": "song",
                "limit": 5,
                "country": _itunes_country(),
            },
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            best = max(results, key=lambda r: _score_result(r, title, artist))
            meta = _extract(best)
            meta["confidence"] = artist_match_confidence(artist, best.get("artistName", ""))
            return meta
    except Exception:
        pass
    return {"genre": "", "year": "", "artwork_url": "", "album": "", "confidence": 0.0}


def resolve_canonical_album(
    spotify_album: str | None,
    itunes_votes: dict[str, int],
    track_title: str,
) -> str:
    if not itunes_votes:
        return spotify_album or f"{track_title} - Single"

    if spotify_album:
        for name in sorted(itunes_votes, key=itunes_votes.get, reverse=True):
            if spotify_album.lower() in name.lower() or name.lower() in spotify_album.lower():
                return name
        return spotify_album

    return max(itunes_votes, key=itunes_votes.get)


def _extract(r: dict) -> dict:
    artwork = (r.get("artworkUrl100") or "").replace("100x100", "1200x1200")
    year = ""
    rd = r.get("releaseDate", "")
    if rd and len(rd) >= 4:
        year = rd[:4]
    return {
        "genre": r.get("primaryGenreName", ""),
        "year": year,
        "artwork_url": artwork,
        "album": r.get("collectionName", ""),
    }


def search_itunes(query: str) -> list[dict]:
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={"term": query, "entity": "song", "limit": 10, "country": "US"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return [
            {
                "id": str(r.get("trackId", "")),
                "title": r.get("trackName", ""),
                "artist": r.get("artistName", ""),
                "artists": [r.get("artistName", "")],
                "album": r.get("collectionName", ""),
                "duration_ms": r.get("trackTimeMillis", 0) or 0,
                "cover_url": (r.get("artworkUrl100") or "").replace("100x100", "1200x1200"),
            }
            for r in results
        ]
    except Exception as exc:
        console.print(f"[red]Search failed: {exc}[/red]")
        return []
