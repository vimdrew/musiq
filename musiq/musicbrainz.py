"""MusicBrainz metadata confirmation — advisory sanity-check layer.

Confirms Spotify track metadata (title, album, genre, duration) against
MusicBrainz's ISRC-indexed recording data. This is pure logging and
diagnostics: it never selects audio, never changes what gets downloaded,
and never raises. MusicBrainz being down, rate-limited or missing the
ISRC maps to "unknown" and the download proceeds unchanged.

Verdicts per field
------------------
agree       expected and MusicBrainz values match (fuzzy for genre).
disagree    both sides have data and it does not match.
no_overlap  genre only — both sides have data but no term overlaps
            (distinct from "unknown": the data is present, it just
            disagrees, and the taxonomy mismatch is the signal).
unknown     one side (or MusicBrainz itself) has no usable data.

Rate limiting
-------------
MusicBrainz enforces ~1 request/second. All requests go through a
module-global spacing gate (threading.Lock + monotonic clock) so parallel
downloads (workers=3) serialise their confirmations instead of bursting.
The whole per-track attempt is bounded by a deadline and gives up to
"unknown" rather than ever blocking a download.
"""

import re
import threading
import time
from urllib.parse import quote

import requests
from rapidfuzz import fuzz

from musiq.display import console

MB_BASE = "https://musicbrainz.org/ws/2"
MB_USER_AGENT = "musiq/0.2.0 (https://github.com/musiq/musiq)"

_REQUEST_TIMEOUT = 10
_RETRYABLE = {500, 502, 503}
_RETRY_SLEEP = 2
_DEADLINE_SEC = 6.0
_MIN_SPACING_SEC = 1.0

_DUR_TOLERANCE_MS = 3000
_FUZZY_TITLE = 90
_FUZZY_GENRE = 70

_VERDICT_STYLE = {
    "agree": "green",
    "disagree": "yellow",
    "no_overlap": "yellow",
    "unknown": "dim",
}

_spacing_lock = threading.Lock()
_last_request_at = 0.0


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace runs."""
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


def _space_for_request(deadline: float) -> bool:
    """Wait for the next allowed MusicBrainz request slot.

    Returns True when a slot is available before ``deadline`` (monotonic
    seconds); False when waiting would push past it (caller bails).
    """
    global _last_request_at
    while True:
        with _spacing_lock:
            now = time.monotonic()
            earliest = _last_request_at + _MIN_SPACING_SEC
            if now >= earliest:
                _last_request_at = now
                return True
        wait = earliest - now
        if wait >= deadline - time.monotonic():
            return False
        time.sleep(min(wait, 0.05))


def _mb_get(
    path: str,
    params: dict,
    deadline: float,
    _retried: bool = False,
) -> tuple[dict | None, int | None]:
    """GET a MusicBrainz endpoint honouring the global spacing gate.

    Returns (json_body_or_None, http_status_or_None). One retry for
    500/502/503 after a short sleep. Never raises.
    """
    if not _space_for_request(deadline):
        return None, None
    try:
        resp = requests.get(
            f"{MB_BASE}/{path}",
            params={**params, "fmt": "json"},
            headers={"User-Agent": MB_USER_AGENT},
            timeout=_REQUEST_TIMEOUT,
        )
        status = resp.status_code
    except requests.RequestException:
        return None, None
    if status == 200:
        try:
            return resp.json(), 200
        except ValueError:
            return None, 200
    if status in _RETRYABLE and not _retried:
        remaining = deadline - time.monotonic()
        if remaining > _RETRY_SLEEP:
            time.sleep(_RETRY_SLEEP)
            return _mb_get(path, params, deadline, _retried=True)
    return None, status


def _titles_equal(a: str, b: str) -> bool:
    return _normalise(a).strip() == _normalise(b).strip()


def _title_matches(mb_title: str, expected: str) -> bool:
    """Title verdict: normalised-exact, else split/joined-variant
    ('Passion Fruit' vs 'Passionfruit'), else fuzzy trade-offs for
    descriptors Spotify keeps but MusicBrainz strips."""
    a = _normalise(mb_title).strip()
    b = _normalise(expected).strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if a.replace(" ", "") == b.replace(" ", ""):
        return True
    return fuzz.token_set_ratio(a, b) >= _FUZZY_TITLE


def _genre_overlaps(tag: str, expected: str) -> bool:
    """Genre verdict: normalised-token overlap or fuzzy. Taxonomies differ
    (iTunes 'Hip-Hop/Rap' vs MB 'hip hop'), so this is deliberately loose."""
    a = _normalise(tag).strip()
    b = _normalise(expected).strip()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return fuzz.token_set_ratio(a, b) >= _FUZZY_GENRE


def _empty_result(
    isrc: str, genre_source: str, expected_genre: str | None
) -> dict:
    return {
        "isrc": isrc,
        "found": False,
        "mb_title": None,
        "mb_album_candidates": [],
        "mb_duration_ms": None,
        "mb_tags": [],
        "matches": {
            "title": "unknown",
            "album": "unknown",
            "genre": "unknown",
            "duration": "unknown",
        },
        "genre_source": genre_source,
        "expected_genre": expected_genre,
        "error": None,
    }


def confirm_metadata(
    isrc: str,
    expected_title: str,
    expected_album: str,
    expected_genre: str | None = None,
    expected_duration_ms: int | None = None,
    genre_source: str = "itunes",
) -> dict:
    """Confirm Spotify metadata against MusicBrainz for an ISRC.

    Returns a plain dict (see _empty_result for the shape) with per-field
    verdicts. Advisory only — never raises, always returns a dict.
    """
    result = _empty_result(isrc, genre_source, expected_genre)
    deadline = time.monotonic() + _DEADLINE_SEC

    try:
        data, status = _mb_get(
            f"isrc/{quote(isrc)}",
            {"inc": "tags"},
            deadline,
        )
        if not data:
            if status == 404:
                result["error"] = "isrc not found"
            elif status in _RETRYABLE:
                result["error"] = "mb unavailable (retry exhausted)"
            elif status is None:
                result["error"] = "mb request failed/timed out"
            else:
                result["error"] = f"mb http {status}"
            return result

        recordings = [r for r in (data.get("recordings") or []) if isinstance(r, dict)]
        if not recordings:
            result["error"] = "no recordings for isrc"
            return result

        result["found"] = True
        matches = result["matches"]

        # Title: any matched recording's title is a candidate.
        result["mb_title"] = recordings[0].get("title")
        title_hits = [_title_matches(r.get("title"), expected_title) for r in recordings]
        if any(title_hits):
            matches["title"] = "agree"
        elif all(not _normalise(r.get("title") or "").strip() for r in recordings):
            matches["title"] = "unknown"
        else:
            matches["title"] = "disagree"

        # Duration (sanity signal only), from the first recording with a length.
        for rec in recordings:
            if rec.get("length"):
                result["mb_duration_ms"] = rec.get("length")
                break
        if expected_duration_ms and result["mb_duration_ms"]:
            diff = abs(result["mb_duration_ms"] - expected_duration_ms)
            matches["duration"] = "agree" if diff <= _DUR_TOLERANCE_MS else "disagree"
        else:
            matches["duration"] = "unknown"

        # Album + release-group tags need a second call: the ISRC resource
        # exposes recordings but not their releases (inc=releases is silently
        # ignored there), so expand via the recording resource.
        recording_id = recordings[0].get("id")
        candidates: list[str] = []
        rg_tags: list[str] = []
        if expected_album and recording_id:
            rel_data, _ = _mb_get(
                f"recording/{quote(recording_id)}",
                {"inc": "releases+release-groups+tags"},
                deadline,
            )
            if rel_data:
                for rel in rel_data.get("releases") or []:
                    if not isinstance(rel, dict):
                        continue
                    if rel.get("title"):
                        candidates.append(rel["title"])
                    rg = rel.get("release-group")
                    if isinstance(rg, dict):
                        if rg.get("title"):
                            candidates.append(rg["title"])
                        for tag in rg.get("tags") or []:
                            if isinstance(tag, dict) and tag.get("name"):
                                rg_tags.append(tag["name"])
        result["mb_album_candidates"] = list(dict.fromkeys(candidates))
        if expected_album:
            if any(_titles_equal(c, expected_album) for c in candidates):
                matches["album"] = "agree"
            elif candidates:
                matches["album"] = "disagree"
            else:
                matches["album"] = "unknown"
        else:
            matches["album"] = "unknown"

        # Genre: recording-level folksonomy tags (plus any rg tags from the
        # album call). MusicBrainz has no strict genre field.
        mb_tags = [
            tag["name"]
            for rec in recordings
            for tag in rec.get("tags") or []
            if isinstance(tag, dict) and tag.get("name")
        ]
        mb_tags.extend(rg_tags)
        result["mb_tags"] = list(dict.fromkeys(mb_tags))

        if not expected_genre:
            matches["genre"] = "unknown"
        elif mb_tags:
            if any(_genre_overlaps(t, expected_genre) for t in mb_tags):
                matches["genre"] = "agree"
            else:
                matches["genre"] = "no_overlap"
        else:
            matches["genre"] = "unknown"

        return result
    except Exception as exc:
        result["error"] = f"unexpected: {exc!r}"
        return result


def log_confirmation(result: dict) -> None:
    """Render a confirmation result as a single console line."""
    isrc = result.get("isrc", "")
    matches = result.get("matches", {})
    source = result.get("genre_source") or "none"

    parts = []
    for name in ("title", "album", "duration"):
        verdict = matches.get(name, "unknown")
        style = _VERDICT_STYLE.get(verdict, "dim")
        parts.append(f"{name}=[{style}]{verdict}[/{style}]")
    gv = matches.get("genre", "unknown")
    gstyle = _VERDICT_STYLE.get(gv, "dim")
    parts.append(f"genre({source})=[{gstyle}]{gv}[/{gstyle}]")

    detail = []
    if result.get("mb_title"):
        detail.append(f"MB: {result['mb_title']}")
    albums = (result.get("mb_album_candidates") or [])[:3]
    if albums:
        detail.append("albums=" + ", ".join(albums))
    duration_ms = result.get("mb_duration_ms")
    if duration_ms:
        detail.append(f"dur={int(duration_ms / 1000)}s")
    tags = (result.get("mb_tags") or [])[:6]
    if tags:
        detail.append("tags=" + ", ".join(tags))
    if result.get("error"):
        detail.append(f"({result['error']})")

    style = (
        "yellow"
        if any(v in ("disagree", "no_overlap") for v in matches.values())
        else "dim"
    )
    text = ", ".join(parts)
    if detail:
        text += f" [dim]({'; '.join(detail)})[/dim]"
    console.print(f"[{style}][mb] {isrc}: {text}[/{style}]")


def confirm_and_log(
    isrc: str,
    expected_title: str,
    expected_album: str,
    expected_genre: str | None,
    genre_source: str,
    expected_duration_ms: int | None,
) -> None:
    """Run confirm_metadata and log the result. Safe as a thread target."""
    result = confirm_metadata(
        isrc,
        expected_title,
        expected_album,
        expected_genre=expected_genre,
        expected_duration_ms=expected_duration_ms,
        genre_source=genre_source,
    )
    log_confirmation(result)