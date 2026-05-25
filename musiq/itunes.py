import requests


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


def fetch_metadata(artist: str, title: str) -> dict:
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term": f"{artist} {title}",
                "entity": "song",
                "limit": 5,
                "country": "US",
            },
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            best = max(results, key=lambda r: _score_result(r, title, artist))
            return _extract(best)
    except Exception:
        pass
    return {"genre": "", "year": "", "artwork_url": ""}


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
