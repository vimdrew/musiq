import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from musiq.display import console

EMBED_BASE = "https://open.spotify.com/embed"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}

# Redirect URI for the Authorization Code + PKCE flow. This must be registered
# as a redirect URI in the Spotify Developer Dashboard for this client.
_REDIRECT_HOST = "127.0.0.1"
_REDIRECT_PORT = 8888
_PLAYLIST_SCOPES = "playlist-read-private playlist-read-collaborative"


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


# ── Config read/write helpers ─────────────────────────────────────

def _read_config_value(key: str) -> str | None:
    """Read a single KEY=value from ~/.musiq/config, or the env var of the same name."""
    value = os.environ.get(key)
    if value:
        return value.strip()
    if _CONFIG_PATH.is_file():
        try:
            for line in _CONFIG_PATH.read_text().splitlines():
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
    return None


def _write_config_value(key: str, value: str) -> None:
    """Set a single KEY=value in ~/.musiq/config, preserving other lines."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if _CONFIG_PATH.is_file():
        lines = _CONFIG_PATH.read_text().splitlines()

    new_lines = []
    written = False
    for line in lines:
        k = line.split("=", 1)[0] if "=" in line else None
        if k == key:
            new_lines.append(f"{key}={value}")
            written = True
        else:
            new_lines.append(line)
    if not written:
        new_lines.append(f"{key}={value}")

    _CONFIG_PATH.write_text("\n".join(new_lines) + "\n")


# ── Authorization Code + PKCE (playlist reads) ────────────────────

class _AuthCodeHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that captures the auth code from Spotify's redirect."""

    code: str | None = None
    error: str | None = None

    def do_GET(self):  # noqa: N802
        query = urlparse(self.path).query
        params = parse_qs(query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "code" in params:
            type(self).code = params["code"][0]
            type(self).error = None
            body = "<html><body><h3>musiq authorization complete</h3><p>You can close this tab and return to the terminal.</p></body></html>"
        else:
            type(self).code = None
            type(self).error = params.get("error", ["unknown"])[0]
            body = f"<html><body><h3>Authorization failed</h3><p>{type(self).error}</p></body></html>"
        self.wfile.write(body.encode())

    def log_message(self, *args):  # noqa: D102
        pass


def _redirect_uri() -> str:
    """Redirect URI, overridable via SPOTIFY_REDIRECT_URI for testing."""
    custom = _read_config_value("SPOTIFY_REDIRECT_URI")
    if custom:
        return custom
    return f"http://{_REDIRECT_HOST}:{_REDIRECT_PORT}/callback"


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _run_redirect_server() -> tuple[http.server.HTTPServer, type]:
    """Start a local HTTP server to capture the auth code.

    Returns (server, handler_class). The captured code is stored on the
    handler class once the browser redirect lands.
    """
    handler = type("Handler", (_AuthCodeHandler,), {})
    server = http.server.HTTPServer((_REDIRECT_HOST, _REDIRECT_PORT), handler)
    return server, handler


def _authorize() -> str:
    """Run the one-time Authorization Code + PKCE login. Returns the refresh token."""
    client_id = _read_config_value("SPOTIFY_CLIENT_ID")
    if not client_id:
        raise SpotifyError(
            "SPOTIFY_CLIENT_ID is not configured. Set it in ~/.musiq/config."
        )

    verifier, challenge = _pkce_pair()
    redirect_uri = _redirect_uri()

    server, handler_cls = _run_redirect_server()

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": _PLAYLIST_SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    auth_url = "https://accounts.spotify.com/authorize?" + urlencode(params)

    console.print("[bold]Opening Spotify in your browser for authorization...[/bold]")
    console.print(f"[dim]If the browser doesn't open, visit:\n  {auth_url}[/dim]")
    console.print(f"[dim]Authorize the app and you'll be redirected back to {redirect_uri}.[/dim]")

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # Wait up to the timeout for the redirect to arrive with the code.
    try:
        server.timeout = 1
        deadline = time.time() + 300
        while time.time() < deadline:
            server.handle_request()
            if handler_cls.code is not None or handler_cls.error is not None:
                break
    finally:
        server.server_close()
        if handler_cls.error:
            raise SpotifyError(f"Spotify authorization failed: {handler_cls.error}")

    if handler_cls.code is None:
        raise SpotifyError("Timed out waiting for Spotify authorization.")

    # Exchange the code for access + refresh tokens. Spotify's token endpoint
    # can be slow, so use a generous timeout.
    token_resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": handler_cls.code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        timeout=90,
    )
    token_resp.raise_for_status()
    data = token_resp.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise SpotifyError("Spotify did not return a refresh token.")
    return refresh_token


class PlaylistAuth:
    """Manages an access token for Spotify playlist-read calls via refresh token."""

    def __init__(self):
        self._access_token: str | None = None
        self._expires_at = 0.0

    @property
    def refresh_token(self) -> str | None:
        return _read_config_value("SPOTIFY_REFRESH_TOKEN")

    def ensure_refresh_token(self) -> str:
        token = self.refresh_token
        if not token:
            token = _authorize()
            _write_config_value("SPOTIFY_REFRESH_TOKEN", token)
            console.print("[green]✓ Spotify login complete. Refresh token saved.[/green]")
        return token

    def _refresh_access_token(self) -> str:
        client_id = _read_config_value("SPOTIFY_CLIENT_ID")
        refresh_token = self.ensure_refresh_token()
        resp = requests.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600) - 60
        return self._access_token

    def access_token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        return self._refresh_access_token()

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token()}"}


_playlist_auth = PlaylistAuth()


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
    album_artists = [a["name"] for a in data.get("artists", []) if isinstance(a, dict) and a.get("name")]
    album_artist = ", ".join(album_artists)
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
            "artist": ", ".join(track_artists) if track_artists else album_artist,
            "artists": track_artists,
            "album": album_name,
            "album_artist": album_artist,
            "album_id": data.get("id", ""),
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


# ── Playlist fetch (Authorization Code + PKCE) ────────────────────

def resolve_playlist_tracks(url: str) -> tuple[str, list[dict]] | None:
    """Fetch a Spotify playlist's name and all of its tracks using the user
    authorized (PKCE) token, paginating through every page (100/page).

    Returns (name, tracks) or None on failure. Requires a one-time browser
    login if SPOTIFY_REFRESH_TOKEN is not yet stored in config.
    """
    kind, sid = extract_id_and_type(url)
    if kind != "playlist":
        raise SpotifyError(f"Expected a playlist URL, got {kind}")

    try:
        return _resolve_playlist_from_api(sid)
    except Exception as exc:
        console.print(
            f"[yellow]⚠ Playlist API unavailable ({exc}) — using embed fallback[/yellow]"
        )
        return _resolve_playlist_from_embed(sid)


def _resolve_playlist_from_api(sid: str) -> tuple[str, list[dict]]:
    """Fetch a playlist's name + all tracks via the Spotify API (OAuth token).

    Raises on any failure so the caller can fall back to the embed scraper.
    """
    headers = _playlist_auth.headers()

    for attempt in range(2):
        resp = requests.get(
            f"https://api.spotify.com/v1/playlists/{sid}",
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        playlist = resp.json()
        break

    name = playlist.get("name", "Musiq Playlist")

    tracks: list[dict] = []
    offset = 0
    limit = 100
    while True:
        page_resp = requests.get(
            f"https://api.spotify.com/v1/playlists/{sid}/tracks",
            headers=headers,
            params={"limit": limit, "offset": offset},
            timeout=15,
        )
        page_resp.raise_for_status()
        data = page_resp.json()
        items = data.get("items", [])
        page = _api_playlist_to_tracks({"tracks": {"items": items}})
        if page:
            tracks.extend(page)
        if len(items) < limit or data.get("total", 0) <= offset + len(items):
            break
        offset += limit

    console.print("[dim][spotify] playlist via API[/dim]")
    return name, tracks


def _resolve_playlist_from_embed(sid: str) -> tuple[str, list[dict]]:
    """Fetch a playlist's name + full track list from the embed page.

    Used when the API is unavailable (e.g. premium gate on the developer
    app). The embed page's own trackList preview is capped (~100 tracks),
    so this tries to go beyond that using the anonymous web-player token
    embedded in the page: that token grants access to Spotify's internal
    spclient playlist endpoint, which returns the authoritative full list.
    Tracks beyond the preview need their own embed fetches for title/artist.
    The old preview-only path is kept as a last resort. Albums come from
    the iTunes metadata lookup the rest of the pipeline already uses, and
    the result is cached as _itunes_cache so process_track reuses it.
    """
    data = fetch_next_data("playlist", sid)
    entity = (
        _navigate_entity(data)
        or data.get("props", {})
        .get("pageProps", {})
        .get("state", {})
        .get("data", {})
        .get("entity", {})
    )
    if not isinstance(entity, dict):
        raise SpotifyError("playlist entity not found in embed page")

    name = entity.get("name") or entity.get("title") or "Musiq Playlist"
    preview = entity.get("trackList") or []

    playlist_cover = ""
    vi = entity.get("visualIdentity") or {}
    if isinstance(vi, dict):
        images = vi.get("image") or []
        if images:
            playlist_cover = max(
                images, key=lambda x: x.get("maxWidth", 0) * x.get("maxHeight", 0)
            ).get("url", "")

    preview_by_id: dict[str, dict] = {}
    for entry in preview:
        if not isinstance(entry, dict):
            continue
        tid = entry.get("uri", "").split(":")[-1] or entry.get("uid", "")
        if tid:
            preview_by_id[tid] = entry

    try:
        session = _embed_session(data)
        if not session or not session.get("accessToken"):
            raise SpotifyError("no anonymous access token in embed page")
        total, uris = _spclient_track_uris(sid, session["accessToken"])
    except Exception as exc:
        return _build_preview_only(preview, name, exc, playlist_cover=playlist_cover)

    track_ids = [uri.split(":")[-1] for uri in uris if uri.startswith("spotify:track:")]

    missing = [tid for tid in track_ids if tid not in preview_by_id]
    missing_meta: dict[str, dict | None] = {}
    if missing:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(_track_embed_metadata, tid): tid for tid in missing}
            for fut in as_completed(futures):
                missing_meta[futures[fut]] = fut.result()

    results: list[dict] = []
    for tid in track_ids:
        entry = preview_by_id.get(tid)
        if entry is not None:
            track = _make_playlist_track(
                tid,
                entry.get("title", ""),
                entry.get("subtitle", ""),
                entry.get("duration", 0) or 0,
                entry.get("isExplicit", False),
                cover_url=playlist_cover,
            )
        else:
            meta = missing_meta.get(tid) or {}
            track = _make_playlist_track(
                tid,
                meta.get("title") or "Unknown",
                meta.get("artist") or "",
                meta.get("duration_ms") or 0,
                False,
                cover_url=meta.get("cover_url", ""),
                year=meta.get("year", ""),
            )
        if track:
            results.append(track)

    if not results:
        raise SpotifyError("no tracks found in playlist embed page")

    console.print(
        f"[dim][spotify] playlist via embed + spclient ({len(results)}/{total} tracks)[/dim]"
    )
    return name, results


def _build_preview_only(preview: list, name: str, exc: Exception,
                        playlist_cover: str = "") -> tuple[str, list[dict]]:
    """Last-resort path: return just the embed page's truncated preview.

    Used when the anonymous token or the spclient call fails for any reason
    (e.g. Spotify changed the page structure). Logs a clear truncation
    warning so a silently shortened list is impossible to miss.
    """
    results = []
    for entry in preview:
        if not isinstance(entry, dict):
            continue
        tid = entry.get("uri", "").split(":")[-1] or entry.get("uid", "")
        if not tid:
            continue
        track = _make_playlist_track(
            tid,
            entry.get("title", ""),
            entry.get("subtitle", ""),
            entry.get("duration", 0) or 0,
            entry.get("isExplicit", False),
            cover_url=playlist_cover,
        )
        if track:
            results.append(track)

    if not results:
        raise SpotifyError("no tracks found in playlist embed page") from exc

    console.print(
        f"[yellow]⚠ Full playlist fetch unavailable ({exc}) — "
        f"embed preview truncated at {len(results)} track(s)[/yellow]"
    )
    console.print(f"[dim][spotify] playlist via embed ({len(results)} tracks)[/dim]")
    return name, results


def _make_playlist_track(
    track_id: str, title: str, artist: str, duration_ms: int, explicit: bool,
    cover_url: str = "", year: str = "",
) -> dict | None:
    """Build a pipeline track dict from embed-derived metadata, enriched
    with album info from iTunes (cached as _itunes_cache)."""
    if not track_id or not title:
        return None
    from musiq.itunes import fetch_metadata

    artists = [artist] if artist else []
    itunes_meta = {}
    if title != "Unknown" and artist:
        itunes_meta = fetch_metadata(artist, title)
    track = {
        "id": track_id,
        "title": title,
        "artist": artist,
        "artists": artists,
        "album": itunes_meta.get("album") or "",
        "track_number": 0,
        "duration_ms": int(duration_ms or 0),
        "cover_url": cover_url or itunes_meta.get("artwork_url") or "",
        "year": year or itunes_meta.get("year") or "",
        "release_date": "",
        "explicit": bool(explicit),
        "_source_type": "playlist",
    }
    if itunes_meta:
        track["_itunes_cache"] = itunes_meta
    return track




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


# ── Anonymous token + spclient (full playlist fetch) ──────────────

_SPCLIENT_PLAYLIST_URL = "https://spclient.wg.spotify.com/playlist/v2/playlist/{sid}"


def _embed_session(data: dict) -> dict | None:
    """Return the anonymous web-player session dict from an embed page's
    __NEXT_DATA__ (contains 'accessToken'), or None if absent. The token
    powers the public, logged-out embed player and stays valid ~1 hour."""
    if not isinstance(data, dict):
        return None
    if "accessToken" in data:
        return data
    for value in data.values():
        if isinstance(value, dict):
            hit = _embed_session(value)
            if hit is not None:
                return hit
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    hit = _embed_session(item)
                    if hit is not None:
                        return hit
    return None


def _spclient_track_uris(sid: str, token: str) -> tuple[int, list[str]]:
    """Fetch the full ordered playlist track list from Spotify's internal
    spclient API, using the anonymous token from the embed page.

    Returns (total, uris): ``total`` is the authoritative playlist size and
    ``uris`` the full list in playlist order. Today the endpoint returns the
    whole list in a single response and ignores offset/limit; the loop is
    kept as resilience and pages via the 'from' parameter if a future
    response is ever truncated. Raises SpotifyError on any failure so the
    caller can fall back to the truncated embed preview.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "app-platform": "WebPlayer",
        "User-Agent": HEADERS["User-Agent"],
    }
    url = _SPCLIENT_PLAYLIST_URL.format(sid=sid)

    uris: list[str] = []
    total = 0
    offset = 0
    for _page in range(300):
        params = {"from": offset} if offset else None
        resp = None
        for attempt in range(3):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=60)
            except requests.RequestException as exc:
                if attempt == 2:
                    raise SpotifyError(f"spclient request failed: {exc}") from exc
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 429:
                time.sleep(int(resp.headers.get("Retry-After", 30)))
                continue
            break
        if resp is None:
            raise SpotifyError("spclient request failed after retries")
        if resp.status_code != 200:
            raise SpotifyError(f"spclient returned HTTP {resp.status_code}")

        data = resp.json()
        total = int(data.get("length") or 0)
        contents = data.get("contents") or {}
        items = contents.get("items") or []
        page_uris = [
            str(item.get("uri", ""))
            for item in items
            if isinstance(item, dict)
            and str(item.get("uri", "")).startswith("spotify:track:")
        ]
        if not page_uris:
            raise SpotifyError("spclient returned no tracks")
        seen = set(uris)
        uris.extend(u for u in page_uris if u not in seen)
        offset += len(page_uris)
        if len(uris) >= total:
            break

    if not uris:
        raise SpotifyError("spclient returned no tracks")
    return total, uris


def _track_embed_metadata(track_id: str) -> dict | None:
    """Fetch a track's metadata (title, artist, duration) from its own embed
    page. Used for tracks beyond the playlist embed preview, which spclient
    reports only as URIs. Returns None only after retries are exhausted
    (transient failures shouldn't silently drop a track)."""
    for attempt in range(3):
        try:
            data = fetch_next_data("track", track_id)
            entity = _navigate_entity(data)
            if entity is None:
                return None
            break
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1.5 * (attempt + 1))

    title = entity.get("name") or entity.get("title") or ""
    if not title:
        return None
    artists = entity.get("artists") or []
    if isinstance(artists, list):
        artist = ", ".join(
            str(a.get("name", ""))
            for a in artists
            if isinstance(a, dict) and a.get("name")
        )
    else:
        artist = entity.get("subtitle") or ""
    duration = entity.get("duration") or 0

    cover_url = ""
    vi = entity.get("visualIdentity")
    if isinstance(vi, dict):
        images = vi.get("image") or []
        if images:
            cover_url = max(
                images, key=lambda x: x.get("maxWidth", 0) * x.get("maxHeight", 0)
            ).get("url", "")

    year = ""
    release = entity.get("releaseDate") or {}
    if isinstance(release, dict):
        iso = release.get("isoString") or ""
        if iso and len(iso) >= 4:
            year = iso[:4]

    return {
        "title": title,
        "artist": artist,
        "duration_ms": int(duration) if duration else 0,
        "cover_url": cover_url,
        "year": year,
    }


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

        album_name = ""
        album_artist = ""
        if kind == "album":
            album_name = entity.get("name", "") or ""
            album_artists = [
                a.get("name", "")
                for a in (entity.get("artists") or [])
                if isinstance(a, dict) and a.get("name")
            ]
            album_artist = ", ".join(album_artists)

        results = []
        for i, entry in enumerate(track_list):
            uid = entry.get("uri", "").split(":")[-1] or entry.get("uid", "")
            if not uid:
                continue
            results.append({
                "id": uid,
                "title": entry.get("title", ""),
                "artist": entry.get("subtitle", ""),
                "album": album_name,
                "album_artist": album_artist,
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
                t["album"] = entry.get("album") or t.get("album") or ""
                if entry.get("album_artist"):
                    t["album_artist"] = entry["album_artist"]
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
                    "album": entry.get("album", ""),
                    "album_artist": entry.get("album_artist", ""),
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
