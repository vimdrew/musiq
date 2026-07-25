from ytmusicapi import YTMusic

from musiq.display import console
from musiq.utils import artist_match

_yt = YTMusic()

_CONFIDENCE_THRESHOLD = 0.4


def _any_artist_match(expected: str, artists: list[dict]) -> bool:
    expected = expected.lower().strip()
    for a in artists:
        name = a.get("name", "").lower().strip()
        if name and (expected == name or expected in name or name in expected):
            return True
    return False


def _score_result(result: dict, title: str, artist: str) -> int:
    score = 0
    result_title = result.get("title", "").lower()
    artists = result.get("artists", [])

    if result_title == title.lower():
        score += 10

    if _any_artist_match(artist, artists):
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

    artists = best.get("artists", [])
    top_artist_name = artists[0].get("name", "") if artists else ""
    confidence = artist_match(artist, top_artist_name)
    if confidence < _CONFIDENCE_THRESHOLD:
        return None

    return {
        "video_id": best.get("videoId"),
        "title": best.get("title"),
        "artists": [a["name"] for a in best.get("artists", [])],
        "album": best.get("album", {}).get("name") if best.get("album") else None,
        "duration_seconds": best.get("duration_seconds", 0),
    }
