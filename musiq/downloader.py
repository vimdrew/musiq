import re
import shutil
import tempfile
import threading
from pathlib import Path

import yt_dlp

from musiq.display import console
from musiq.history import track_exists, add_download, insert_perf
from musiq.itunes import fetch_metadata as itunes_search
from musiq.perf import Timer, PerfRecord
from musiq.spotify import resolve_entity
from musiq.tagger import tag_file
from musiq.ytmusic import search_song

OUTPUT_BASE = Path.home() / "Music"
MUSIQ_DIR = Path.home() / ".musiq"

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')
_PARALLEL_LOCK = threading.Lock()
_CONFIDENCE_THRESHOLD = 0.6


def sanitize(name: str) -> str:
    """Make a name safe for use in a folder/file path.

    Keeps the original casing so paths use the exact artist/album name
    provided by Spotify (its canonical casing) rather than a lowercased
    derivation from scraped text.
    """
    return _INVALID_CHARS.sub("_", name.strip())


def primary_artist(artist: str) -> str:
    return artist.split(",")[0].strip()


def _folder_artist(meta: dict, artist: str) -> str:
    """Artist used for the output folder.

    For album downloads the primary artist comes from the Spotify album
    object (album_artist) so every track lands in the same folder. Other
    sources fall back to the track's own primary artist.
    """
    if meta.get("album_artist"):
        return primary_artist(meta["album_artist"])
    return primary_artist(artist)


def _join_artists(meta: dict) -> str:
    artists = meta.get("artists") or []
    if artists:
        return ", ".join(artists)
    return meta.get("artist", "Unknown Artist")


def _do_download(
    dl_url: str,
    ydl_opts: dict,
    title: str,
    artist: str,
    duration_sec: int,
    perf: PerfRecord,
) -> tuple[Path, int] | None:
    dur_str = f"{duration_sec // 60}:{duration_sec % 60:02d}" if duration_sec else "--:--"
    console.print(f"[bold blue]⬇[/bold blue] [bold white]{title}[/bold white] — [cyan]{artist}[/cyan] [dim]({dur_str})[/dim]")
    with Timer() as t_dl:
        try:
            downloaded = _ytm_download(dl_url, ydl_opts)
            filesize_kb = int(downloaded.stat().st_size / 1024)
        except Exception as exc:
            console.print(f"  [red]✖ Download failed for {title}: {exc}[/red]")
            t_dl.stop()
            perf.t_download_ms = t_dl.elapsed_ms
            perf.status = "failed"
            return None
        t_dl.stop()
        perf.t_download_ms = t_dl.elapsed_ms
        perf.filesize_kb = filesize_kb
        perf.bitrate_kbps = 320
        return downloaded, filesize_kb


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

    with Timer() as total_timer:
        with Timer() as t_search:
            video_url = None
            if title != "Unknown":
                yt_result = search_song(title, artist)
                if yt_result:
                    video_url = f"https://www.youtube.com/watch?v={yt_result['video_id']}"
        perf.t_search_ms = t_search.elapsed_ms

        itunes_meta = meta.get("_itunes_cache", {})
        itunes_cached = bool(itunes_meta)

        temp_dir = Path(tempfile.mkdtemp(prefix="musiq_"))
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
        dl_url = video_url or f"ytsearch1:{title} {artist}"

        if not itunes_cached and not no_itunes:
            from concurrent.futures import ThreadPoolExecutor
            with Timer() as itunes_timer:
                with ThreadPoolExecutor(max_workers=1) as ex:
                    itunes_future = ex.submit(itunes_search, artist, title)
                    dl_result = _do_download(dl_url, ydl_opts, title, artist, duration_sec, perf)
                    if dl_result is None:
                        total_timer.stop()
                        perf.t_total_ms = total_timer.elapsed_ms
                        itunes_timer.stop()
                        perf.t_itunes_ms = itunes_timer.elapsed_ms
                        insert_perf(perf)
                        shutil.rmtree(temp_dir, ignore_errors=True)
                        return {"status": "failed", "title": title, "artist": artist, "duration": duration_sec}
                    downloaded, filesize_kb = dl_result
                itunes_meta = itunes_future.result()
            perf.t_itunes_ms = itunes_timer.elapsed_ms
        else:
            perf.t_itunes_ms = 0
            dl_result = _do_download(dl_url, ydl_opts, title, artist, duration_sec, perf)
            if dl_result is None:
                total_timer.stop()
                perf.t_total_ms = total_timer.elapsed_ms
                insert_perf(perf)
                shutil.rmtree(temp_dir, ignore_errors=True)
                return {"status": "failed", "title": title, "artist": artist, "duration": duration_sec}
            downloaded, filesize_kb = dl_result

        album = meta.get("album") or f"{title} - Single"

        if cover_url is None and itunes_meta.get("artwork_url"):
            cover_url = itunes_meta["artwork_url"]
        if cover_url is None:
            cover_url = ""

        spotify_year = meta.get("year")
        confidence = itunes_meta.get("confidence", 0.0)
        if confidence >= _CONFIDENCE_THRESHOLD:
            genre = itunes_meta.get("genre", "")
            year = spotify_year or itunes_meta.get("year", "")
        else:
            genre = meta.get("resolved_genre") or itunes_meta.get("genre", "")
            year = spotify_year or meta.get("resolved_year") or itunes_meta.get("year", "")

        album_dir = OUTPUT_BASE / sanitize(_folder_artist(meta, artist)) / sanitize(album)
        album_dir.mkdir(parents=True, exist_ok=True)
        out_stem = f"{track_num:02d} - {sanitize(artist)} - {sanitize(title)}"
        final_path = album_dir / f"{out_stem}.mp3"

        if final_path.exists() and not force:
            console.print(f"[yellow]⚠ File exists at destination, skipping: {title}[/yellow]")
            downloaded.unlink()
            shutil.rmtree(temp_dir, ignore_errors=True)
            total_timer.stop()
            perf.t_total_ms = total_timer.elapsed_ms
            perf.status = "skipped"
            insert_perf(perf)
            return {"status": "skipped", "title": title, "artist": artist, "duration": duration_sec}

        shutil.copy2(str(downloaded), str(final_path))
        downloaded.unlink()
        shutil.rmtree(temp_dir, ignore_errors=True)

        with Timer() as ttag:
            try:
                tag_file(
                    final_path,
                    title=title,
                    artist=primary_artist(artist),
                    album=album,
                    track_number=track_num,
                    year=year,
                    genre=genre,
                    cover_url=cover_url,
                    artists=meta.get("artists") or None,
                    album_artist=_folder_artist(meta, artist),
                )
            except Exception:
                console.print(f"  [yellow]⚠ Tags not written for {title}[/yellow]")
        perf.t_tag_ms = ttag.elapsed_ms

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
        path = Path(filename)
        # prepare_filename returns pre-post-processor path (e.g. .webm).
        # After FFmpegExtractAudio, the .mp3 is the real output.
        audio_exts = {".webm", ".m4a", ".opus", ".ogg", ".aac", ".wav", ".flac"}
        if path.suffix in audio_exts:
            return path.with_suffix(".mp3")
        # Fallback: glob .mp3s in the temp dir.
        # Assumes temp_dir is freshly created (mkdtemp above) so no stale files match.
        temp_dir = path.parent
        mp3_files = sorted(temp_dir.glob("*.mp3"))
        if mp3_files:
            return mp3_files[0]
        return path.with_suffix(".mp3")


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
        return _fallback_url(url, dry_run=dry_run, no_itunes=no_itunes, force=force)

    tracks = resolved if isinstance(resolved, list) else [resolved]
    if not tracks:
        console.print("[red]✖ No tracks found from URL[/red]")
        return []

    if not no_itunes and len(tracks) > 1 and tracks[0].get("_source_type") == "album":
        itunes_cache: dict[str, dict] = {}
        genre_votes: dict[str, int] = {}
        year_votes: dict[str, int] = {}
        console.print("[dim]Fetching iTunes metadata (genre/year enrichment)...[/dim]")
        for track in tracks:
            meta = itunes_search(
                _join_artists(track),
                track.get("title", ""),
            )
            if meta:
                sid = track.get("id", "")
                if sid:
                    itunes_cache[sid] = meta
                confidence = meta.get("confidence", 0.0)
                if confidence >= _CONFIDENCE_THRESHOLD:
                    if meta.get("genre"):
                        genre_votes[meta["genre"]] = genre_votes.get(meta["genre"], 0) + 1
                    if meta.get("year"):
                        year_votes[str(meta["year"])] = year_votes.get(str(meta["year"]), 0) + 1

        canonical_genre = max(genre_votes, key=genre_votes.get) if genre_votes else ""
        canonical_year = max(year_votes, key=year_votes.get) if year_votes else ""

        for track in tracks:
            track["resolved_genre"] = canonical_genre
            track["resolved_year"] = canonical_year
            track["_itunes_cache"] = itunes_cache.get(track.get("id", ""), {})

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
    from concurrent.futures import ThreadPoolExecutor
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


def _fallback_url(url: str, *, dry_run: bool = False, no_itunes: bool = False, force: bool = False) -> list[dict]:
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
                        t["album"] = entry.get("album") or t.get("album") or ""
                        if entry.get("album_artist"):
                            t["album_artist"] = entry["album_artist"]
                        t["cover_url"] = t.get("cover_url") or entry.get("cover_url", "")
                        t["track_number"] = entry.get("track_number", 0) or 0
                        t["_source_type"] = kind
                        results.append(process_track(t, no_itunes=no_itunes, dry_run=False, force=force))
                    else:
                        artist = entry.get("artist", "")
                        fallback = {
                            "id": entry["id"],
                            "title": entry.get("title", ""),
                            "artist": artist,
                            "artists": [artist] if artist else [],
                            "album": entry.get("album", ""),
                            "album_artist": entry.get("album_artist", ""),
                            "track_number": entry.get("track_number", 0) or 0,
                            "duration_ms": entry.get("duration_ms", 0) or 0,
                            "cover_url": entry.get("cover_url", ""),
                            "year": "",
                            "release_date": "",
                            "explicit": False,
                            "_source_type": kind,
                        }
                        results.append(process_track(fallback, no_itunes=no_itunes, dry_run=False, force=force))
                if results:
                    return results
        except Exception as exc:
            console.print(f"[yellow]⚠ Fallback handler failed: {exc}[/yellow]")

    console.print(f"[red]✖ Could not resolve Spotify URL: {url}[/red]")
    return []


def handle_search_selection(
    song_meta: dict,
    *,
    no_itunes: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    MUSIQ_DIR.mkdir(parents=True, exist_ok=True)
    from musiq.spotify import search_track_on_spotify, _spotify_client, _api_to_track_dict
    spotify_id = search_track_on_spotify(
        song_meta.get("title", ""),
        song_meta.get("artist", ""),
    )
    meta = {
        "id": spotify_id or "",
        "title": song_meta.get("title", ""),
        "artist": song_meta.get("artist", ""),
        "artists": song_meta.get("artists", [song_meta.get("artist", "")]),
        "album": song_meta.get("album", "Unknown"),
        "track_number": 0,
        "duration_ms": song_meta.get("duration_ms", 0),
        "cover_url": song_meta.get("cover_url", ""),
    }
    if spotify_id:
        track_data = _spotify_client.get_track(spotify_id)
        if track_data:
            meta = _api_to_track_dict(track_data, spotify_id)
    return process_track(meta, no_itunes=no_itunes, dry_run=dry_run, force=force)
