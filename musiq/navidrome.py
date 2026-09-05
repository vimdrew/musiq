import hashlib
import os
import secrets
import time
from pathlib import Path

import requests

from musiq.display import console

_CONFIG_PATH = Path.home() / ".musiq" / "config"


# ── Config ────────────────────────────────────────────────────────

def _load_navidrome_config() -> tuple[str | None, str | None, str | None]:
    """Load navidrome_url, navidrome_user, navidrome_password from env or config file."""
    url = os.environ.get("NAVIDROME_URL")
    user = os.environ.get("NAVIDROME_USER")
    password = os.environ.get("NAVIDROME_PASSWORD")
    if url and user and password:
        return url, user, password

    cfg = _CONFIG_PATH
    if cfg.is_file():
        try:
            for line in cfg.read_text().splitlines():
                line = line.strip()
                if line.startswith("NAVIDROME_URL="):
                    url = url or line.split("=", 1)[1]
                elif line.startswith("NAVIDROME_USER="):
                    user = user or line.split("=", 1)[1]
                elif line.startswith("NAVIDROME_PASSWORD="):
                    password = password or line.split("=", 1)[1]
        except OSError:
            pass
    return url, user, password


def _save_navidrome_config(url: str, user: str, password: str) -> None:
    """Append navidrome fields to ~/.musiq/config, preserving existing lines."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if _CONFIG_PATH.is_file():
        lines = _CONFIG_PATH.read_text().splitlines()

    keys = {"NAVIDROME_URL": url, "NAVIDROME_USER": user, "NAVIDROME_PASSWORD": password}
    new_lines = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else None
        if key in keys:
            new_lines.append(f"{key}={keys.pop(key)}")
        else:
            new_lines.append(line)
    for key, val in keys.items():
        new_lines.append(f"{key}={val}")

    _CONFIG_PATH.write_text("\n".join(new_lines) + "\n")


def ensure_navidrome_config() -> tuple[str, str, str] | None:
    """Return (url, user, password), prompting interactively if missing. Returns None on cancel."""
    url, user, password = _load_navidrome_config()
    if url and user and password:
        return url, user, password

    console.print("[yellow]Navidrome credentials not found.[/yellow]")
    console.print("[dim]Enter your Subsonic-compatible server details (e.g. Navidrome).[/dim]")

    import click
    url = url or click.prompt("Navidrome URL (e.g. http://localhost:4533)")
    user = user or click.prompt("Username")
    password = password or click.prompt("Password", hide_input=True)

    _save_navidrome_config(url, user, password)
    console.print(f"[green]✓ Config saved to {_CONFIG_PATH}[/green]")
    return url, user, password


# ── Subsonic auth ─────────────────────────────────────────────────

def _auth_params(user: str, password: str) -> dict:
    """Build the salted-token auth params for the Subsonic API."""
    salt = secrets.token_hex(16)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    return {
        "u": user,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "musiq",
        "f": "json",
    }


# ── Subsonic API helpers ──────────────────────────────────────────

def _api_call(base_url: str, user: str, password: str, endpoint: str, **extra) -> dict:
    """Make a Subsonic API call and return the parsed JSON response."""
    url = f"{base_url.rstrip('/')}/rest/{endpoint}"
    params = {**_auth_params(user, password), **extra}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    status = data.get("subsonic-response", {})
    if status.get("status") != "ok":
        code = status.get("status", "unknown")
        message = status.get("error", {}).get("message", "unknown error")
        raise RuntimeError(f"Subsonic API error ({code}): {message}")
    return status


def _api_call_list(base_url: str, user: str, password: str, endpoint: str, params: list) -> dict:
    """Make a Subsonic API call with an explicit param list (allows repeated keys)."""
    url = f"{base_url.rstrip('/')}/rest/{endpoint}"
    full = [*_auth_params(user, password).items(), *params]
    resp = requests.get(url, params=full, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    status = data.get("subsonic-response", {})
    if status.get("status") != "ok":
        code = status.get("status", "unknown")
        message = status.get("error", {}).get("message", "unknown error")
        raise RuntimeError(f"Subsonic API error ({code}): {message}")
    return status


# ── Library scan ──────────────────────────────────────────────────

def start_scan(base_url: str, user: str, password: str) -> bool:
    """Trigger a Navidrome library scan. Returns True on success."""
    try:
        _api_call(base_url, user, password, "startScan")
        return True
    except Exception as exc:
        console.print(f"[yellow]⚠ Could not start scan: {exc}[/yellow]")
        return False


def get_scan_status(base_url: str, user: str, password: str) -> dict | None:
    """Return scan status dict with 'scanning' bool, or None on error."""
    try:
        status = _api_call(base_url, user, password, "getScanStatus")
        return status.get("scanStatus", {})
    except Exception:
        return None


def wait_for_scan(base_url: str, user: str, password: str, timeout: int = 180) -> bool:
    """Poll getScanStatus until scanning is false. Returns True when done."""
    deadline = time.time() + timeout
    # Give Navidrome a moment to register the scan request
    time.sleep(2)

    while time.time() < deadline:
        st = get_scan_status(base_url, user, password)
        if st is None:
            console.print("[yellow]⚠ Could not query scan status — retrying[/yellow]")
        elif st.get("scanning", False):
            count = st.get("count", "?")
            console.print(f"[dim]Scanning library... ({count} items)[/dim]")
        else:
            console.print("[green]✓ Library scan complete[/green]")
            return True
        time.sleep(3)

    console.print("[yellow]⚠ Scan timed out — proceeding anyway[/yellow]")
    return False


# ── Search ────────────────────────────────────────────────────────

def search3(base_url: str, user: str, password: str, query: str) -> list[dict]:
    """Search for songs via Subsonic search3. Returns list of song dicts."""
    try:
        status = _api_call(base_url, user, password, "search3", query=query, songCount=5)
        result = status.get("searchResult3") or status.get("searchResult")
        songs = result.get("song", []) if isinstance(result, dict) else []
        return songs if isinstance(songs, list) else []
    except Exception:
        return []


# ── Playlists ─────────────────────────────────────────────────────

def get_playlists(base_url: str, user: str, password: str) -> list[dict]:
    """Return list of existing playlists (each has 'id' and 'name')."""
    try:
        status = _api_call(base_url, user, password, "getPlaylists")
        playlists = status.get("playlists", {}).get("playlist", [])
        return playlists if isinstance(playlists, list) else []
    except Exception:
        return []


def create_playlist(base_url: str, user: str, password: str, name: str, song_ids: list[str]) -> str | None:
    """Create a playlist. Returns its ID, or None on failure."""
    params: list = [("name", name)]
    for sid in song_ids:
        params.append(("songId", sid))
    try:
        status = _api_call_list(base_url, user, password, "createPlaylist", params)
        pl = status.get("playlist", {})
        return pl.get("id") if isinstance(pl, dict) else None
    except Exception as exc:
        console.print(f"[yellow]⚠ Could not create playlist '{name}': {exc}[/yellow]")
        return None


def update_playlist(base_url: str, user: str, password: str, playlist_id: str, song_ids: list[str]) -> bool:
    """Replace a playlist's track list entirely."""
    params: list = [("playlistId", playlist_id)]
    for sid in song_ids:
        params.append(("songId", sid))
    try:
        _api_call_list(base_url, user, password, "updatePlaylist", params)
        return True
    except Exception as exc:
        console.print(f"[yellow]⚠ Could not update playlist: {exc}[/yellow]")
        return False
