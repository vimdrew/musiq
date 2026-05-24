import re
import shutil
import tempfile
import threading
from pathlib import Path

import yt_dlp

from musiq.display import console, show_track_card
from musiq.history import track_exists, add_download, insert_perf
from musiq.itunes import search as itunes_search
from musiq.perf import Timer, PerfRecord
from musiq.spotify import resolve_entity
from musiq.tagger import tag_file

OUTPUT_BASE = Path.home() / "Downloads" / "Music"
MUSIQ_DIR = Path.home() / ".musiq"

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')
_PARALLEL_LOCK = threading.Lock()


def sanitize(name: str) -> str:
    return _INVALID_CHARS.sub("_", name.strip())


def _join_artists(meta: dict) -> str:
    artists = meta.get("artists") or []
    if artists:
        return ", ".join(artists)
    return meta.get("artist", "Unknown Artist")


def process_track(
    meta: dict,
    *,
    no_itunes: bool = False,
    dry_run: bool = False,
    force: bool = False,
    perf_only: PerfRecord | None = None,
) -> dict:
    track_id = meta.get("id", "")
    title = meta.get("title", "Unknown")
    artist = _join_artists(meta)
    album = meta.get("album")
    track_num = meta.get("track_number", 1) or 1
    duration_ms = meta.get("duration_ms", 0) or 0
    cover_url = meta.get("cover_url")
    duration_sec = duration_ms // 1000

    perf = perf_only or PerfRecord(
        spotify_id=track_id,
        title=title,
        artist=artist,
        source="spotify_embed" if track_id else "fallback",
    )

    if not dry_run and not force and track_id and track_exists(track_id):
        console.print(f"[yellow]⚠ Already downloaded: {title} — skipping[/yellow]")
        perf.status = "skipped"
        insert_perf(perf)
        return {"status": "skipped", "title": title, "artist": artist, "duration": duration_sec}

    if dry_run:
        console.print(f"  [cyan]Would download:[/cyan] {title} — {artist}")
        return {"status": "dry-run", "title": title, "artist": artist, "duration": duration_sec}

    total_timer = Timer()
    total_timer.__enter__()

    from concurrent.futures import ThreadPoolExecutor
    itunes_future = None
    itunes_timer = None

    with ThreadPoolExecutor(max_workers=1) as ex:
        if not no_itunes:
            itunes_timer = Timer()
            itunes_timer.__enter__()
            itunes_future = ex.submit(itunes_search, artist, title)
        else:
            perf.t_itunes_ms = 0

        with Timer() as t_dl:
            try:
                temp_dir = Path(tempfile.mkdtemp(prefix="musiq_"))
                search_query = f"ytsearch1:{title} {artist}"
                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": str(temp_dir / "%(title)s.%(ext)s"),
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "320",
                        }
                    ],
                    "quiet": True,
                    "no_warnings": True,
                }
                show_track_card({
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "year": meta.get("year"),
                    "duration_ms": duration_ms,
                    "cover_url": cover_url,
                })
                downloaded = _ytm_download(search_query, ydl_opts)
                filesize_kb = int(downloaded.stat().st_size / 1024)
            except Exception as exc:
                console.print(f"  [red]✖ Download failed for {title}: {exc}[/red]")
                perf.status = "failed"
                total_timer.__exit__(None, None, None)
                perf.t_total_ms = total_timer.elapsed_ms
                perf.t_download_ms = t_dl.elapsed_ms
                if itunes_timer:
                    itunes_timer.__exit__(None, None, None)
                    perf.t_itunes_ms = itunes_timer.elapsed_ms
                insert_perf(perf)
                return {"status": "failed", "title": title, "artist": artist, "duration": duration_sec}
        perf.t_download_ms = t_dl.elapsed_ms
        perf.filesize_kb = filesize_kb
        perf.bitrate_kbps = 320

        itunes_meta = {}
        if itunes_future is not None:
            itunes_meta = itunes_future.result()
            itunes_timer.__exit__(None, None, None)
            perf.t_itunes_ms = itunes_timer.elapsed_ms

    if album is None and itunes_meta.get("album"):
        album = itunes_meta["album"]
    if album is None:
        album = "Unknown Album"

    if cover_url is None and itunes_meta.get("artwork_url"):
        cover_url = itunes_meta["artwork_url"]
    if cover_url is None:
        cover_url = ""

    spotify_year = meta.get("year")
    year = spotify_year or itunes_meta.get("year", "")
    genre = itunes_meta.get("genre", "")

    album_dir = OUTPUT_BASE / sanitize(artist) / sanitize(album)
    album_dir.mkdir(parents=True, exist_ok=True)
    out_stem = f"{track_num:02d} - {sanitize(artist)} - {sanitize(title)}"
    final_path = album_dir / f"{out_stem}.mp3"
    shutil.move(str(downloaded), str(final_path))
    shutil.rmtree(temp_dir, ignore_errors=True)

    with Timer() as ttag:
        tag_file(
            final_path,
            title=title,
            artist=artist,
            album=album,
            track_number=track_num,
            year=year,
            genre=genre,
            cover_url=cover_url,
        )
    perf.t_tag_ms = ttag.elapsed_ms

    total_timer.__exit__(None, None, None)
    perf.t_total_ms = total_timer.elapsed_ms
    perf.t_spotify_ms = perf.t_spotify_ms or 0
    perf.status = "ok"

    add_download(track_id, title, artist, album, str(final_path))
    insert_perf(perf)

    duration_str = f"{duration_sec // 60}:{duration_sec % 60:02d}" if duration_sec else "--:--"
    console.print(f"  [green]✓ Downloaded:[/green] {title} — {artist} ({duration_str})")

    return {"status": "downloaded", "title": title, "artist": artist, "duration": duration_sec}


def _ytm_download(query: str, ydl_opts: dict) -> Path:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=True)
        entry = info["entries"][0] if "entries" in info else info
        filename = ydl.prepare_filename(entry)
        for ext in [".webm", ".m4a", ".opus", ".ogg", ".aac", ".wav", ".flac"]:
            filename = filename.replace(ext, ".mp3")
        return Path(filename)


def handle_url(
    url: str,
    *,
    no_itunes: bool = False,
    dry_run: bool = False,
    force: bool = False,
    workers: int = 3,
) -> list[dict]:
    MUSIQ_DIR.mkdir(parents=True, exist_ok=True)

    resolved = resolve_entity(url)
    if resolved is None:
        console.print("[yellow]Could not resolve Spotify URL. Falling back to fallback handler...[/yellow]")
        return _fallback_url(url, dry_run=dry_run)

    tracks = resolved if isinstance(resolved, list) else [resolved]
    if not tracks:
        console.print("[red]✖ No tracks found from URL[/red]")
        return []

    if dry_run:
        console.print(f"\n[bold]Would download {len(tracks)} track(s):[/bold]")
        for t in tracks:
            a = _join_artists(t)
            console.print(f"  [cyan]{t.get('title', '?')}[/cyan] — {a}")
        return [{"status": "dry-run", "title": t.get("title", "?"), "artist": _join_artists(t), "duration": 0} for t in tracks]

    if len(tracks) >= 3 and workers > 1:
        return _parallel_process(tracks, no_itunes=no_itunes, dry_run=False, force=force, workers=workers)
    else:
        results = []
        for t in tracks:
            r = process_track(t, no_itunes=no_itunes, dry_run=False, force=force)
            results.append(r)
        return results


def _parallel_process(tracks: list[dict], **kwargs) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from musiq.display import make_progress

    total = len(tracks)
    results_map: dict[int, dict] = {}
    result_lock = threading.Lock()
    track_kwargs = {k: v for k, v in kwargs.items() if k in ("no_itunes", "dry_run", "force", "perf_only")}
    max_workers = kwargs.get("workers", 3)

    with make_progress() as progress:
        task = progress.add_task("[cyan]Downloading tracks...", total=total)

        def worker(idx_track):
            idx, track = idx_track
            try:
                r = process_track(track, **track_kwargs)
            except Exception as exc:
                r = {"status": "failed", "title": track.get("title", "?"), "artist": _join_artists(track), "duration": 0}
            with result_lock:
                results_map[idx] = r
                progress.advance(task)
            return r

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(worker, enumerate(tracks)))

    return [results_map[i] for i in range(total)]


def _fallback_url(url: str, *, dry_run: bool = False) -> list[dict]:
    if dry_run:
        console.print(f"  [cyan]Would download from URL:[/cyan] {url}")
        return [{"status": "dry-run", "title": url, "artist": "?", "duration": 0}]

    import re as _re
    m = _re.search(r"spotify\.com/(track|album|playlist)/([A-Za-z0-9]+)", url)
    if not m:
        console.print("[red]✖ Could not parse Spotify URL[/red]")
        return []

    kind = m.group(1)
    sid = m.group(2)

    if kind in ("album", "playlist"):
        from musiq.spotify import resolve_embed_track_list, resolve_track
        try:
            tracks_data = resolve_embed_track_list(kind, sid)
            if tracks_data:
                results = []
                for entry in tracks_data:
                    t = resolve_track(f"https://open.spotify.com/track/{entry['id']}")
                    if t:
                        t["album"] = entry.get("album_name", "")
                        t["cover_url"] = t.get("cover_url") or entry.get("cover_url", "")
                        t["track_number"] = entry.get("track_number", 0) or 0
                        results.append(process_track(t, no_itunes=False, dry_run=False, force=True))
                    else:
                        artist = entry.get("artist", "")
                        fallback = {
                            "id": entry["id"],
                            "title": entry.get("title", ""),
                            "artist": artist,
                            "artists": [artist] if artist else [],
                            "album": entry.get("album_name", ""),
                            "track_number": entry.get("track_number", 0) or 0,
                            "duration_ms": entry.get("duration_ms", 0) or 0,
                            "cover_url": entry.get("cover_url", ""),
                            "year": "",
                            "release_date": "",
                            "explicit": False,
                        }
                        results.append(process_track(fallback, no_itunes=False, dry_run=False, force=True))
                if results:
                    return results
        except Exception as exc:
            console.print(f"[yellow]⚠ Fallback handler failed: {exc}[/yellow]")

    console.print(f"[red]✖ Could not resolve Spotify URL: {url}[/red]")
    return []


def handle_search_selection(song_meta: dict) -> dict:
    MUSIQ_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": song_meta.get("id", ""),
        "title": song_meta.get("title", ""),
        "artist": song_meta.get("artist", ""),
        "artists": song_meta.get("artists", [song_meta.get("artist", "")]),
        "album": song_meta.get("album", "Unknown"),
        "track_number": 0,
        "duration_ms": song_meta.get("duration_ms", 0),
        "cover_url": song_meta.get("cover_url", ""),
    }
    return process_track(meta, no_itunes=False, dry_run=False, force=False)
