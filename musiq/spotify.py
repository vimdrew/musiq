import json
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from musiq.display import console

EMBED_BASE = "https://open.spotify.com/embed"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}


class SpotifyError(Exception):
    pass


_CONFIG_PATH = Path.home() / ".musiq" / "config"


def _load_env_fallback() -> tuple[str | None, str | None]:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret
    cfg = _CONFIG_PATH
    if cfg.is_file():
        try:
            for line in cfg.read_text().splitlines():
                line = line.strip()
                if line.startswith("SPOTIFY_CLIENT_ID="):
                    client_id = line.split("=", 1)[1]
                elif line.startswith("SPOTIFY_CLIENT_SECRET="):
                    client_secret = line.split("=", 1)[1]
        except OSError:
            pass
    return client_id, client_secret


class SpotifyClient:
    def __init__(self):
        self._token = None
        self._token_expires_at = 0.0
        self._client_id, self._client_secret = _load_env_fallback()

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _ensure_token(self) -> None:
        if time.time() < self._token_expires_at:
            return
        for _ in range(2):
            resp = requests.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
                timeout=15,
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            self._token_expires_at = time.time() + data["expires_in"] - 60
            return

    def _headers(self) -> dict:
        self._ensure_token()
        return {"Authorization": f"Bearer {self._token}"}

    def get_track(self, track_id: str) -> dict | None:
        if not self.configured:
            return None
        for attempt in range(2):
            try:
                resp = requests.get(
                    f"https://api.spotify.com/v1/tracks/{track_id}",
                    headers=self._headers(),
                    timeout=15,
                )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception:
                if attempt:
                    return None

    def get_album(self, album_id: str) -> dict | None:
        if not self.configured:
            return None
        for attempt in range(2):
            try:
                resp = requests.get(
                    f"https://api.spotify.com/v1/albums/{album_id}",
                    headers=self._headers(),
                    timeout=15,
                )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception:
                if attempt:
                    return None

    def get_playlist(self, playlist_id: str) -> dict | None:
        if not self.configured:
            return None
        for attempt in range(2):
            try:
                resp = requests.get(
                    f"https://api.spotify.com/v1/playlists/{playlist_id}",
                    headers=self._headers(),
                    timeout=15,
                )
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception:
                if attempt:
                    return None


_spotify_client = SpotifyClient()


def _best_image(images: list) -> str:
    if not images:
        return ""
    best = max(images, key=lambda x: (x.get("width", 0) or 0) * (x.get("height", 0) or 0))
    return best.get("url", "")


def _extract_year(release_date: str) -> str:
    if release_date and len(release_date) >= 4:
        return release_date[:4]
    return ""


# ── API response mappers ──────────────────────────────────────────

def _api_to_track_dict(data: dict, track_id: str) -> dict:
    artists = [a["name"] for a in data.get("artists", []) if isinstance(a, dict)]
    album = data.get("album") or {}
    release_date = (album.get("release_date") or "") if isinstance(album, dict) else ""
    images = (album.get("images") or []) if isinstance(album, dict) else []
    external_ids = data.get("external_ids") or {}
    return {
        "id": track_id,
        "title": data.get("name", ""),
        "artist": ", ".join(artists) if artists else "",
        "artists": artists,
        "album": album.get("name") if isinstance(album, dict) else None,
        "track_number": data.get("track_number"),
        "duration_ms": data.get("duration_ms", 0) or 0,
        "release_date": release_date,
        "cover_url": _best_image(images),
        "year": _extract_year(release_date),
        "explicit": data.get("explicit", False),
        "isrc": (external_ids.get("isrc") or "") if isinstance(external_ids, dict) else "",
    }


def _api_album_to_tracks(data: dict) -> list[dict] | None:
    album_name = data.get("name", "")
    release_date = data.get("release_date", "")
    year = _extract_year(release_date)
    cover_url = _best_image(data.get("images", []))
    tracks_container = data.get("tracks") or {}
    items = tracks_container.get("items", []) if isinstance(tracks_container, dict) else []
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        track_artists = [a["name"] for a in item.get("artists", []) if isinstance(a, dict)]
        results.append({
            "id": item.get("id", ""),
            "title": item.get("name", ""),
            "artist": ", ".join(track_artists) if track_artists else "",
            "artists": track_artists,
            "album": album_name,
            "track_number": item.get("track_number", 0) or 0,
            "duration_ms": item.get("duration_ms", 0) or 0,
            "cover_url": cover_url,
            "year": year,
            "release_date": release_date,
            "explicit": item.get("explicit", False),
            "_source_type": "album",
        })
    return results if results else None


def _api_playlist_to_tracks(data: dict) -> list[dict] | None:
    tracks_container = data.get("tracks") or {}
    items = tracks_container.get("items", []) if isinstance(tracks_container, dict) else []
    results = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        track = entry.get("track")
        if not track or not isinstance(track, dict):
            continue
        track_artists = [a["name"] for a in track.get("artists", []) if isinstance(a, dict)]
        album_info = track.get("album") or {}
        release_date = (album_info.get("release_date") or "") if isinstance(album_info, dict) else ""
        year = _extract_year(release_date)
        images = (album_info.get("images") or []) if isinstance(album_info, dict) else []
        cover_url = _best_image(images)
        results.append({
            "id": track.get("id", ""),
            "title": track.get("name", ""),
            "artist": ", ".join(track_artists) if track_artists else "",
            "artists": track_artists,
            "album": album_info.get("name") if isinstance(album_info, dict) else "",
            "track_number": track.get("track_number", 0) or 0,
            "duration_ms": track.get("duration_ms", 0) or 0,
            "cover_url": cover_url,
            "year": year,
            "release_date": release_date,
            "explicit": track.get("explicit", False),
            "_source_type": "playlist",
        })
    return results if results else None


# ── ID / type extraction ──────────────────────────────────────────

def extract_id_and_type(url: str) -> tuple[str, str]:
    m = re.search(r"spotify\.com/(track|album|playlist)/([A-Za-z0-9]+)", url)
    if not m:
        raise SpotifyError(f"Unrecognised Spotify URL: {url}")
    return m.group(1), m.group(2)


def parse_url(url: str) -> tuple[str, str]:
    return extract_id_and_type(url)


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
    api_data = _spotify_client.get_track(sid)
    if api_data is not None:
        console.print("[dim][spotify] using API[/dim]")
        return _api_to_track_dict(api_data, sid)
    console.print("[dim][spotify] using scraper[/dim]")
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
                    year_raw = parts[3]
                    if year_raw.isdigit() and len(year_raw) == 4:
                        result["year"] = year_raw
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

        vi = entity.get("visualIdentity") or {}
        images = vi.get("image", []) if isinstance(vi, dict) else []
        cover_url = max(images, key=lambda x: x.get("maxWidth", 0) * x.get("maxHeight", 0)).get("url", "") if images else ""

        results = []
        for i, entry in enumerate(track_list):
            uid = entry.get("uri", "").split(":")[-1] or entry.get("uid", "")
            if not uid:
                continue
            results.append({
                "id": uid,
                "title": entry.get("title", ""),
                "artist": entry.get("subtitle", ""),
                "duration_ms": entry.get("duration", 0),
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


# ── Album / Playlist (embed only) ─────────────────────────────────

def resolve_album(url: str) -> list[dict] | None:
    _, sid = extract_id_and_type(url)
    api_data = _spotify_client.get_album(sid)
    if api_data is not None:
        console.print("[dim][spotify] using API[/dim]")
        return _api_album_to_tracks(api_data)
    console.print("[dim][spotify] using scraper[/dim]")
    return _resolve_collection_from_embed("album", sid)


def resolve_playlist(url: str) -> list[dict] | None:
    _, sid = extract_id_and_type(url)
    api_data = _spotify_client.get_playlist(sid)
    if api_data is not None:
        console.print("[dim][spotify] using API[/dim]")
        return _api_playlist_to_tracks(api_data)
    console.print("[dim][spotify] using scraper[/dim]")
    return _resolve_collection_from_embed("playlist", sid)


def _resolve_collection_from_embed(kind: str, sid: str) -> list[dict] | None:
    try:
        tracks_data = resolve_embed_track_list(kind, sid)
        if not tracks_data:
            return None
        results = []
        for entry in tracks_data:
            t = resolve_track(f"https://open.spotify.com/track/{entry['id']}")
            if t:
                t.setdefault("album", "")
                t["cover_url"] = t.get("cover_url") or entry.get("cover_url", "")
                t["track_number"] = entry.get("track_number", 0) or 0
                t["_source_type"] = kind
                results.append(t)
            else:
                artist = entry.get("artist", "")
                results.append({
                    "id": entry["id"],
                    "title": entry.get("title", ""),
                    "artist": artist,
                    "artists": [artist] if artist else [],
                    "album": "",
                    "track_number": entry.get("track_number", 0) or 0,
                    "duration_ms": entry.get("duration_ms", 0) or 0,
                    "cover_url": entry.get("cover_url", ""),
                    "year": "",
                    "release_date": "",
                    "explicit": False,
                    "_source_type": kind,
                })
        return results if results else None
    except Exception:
        return None


# ── Search (Spotify API) ─────────────────────────────────────────

def search_track_on_spotify(title: str, artist: str) -> str | None:
    if not _spotify_client.configured:
        return None
    for attempt in range(2):
        try:
            resp = requests.get(
                "https://api.spotify.com/v1/search",
                params={"q": f"{artist} {title}", "type": "track", "limit": 3},
                headers=_spotify_client._headers(),
                timeout=10,
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            data = resp.json()
            tracks = data.get("tracks", {}).get("items", [])
            if tracks:
                return tracks[0].get("id")
        except Exception:
            if attempt:
                return None
    return None


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
