from difflib import SequenceMatcher

from ytmusicapi import YTMusic

from musiq.display import console

_yt = YTMusic()


def artist_match(expected: str, returned: str) -> float:
    expected = expected.lower().strip()
    returned = returned.lower().strip()
    if expected == returned:
        return 1.0
    if expected in returned or returned in expected:
        return 0.9
    return SequenceMatcher(None, expected, returned).ratio()


def _score_result(result: dict, title: str, artist: str) -> int:
    score = 0
    result_title = result.get("title", "").lower()
    result_artist = result.get("artists", [{}])[0].get("name", "").lower()

    if result_title == title.lower():
        score += 10

    if result_artist == artist.lower():
        score += 5

    if result_title in title.lower() or title.lower() in result_title:
        score += 3

    duration = result.get("duration_seconds", 0)
    if duration and 120 <= duration <= 600:
        score += 2

    live_keywords = ["live", "concert", "performance", "cover", "remix", "karaoke", "tribute"]
    for kw in live_keywords:
        if kw in result_title:
            score -= 3

    return score


def search_song(title: str, artist: str) -> dict | None:
    query = f"{title} {artist}"
    try:
        results = _yt.search(query, filter="songs", limit=10)
    except Exception as exc:
        console.print(f"[yellow]⚠ YouTube Music search failed: {exc}[/yellow]")
        return None

    if not results:
        return None

    best = max(results, key=lambda r: _score_result(r, title, artist))

    confidence = artist_match(artist, best.get("artists", [{}])[0].get("name", ""))
    if confidence < 0.6:
        return None

    return {
        "video_id": best.get("videoId"),
        "title": best.get("title"),
        "artists": [a["name"] for a in best.get("artists", [])],
        "album": best.get("album", {}).get("name") if best.get("album") else None,
        "duration_seconds": best.get("duration_seconds", 0),
    }
