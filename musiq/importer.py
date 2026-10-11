import re
import shutil
import tempfile
from pathlib import Path

import click
from rich import box
from rich.table import Table

from musiq.config import music_dir
from musiq.display import console
from musiq.downloader import (
    _build_ydl_opts,
    _ytm_download,
    primary_artist,
    resolve_video,
    sanitize,
)
from musiq.history import add_download, get_download, insert_perf
from musiq.perf import Timer, PerfRecord
from musiq.tagger import crop_to_square, fetch_cover_image, image_from_path, rewrite_id3_tags

_BRACKET_RE = re.compile(r"[\[\(].*?[\]\)]")


def parse_title(title: str, uploader: str) -> tuple[str, str]:
    """Guess the (artist, song) from a raw YouTube title.

    Strips bracketed/parenthetical annotations (e.g. "[OFFICIAL LEAK]",
    "(Official Video)") and splits on ' - '. Falls back to the uploader
    for the artist and the cleaned title for the song.
    """
    cleaned = " ".join(_BRACKET_RE.sub(" ", title or "").split())
    candidate_artist = ""
    candidate_song = ""
    if " - " in cleaned:
        left, _, right = cleaned.partition(" - ")
        left = left.strip()
        right = right.strip()
        if left and right and len(right) <= 60:
            candidate_artist, candidate_song = left, right
    if not candidate_artist:
        u = (uploader or "").strip()
        if u.lower().endswith(" - topic"):
            u = u[: -len(" - Topic")]
        candidate_artist = u.strip()
    if not candidate_song:
        candidate_song = cleaned or (title or "").strip()
    return candidate_artist, candidate_song


def _prompt_year(default: str) -> str:
    while True:
        raw = click.prompt("Year", default=default or "", type=str, show_default=False).strip()
        if not raw:
            return ""
        if raw.isdigit():
            return str(int(raw))
        console.print("[yellow]⚠ Invalid year — enter a number or press Enter to skip[/yellow]")


def prompt_metadata(info: dict, current: dict | None = None) -> dict:
    """Interactively collect ID3 metadata, using the current values as defaults."""
    guess_artist, guess_song = parse_title(info.get("title", ""), info.get("uploader", ""))
    upload_year = (info.get("upload_date") or "")[:4] or ""

    cur = current or {}
    artist = click.prompt("Artist", default=cur.get("artist") or guess_artist, type=str)
    title = click.prompt("Title", default=cur.get("title") or guess_song, type=str)
    album = click.prompt("Album", default=cur.get("album") or title, type=str)
    album_artist = click.prompt("Album artist", default=cur.get("album_artist") or artist, type=str)
    track_num = click.prompt(
        "Track number",
        default=cur.get("track_num") or 1,
        type=click.IntRange(1, 99),
    )
    disc_num = click.prompt(
        "Disc number",
        default=cur.get("disc_num") or 0,
        type=click.IntRange(0, 99),
    )
    year = _prompt_year(cur.get("year") or upload_year)
    genre = click.prompt("Genre", default=cur.get("genre") or "", type=str, show_default=False).strip()
    # Composer intentionally defaults blank: a YouTube uploader is frequently
    # not the composer (leak aggregator, fan reupload...) so we must not seed
    # confidently-wrong values a user might click past.
    composer = click.prompt("Composer", default=cur.get("composer") or "", type=str, show_default=False).strip()

    return {
        "title": title.strip(),
        "artist": artist.strip(),
        "artists": [artist.strip()],
        "album": album.strip(),
        "album_artist": album_artist.strip(),
        "track_num": track_num,
        "disc_num": disc_num,
        "year": year,
        "genre": genre,
        "composer": composer,
    }


def prompt_artwork(info: dict) -> dict | None:
    """Let the user pick cover art and return an image dict (or None to skip)."""
    choice = click.prompt(
        "Album art",
        type=click.Choice(["thumbnail", "file", "none"], case_sensitive=False),
        default="thumbnail",
        show_choices=True,
    ).lower()

    if choice == "none":
        return None

    if choice == "thumbnail":
        thumb = info.get("thumbnail") or ""
        if not thumb:
            console.print("[yellow]⚠ No thumbnail available for this video[/yellow]")
            return None
        raw = fetch_cover_image(thumb)
        if raw is None:
            console.print("[yellow]⚠ Could not fetch thumbnail[/yellow]")
            return None
        data = crop_to_square(raw["image_data"])
        return {
            "picture_type": 3,
            "image_data": data,
            "mime_type": "image/jpeg",
            "description": "",
            "label": "YouTube thumbnail",
        }

    while True:
        raw_path = click.prompt("Path to image", type=str, show_default=False).strip()
        if not raw_path:
            return None
        path = Path(raw_path).expanduser()
        img = image_from_path(path) if path.is_file() else None
        if img is not None:
            data = crop_to_square(img["image_data"])
            return {
                "picture_type": 3,
                "image_data": data,
                "mime_type": "image/jpeg",
                "description": "",
                "label": str(path),
            }
        console.print(f"[red]✖ Not a readable image file: {path}[/red]")


def metadata_table(info: dict, meta: dict, artwork: dict | None) -> Table:
    table = Table(box=box.ROUNDED, title="Confirm import metadata")
    table.add_column("Field", style="bold cyan")
    table.add_column("Value")

    track = f"{meta['track_num']:02d}" if meta.get("track_num") else "—"
    disc = str(meta.get("disc_num")) if meta.get("disc_num") else "—"
    rows = [
        ("Source", f"https://www.youtube.com/watch?v={info.get('video_id', '')}"),
        ("Title", meta["title"] or "—"),
        ("Artist", meta["artist"] or "—"),
        ("Album", meta["album"] or "—"),
        ("Album artist", meta["album_artist"] or "—"),
        ("Track", track),
        ("Disc", disc),
        ("Year", meta.get("year") or "—"),
        ("Genre", meta.get("genre") or "—"),
        ("Composer", meta.get("composer") or "—"),
        ("Album art", artwork["label"] if artwork else "none"),
    ]
    for field, value in rows:
        table.add_row(field, str(value))
    return table


def _confirm_loop(info: dict, meta: dict, artwork: dict | None) -> tuple[dict, dict | None] | None:
    """Show the metadata table and let the user confirm, edit, or abort.

    Returns (meta, artwork) when confirmed, None when cancelled.
    """
    while True:
        console.print(metadata_table(info, meta, artwork))
        choice = click.prompt(
            "Import as shown?",
            type=click.Choice(["yes", "edit", "artwork", "no"], case_sensitive=False),
            default="yes",
            show_choices=True,
        ).lower()
        if choice == "yes":
            return meta, artwork
        if choice == "no":
            return None
        if choice == "edit":
            meta = prompt_metadata(info, current=meta)
        else:
            artwork = prompt_artwork(info)


def import_youtube(url: str) -> dict:
    """Import a single new track from a YouTube URL.

    Order matters: metadata is collected and confirmed first, then the
    destination path is resolved and checked (history.db by video_id,
    then the filesystem), and only then is any audio downloaded — so a
    re-import or collision never burns a download.
    """
    console.print(f"[bold]Resolving:[/bold] {url}")
    info = resolve_video(url)
    if info is None or not info.get("video_id"):
        return {"status": "failed", "title": url, "artist": "?", "message": "Could not resolve video"}
    video_id = info["video_id"]

    meta = prompt_metadata(info)
    artwork = prompt_artwork(info)

    confirmed = _confirm_loop(info, meta, artwork)
    if confirmed is None:
        console.print("[yellow]Cancelled[/yellow]")
        return {"status": "skipped", "title": meta["title"], "artist": meta["artist"], "message": "Cancelled by user"}

    meta, artwork = confirmed
    title = meta["title"] or "Unknown"
    artist = meta["artist"] or "Unknown Artist"
    album = meta["album"] or title
    album_artist = meta["album_artist"] or artist

    # ── Destination resolution (no download needed) ──────────────
    album_dir = music_dir() / sanitize(primary_artist(album_artist)) / sanitize(album)
    out_stem = f"{meta['track_num']:02d} - {sanitize(artist)} - {sanitize(title)}"
    final_path = album_dir / f"{out_stem}.mp3"

    # History.db is the authoritative duplicate signal — a corrected artist
    # name changes the file path but the video is still the same import.
    existing = get_download(video_id)
    file_exists = final_path.is_file()

    if existing and file_exists:
        console.print(
            f"[yellow]⚠ Already imported:[/yellow] {existing['title']} — {existing['artist']}\n"
            f"  [dim]File also exists at: {final_path}[/dim]"
        )
        if not click.confirm("Re-import and overwrite?", default=False):
            console.print("[yellow]Skipped — already in history[/yellow]")
            return {"status": "skipped", "title": title, "artist": artist, "message": "Already in history"}
    elif existing:
        console.print(f"[yellow]⚠ Already imported:[/yellow] {existing['title']} — {existing['artist']}")
        if not click.confirm("Re-import?", default=False):
            console.print("[yellow]Skipped — already in history[/yellow]")
            return {"status": "skipped", "title": title, "artist": artist, "message": "Already in history"}
    elif file_exists:
        console.print(f"[yellow]⚠ File exists at destination:[/yellow] {final_path}")
        if not click.confirm("Different track? Overwrite?", default=False):
            console.print("[yellow]Skipped — file exists[/yellow]")
            return {"status": "skipped", "title": title, "artist": artist, "message": "File exists"}

    # ── Download + convert (first actual network work) ───────────
    console.print(f"[bold]Downloading:[/bold] {title} — {artist}")
    album_dir.mkdir(parents=True, exist_ok=True)

    temp_dir = Path(tempfile.mkdtemp(prefix="musiq_import_"))
    perf = PerfRecord(spotify_id=video_id, title=title, artist=artist, source="youtube")
    try:
        with Timer() as total_timer:
            with Timer() as t_dl:
                try:
                    downloaded = _ytm_download(url, _build_ydl_opts(temp_dir))
                except Exception as exc:
                    console.print(f"[red]✖ Download failed: {exc}[/red]")
                    return {"status": "failed", "title": title, "artist": artist, "message": str(exc)}
                t_dl.stop()
                perf.t_download_ms = t_dl.elapsed_ms
                perf.filesize_kb = int(downloaded.stat().st_size / 1024)
                perf.bitrate_kbps = 320

            shutil.copy2(str(downloaded), str(final_path))

            with Timer() as t_tag:
                tags = dict(meta)
                tags["images"] = [artwork] if artwork else []
                rewrite_id3_tags(final_path, tags)
                t_tag.stop()
                perf.t_tag_ms = t_tag.elapsed_ms

            perf.t_total_ms = total_timer.elapsed_ms
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    add_download(video_id, title, artist, album, str(final_path))
    perf.status = "ok"
    insert_perf(perf)

    console.print(f"[green]✓ Imported:[/green] {title} — {artist}")
    console.print(f"  [dim]{final_path}[/dim]")
    artist_label = artist
    album_label = album
    genre = meta.get("genre") or "—"
    composer = meta.get("composer") or "—"
    console.print(
        f"  [dim]Tags — {title} / {artist_label} / {album_label} · {genre} · "
        f"composer: {composer}[/dim]"
    )
    return {"status": "downloaded", "title": title, "artist": artist, "path": str(final_path)}