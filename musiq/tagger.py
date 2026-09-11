import logging
from pathlib import Path

import eyed3
import requests
from eyed3.id3 import frames

from musiq.display import console

logging.getLogger("eyed3").setLevel(logging.CRITICAL)

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


def fetch_cover_image(cover_url: str) -> dict | None:
    """Download a cover URL into a dict usable as a rewrite_id3_tags image.

    Returns {'picture_type', 'image_data', 'mime_type', 'description'} or
    None if the image can't be fetched.
    """
    if not cover_url:
        return None
    try:
        resp = requests.get(cover_url, timeout=10)
        if resp.status_code != 200:
            return None
        return {
            "picture_type": 3,
            "image_data": resp.content,
            "mime_type": resp.headers.get("content-type", "image/jpeg"),
            "description": "",
        }
    except Exception:
        return None


def _set_artist_frames(tag, artists) -> None:
    """Write TPE1 as a multi-value field (one frame per artist)."""
    if isinstance(artists, str):
        artists = [artists]
    artists = [a for a in (artists or []) if a]

    tag.frame_set.pop(frames.ARTIST_FID, None)
    for artist in artists or [""]:
        tag.frame_set[frames.ARTIST_FID] = frames.TextFrame(frames.ARTIST_FID, artist)


def tag_file(
    filepath: Path,
    title: str,
    artist: str,
    album: str,
    track_number: int,
    year: str,
    genre: str,
    cover_url: str | None = None,
    artists: list[str] | None = None,
    album_artist: str | None = None,
) -> None:
    try:
        audio = eyed3.load(str(filepath))
        if audio is None:
            console.print(f"[yellow]⚠ Could not load {filepath} for tagging[/yellow]")
            return
        if audio.tag is None:
            audio.initTag()

        audio.tag.title = title
        _set_artist_frames(audio.tag, artists or artist)
        audio.tag.album_artist = album_artist or ""
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
            img = fetch_cover_image(cover_url)
            if img:
                audio.tag.images.set(
                    img["picture_type"],
                    img["image_data"],
                    img["mime_type"],
                    description=img.get("description", ""),
                )

        audio.tag.save(encoding="utf-8")
    except Exception as exc:
        console.print(f"[yellow]⚠ Tagging warning: {exc}[/yellow]")
        raise


def read_id3_tags(filepath: Path) -> dict:
    """Read all ID3 tags from an MP3 file and return them as a dict."""
    audio = eyed3.load(str(filepath))
    if audio is None or audio.tag is None:
        return {}

    tag = audio.tag
    artist_frames = tag.frame_set[frames.ARTIST_FID] or []
    artists = [f.text for f in artist_frames if f.text]
    tags: dict = {
        "title": tag.title or "",
        "artist": tag.artist or "",
        "artists": artists,
        "album_artist": tag.album_artist or "",
        "album": tag.album or "",
        "track_num": tag.track_num[0] if tag.track_num and tag.track_num[0] else 0,
        "disc_num": tag.disc_num[0] if tag.disc_num and tag.disc_num[0] else 0,
        "genre": tag.genre.name if tag.genre else "",
        "year": "",
        "images": [],
    }

    if tag.recording_date:
        tags["year"] = str(tag.recording_date.year)

    for img in tag.images:
        tags["images"].append({
            "description": img.description or "",
            "image_data": img.image_data,
            "mime_type": img.mime_type or "image/jpeg",
            "picture_type": img.picture_type,
        })

    if hasattr(tag, "frame_set") and tag.frame_set:
        for frame_id, frame_list in tag.frame_set.items():
            if frame_id not in ("TIT2", "TPE1", "TPE2", "TALB", "TRCK", "TPOS", "TCON", "TDRC", "APIC"):
                tags.setdefault("other_frames", []).append(frame_id)

    return tags


def rewrite_id3_tags(filepath: Path, tags: dict) -> None:
    """Reapply a previously-read set of ID3 tags to an MP3 file.

    Nukes the entire existing tag first so nothing from a prior
    embed (e.g. yt-dlp metadata) survives, then writes only the
    fields from *tags*.
    """
    try:
        audio = eyed3.load(str(filepath))
        if audio is None:
            console.print(f"[yellow]⚠ Could not load {filepath} for tagging[/yellow]")
            return

        # Wipe the tag completely — drop every frame so leftover
        # metadata from yt-dlp (or anything else) cannot linger.
        if audio.tag is None:
            audio.initTag()
        if audio.tag.frame_set:
            audio.tag.frame_set.clear()

        audio.tag.title = tags.get("title", "")
        _set_artist_frames(audio.tag, tags.get("artists") or tags.get("artist", ""))
        audio.tag.album_artist = tags.get("album_artist", "")
        audio.tag.album = tags.get("album", "")
        audio.tag.track_num = tags.get("track_num", 0)
        disc_num = tags.get("disc_num", 0)
        if disc_num:
            audio.tag.disc_num = disc_num
        genre = tags.get("genre", "")
        audio.tag.genre = genre if genre else None
        year = tags.get("year", "")
        if year:
            try:
                audio.tag.recording_date = eyed3.core.Date(int(year))
            except Exception:
                pass

        for img in tags.get("images", []):
            audio.tag.images.set(
                img["picture_type"],
                img["image_data"],
                img["mime_type"],
                description=img.get("description", ""),
            )

        audio.tag.save(encoding="utf-8")
    except Exception as exc:
        console.print(f"[yellow]⚠ Tag rewrite warning: {exc}[/yellow]")
        raise
