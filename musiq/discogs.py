"""Discogs API client — database search + release metadata lookups.

Auth uses the Discogs Auth header: a personal token if present, otherwise
the consumer key/secret pair. Both live in ~/.musiq/config (or env vars),
search and cover-image URLs require one of them. Read-only, never raises.
"""

import os
import re
from pathlib import Path

import requests
from rapidfuzz import fuzz

from musiq.display import console

_API_ROOT = "https://api.discogs.com"
_USER_AGENT = "musiq/0.3.0 (https://github.com/musiq/musiq)"
_TIMEOUT = 10

_FUZZY_TITLE = 85
_NORMALISABLE = re.compile(r"[^a-z0-9 ]")

_ERRORS = {
    401: "auth rejected — check DISCOGS_KEY/SECRET (or DISCOGS_TOKEN)",
    403: "forbidden — check Discogs credentials",
    404: "resource not found",
    429: "rate limited",
}


def _load_credentials() -> tuple[str, str, str]:
    """Return (key, secret, token). Empty strings mean unset."""
    cfg: dict[str, str] = {}
    config_path = Path.home() / ".musiq" / "config"
    if config_path.is_file():
        try:
            for line in config_path.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip()
        except OSError:
            pass

    token = os.environ.get("DISCOGS_TOKEN", "").strip() or cfg.get("DISCOGS_TOKEN", "").strip()
    key = os.environ.get("DISCOGS_KEY", "").strip() or cfg.get("DISCOGS_KEY", "").strip()
    secret = os.environ.get("DISCOGS_SECRET", "").strip() or cfg.get("DISCOGS_SECRET", "").strip()
    return key, secret, token


def _auth_headers() -> dict[str, str]:
    key, secret, token = _load_credentials()
    headers = {"User-Agent": _USER_AGENT}
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    elif key and secret:
        headers["Authorization"] = f"Discogs key={key}, secret={secret}"
    return headers


def _discogs_get(path: str, params: dict) -> tuple[dict | None, int | None]:
    """GET an endpoint. Returns (json_or_None, http_status_or_None). Never raises."""
    try:
        resp = requests.get(
            f"{_API_ROOT}/{path}",
            params=params,
            headers=_auth_headers(),
            timeout=_TIMEOUT,
        )
    except requests.RequestException:
        return None, None
    if resp.status_code != 200:
        return None, resp.status_code
    try:
        return resp.json(), 200
    except ValueError:
        return None, 200


def _normalise(text: str) -> str:
    return _NORMALISABLE.sub(" ", (text or "").lower()).strip()


def _titles_match(a: str, b: str) -> bool:
    na, nb = _normalise(a), _normalise(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na.startswith(nb + " ") or nb.startswith(na + " "):
        return True
    # Discogs bundles related tracks into one entry ("Marvins Room / Buried
    # Alive (Interlude)") — match if any "/"-separated part matches.
    for part in na.split("/"):
        part = part.strip()
        if not part:
            continue
        if part == nb or part.startswith(nb + " ") or nb.startswith(part + " "):
            return True
    return fuzz.token_set_ratio(na, nb) >= _FUZZY_TITLE


def _search_releases(artist: str, release_title: str) -> list[dict]:
    params: dict = {"type": "release", "per_page": "10"}
    if artist:
        params["artist"] = artist
    if release_title:
        params["release_title"] = release_title
    data, _ = _discogs_get("database/search", params)
    results = data.get("results", []) if isinstance(data, dict) else []
    return [r for r in results if isinstance(r, dict)]


def _pick_release(candidates: list[dict], artist: str, album: str, title: str, year: str) -> list[dict]:
    """Rank search hits locally and return them best-first (no extra requests)."""
    target = album or title

    def _score(r: dict) -> tuple[float, str]:
        combined = r.get("title") or ""
        release_part = combined.partition(" - ")[2] or combined
        score = fuzz.token_set_ratio(_normalise(release_part), _normalise(target))
        if title and _normalise(title) in _normalise(combined):
            score += 15
        if artist:
            artist_part = combined.partition(" - ")[0]
            if artist_part and _titles_match(artist_part, artist):
                score += 10
        ry = r.get("year")
        if ry:
            score += 6
            if year and str(ry) == str(year):
                score += 8
        return score, str(r.get("id") or "")

    ranked = sorted(candidates, key=_score, reverse=True)
    return ranked


def _find_track(tracklist: list[dict], title: str) -> tuple[dict, int, bool]:
    """Match a track by name. Returns (track, index, matched)."""
    tracklist = [t for t in tracklist if isinstance(t, dict)]
    if not tracklist:
        return {}, -1, False
    for i, track in enumerate(tracklist):
        if _titles_match(track.get("title") or "", title or ""):
            return track, i, True
    if len(tracklist) == 1:
        return tracklist[0], 0, True
    return {}, -1, False


def _parse_position(position: str, index: int) -> int:
    m = re.search(r"\d+", position or "")
    if m:
        try:
            return int(m.group())
        except ValueError:
            pass
    return index + 1


def _duration_to_s(duration: str) -> int:
    total = 0
    for part in (duration or "").split(":"):
        if not part.isdigit():
            return 0
        total = total * 60 + int(part)
    return total


def _composers(track: dict) -> str:
    out: list[str] = []
    for ea in track.get("extraartists") or []:
        if not isinstance(ea, dict):
            continue
        role = ea.get("role") or ""
        name = ea.get("name")
        if "Written-By" in role and name and name not in out:
            out.append(name)
    return ", ".join(out)


def _image_url(release: dict) -> str:
    for img in release.get("images") or []:
        if isinstance(img, dict) and img.get("type") == "primary":
            return img.get("uri") or ""
    return ""


def fetch_track_metadata(artist: str, title: str, album: str = "", year: str = "") -> dict:
    """Look up a single track's metadata on Discogs. Never raises."""
    empty = {
        "ok": False,
        "error": "",
        "album": "",
        "year": "",
        "genre": "",
        "styles": [],
        "track_num": 0,
        "composer": "",
        "duration_s": 0,
        "country": "",
        "data_quality": "",
        "match": False,
        "image_url": "",
    }

    key, secret, token = _load_credentials()
    if not token and not (key and secret):
        console.print("[dim][discogs] No DISCOGS credentials configured — skipping[/dim]")
        empty["error"] = "no DISCOGS credentials in ~/.musiq/config"
        return empty

    # Prefer the album as search target (better release), then the track title.
    attempts = [(artist, album), (artist, title)] if album and album != title else [(artist, title)]
    candidates: list[dict] = []
    for a, t in attempts:
        candidates = _search_releases(a, t)
        if candidates:
            break
    if not candidates:
        empty["error"] = "no search results"
        return empty

    ranked = _pick_release(candidates, artist, album, title, year)

    # Fetch up to 3 best candidates, stopping at the first whose tracklist
    # actually contains the track (pressings vary). If none match, keep the
    # first fetched release for release-level data only.
    fetched: list[dict] = []
    last_status: int | None = None
    track, index, match = {}, -1, False
    for cand in ranked[: min(3, len(ranked))]:
        data, status = _discogs_get(f"releases/{cand.get('id')}", {})
        if data is None:
            last_status = status
            continue
        fetched.append(data)
        track, index, match = _find_track(data.get("tracklist") or [], title)
        if match:
            break

    if not fetched:
        empty["error"] = _ERRORS.get(last_status, "request failed")
        return empty

    release = fetched[0]
    genres = [g for g in (release.get("genres") or []) if g]
    styles = [s for s in (release.get("styles") or []) if s]
    raw_year = release.get("year") or 0
    track_num = _parse_position(track.get("position"), index) if track and match else 0

    return {
        "ok": True,
        "error": "",
        "album": release.get("title") or "",
        "year": str(raw_year) if raw_year else "",
        "genre": genres[0] if genres else "",
        "styles": styles,
        "track_num": track_num,
        "composer": _composers(track) if match else "",
        "duration_s": _duration_to_s(track.get("duration") or "") if track and match else 0,
        "country": release.get("country") or "",
        "data_quality": release.get("data_quality") or "",
        "match": match,
        "image_url": _image_url(release),
    }