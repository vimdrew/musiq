import io
from pathlib import Path

import eyed3
import requests

from musiq.display import console

GENRE_MAP = {
    "Hip-Hop/Rap": "Hip-Hop",
    "R&B/Soul": "R&B",
    "Singer/Songwriter": "Folk",
    "Alternative/Indie": "Alternative",
    "Dance/Electronic": "Electronic",
    "Pop/Rock": "Rock",
}


def normalise_genre(genre: str) -> str:
    return GENRE_MAP.get(genre, genre)


def tag_file(
    filepath: Path,
    title: str,
    artist: str,
    album: str,
    track_number: int,
    year: str,
    genre: str,
    cover_url: str | None = None,
) -> None:
    try:
        audio = eyed3.load(str(filepath))
        if audio is None:
            audio = eyed3.load(str(filepath))
        if audio is None:
            console.print(f"[yellow]⚠ Could not load {filepath} for tagging[/yellow]")
            return
        if audio.tag is None:
            audio.initTag()

        audio.tag.title = title
        audio.tag.artist = artist
        audio.tag.album = album
        audio.tag.track_num = track_number
        normalised = normalise_genre(genre) if genre else None
        audio.tag.genre = normalised
        if year:
            try:
                audio.tag.recording_date = eyed3.core.Date(int(year))
            except Exception:
                pass

        if cover_url:
            try:
                resp = requests.get(cover_url, timeout=10)
                if resp.status_code == 200:
                    audio.tag.images.set(3, resp.content, resp.headers.get("content-type", "image/jpeg"))
            except Exception:
                pass

        audio.tag.save(encoding="utf-8")
    except Exception as exc:
        console.print(f"[yellow]⚠ Tagging warning: {exc}[/yellow]")
