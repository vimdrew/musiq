import requests


def search(artist: str, title: str) -> dict:
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term": f"{artist} {title}",
                "entity": "song",
                "limit": 3,
                "country": "US",
            },
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        for r in results:
            r_artist = r.get("artistName", "").lower()
            r_track = r.get("trackName", "").lower()
            q_artist = artist.lower()
            q_title = title.lower()
            if q_artist in r_artist or r_artist in q_artist:
                if q_title in r_track or r_track in q_title:
                    return _extract(r)
        if results:
            return _extract(results[0])
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
