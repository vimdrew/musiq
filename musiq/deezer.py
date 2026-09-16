"""Deezer API client — public catalog lookups for `musiq retag`.

Deezer's public API needs no auth, unlike Tidal/Qobuz/Amazon. Search is
free-text only (field-scoped queries are unreliable), so candidates are
ranked locally by artist + title similarity. Deezer tracks don't expose
genre or a full release date in the search payload, so the matching
track's album is fetched to supply genre + release year.

Read-only, never raises.
"""

import requests
from rapidfuzz import fuzz

from musiq.display import console
from musiq.utils import artist_match as artist_match_confidence

_API_ROOT = "https://api.deezer.com"
_TIMEOUT = 10


def _empty() -> dict:
    return {
        "ok": False,
        "confidence": 0.0,
        "title": "",
        "artist": "",
        "album": "",
        "year": "",
        "genre": "",
        "track_num": 0,
        "duration_ms": 0,
        "artwork_url": "",
        "id": "",
        "url": "",
        "error": "",
    }


def _title_ratio(candidate: str, wanted: str) -> float:
    c = (candidate or "").strip().lower()
    w = (wanted or "").strip().lower()
    if c == w:
        return 1.0
    return fuzz.token_set_ratio(c, w) / 100.0


def _search_tracks(artist: str, title: str) -> list[dict]:
    try:
        resp = requests.get(
            f"{_API_ROOT}/search",
            params={"q": f"{artist} {title}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return [r for r in resp.json().get("data") or [] if isinstance(r, dict)]
    except Exception as exc:
        console.print(f"[dim][deezer] Search failed: {exc}[/dim]")
        return []


def _album_detail(album_id: int | str) -> dict:
    try:
        resp = requests.get(f"{_API_ROOT}/album/{album_id}", timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return {}


def _genre_from_album(album: dict) -> str:
    for g in (album.get("genres") or {}).get("data") or []:
        name = (g or {}).get("name") or ""
        if name:
            return name
    return ""


def _year_from(date_str: str) -> str:
    return (date_str or "").strip()[:4]


def fetch_track_metadata(artist: str, title: str) -> dict:
    """Look up track metadata on Deezer. Never raises.

    Returns a dict with keys: ok, confidence, title, artist, album, year,
    genre, track_num, duration_ms, artwork_url, id, url, error. `ok` is
    True when a plausible hit was found; `confidence` is the min of the
    artist and title match (0..1) and is used to gate genre/album use.
    """
    result = _empty()
    if not artist or not title:
        result["error"] = "no artist/title to search"
        return result

    hits = _search_tracks(artist, title)
    if not hits:
        result["error"] = "no search results"
        return result

    ranked = sorted(
        hits,
        key=lambda r: (
            artist_match_confidence(artist, ((r.get("artist") or {}).get("name") or ""))
            + _title_ratio(r.get("title") or "", title)
        )
        / 2,
        reverse=True,
    )
    hit = ranked[0]

    artist_conf = artist_match_confidence(artist, ((hit.get("artist") or {}).get("name") or ""))
    title_ratio = _title_ratio(hit.get("title") or "", title)
    confidence = min(artist_conf, title_ratio)

    album = _album_detail(((hit.get("album") or {}).get("id") or 0))

    result.update(
        {
            "ok": True,
            "confidence": confidence,
            "title": hit.get("title") or "",
            "artist": ((hit.get("artist") or {}).get("name") or ""),
            "album": album.get("title") or ((hit.get("album") or {}).get("title") or ""),
            "year": _year_from(album.get("release_date") or ""),
            "genre": _genre_from_album(album),
            "track_num": hit.get("track_position") or 0,
            "duration_ms": (hit.get("duration") or 0) * 1000,
            "artwork_url": (hit.get("album") or {}).get("cover_xl") or "",
            "id": str(hit.get("id") or ""),
            "url": f"https://www.deezer.com/track/{hit.get('id')}" if hit.get("id") else "",
        }
    )
    return result