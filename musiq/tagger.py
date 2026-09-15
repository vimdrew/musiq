import io
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


def image_from_path(path: str | Path) -> dict | None:
    """Read a local image file into a dict usable as a rewrite_id3_tags image.

    Returns {'picture_type', 'image_data', 'mime_type', 'description'} or
    None if the file doesn't exist.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return {
        "picture_type": 3,
        "image_data": p.read_bytes(),
        "mime_type": mime,
        "description": "",
    }


def crop_to_square(image_data: bytes) -> bytes:
    """Center-crop an image to a square (shorter dimension) and re-encode JPEG.

    Album art is conventionally 1:1; YouTube thumbnails and screenshots are
    not. Keeps the middle of the frame, drops the excess on the longer axis.
    """
    from PIL import Image

    img = Image.open(io.BytesIO(image_data))
    img = img.convert("RGB")
    w, h = img.size
    s = min(w, h)
    left = (w - s) // 2
    top = (h - s) // 2
    img = img.crop((left, top, left + s, top + s))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()


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
    composer: str | None = None,
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
        audio.tag.composer = composer or ""
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


def _sanitize_tag_text(value: str) -> str:
    """Strip null bytes and other control characters from ID3-text-derived strings.

    ID3v2 text frames separate multiple values with a null byte, and some
    taggers (e.g. iTunes) pack several artists into one TPE1 frame. eyeD3
    surfaces that byte verbatim, which then mangles displays and crashes
    readline-based prompts (click.prompt rejects null bytes). Stripping
    control characters keeps tag-derived text safe to display and edit
    anywhere else in the pipeline too.
    """
    return "".join(ch for ch in str(value or "") if ch >= " " and ch != "\x7f").strip()


def _split_tag_values(text: str) -> list[str]:
    """Split a single ID3 text frame's value into its individual values.

    ID3v2 frames can carry several values in one frame, separated by a null
    byte. Split on the separator, sanitize each part and drop blanks, so a
    TPE1 frame like ``"Drake\\x00Jhené Aiko"`` reads back as two names
    instead of one mangled string.
    """
    values: list[str] = []
    for part in str(text or "").split("\x00"):
        clean = _sanitize_tag_text(part)
        if clean:
            values.append(clean)
    return values


def _frame_values(frame_list) -> list[str]:
    """Flatten one or more ID3 text frames into a sanitized list of values.

    Handles both layouts found in the wild: one frame per value (musiq's own
    writer) and several null-separated values packed into a single frame
    (iTunes and friends).
    """
    values: list[str] = []
    for frame in frame_list or []:
        if getattr(frame, "text", None):
            values.extend(_split_tag_values(frame.text))
    return values


def read_id3_tags(filepath: Path) -> dict:
    """Read all ID3 tags from an MP3 file and return them as a dict."""
    audio = eyed3.load(str(filepath))
    if audio is None or audio.tag is None:
        return {}

    tag = audio.tag
    artists = _frame_values(tag.frame_set.get(frames.ARTIST_FID))
    album_artists = _frame_values(tag.frame_set.get(frames.ALBUM_ARTIST_FID))
    composers = _frame_values(tag.frame_set.get(frames.COMPOSER_FID))
    tags: dict = {
        "title": _sanitize_tag_text(tag.title),
        "artist": ", ".join(artists),
        "artists": artists,
        "album_artist": ", ".join(album_artists),
        "album": _sanitize_tag_text(tag.album),
        "track_num": tag.track_num[0] if tag.track_num and tag.track_num[0] else 0,
        "disc_num": tag.disc_num[0] if tag.disc_num and tag.disc_num[0] else 0,
        "genre": _sanitize_tag_text(tag.genre.name if tag.genre else ""),
        "composer": ", ".join(composers),
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
        audio.tag.composer = tags.get("composer", "")
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
