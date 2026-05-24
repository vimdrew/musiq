import json
import re
import urllib.parse
from typing import Literal

import requests
from bs4 import BeautifulSoup

from musiq.display import console

EMBED_BASE = "https://open.spotify.com/embed"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}

_SPOTIFY_API_CLIENT = "5f573c9620494bae87890c0f08a60293"
_SPOTIFY_API_SECRET = "212476d9b0f3472eaa762d90b19b0ba8"
_api_token: str = ""


class SpotifyError(Exception):
    pass


# ── ID / type extraction ──────────────────────────────────────────

def extract_id_and_type(url: str) -> tuple[str, str]:
    m = re.search(r"spotify\.com/(track|album|playlist)/([A-Za-z0-9]+)", url)
    if not m:
        raise SpotifyError(f"Unrecognised Spotify URL: {url}")
    return m.group(1), m.group(2)


def parse_url(url: str) -> tuple[str, str]:
    return extract_id_and_type(url)


def resolve_url(url: str) -> tuple[Literal["track", "album", "playlist"], str]:
    t, eid = extract_id_and_type(url)
    return t, eid


# ── Embed scraper ─────────────────────────────────────────────────

def fetch_next_data(kind: str, spotify_id: str) -> dict:
    url = f"{EMBED_BASE}/{kind}/{spotify_id}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if not tag or not tag.string:
        raise SpotifyError("__NEXT_DATA__ script tag not found in embed page")
    return json.loads(tag.string)


# ── Track ─────────────────────────────────────────────────────────

def resolve_track(url: str) -> dict | None:
    kind, sid = extract_id_and_type(url)
    try:
        data = fetch_next_data(kind, sid)
        entity = _navigate_entity(data)
        if not entity:
            raise SpotifyError("entity not found in __NEXT_DATA__")
        result = _extract_track_from_entity(entity, sid)
        _enrich_with_og(result, sid)
        return result
    except Exception as exc:
        console.print(f"[yellow]⚠ Spotify embed parse failed for track: {exc}[/yellow]")
        return None


def _enrich_with_og(result: dict, sid: str) -> None:
    try:
        main_url = f"https://open.spotify.com/track/{sid}"
        r = requests.get(main_url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for m in soup.find_all("meta"):
            prop = m.get("property", "") or m.get("name", "")
            if prop == "og:image" and not result.get("cover_url"):
                result["cover_url"] = m.get("content", "")
            elif prop == "og:description":
                parts = [p.strip() for p in m.get("content", "").split("·")]
                if len(parts) >= 2 and not result.get("album"):
                    result["album"] = parts[1]
                if len(parts) >= 4 and not result.get("year"):
                    result["year"] = parts[3]
    except Exception:
        pass


def _extract_track_from_entity(entity: dict, sid: str) -> dict:
    artists_raw = entity.get("artists") or []
    artists = [a["name"] for a in artists_raw if isinstance(a, dict) and a.get("name")]

    duration = entity.get("duration", 0)
    if isinstance(duration, dict):
        duration = duration.get("totalMilliseconds", 0) or 0

    release = entity.get("releaseDate") or {}
    release_date = ""
    if isinstance(release, dict):
        release_date = release.get("isoString", "") or release.get("year", "") or ""

    year = None
    if isinstance(release, dict):
        year = release.get("year")
    if not year and release_date:
        m = re.match(r"(\d{4})", release_date)
        if m:
            year = m.group(1)

    cover_url = None
    vi = entity.get("visualIdentity")
    if isinstance(vi, dict):
        images = vi.get("image") or []
        if images:
            best = max(images, key=lambda x: x.get("maxWidth", 0) * x.get("maxHeight", 0))
            cover_url = best.get("url", "")

    return {
        "id": sid,
        "title": entity.get("name", "") or entity.get("title", ""),
        "artist": ", ".join(artists) if artists else "",
        "artists": artists,
        "album": None,
        "track_number": entity.get("trackNumber") or entity.get("track_number") or None,
        "duration_ms": int(duration),
        "release_date": release_date,
        "cover_url": cover_url,
        "year": year,
        "explicit": entity.get("isExplicit", False) or entity.get("explicit", False),
    }


def resolve_embed_track_list(kind: str, sid: str) -> list[dict] | None:
    try:
        data = fetch_next_data(kind, sid)
        entity = _navigate_entity(data) or data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
        if not isinstance(entity, dict):
            return None

        track_list = entity.get("trackList") or []
        if not track_list:
            return None

        album_name = entity.get("name", "")
        vi = entity.get("visualIdentity") or {}
        images = vi.get("image", []) if isinstance(vi, dict) else []
        cover_url = max(images, key=lambda x: x.get("maxWidth", 0) * x.get("maxHeight", 0)).get("url", "") if images else ""

        results = []
        for i, entry in enumerate(track_list):
            uid = entry.get("uid", "")
            if not uid:
                continue
            results.append({
                "id": uid,
                "title": entry.get("title", ""),
                "artist": entry.get("subtitle", ""),
                "duration_ms": entry.get("duration", 0),
                "album_name": album_name,
                "cover_url": cover_url,
                "track_number": i + 1,
            })
        return results
    except Exception:
        return None


def _navigate_entity(data: dict) -> dict | None:
    paths = [
        ["props", "pageProps", "state", "data", "entity"],
        ["props", "pageProps", "entity"],
    ]
    for path in paths:
        obj = data
        for key in path:
            if isinstance(obj, dict) and key in obj:
                obj = obj[key]
            else:
                break
        else:
            if isinstance(obj, dict) and obj.get("type") == "track":
                return obj
    return None


# ── Album / Playlist (via API — embed no longer serves track list) ─

def _ensure_api_token() -> str:
    global _api_token
    if _api_token:
        return _api_token
    import base64
    creds = base64.b64encode(f"{_SPOTIFY_API_CLIENT}:{_SPOTIFY_API_SECRET}".encode()).decode()
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {creds}"},
        data={"grant_type": "client_credentials"},
        timeout=10,
    )
    resp.raise_for_status()
    _api_token = resp.json()["access_token"]
    return _api_token


def _api_get(path_or_url: str, params: dict | None = None) -> dict:
    token = _ensure_api_token()
    url = path_or_url if path_or_url.startswith("http") else f"https://api.spotify.com/v1/{path_or_url.lstrip('/')}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=15)
    if resp.status_code == 401:
        _api_token = ""
        token = _ensure_api_token()
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def resolve_album(url: str) -> list[dict] | None:
    _, sid = extract_id_and_type(url)
    try:
        d = _api_get(f"albums/{sid}")
    except Exception as exc:
        console.print(f"[yellow]⚠ Spotify API album lookup failed: {exc}[/yellow]")
        console.print("[yellow]Falling back to embed track list for album...[/yellow]")
        return _resolve_collection_from_embed("album", sid)

    album_artists = [a["name"] for a in d.get("artists", [])]
    album_name = d.get("name", "")
    images = d.get("images", [])
    cover = images[0]["url"] if images else None
    release_date = d.get("release_date", "")

    items = d.get("tracks", {}).get("items", [])
    results = []
    for t in items:
        artists = [a["name"] for a in t.get("artists", [])]
        results.append({
            "id": t["id"],
            "title": t.get("name", ""),
            "artist": ", ".join(artists) if artists else ", ".join(album_artists),
            "artists": artists or album_artists,
            "album": album_name,
            "album_id": sid,
            "track_number": t.get("track_number", 0),
            "duration_ms": t.get("duration_ms", 0),
            "release_date": release_date,
            "cover_url": cover,
            "explicit": t.get("explicit", False),
        })
    return results if results else None


def resolve_playlist(url: str) -> list[dict] | None:
    _, sid = extract_id_and_type(url)
    try:
        d = _api_get(f"playlists/{sid}")
    except Exception as exc:
        console.print(f"[yellow]⚠ Spotify API playlist lookup failed: {exc}[/yellow]")
        console.print("[yellow]Falling back to embed track list for playlist...[/yellow]")
        return _resolve_collection_from_embed("playlist", sid)

    results = []
    items = d.get("tracks", {}).get("items", [])
    for item in items:
        t = item.get("track") or item
        if not isinstance(t, dict) or not t.get("id"):
            continue
        artists = [a["name"] for a in t.get("artists", [])]
        album_data = t.get("album") or {}
        images = album_data.get("images", []) if isinstance(album_data, dict) else []
        results.append({
            "id": t["id"],
            "title": t.get("name", ""),
            "artist": ", ".join(artists),
            "artists": artists,
            "album": album_data.get("name", "") if isinstance(album_data, dict) else "",
            "album_id": album_data.get("id", "") if isinstance(album_data, dict) else "",
            "track_number": t.get("track_number", 0),
            "duration_ms": t.get("duration_ms", 0),
            "release_date": album_data.get("release_date", "") if isinstance(album_data, dict) else "",
            "cover_url": images[0]["url"] if images else None,
            "explicit": t.get("explicit", False),
        })
    return results if results else None


def _resolve_collection_from_embed(kind: str, sid: str) -> list[dict] | None:
    try:
        tracks_data = resolve_embed_track_list(kind, sid)
        if not tracks_data:
            return None
        results = []
        for entry in tracks_data:
            t = resolve_track(f"https://open.spotify.com/track/{entry['id']}")
            if t:
                t["album"] = entry.get("album_name", "")
                t["cover_url"] = t.get("cover_url") or entry.get("cover_url", "")
                t["track_number"] = entry.get("track_number", 0) or 0
                results.append(t)
            else:
                artist = entry.get("artist", "")
                results.append({
                    "id": entry["id"],
                    "title": entry.get("title", ""),
                    "artist": artist,
                    "artists": [artist] if artist else [],
                    "album": entry.get("album_name", ""),
                    "track_number": entry.get("track_number", 0) or 0,
                    "duration_ms": entry.get("duration_ms", 0) or 0,
                    "cover_url": entry.get("cover_url", ""),
                    "year": "",
                    "release_date": "",
                    "explicit": False,
                })
        return results if results else None
    except Exception:
        return None


# ── Search ────────────────────────────────────────────────────────

def search_spotify(query: str) -> list[dict]:
    try:
        d = _api_get("search", {"q": query, "type": "track", "limit": 10})
        items = d.get("tracks", {}).get("items", [])
        return [
            {
                "id": t["id"],
                "title": t["name"],
                "artist": ", ".join(a["name"] for a in t["artists"]),
                "artists": [a["name"] for a in t["artists"]],
                "album": t["album"]["name"],
                "duration_ms": t.get("duration_ms", 0),
                "cover_url": t["album"]["images"][0]["url"] if t["album"].get("images") else None,
            }
            for t in items
        ]
    except Exception as exc:
        console.print(f"[red]Search failed: {exc}[/red]")
        return []


# ── Unified entry point ───────────────────────────────────────────

def resolve_entity(url: str) -> dict | list[dict] | None:
    kind, _ = extract_id_and_type(url)

    if kind == "track":
        return resolve_track(url)

    if kind == "album":
        return resolve_album(url)

    if kind == "playlist":
        return resolve_playlist(url)

    return None
