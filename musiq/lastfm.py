"""Last.fm API client — metadata lookups via track.getInfo."""

import os
from pathlib import Path

import requests

from musiq.display import console

_API_ROOT = "https://ws.audioscrobbler.com/2.0/"
_TIMEOUT = 10


def _load_api_key() -> str | None:
    key = os.environ.get("LASTFM_API_KEY", "").strip()
    if key:
        return key

    config_path = Path.home() / ".musiq" / "config"
    if config_path.is_file():
        for line in config_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                if k.strip() == "LASTFM_API_KEY":
                    return v.strip()
    return None


def fetch_track_info(artist: str, title: str) -> dict:
    """Fetch track metadata from Last.fm.

    Returns a dict with keys: title, artist, album, track_num,
    duration_ms, toptags, mbid, url. On failure or missing API key
    returns a dict with empty values (never raises).
    """
    _empty = {
        "title": "",
        "artist": "",
        "album": "",
        "track_num": 0,
        "duration_ms": 0,
        "toptags": [],
        "mbid": "",
        "url": "",
    }

    api_key = _load_api_key()
    if not api_key:
        console.print("[dim][lastfm] No LASTFM_API_KEY configured — skipping[/dim]")
        return _empty

    try:
        resp = requests.get(
            _API_ROOT,
            params={
                "method": "track.getInfo",
                "api_key": api_key,
                "artist": artist,
                "track": title,
                "autocorrect": 1,
                "format": "json",
            },
            headers={"User-Agent": "musiq (https://github.com/musiq/musiq)"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        console.print(f"[dim][lastfm] Request failed: {exc}[/dim]")
        return _empty

    if "error" in data:
        console.print(f"[dim][lastfm] API error {data['error']}: {data.get('message', '')}[/dim]")
        return _empty

    track = data.get("track", {})
    if not track:
        return _empty

    artist_info = track.get("artist", {})
    album_info = track.get("album", {})
    toptags = track.get("toptags", {})
    tags_list = toptags.get("tag", [])

    track_num = 0
    if album_info and album_info.get("position"):
        try:
            track_num = int(album_info["position"])
        except (ValueError, TypeError):
            pass

    duration_ms = track.get("duration") or 0
    try:
        duration_ms = int(duration_ms)
    except (ValueError, TypeError):
        duration_ms = 0

    return {
        "title": track.get("name", ""),
        "artist": artist_info.get("name", ""),
        "album": album_info.get("title", ""),
        "track_num": track_num,
        "duration_ms": duration_ms,
        "toptags": [t["name"] for t in tags_list if t.get("name")],
        "mbid": track.get("mbid", ""),
        "url": track.get("url", ""),
    }
