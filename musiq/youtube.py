"""Audio-optimised fallback search for regular YouTube.

Used when the primary YouTube Music search (ytmusic.search_song) fails
or lacks confidence: fetch the top results of a plain YouTube search and
prefer official audio / lyrics uploads over music videos (which carry
acting cuts, skits and intros that are not part of the song).

Ranking notes
-------------
The fuzzy title bonus is a *relevance floor* only. Topic uploads, music
videos and fan uploads all contain the expected artist + title tokens,
so they all score near-identically on fuzzy. The actual discrimination
comes from the signal weight-tables (_POSITIVE / _NEGATIVE). Do NOT tune
the fuzzy weight to fix a ranking bug — fix the signal table instead.
"""

import re
from typing import Callable

import yt_dlp
from rapidfuzz import fuzz

from musiq.display import console

_TOP_N = 10

_SANE_MIN_SEC = 120
_SANE_MAX_SEC = 600
_DUR_VERY_CLOSE_SEC = 10
_DUR_CLOSE_SEC = 30
_DUR_RATIO_MIN = 0.5
_DUR_RATIO_MAX = 2.0
_DUR_ABS_TOL_SEC = 120

_FUZZY_EXACT = 95
_FUZZY_CLOSE = 88
_FUZZY_WEAK = 70

_POSITIVE = {
    "topic": 12,          # channel name ends in "- Topic" (official auto-upload audio)
    "official_audio": 10,
    "audio": 6,
    "lyrics": 6,
    "no_intro": 3,
    "dur_very_close": 6,  # |diff| <= 10s
    "dur_close": 4,       # |diff| <= 30s
    "dur_sane": 2,        # generic 120-600s bucket, only when expected duration unknown
}

_NEGATIVE = {
    "official_video": -8,
    "music_video": -8,
    "mv": -8,
    "video": -6,          # bare "video" tag — weaker signal than the specific ones
    "fan": -10,
    "live": -6,
    "concert": -6,
    "cover": -6,
    "remix": -8,
    "karaoke": -6,
    "type_beat": -10,
    "instrumental": -8,
    "sped_up": -8,
    "slowed": -8,
    "8d_audio": -8,
    "reaction": -10,
    "tutorial": -10,
}

_SIGNAL_PREDICATES: dict[str, Callable[[str], bool]] = {
    "official_audio": lambda title: "official audio" in title,
    "audio": lambda title: "audio" in title and "official audio" not in title,
    "lyrics": lambda title: "lyrics" in title or "lyric video" in title,
    "no_intro": lambda title: "no intro" in title or "without intro" in title,
    "official_video": lambda title: "official video" in title,
    "music_video": lambda title: "music video" in title,
    "mv": lambda title: bool(re.search(r"\bmv\b", title)),
    "video": lambda title: "video" in title,
    "fan": lambda title: "fan" in title,
    "live": lambda title: "live" in title,
    "concert": lambda title: "concert" in title,
    "cover": lambda title: "cover" in title,
    "remix": lambda title: "remix" in title,
    "karaoke": lambda title: "karaoke" in title,
    "type_beat": lambda title: "type beat" in title,
    "instrumental": lambda title: "instrumental" in title,
    "sped_up": lambda title: "sped up" in title or "speed up" in title or "sped up" in title,
    "slowed": lambda title: "slowed" in title,
    "8d_audio": lambda title: "8d" in title,
    "reaction": lambda title: "reaction" in title,
    "tutorial": lambda title: "tutorial" in title,
}


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


def _fuzzy(query: str, candidate: str) -> tuple[int, float]:
    """Relevance floor: does the candidate contain the query tokens?

    token_set_ratio is used (not token_sort_ratio) because candidates are
    always noisier than the query — "{title} {artist} (Lyrics)", "{title}
    {artist} [Official Video]" etc. Sorting-based metrics penalise those
    extra descriptor words; set-based metrics only require the query's
    tokens to actually appear, which is what we want here.
    """
    ratio = fuzz.token_set_ratio(query, _normalise(candidate))
    if ratio >= _FUZZY_EXACT:
        return 8, ratio
    if ratio >= _FUZZY_CLOSE:
        return 6, ratio
    if ratio >= _FUZZY_WEAK:
        return 3, ratio
    return 0, ratio


def _fuzzy_tier(ratio: float) -> str:
    if ratio >= _FUZZY_EXACT:
        return "exact"
    if ratio >= _FUZZY_CLOSE:
        return "close"
    if ratio >= _FUZZY_WEAK:
        return "weak"
    return "none"


def _duration_signals(duration: float | None, expected: int | None) -> list[tuple[str, int]]:
    if not duration or duration <= 0:
        return []
    if expected and expected > 0:
        diff = abs(duration - expected)
        if diff <= _DUR_VERY_CLOSE_SEC:
            return [("dur_very_close", _POSITIVE["dur_very_close"])]
        if diff <= _DUR_CLOSE_SEC:
            return [("dur_close", _POSITIVE["dur_close"])]
        return []
    if _SANE_MIN_SEC <= duration <= _SANE_MAX_SEC:
        return [("dur_sane", _POSITIVE["dur_sane"])]
    return []


def _duration_excluded(duration: float | None, expected: int | None) -> bool:
    """Hard duration exclusion — keep anything still plausibly the song.

    A candidate survives when it is within the multiplicative range
    [expected*0.5, expected*2.0] OR within an absolute tolerance of 120s;
    it is only dropped when BOTH tests fail. When expected_duration is
    unknown (None) nothing is excluded here — the 120-600s sanity bucket
    still applies as a low-weight bonus instead.
    """
    if not duration or duration <= 0:
        return False
    if not expected or expected <= 0:
        return False
    if expected * _DUR_RATIO_MIN <= duration <= expected * _DUR_RATIO_MAX:
        return False
    if abs(duration - expected) <= _DUR_ABS_TOL_SEC:
        return False
    return True


def _score_entry(entry: dict[str, object], query: str) -> tuple[int, list[tuple[str, int]], float]:
    title = _normalise(entry.get("title") or "")
    channel = _normalise(entry.get("channel") or "")

    score = 0
    signals: list[tuple[str, int]] = []

    for name, weight in _POSITIVE.items():
        if name.startswith("dur_"):
            continue
        if name == "topic":
            if channel.rstrip().endswith(" topic"):
                score += weight
                signals.append((name, weight))
        elif _SIGNAL_PREDICATES[name](title):
            score += weight
            signals.append((name, weight))

    for name, weight in _NEGATIVE.items():
        if _SIGNAL_PREDICATES[name](title):
            score += weight
            signals.append((name, weight))

    fz_score, fz_ratio = _fuzzy(query, title)
    score += fz_score

    return score, signals, fz_ratio


def _describe(score: int, entry: dict[str, object], signals: list[tuple[str, int]], fz: float) -> str:
    joined = ", ".join(f"{name}:{weight:+d}" for name, weight in signals) or "no title signals"
    return (
        f'"{entry.get("title")}" ({entry.get("channel")}) — '
        f"{score} pts (fuzz {fz:.0f}, {joined})"
    )


def search_best_audio(
    title: str,
    artist: str,
    expected_duration_sec: int | None = None,
) -> str | None:
    """Pick the most audio-friendly match from a plain YouTube search.

    Returns a watch URL, or None if the search failed or every result was
    hard-excluded on duration (the caller falls back to ytsearch1:).
    """
    query = f"{title} {artist}"
    query_norm = _normalise(query)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 15,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{_TOP_N}:{query}", download=False)
    except Exception as exc:
        console.print(f"[yellow]⚠ YouTube search failed: {exc}[/yellow]")
        return None

    entries = [e for e in (info or {}).get("entries", []) if e and e.get("id")]
    if not entries:
        return None

    scored: list[tuple[int, dict[str, object], list[tuple[str, int]], float]] = []
    excluded = 0
    for entry in entries:
        duration = entry.get("duration")
        if _duration_excluded(duration, expected_duration_sec):
            excluded += 1
            continue
        score, signals, fz_ratio = _score_entry(entry, query_norm)
        for name, weight in _duration_signals(duration, expected_duration_sec):
            score += weight
            signals.append((name, weight))
        scored.append((score, entry, signals, fz_ratio))

    if not scored:
        if excluded:
            console.print(
                f"[dim]  yt-fallback: all {_TOP_N} results duration-excluded for "
                f"{expected_duration_sec}s; falling through to ytsearch1[/dim]"
            )
        return None

    scored.sort(key=lambda s: (s[0], s[1].get("view_count") or 0), reverse=True)

    console.print(f"[dim]  yt-fallback pick:[/dim] {_describe(*scored[0])}")
    for run in scored[1:4]:
        console.print(f"[dim]    vs [/dim]{_describe(*run)}")
    if excluded:
        console.print(f"[dim]    ({excluded} of {len(entries)} results duration-excluded)[/dim]")

    return f"https://www.youtube.com/watch?v={scored[0][1]['id']}"