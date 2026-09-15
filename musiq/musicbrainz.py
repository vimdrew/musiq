"""MusicBrainz metadata confirmation and disambiguation.

Two independent roles:

1. Confirmation (advisory sanity-check for downloads). Confirms Spotify
   track metadata (title, album, genre, duration) against MusicBrainz's
   ISRC-indexed recording data. This is pure logging and diagnostics: it
   never selects audio, never changes what gets downloaded, and never
   raises. MusicBrainz being down, rate-limited or missing the ISRC maps
   to "unknown" and the download proceeds unchanged.
2. Disambiguation (`recording_metadata`, used by ``musiq retag``). Looks a
   recording up by artist + title across release-groups and picks the best
   album/year/genre candidate for a track whose exact release identity is
   ambiguous (see the function's docstring for the selection rules).

Verdicts per field (role 1)
---------------------------
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
downloads (workers=3) serialise their confirmations instead of bursting,
and interactive retag lookups respect the same gate. Each per-track
attempt is bounded by a deadline and gives up with no result rather than
ever blocking.
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
MB_RETAG_USER_AGENT = "musiq-cli/1.0 (vimdrew@proton.me)"

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
    ua: str | None = None,
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
            headers={"User-Agent": ua or MB_USER_AGENT},
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
            return _mb_get(path, params, deadline, ua=ua, _retried=True)
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


# --- Recording disambiguation lookup (used by `musiq retag`) ---------------

_NON_ALBUM_SECONDARY = {
    "Live", "Compilation", "Remix", "Bootleg", "Soundtrack", "Mixtape/Street",
    "Audio drama", "Interview", "Spokenword", "Demo", "Field recording",
}

_RETAG_DEADLINE_SEC = 8.0


def _lucene_quote(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace('"', '\\"')


def _mb_year(date_str: str) -> str:
    return (date_str or "").strip()[:4]


def _album_matches(album_value: str, current_album: str) -> bool:
    if not album_value or not current_album:
        return False
    a = _normalise(album_value).strip()
    b = _normalise(current_album).strip()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return fuzz.token_set_ratio(a, b) >= 90


def _date_sort_key(date_str: str) -> tuple[int, int, int]:
    """Sortable key for a 'YYYY[-MM[-DD]]' date; missing dates sort last."""
    digits = (date_str or "").strip().split("-")
    try:
        y = int(digits[0]) if digits and digits[0].isdigit() else 9999
    except (IndexError, ValueError):
        y = 9999
    m = int(digits[1]) if len(digits) > 1 and digits[1].isdigit() else 0
    d = int(digits[2]) if len(digits) > 2 and digits[2].isdigit() else 0
    return y, m, d


def _empty_recording_metadata() -> dict:
    return {
        "ok": False,
        "error": "",
        "album": "",
        "year": "",
        "genre": "",
        "genres": [],
        "match": False,
        "reason": "",
        "candidates": 0,
        "rg_title": "",
        "release_title": "",
        "primary_type": "",
        "secondary_types": [],
        "first_release_date": "",
        "recording_title": "",
    }


def recording_metadata(artist: str, title: str, album: str = "") -> dict:
    """Look up a recording by artist + title and pick its best release.

    MusicBrainz is MBID-keyed, so an artist/title search returns only true
    recordings of that work — but usually several (studio, live, remix,
    acapella, compilation appearances). Selection, best first:

    a. A candidate whose release-group title matches the file's current
       album tag (fuzzy) — the existing tag is strong provenance evidence.
    b. Otherwise a release-group that is a clean ``Album`` (primary type
       Album, no secondary types), which rules out Live/Compilation/Remix/
       Bootleg/Soundtrack etc.
    c. Among the remaining ties, the earliest release date.
    d. If nothing qualifies, ``match`` is False and album/year are left
       empty — callers must keep their existing values instead of applying
       a weak match.

    Genre is only reported when MusicBrainz actually carries it (rare); the
    caller keeps iTunes as the genre source otherwise. Returns a plain dict,
    never raises.
    """
    result = _empty_recording_metadata()
    if not artist or not title:
        result["error"] = "no artist/title to search"
        return result

    deadline = time.monotonic() + _RETAG_DEADLINE_SEC
    query = f'artist:"{_lucene_quote(artist)}" AND recording:"{_lucene_quote(title)}"'

    try:
        data, status = _mb_get(
            "recording",
            {"query": query, "limit": 25, "inc": "releases+release-groups+genres"},
            deadline,
            ua=MB_RETAG_USER_AGENT,
        )
    except Exception as exc:
        result["error"] = f"unexpected: {exc!r}"
        return result
    if not data:
        if status in _RETRYABLE:
            result["error"] = "mb unavailable (retry exhausted)"
        elif status is None:
            result["error"] = "mb request failed/timed out"
        else:
            result["error"] = f"mb http {status}"
        return result

    recordings = [r for r in data.get("recordings") or [] if isinstance(r, dict)]
    if not recordings:
        result["error"] = "no recordings found"
        return result

    # Gather one candidate per (recording, release-group) pair.
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for rec in recordings:
        rec_title = rec.get("title") or ""
        title_sim = (
            1.0
            if _titles_equal(rec_title, title)
            else fuzz.token_set_ratio(_normalise(rec_title), _normalise(title)) / 100.0
        )
        for rel in (rec.get("releases") or []):
            if not isinstance(rel, dict):
                continue
            rg = rel.get("release-group") if isinstance(rel.get("release-group"), dict) else {}
            rg_title = rg.get("title") or ""
            rg_id = rg.get("id") or ""
            rec_id = rec.get("id") or ""
            key = (rec_id, rg_id or rel.get("id") or rg_title)
            if key in seen:
                continue
            seen.add(key)

            album_value = rg_title or rel.get("title") or ""
            primary = rg.get("primary-type") or ""
            secondary = [s for s in (rg.get("secondary-types") or []) if s]
            genres = []
            for src in (rg, rec, rel):
                for g in src.get("genres") or []:
                    if isinstance(g, dict) and g.get("name"):
                        genres.append(g["name"])
            date = (
                rg.get("first-release-date")
                or rel.get("date")
                or ""
            )
            candidates.append({
                "recording_title": rec_title,
                "recording_id": rec_id,
                "title_sim": title_sim,
                "album_value": album_value,
                "rg_title": rg_title,
                "release_title": rel.get("title") or "",
                "rg_id": rg_id,
                "primary_type": primary,
                "secondary_types": secondary,
                "clean_album": primary == "Album" and not secondary,
                "date": date,
                "genres": list(dict.fromkeys(genres)),
                "official": rel.get("status") in (None, "", "Official"),
            })

    result["candidates"] = len(candidates)
    if not candidates:
        result["error"] = "recordings have no releases"
        return result

    def _tie_sort(c: dict):
        return (
            -c["title_sim"],
            _date_sort_key(c["date"]),
            0 if c["official"] else 1,
            c["release_title"],
        )

    chosen = None
    if album:
        matched = sorted(
            [c for c in candidates if _album_matches(c["album_value"], album)],
            key=_tie_sort,
        )
        if matched:
            chosen = matched[0]
            result["reason"] = f"album '{album}' matched release-group '{chosen['rg_title'] or chosen['release_title']}'"
    if chosen is None:
        clean = sorted(
            [c for c in candidates if c["clean_album"]],
            key=_tie_sort,
        )
        if clean:
            chosen = clean[0]
            base = chosen["rg_title"] or chosen["release_title"]
            result["reason"] = (
                f"clean Album release-group '{base}'"
                + (f" ({chosen['date']})" if chosen["date"] else "")
            )

    if chosen is None:
        albums = sorted(
            {c["rg_title"] or c["release_title"] or "?"
             for c in candidates if c["primary_type"] == "Album"},
        )
        sec_album = sorted({
            s for c in candidates for s in c["secondary_types"]
        })
        if albums and sec_album:
            result["reason"] = (
                f"{len(albums)} Album release-group(s) are all secondary "
                f"({', '.join(sec_album)}) - no clean Album release"
            )
        else:
            types = sorted({c["primary_type"] for c in candidates if c["primary_type"]})
            result["reason"] = (
                "no clean Album release among "
                f"{len(candidates)} candidate(s)"
                + (f" ({', '.join(types)})" if types else "")
            )
        result["ok"] = True
        return result

    result["ok"] = True
    result["match"] = True
    result["album"] = chosen["album_value"]
    result["year"] = _mb_year(chosen["date"])
    result["genres"] = chosen["genres"]
    result["genre"] = chosen["genres"][0] if chosen["genres"] else ""
    result["rg_title"] = chosen["rg_title"]
    result["release_title"] = chosen["release_title"]
    result["primary_type"] = chosen["primary_type"]
    result["secondary_types"] = chosen["secondary_types"]
    result["first_release_date"] = chosen["date"]
    result["recording_title"] = chosen["recording_title"]
    return result


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