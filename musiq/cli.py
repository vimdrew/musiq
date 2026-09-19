import difflib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import click
from rich import box
from rich.markup import escape
from rich.table import Table

from musiq.display import console, summary_table, stats_table
from musiq.deezer import fetch_track_metadata as deezer_meta
from musiq.downloader import (
    handle_url, handle_search_selection, process_track, _ytm_download,
    _build_ydl_opts,
)
from musiq.duplicates import dedupe
from musiq.history import get_all_downloads, clear_downloads, get_stats, get_file_path
from musiq.itunes import search_itunes, fetch_metadata
from musiq.lastfm import fetch_track_info
from musiq.musicbrainz import recording_metadata
from musiq.tagger import (
    read_id3_tags, rewrite_id3_tags, fetch_cover_image, normalise_genre,
    _sanitize_tag_text, _split_multi_value,
)
from musiq import navidrome
from musiq.spotify import resolve_playlist_tracks


def _prompt(*args, **kwargs):
    """Defense-in-depth wrapper around ``click.prompt``.

    Scrubs null bytes and other control characters from the *default* string
    before passing it through.  ``read_id3_tags`` already sanitizes tag text
    at the source, but the scrub here acts as a safety net: if a value
    somehow slips past (or reaches here from a path-derived name), the prompt
    will never crash readline with ``ValueError("prompt string cannot contain
    null characters")``.
    """
    default = kwargs.get("default")
    if isinstance(default, str):
        kwargs["default"] = _sanitize_tag_text(default)
    return click.prompt(*args, **kwargs)


@click.group()
def cli():
    pass


@cli.command()
@click.argument("url")
@click.option("--no-itunes", is_flag=True, help="Skip iTunes metadata lookup")
@click.option("--no-deezer", is_flag=True, help="Skip Deezer metadata lookup")
@click.option("--dry-run", is_flag=True, help="Show what would download without fetching")
@click.option("--force", is_flag=True, help="Re-download even if already in history")
@click.option("--workers", default=3, type=click.IntRange(1, 5), help="Parallel downloads for playlists (1-5)")
def download(url: str, no_itunes: bool, no_deezer: bool, dry_run: bool, force: bool, workers: int):
    """Download a track, album, or playlist from a Spotify URL."""
    results = handle_url(url, no_itunes=no_itunes, no_deezer=no_deezer, dry_run=dry_run, force=force, workers=workers)

    if not results:
        return

    d = sum(1 for r in results if r.get("status") == "downloaded")
    s = sum(1 for r in results if r.get("status") == "skipped")
    f = sum(1 for r in results if r.get("status") == "failed")
    dr = sum(1 for r in results if r.get("status") == "dry-run")

    if dry_run:
        console.print(f"\n[bold]Dry-run complete:[/bold] {len(results)} track(s)")
        return

    console.print(
        f"\n[bold]Summary:[/bold] [green]{d} downloaded[/green], "
        f"[yellow]{s} skipped[/yellow], [red]{f} failed[/red]"
    )
    if d + s + f > 0:
        console.print(summary_table(results))


@cli.command("playlist")
@click.argument("url")
@click.option("--no-itunes", is_flag=True, help="Skip iTunes metadata lookup")
@click.option("--no-deezer", is_flag=True, help="Skip Deezer metadata lookup")
@click.option("--dry-run", is_flag=True, help="Show what would download without fetching")
@click.option("--force", is_flag=True, help="Re-download even if already in history")
def playlist(url: str, no_itunes: bool, no_deezer: bool, dry_run: bool, force: bool):
    """Download every track in a Spotify playlist and create a matching Navidrome playlist."""
    from musiq.spotify import extract_id_and_type

    kind, sid = extract_id_and_type(url)
    if kind != "playlist":
        console.print(f"[red]✖ Expected a playlist URL, got {kind}[/red]")
        raise SystemExit(1)

    console.print(f"[bold]Fetching playlist:[/bold] {url}")
    try:
        playlist_info = resolve_playlist_tracks(url)
    except Exception as exc:
        console.print(f"[red]✖ Could not fetch playlist: {exc}[/red]")
        raise SystemExit(1)

    if playlist_info is None:
        console.print("[red]✖ Could not fetch playlist or it is empty[/red]")
        raise SystemExit(1)

    playlist_name, tracks = playlist_info
    console.print(f"[dim]'{playlist_name}' — {len(tracks)} track(s)[/dim]")

    if dry_run:
        console.print(f"\n[bold]Would download {len(tracks)} track(s):[/bold]")
        for t in tracks:
            a = ", ".join(t.get("artists", [])) or t.get("artist", "?")
            console.print(f"  [cyan]{t.get('title', '?')}[/cyan] — {a}")
        return

    results: list[dict] = []
    for i, track in enumerate(tracks, 1):
        console.print(f"\n[bold]({i}/{len(tracks)})[/bold]", end=" ")
        r = process_track(track, no_itunes=no_itunes, no_deezer=no_deezer, dry_run=False, force=force)
        results.append(r)

    d = sum(1 for r in results if r.get("status") == "downloaded")
    s = sum(1 for r in results if r.get("status") == "skipped")
    f = sum(1 for r in results if r.get("status") == "failed")

    # Tracks that are present in the library (downloaded now or already present)
    # are eligible for the Navidrome playlist. Failed tracks are excluded.
    eligible = [
        (track, r)
        for track, r in zip(tracks, results)
        if r.get("status") in ("downloaded", "skipped")
    ]
    failed_tracks = [
        (track, r)
        for track, r in zip(tracks, results)
        if r.get("status") == "failed"
    ]

    if d + s + f > 0:
        console.print(
            f"\n[bold]Downloads:[/bold] [green]{d} downloaded[/green], "
            f"[yellow]{s} skipped[/yellow], [red]{f} failed[/red]"
        )
        console.print(summary_table(results))

    # ── Navidrome playlist ────────────────────────────────────────
    nd = navidrome.ensure_navidrome_config()
    if nd is None:
        console.print("[yellow]Skipping Navidrome playlist — no config[/yellow]")
        return
    base_url, user, password = nd

    if not eligible:
        console.print("[yellow]No tracks to add to Navidrome playlist — skipping[/yellow]")
        return

    console.print("[bold]Scanning Navidrome library...[/bold]")
    if not navidrome.start_scan(base_url, user, password):
        console.print("[yellow]⚠ Skipping Navidrome playlist — scan could not start[/yellow]")
        return
    navidrome.wait_for_scan(base_url, user, password)

    matched_ids: list[str] = []
    unmatched: list[str] = []
    for track, _r in eligible:
        title = track.get("title", "")
        artist = ", ".join(track.get("artists", [])) or track.get("artist", "")
        query = f"{title} {artist}".strip()
        if not query:
            unmatched.append(f"{title} (no metadata)")
            continue
        hits = navidrome.search3(base_url, user, password, query)
        song_id = _best_song_match(hits, title, artist)
        if song_id:
            matched_ids.append(song_id)
        else:
            unmatched.append(f"{title} — {artist}")
            console.print(f"  [yellow]⚠ Not matched in Navidrome:[/yellow] {title} — {artist}")

    if not matched_ids:
        console.print("[yellow]No tracks matched in Navidrome — playlist not created[/yellow]")
        return

    existing_id = _find_playlist_by_name(base_url, user, password, playlist_name)
    if existing_id:
        console.print(f"[bold]Updating existing playlist:[/bold] {playlist_name}")
        if navidrome.update_playlist(base_url, user, password, existing_id, matched_ids):
            console.print(f"[green]✓ Playlist updated:[/green] {playlist_name} ({len(matched_ids)} tracks)")
        else:
            console.print(f"[red]✖ Failed to update playlist[/red]")
    else:
        console.print(f"[bold]Creating playlist:[/bold] {playlist_name}")
        pl_id = navidrome.create_playlist(base_url, user, password, playlist_name, matched_ids)
        if pl_id:
            console.print(f"[green]✓ Playlist created:[/green] {playlist_name} ({len(matched_ids)} tracks)")
        else:
            console.print(f"[red]✖ Failed to create playlist[/red]")

    if unmatched:
        console.print(f"[yellow]Skipped {len(unmatched)} track(s) not matched in Navidrome:[/yellow]")
        for name in unmatched:
            console.print(f"  [dim]• {name}[/dim]")
    if failed_tracks:
        console.print(f"[yellow]{len(failed_tracks)} track(s) failed to download and were not added:[/yellow]")
        for track, _r in failed_tracks:
            console.print(f"  [dim]• {track.get('title', '?')}[/dim]")


@cli.command("retag-playlist")
@click.argument("url")
def retag_playlist(url: str):
    """Re-apply corrected metadata (cover art, year) to tracks already
    downloaded from a Spotify playlist, matching them via history.db."""
    from musiq.spotify import extract_id_and_type

    kind, sid = extract_id_and_type(url)
    if kind != "playlist":
        console.print(f"[red]✖ Expected a playlist URL, got {kind}[/red]")
        raise SystemExit(1)

    console.print(f"[bold]Fetching playlist:[/bold] {url}")
    try:
        playlist_info = resolve_playlist_tracks(url)
    except Exception as exc:
        console.print(f"[red]✖ Could not fetch playlist: {exc}[/red]")
        raise SystemExit(1)

    if playlist_info is None:
        console.print("[red]✖ Could not fetch playlist[/red]")
        raise SystemExit(1)

    playlist_name, tracks = playlist_info
    console.print(f"[dim]'{playlist_name}' — {len(tracks)} track(s)[/dim]")

    retagged: list[str] = []
    skipped_missing: list[str] = []
    no_history: list[str] = []

    for track in tracks:
        sid = track.get("id", "")
        title = track.get("title", "?")
        artist = ", ".join(track.get("artists", [])) or track.get("artist", "")
        label = f"{title} — {artist}"

        file_path = get_file_path(sid) if sid else None
        if not file_path:
            no_history.append(label)
            continue

        path = Path(file_path)
        if not path.is_file():
            skipped_missing.append(label)
            continue

        tags = read_id3_tags(path)
        if not tags:
            skipped_missing.append(f"{label} (no ID3 tags)")
            continue

        year = track.get("year")
        if year:
            tags["year"] = str(year)
        if track.get("album_artist"):
            tags["album_artist"] = track["album_artist"]
        if track.get("artist"):
            tags.setdefault("artist", track["artist"])
        if track.get("artists"):
            tags["artists"] = track["artists"]
        if track.get("album"):
            tags["album"] = track["album"]

        cover_url = track.get("cover_url")
        if cover_url:
            img = fetch_cover_image(cover_url)
            if img:
                tags["images"] = [img]

        try:
            rewrite_id3_tags(path, tags)
        except Exception as exc:
            console.print(f"  [red]✖ Failed to retag {label}: {exc}[/red]")
            continue

        retagged.append(label)
        console.print(f"  [green]✓ Retagged:[/green] {label}")

    console.print(
        f"\n[bold]Summary:[/bold] [green]{len(retagged)} retagged[/green], "
        f"[yellow]{len(skipped_missing)} skipped (file missing)[/yellow], "
        f"[red]{len(no_history)} no history entry[/red]"
    )


def _find_playlist_by_name(base_url: str, user: str, password: str, name: str) -> str | None:
    """Return the ID of an existing Navidrome playlist matching *name*, or None."""
    playlists = navidrome.get_playlists(base_url, user, password)
    for pl in playlists:
        if pl.get("name", "").lower() == name.lower():
            return pl.get("id")
    return None


def _best_song_match(hits: list[dict], title: str, artist: str) -> str | None:
    """Pick the best Navidrome song ID from search hits. Returns None if nothing is close enough."""
    if not hits:
        return None

    title_l = title.lower().strip()
    artist_l = artist.lower().strip()

    def _score(hit: dict) -> int:
        score = 0
        h_title = (hit.get("title") or "").lower().strip()
        h_artist = (hit.get("artist") or hit.get("albumArtist") or "").lower().strip()
        if h_title == title_l:
            score += 10
        elif title_l in h_title or h_title in title_l:
            score += 5
        if h_artist and artist_l:
            if h_artist == artist_l:
                score += 10
            elif artist_l in h_artist or h_artist in artist_l:
                score += 5
            for part in artist_l.split(","):
                part = part.strip()
                if part and part in h_artist:
                    score += 3
        return score

    best = max(hits, key=_score)
    if _score(best) >= 10:
        return best.get("id")
    return None


@cli.command()
@click.argument("query", nargs=-1, required=True)
@click.option("--no-itunes", is_flag=True, help="Skip iTunes metadata lookup")
@click.option("--no-deezer", is_flag=True, help="Skip Deezer metadata lookup")
@click.option("--dry-run", is_flag=True, help="Show what would download without fetching")
@click.option("--force", is_flag=True, help="Re-download even if already in history")
def search(query: tuple[str, ...], no_itunes: bool, no_deezer: bool, dry_run: bool, force: bool):
    """Search and download your selection."""
    q = " ".join(query)
    console.print(f"[bold]Searching for:[/bold] {q}")

    try:
        results = search_itunes(q)
    except Exception as exc:
        console.print(f"[red]Search failed: {exc}[/red]")
        console.print("[yellow]Tip: iTunes Search API may be rate-limited. Try again later.[/yellow]")
        return

    if not results:
        console.print("[red]No results found[/red]")
        return

    table = Table(box=None)
    table.add_column("#", style="dim")
    table.add_column("Title", style="cyan")
    table.add_column("Artist", style="magenta")
    table.add_column("Album", style="white")
    for i, r in enumerate(results[:10], 1):
        d = r.get("duration_ms", 0) or 0
        dur = f"{d // 60000}:{(d % 60000) // 1000:02d}"
        table.add_row(str(i), r.get("title", "?"), r.get("artist", "?"), r.get("album", "?"))
    console.print(table)

    choice = click.prompt(
        "Select a track to download",
        type=click.IntRange(1, min(len(results), 10)),
    )
    selected = results[choice - 1]
    console.print(f"\n[bold]Downloading:[/bold] {selected.get('title', '?')} — {selected.get('artist', '?')}")

    result = handle_search_selection(selected, no_itunes=no_itunes, no_deezer=no_deezer, dry_run=dry_run, force=force)
    if result.get("status") == "downloaded":
        console.print(f"[green]✓ Downloaded {selected.get('title')}[/green]")
    elif result.get("status") == "skipped":
        console.print("[yellow]⚠ Already in history[/yellow]")
    elif result.get("status") == "dry-run":
        console.print(f"[cyan]Would download: {selected.get('title')}[/cyan]")
    else:
        console.print(f"[red]✖ Failed: {selected.get('title')}[/red]")


@cli.command()
@click.option("--clear", "clear_flag", is_flag=True, help="Clear download history")
def history(clear_flag: bool):
    """Show or clear download history."""
    if clear_flag:
        clear_downloads()
        console.print("[green]History cleared[/green]")
        return

    rows = get_all_downloads()
    if not rows:
        console.print("[yellow]No download history[/yellow]")
        return

    table = Table(title="Download History")
    table.add_column("Title", style="cyan")
    table.add_column("Artist", style="magenta")
    table.add_column("Album", style="white")
    table.add_column("Downloaded", style="dim")
    for _sid, title, artist, album, _fp, ts in rows:
        table.add_row(title, artist, album or "", ts or "")
    console.print(table)


@cli.command()
def stats():
    """Show performance report."""
    s = get_stats()
    if s["ok"] + s["failed"] + s["skipped"] == 0:
        console.print("[yellow]No performance data yet. Download some tracks first.[/yellow]")
        return
    console.print(stats_table(s))


def _normalize(s: str) -> str:
    """Lowercase and strip punctuation so 'im upset' matches "I'm Upset"."""
    out = []
    for ch in s.lower():
        if ch.isalnum() or ch in " -":
            out.append(ch)
        elif ch == ".":
            out.append(" ")
    return "".join(out)


def _track_parts(music_dir: Path, path: Path) -> tuple[str, str, str]:
    """Return the (artist, album, song) an mp3 under music_dir represents."""
    rel = path.relative_to(music_dir).parts
    artist = rel[0] if len(rel) > 1 else ""
    album = rel[1] if len(rel) > 2 else ""
    return artist, album, path.stem


def _find_tracks(music_dir: Path, query: str) -> list[Path]:
    """Find library .mp3 files matching a query: substring first, then fuzzy."""
    files = sorted(p for p in music_dir.rglob("*.mp3") if p.is_file())

    def rel_str(p: Path) -> str:
        return str(p.relative_to(music_dir).with_suffix(""))

    needle = _normalize(query)
    hits = [p for p in files if needle in _normalize(rel_str(p))]
    if not hits:
        parts = {p: _track_parts(music_dir, p) for p in files}
        candidates = set()
        for idx in range(3):
            by_component = {}
            for p, (a, b, c) in parts.items():
                comp = _normalize((a, b, c)[idx])
                if comp and comp not in by_component:
                    by_component[comp] = p
            for m in difflib.get_close_matches(needle, list(by_component), n=len(by_component), cutoff=0.6):
                candidates.add(by_component[m])

        def best_score(p: Path) -> float:
            return max(
                difflib.SequenceMatcher(None, needle, _normalize(comp)).ratio()
                for comp in parts[p]
                if comp
            )

        hits = sorted(candidates, key=lambda p: (-best_score(p), rel_str(p)))

    return hits


def _select_track(music_dir: Path, query: str) -> Path | None:
    """Find the track for a query; single hits skip straight to the caller."""
    console.print(f"[bold]Searching library for:[/bold] {query}")
    hits = _find_tracks(music_dir, query)
    if not hits:
        console.print("[red]No matching tracks found in ~/Music[/red]")
        return None

    if len(hits) > 1:
        table = Table(box=None)
        table.add_column("#", style="dim")
        table.add_column("Artist", style="magenta")
        table.add_column("Album", style="white")
        table.add_column("Song", style="cyan")
        for i, p in enumerate(hits[:10], 1):
            artist, album, song = _track_parts(music_dir, p)
            table.add_row(str(i), artist, album, song)
        console.print(table)

        choice = click.prompt(
            "Select a track",
            type=click.IntRange(1, min(len(hits), 10)),
        )
        return hits[choice - 1]

    return hits[0]


def _music_dir() -> Path:
    music_dir = Path.home() / "Music"
    if not music_dir.is_dir():
        console.print(f"[red]✖ Music library not found: {music_dir}[/red]")
        raise SystemExit(1)
    return music_dir


@cli.command("import")
@click.argument("url")
def import_track(url: str):
    """Import a single new track from a YouTube URL with interactive metadata.

    Built for underground/leaked uploads that have no Spotify presence:
    collects and confirms ID3 metadata + cover art, then downloads the
    audio into the standard ~/Music/<Artist>/<Album>/ layout.
    """
    from musiq.importer import import_youtube

    result = import_youtube(url)
    if result.get("status") == "downloaded":
        console.print(f"[green]✓ Imported:[/green] {result['title']} — {result['artist']}")
    elif result.get("status") == "skipped":
        console.print(f"[yellow]⚠ Skipped:[/yellow] {result.get('message', '')}")
    else:
        console.print(f"[red]✖ Failed:[/red] {result.get('message', '')}")


@cli.command("fix")
@click.argument("query", nargs=-1, required=True)
def fix(query: tuple[str, ...]):
    """Find a track in the local library and re-download its audio from a YouTube URL."""
    q = " ".join(query)
    music_dir = _music_dir()

    filepath = _select_track(music_dir, q)
    if filepath is None:
        return

    artist, album, song = _track_parts(music_dir, filepath)
    if not click.confirm(f'Fix "{song}" — {artist}?', default=True):
        console.print("[yellow]Cancelled[/yellow]")
        return

    url = click.prompt("YouTube URL")
    console.print(f"[bold]Replacing audio for:[/bold] {filepath}")

    if filepath.suffix.lower() != ".mp3":
        console.print(f"[red]✖ Not an MP3 file: {filepath}[/red]")
        raise SystemExit(1)

    console.print(f"[bold]Reading tags from:[/bold] {filepath}")
    tags = read_id3_tags(filepath)
    if not tags:
        console.print(f"[red]✖ No ID3 tags found in {filepath}[/red]")
        raise SystemExit(1)

    title = tags.get("title") or filepath.stem
    artist = tags.get("artist") or "Unknown Artist"
    console.print(f"  [dim]{title} — {artist}[/dim]")

    temp_dir = Path(tempfile.mkdtemp(prefix="musiq_fix_"))
    ydl_opts = _build_ydl_opts(temp_dir)

    console.print(f"[bold]Downloading from:[/bold] {url}")
    try:
        downloaded = _ytm_download(url, ydl_opts)
    except Exception as exc:
        console.print(f"[red]✖ Download failed: {exc}[/red]")
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise SystemExit(1)

    try:
        shutil.copy2(str(downloaded), str(filepath))
    except Exception as exc:
        console.print(f"[red]✖ Failed to replace file: {exc}[/red]")
        downloaded.unlink(missing_ok=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise SystemExit(1)
    finally:
        downloaded.unlink(missing_ok=True)
        shutil.rmtree(temp_dir, ignore_errors=True)

    try:
        rewrite_id3_tags(filepath, tags)
    except Exception as exc:
        console.print(f"[red]✖ Failed to rewrite tags: {exc}[/red]")
        raise SystemExit(1)

    console.print(f"[green]✓ Fixed:[/green] {filepath}")

    verify = read_id3_tags(filepath)
    if verify:
        v_title = verify.get("title", "?")
        v_artist = verify.get("artist", "?")
        v_album = verify.get("album", "?")
        console.print(f"  [dim]Verified tags — {v_title} / {v_artist} / {v_album}[/dim]")


@cli.command("edit")
@click.argument("query", nargs=-1, required=True)
def edit(query: tuple[str, ...]):
    """Edit a track's ID3 tags interactively."""
    q = " ".join(query)
    music_dir = _music_dir()

    filepath = _select_track(music_dir, q)
    if filepath is None:
        return

    artist, album, song = _track_parts(music_dir, filepath)
    if not click.confirm(f'Edit "{song}" — {artist}?', default=True):
        console.print("[yellow]Cancelled[/yellow]")
        return

    tags = read_id3_tags(filepath)
    if not tags:
        console.print(f"[red]✖ No ID3 tags found in {filepath}[/red]")
        raise SystemExit(1)

    console.print(f"[bold]Editing tags for:[/bold] {filepath}")
    console.print(f"  [dim]{tags.get('title') or song} — {tags.get('artist') or artist}[/dim]")

    tags["title"] = _prompt("Title", default=tags.get("title") or "", type=str)
    orig_artist = tags.get("artist") or ""
    new_artist = _prompt("Artist", default=orig_artist, type=str)
    tags["artist"] = new_artist
    if new_artist != orig_artist:
        tags["artists"] = [new_artist]
    tags["album"] = _prompt("Album", default=tags.get("album") or "", type=str)
    tags["track_num"] = _prompt("Track number", default=tags.get("track_num") or 0, type=int)
    tags["disc_num"] = _prompt("Disc number", default=tags.get("disc_num") or 0, type=int)
    tags["genre"] = _prompt("Genre", default=tags.get("genre") or "", type=str)

    while True:
        year_raw = _prompt("Year", default=tags.get("year") or "", type=str).strip()
        if not year_raw:
            break
        if year_raw.isdigit():
            tags["year"] = str(int(year_raw))
            break
        console.print("[yellow]⚠ Invalid year — enter a number or press Enter to skip[/yellow]")

    rewrite_id3_tags(filepath, tags)

    console.print(f"[green]✓ Saved:[/green] {filepath}")
    console.print(
        f"  [dim]Title:[/dim] {tags['title'] or '—'}  "
        f"[dim]Artist:[/dim] {tags['artist'] or '—'}  "
        f"[dim]Album:[/dim] {tags['album'] or '—'}"
    )
    console.print(
        f"  [dim]Track:[/dim] {tags['track_num'] or '—'}  "
        f"[dim]Disc:[/dim] {tags['disc_num'] or '—'}  "
        f"[dim]Genre:[/dim] {tags['genre'] or '—'}  "
        f"[dim]Year:[/dim] {tags['year'] or '—'}"
    )


@cli.command("view")
@click.argument("query", nargs=-1, required=True)
def view(query: tuple[str, ...]):
    """View a track's current ID3 metadata in a read-only table."""
    q = " ".join(query)
    music_dir = _music_dir()
    filepath = _select_track(music_dir, q)
    if filepath is None:
        return

    tags = read_id3_tags(filepath)
    if not tags:
        console.print(f"[red]✖ No ID3 tags found in {filepath}[/red]")
        raise SystemExit(1)

    artist, album, song = _track_parts(music_dir, filepath)

    def cell(value) -> str:
        return escape(str(value)) if value not in ("", None, 0) else "[dim]—[/dim]"

    console.print(f"[bold]Metadata for:[/bold] {filepath}")
    table = Table(box=box.ROUNDED, title=f"Metadata — {tags.get('title') or song}")
    table.add_column("Field", style="bold cyan", width=14)
    table.add_column("Value")
    table.add_row("File", f"[dim]{escape(str(filepath))}[/dim]")
    table.add_row("Folder", f"[dim]{escape(artist)} / {escape(album)}[/dim]" if album else f"[dim]{escape(artist)}[/dim]")
    table.add_row("Title", cell(tags.get("title")))
    table.add_row("Artist", cell(tags.get("artist")))
    table.add_row("Album artist", cell(tags.get("album_artist")))
    table.add_row("Album", cell(tags.get("album")))
    table.add_row("Track", cell(tags.get("track_num")))
    table.add_row("Disc", cell(tags.get("disc_num")))
    table.add_row("Year", cell(tags.get("year")))
    table.add_row("Genre", cell(tags.get("genre")))
    table.add_row("Composer", cell(tags.get("composer")))
    table.add_row(
        "Artwork",
        f"[dim]{n} image{'s' if n != 1 else ''}[/dim]"
        if (n := len(tags.get("images") or []))
        else "[dim]—[/dim]",
    )
    console.print(table)


@cli.command("organize")
@click.option("--apply", "do_apply", is_flag=True,
              help="Actually move folders (default: dry-run only)")
@click.option("-y", "--yes", "yes_mode", is_flag=True,
              help="Ask one confirmation for the whole batch instead of one per folder")
@click.option("--no-navidrome", "no_navidrome", is_flag=True,
              help="Skip Navidrome rescan after --apply")
def organize(do_apply: bool, no_navidrome: bool, yes_mode: bool = False):
    """Scan the library for album-folder / tag mismatches and fix them.

    The rule is literal: for every album folder the folder name must equal
    the current album tag on the files inside it.  Any folder whose name
    doesn't match its album tag is a mismatch and is renamed to match the
    tag — there is no exception for singles (a ``- Single`` suffix
    mismatch is fixed too).  Folders already correct modulo character
    substitution (``:`` -> ``_``, etc.) stay filtered out, as do folders
    whose files disagree on the album tag (``mixed``, left for manual
    review).

    With ``--apply``, the command asks for confirmation per folder before
    moving (or merging into an existing target folder).  Pass ``-y`` to
    skip the per-folder prompts: the full list of proposed moves is shown
    up front (as in the dry run) and a single "Apply all?" confirmation
    covers the whole batch.  When a file would overwrite one that already
    exists in the target folder, only that file is skipped and reported —
    the rest of the folder still moves.  Finally a Navidrome library scan
    is triggered (unless ``--no-navidrome``).
    """
    from musiq.folders import scan_library, rename_folder
    from musiq import navidrome

    music_dir = _music_dir()
    console.print("[bold]Scanning library for album-folder mismatches...[/bold]")
    issues = scan_library(music_dir)

    renames = [i for i in issues if i.category == "rename"]
    merges = [i for i in issues if i.category == "merge"]
    collisions = [i for i in issues if i.category == "collision"]
    mixed = [i for i in issues if i.category == "mixed"]
    has_mismatches = renames or merges or collisions or mixed

    # ── main mismatches table ────────────────────────────────────────
    table = Table(box=box.ROUNDED, title="Album-folder mismatches")
    table.add_column("Artist / Folder", style="magenta")
    table.add_column("Album (tag)", style="white")
    table.add_column("→ Target", style="green")
    table.add_column("Files", justify="right", style="dim")

    for i in renames:
        table.add_row(
            f"{i.artist_folder} / {escape(i.album_folder)}",
            escape(i.tag_album),
            f"[green]{escape(i.target_folder)}[/green]",
            str(len(i.files)),
        )
    for i in merges:
        table.add_row(
            f"{i.artist_folder} / {escape(i.album_folder)}",
            escape(i.tag_album),
            f"[green]{escape(i.target_folder)}[/green] (merge)",
            str(len(i.files)),
        )
    for i in collisions:
        table.add_row(
            f"{i.artist_folder} / {escape(i.album_folder)}",
            escape(i.tag_album),
            f"[yellow]{escape(i.target_folder)}[/yellow] (file exists)",
            str(len(i.files)),
        )
    for i in mixed:
        table.add_row(
            f"{i.artist_folder} / {escape(i.album_folder)}",
            f"[dim](mixed)[/dim] {escape(i.tag_album)}",
            "[dim]—[/dim]",
            str(len(i.files)),
        )

    if has_mismatches:
        console.print(table)

    if not has_mismatches:
        console.print("[green]✓ No album-folder mismatches found[/green]")
        return

    # ── summary line ─────────────────────────────────────────────────
    parts = []
    if renames:
        parts.append(f"[green]{len(renames)}[/green] renamed")
    if merges:
        parts.append(f"[green]{len(merges)}[/green] merged")
    if collisions:
        parts.append(f"[yellow]{len(collisions)}[/yellow] collision(s)")
    if mixed:
        parts.append(f"[dim]{len(mixed)}[/dim] mixed tags")
    console.print("[bold]Summary:[/bold] " + " | ".join(parts))

    applicable = renames + merges + collisions
    if not do_apply:
        if not applicable:
            console.print("[yellow]Nothing to move — mixed tags need manual review[/yellow]")
            return
        console.print("[dim]Dry run — no changes made. Re-run with --apply to move.[/dim]")
        return

    if not applicable:
        console.print("[yellow]Nothing to move — mixed tags need manual review[/yellow]")
        return

    # ── apply: decide which moves to run ───────────────────────────────
    skipped = 0
    if yes_mode:
        if not click.confirm(
            f"Apply all {len(applicable)} move(s)? "
            f"({len(renames)} rename, {len(merges)} merge, {len(collisions)} collision)",
            default=False,
        ):
            console.print("[yellow]Cancelled — nothing applied[/yellow]")
            return
        selected = list(applicable)
    else:
        selected = []
        for issue in applicable:
            src = f"{issue.artist_folder} / {issue.album_folder}"
            tgt = f"{issue.artist_folder} / {issue.target_folder}"
            if not click.confirm(f'Move "{src}" → "{tgt}"?', default=True):
                console.print(f"[dim]Skipped:[/dim] {src}")
                skipped += 1
                continue
            selected.append(issue)

    # ── apply ────────────────────────────────────────────────────────
    renamed = 0
    merged = 0
    failed = 0
    collided = 0
    for issue in selected:
        result = rename_folder(issue, dry_run=False)
        if result == "renamed":
            renamed += 1
        elif result == "merged":
            merged += 1
        elif result == "failed":
            failed += 1
        elif result == "collision":
            collided += 1

    summary = f"[bold]Done:[/bold] {renamed} renamed, {merged} merged"
    if skipped:
        summary += f", {skipped} skipped"
    if collided:
        summary += f", {collided} collided during apply"
    if failed:
        summary += f", {failed} failed"
    console.print(summary)

    if no_navidrome or (renamed == 0 and merged == 0):
        return

    creds = navidrome.ensure_navidrome_config()
    if creds:
        console.print("[bold]Triggering Navidrome library scan...[/bold]")
        if navidrome.start_scan(*creds):
            navidrome.wait_for_scan(*creds)


cli.add_command(organize, name="organise")


@cli.command("retag")
@click.argument("query", nargs=-1, required=True)
def retag(query: tuple[str, ...]):
    """Fetch metadata for a track and fix its ID3 tags.

    Looks the track up in MusicBrainz, iTunes, Deezer and Last.fm
    (artist/title based), shows what each source returns versus what's
    tagged now. When sources genuinely disagree on a field it shows the
    alternatives inline; edit a field by letter (a-g) and name to see its
    candidates numbered and pick one — or press Enter to accept the proposal
    and write the tags. Cover art is left untouched.
    """
    q = " ".join(query)
    music_dir = _music_dir()

    filepath = _select_track(music_dir, q)
    if filepath is None:
        return

    artist, album, song = _track_parts(music_dir, filepath)
    if not click.confirm(f'Retag "{song}" — {artist}?', default=True):
        console.print("[yellow]Cancelled[/yellow]")
        return

    tags = read_id3_tags(filepath)
    if not tags:
        console.print(f"[red]✖ No ID3 tags found in {filepath}[/red]")
        raise SystemExit(1)

    cur_title = tags.get("title") or song
    cur_artist = tags.get("artist") or artist
    console.print(f"[bold]Fetching metadata for:[/bold] {cur_title} — {cur_artist}")

    itunes_meta = fetch_metadata(cur_artist, cur_title)
    dz = deezer_meta(cur_artist, cur_title)
    lastfm_meta = fetch_track_info(cur_artist, cur_title)
    mb = recording_metadata(cur_artist, cur_title, tags.get("album") or album)

    proposed = _propose_metadata(tags, mb, itunes_meta, dz, lastfm_meta, song, artist)

    changed = [
        f
        for f in ("title", "artist", "album", "track_num", "year", "genre", "composer")
        if _field_changed(tags, proposed, f)
    ]
    if tags.get("artist_repair") and "artist" not in changed:
        changed.append("artist")
    if not changed:
        console.print("[yellow]No metadata changes suggested — already up to date[/yellow]")
        return

    while True:
        console.print(_retag_table(tags, proposed, mb, itunes_meta, dz, lastfm_meta))
        choice = _prompt(
            "Accept [yes] / skip / edit a field (a-g or name)",
            default="yes",
            show_default=True,
        ).strip()
        action = _retag_action(choice)
        if action == "yes":
            break
        if action == "skip":
            console.print("[yellow]Skipped[/yellow]")
            return
        if action is None:
            hint = (
                "numbers pick a candidate — choose a field letter/name first"
                if choice.strip().isdigit()
                else "use yes, skip, or a field letter/name"
            )
            console.print(f"[yellow]Unknown choice — {hint}[/yellow]")
            continue
        proposed = _retag_prompt_field(
            tags, proposed, action, mb, itunes_meta, dz, lastfm_meta
        )

    try:
        rewrite_id3_tags(filepath, proposed)
    except Exception as exc:
        console.print(f"[red]✖ Failed to rewrite tags: {exc}[/red]")
        raise SystemExit(1)

    console.print(f"[green]✓ Retagged:[/green] {filepath}")
    composer = proposed.get("composer") or "—"
    console.print(
        f"  [dim]{proposed.get('title')} / {proposed.get('artist')} / "
        f"{proposed.get('album')} · {proposed.get('genre') or '—'} · "
        f"{proposed.get('year') or '—'} · composer: {composer}[/dim]"
    )


def _str_value(tags: dict, field: str) -> str:
    v = tags.get(field) or ""
    if field == "track_num":
        return f"{v:02d}" if isinstance(v, int) and v else ""
    return str(v)


def _field_changed(current: dict, proposed: dict, field: str) -> bool:
    c = _str_value(current, field)
    p = _str_value(proposed, field)
    return bool(p and c != p)


def _fallback_first(strong: list[str], weak: list[str]) -> str:
    """First non-empty candidate, never defaulting to a weak match.

    iTunes/Deezer values whose search confidence is < 0.9 are weak matches;
    such a candidate is only proposed when nothing stronger contributes the
    same field. Within a tier, list order is the priority (MusicBrainz
    first, then Deezer over iTunes, then Last.fm and the existing tags).
    """
    for value in (*strong, *weak):
        if value:
            return value
    return ""


def _propose_metadata(
    tags: dict,
    mb_meta: dict,
    itunes_meta: dict,
    deezer_meta_arg: dict,
    lastfm_meta: dict,
    fallback_song: str,
    fallback_artist: str,
) -> dict:
    """Merge the best available metadata into a proposed tag set.

    Details wins over broad strokes: MusicBrainz for album/year/genre when
    it has a confident release-group match (MBID-keyed, unambiguous), Deezer
    preferred over iTunes as the fallback, Last.fm for artist/title
    cross-check via autocorrect and the track position, and existing values
    are kept when a source has nothing to add. A weak-match source is never
    the default while a stronger value exists for the same field. Images
    are carried through untouched.
    """
    proposed = {k: v for k, v in tags.items() if k != "images"}
    cur_title = tags.get("title") or fallback_song
    cur_artist = tags.get("artist") or fallback_artist

    proposed["title"] = lastfm_meta.get("title") or deezer_meta_arg.get("title") or cur_title
    proposed["artist"] = (
        lastfm_meta.get("artist") or deezer_meta_arg.get("artist") or cur_artist
    )
    proposed["artists"] = _split_multi_value(proposed["artist"]) or tags.get("artists", [])
    proposed["artist"] = ", ".join(proposed["artists"])
    mb_confident = bool(mb_meta.get("ok") and mb_meta.get("match"))
    itunes_confident = itunes_meta.get("confidence", 1.0) >= 0.9
    deezer_confident = deezer_meta_arg.get("confidence", 1.0) >= 0.9

    dz_album = deezer_meta_arg.get("album") or ""
    it_album = itunes_meta.get("album") or ""
    dz_genre = normalise_genre(deezer_meta_arg["genre"]) if deezer_meta_arg.get("genre") else ""
    it_genre = normalise_genre(itunes_meta["genre"]) if itunes_meta.get("genre") else ""
    dz_year = deezer_meta_arg.get("year") or ""
    it_year = itunes_meta.get("year") or ""

    proposed["album"] = _fallback_first(
        [
            mb_meta.get("album") if mb_confident else "",
            dz_album if deezer_confident else "",
            it_album if itunes_confident else "",
            lastfm_meta.get("album") or "",
            tags.get("album") or "",
            cur_title,
        ],
        [dz_album, it_album],
    )
    proposed["genre"] = _fallback_first(
        [
            normalise_genre(mb_meta["genre"]) if mb_confident and mb_meta.get("genre") else "",
            dz_genre if deezer_confident else "",
            it_genre if itunes_confident else "",
        ],
        [dz_genre, it_genre],
    )
    proposed["year"] = _fallback_first(
        [
            mb_meta.get("year") if mb_confident else "",
            dz_year if deezer_confident else "",
            it_year if itunes_confident else "",
        ],
        [dz_year, it_year],
    )
    if lastfm_meta.get("track_num"):
        proposed["track_num"] = lastfm_meta["track_num"]

    proposed["images"] = tags.get("images", [])
    return proposed


_RETAG_ROWS = [
    ("Title", "title"),
    ("Artist", "artist"),
    ("Album", "album"),
    ("Track", "track_num"),
    ("Year", "year"),
    ("Genre", "genre"),
    ("Composer", "composer"),
]

_RETAG_LETTERS = "abcdefghij"
_RETAG_LABEL = {field: label for label, field in _RETAG_ROWS}


def _retag_letter(field: str) -> str:
    for i, (_label, f) in enumerate(_RETAG_ROWS):
        if f == field:
            return _RETAG_LETTERS[i]
    return ""


def _group_key(value) -> str:
    if isinstance(value, int):
        return str(value)
    return " ".join(str(value).lower().split())


def _tag_tokens(value) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", str(value).lower()))


def _is_disputed(group_list: list[dict]) -> bool:
    """True only for meaningful disagreements.

    Near-duplicate values (case/whitespace/punctuation variance, word order,
    genre aliases like "Rap/Hip Hop" vs "Hip-Hop/Rap", "(Deluxe)" suffixes)
    collapse into a single compact proposal line; a breakdown only surfaces
    when sources actually disagree on the value of a tag.
    """
    if len(group_list) < 2:
        return False
    sets = [_tag_tokens(g["value"]) for g in group_list]
    sets = [s for s in sets if s]
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            union = len(sets[i] | sets[j])
            if union and len(sets[i] & sets[j]) / union < 0.5:
                return True
    return False


def _retag_field_candidates(
    tags: dict,
    proposed: dict,
    field: str,
    mb_meta: dict,
    itunes_meta: dict,
    deezer_meta_arg: dict,
    lastfm_meta: dict,
) -> dict:
    """Group the already-fetched provider values for one tag field.

    Returns a dict with:
      "current"   — what's tagged now,
      "groups"    — distinct values, each with the providers that returned it
                    and any caveats (weak match),
      "proposing" — index of the group matching the merged proposal
                    (None when the proposal came from the existing tags),
      "disputed"  — True when sources meaningfully disagree (see
                    ``_is_disputed``).
    Groups are ordered so the proposed value comes first.
    """
    it_caveat = "weak match" if 0 < itunes_meta.get("confidence", 1.0) < 0.9 else ""
    dz_caveat = (
        "weak match"
        if deezer_meta_arg.get("ok") and 0 < deezer_meta_arg.get("confidence", 1.0) < 0.9
        else ""
    )

    def add(provider, value, caveat=""):
        if value is None or value == "" or (isinstance(value, (int, float)) and value == 0):
            return
        if field == "genre":
            value = normalise_genre(str(value))
        pairs.append((provider, value, caveat))

    pairs = []
    if field == "title":
        add("Last.fm", lastfm_meta.get("title"))
        add("Deezer", deezer_meta_arg.get("title"), dz_caveat)
    elif field == "artist":
        add("Last.fm", lastfm_meta.get("artist"))
        add("Deezer", deezer_meta_arg.get("artist"), dz_caveat)
    elif field in ("album", "year", "genre"):
        if mb_meta.get("ok") and mb_meta.get("match") and mb_meta.get(field):
            add("MusicBrainz", mb_meta.get(field))
        add("iTunes", itunes_meta.get(field), it_caveat)
        add("Deezer", deezer_meta_arg.get(field), dz_caveat)
        if field == "album":
            add("Last.fm", lastfm_meta.get("album"))
    elif field == "track_num":
        add("Last.fm", lastfm_meta.get("track_num"))

    groups = {}
    order = []
    for provider, value, caveat in pairs:
        key = _group_key(value)
        if key not in groups:
            groups[key] = {
                "value": value,
                "display": _str_value({field: value}, field),
                "providers": [],
                "caveats": [],
            }
            order.append(key)
        g = groups[key]
        if provider not in g["providers"]:
            g["providers"].append(provider)
        if caveat and caveat not in g["caveats"]:
            g["caveats"].append(caveat)

    group_list = [groups[k] for k in order]

    current = _str_value(tags, field)
    pv = proposed.get(field)
    prop_key = None
    if pv is not None and str(pv).strip() not in ("", "0"):
        prop_key = _group_key(pv)
    kept = bool(current and prop_key is not None and _group_key(current) == prop_key)
    proposing = None
    for i, g in enumerate(group_list):
        if prop_key is not None and _group_key(g["value"]) == prop_key:
            proposing = i
            break
    if proposing is None and kept:
        group_list.append(
            {
                "value": tags.get(field),
                "display": current,
                "providers": ["existing tag"],
                "caveats": [],
            }
        )
        proposing = len(group_list) - 1
    if proposing is not None and proposing > 0:
        group_list.insert(0, group_list.pop(proposing))
        proposing = 0

    return {
        "current": current,
        "groups": group_list,
        "proposing": proposing,
        "kept": kept,
        "disputed": _is_disputed(group_list),
    }


def _retag_breakdown_text(cands: dict) -> str:
    """Render the candidate alternatives for a disputed field.

    Numbers are reserved for the per-field picker, so the inline breakdown
    marks the proposed value with an arrow instead of renumbering everything.
    When the proposal keeps the existing tag, the arrowed line is labelled
    "(kept)" so it is unambiguous which value will be written on accept.
    """
    lines = []
    proposing = cands["proposing"]
    kept = cands.get("kept")
    for i, g in enumerate(cands["groups"], 1):
        src = ", ".join(g["providers"])
        if g["caveats"]:
            src += " — " + ", ".join(g["caveats"])
        arrowed = proposing is not None and i == proposing + 1
        mark = "[green]→[/green] " if arrowed else "  "
        note = " [dim](kept)[/dim]" if arrowed and kept else ""
        lines.append(mark + escape(g["display"] or "—") + note + "  " + escape(f"[{src}]"))
    return "\n".join(lines)


def _retag_table(
    tags: dict,
    proposed: dict,
    mb_meta: dict,
    itunes_meta: dict,
    deezer_meta_arg: dict,
    lastfm_meta: dict,
) -> Table:
    table = Table(box=box.ROUNDED, title="Retag — proposed metadata")
    table.add_column("Field", style="bold cyan")
    table.add_column("Current")
    table.add_column("Proposed")
    table.add_column("Source", style="dim")

    for label, field in _RETAG_ROWS:
        cands = _retag_field_candidates(
            tags, proposed, field, mb_meta, itunes_meta, deezer_meta_arg,
            lastfm_meta,
        )
        cur = cands["current"]
        field_cell = f"{_retag_letter(field)}) {label}"
        if cands["disputed"]:
            table.add_row(field_cell, cur or "[dim]—[/dim]", _retag_breakdown_text(cands), "")
            continue
        prop = _str_value(proposed, field)
        style = "green" if prop and cur == prop else ("yellow" if prop else "dim")
        source = ""
        if cands["groups"]:
            g = cands["groups"][0]
            source = ", ".join(g["providers"])
            if g["caveats"]:
                source += " — " + ", ".join(g["caveats"])
        table.add_row(
            field_cell,
            cur or "[dim]—[/dim]",
            f"[{style}]{escape(prop) or '—'}[/{style}]",
            source,
        )

    if mb_meta.get("ok") and mb_meta.get("match"):
        detail = mb_meta["reason"] or (
            mb_meta.get("rg_title") or mb_meta.get("release_title") or ""
        )
        sec_types = mb_meta.get("secondary_types") or []
        type_str = mb_meta.get("primary_type") or "?"
        if sec_types:
            type_str += " · " + ", ".join(sec_types)
        table.add_row(
            "MusicBrainz release",
            "",
            "[dim]" + detail + "[/dim]",
            f"release-group ({type_str})",
        )
    elif mb_meta.get("ok") and not mb_meta.get("match"):
        table.add_row(
            "⚠ MusicBrainz match",
            "",
            "[yellow]weak[/yellow]",
            mb_meta.get("reason") or "no confident release-group match",
        )
    elif not mb_meta.get("ok") and mb_meta.get("error"):
        table.add_row(
            "⚠ MusicBrainz lookup",
            "",
            "[yellow]unavailable[/yellow]",
            mb_meta["error"],
        )

    if not deezer_meta_arg.get("ok") and deezer_meta_arg.get("error"):
        table.add_row(
            "⚠ Deezer lookup",
            "",
            "[yellow]unavailable[/yellow]",
            deezer_meta_arg["error"],
        )

    if lastfm_meta.get("toptags"):
        table.add_row(
            "Last.fm tags",
            "",
            "[dim]" + ", ".join(lastfm_meta["toptags"][:6]) + "[/dim]",
            "Last.fm (no tag applied)",
        )

    return table


def _retag_action(choice: str) -> str | None:
    """Classify the retag prompt input.

    Returns "yes", "skip", a field name, or None when unknown. Fields are
    picked by letter (a-g, shown in the table) or by full name / unique
    prefix. Numbers are reserved for picking a candidate *inside* a field,
    so they're rejected here.
    """
    c = (choice or "").strip().lower()
    if c in ("", "y", "yes"):
        return "yes"
    if c in ("s", "skip"):
        return "skip"
    if len(c) == 1 and c in _RETAG_LETTERS[: len(_RETAG_ROWS)]:
        return _RETAG_ROWS[_RETAG_LETTERS.index(c)][1]
    for label, field in _RETAG_ROWS:
        if c == field or c == label.lower():
            return field
    matches = [
        field
        for label, field in _RETAG_ROWS
        if len(c) >= 2 and (field.startswith(c) or label.lower().startswith(c))
    ]
    return matches[0] if len(matches) == 1 else None


def _retag_prompt_field(
    tags: dict,
    proposed: dict,
    field: str,
    mb_meta: dict,
    itunes_meta: dict,
    deezer_meta_arg: dict,
    lastfm_meta: dict,
) -> dict:
    """Pick or type one tag field, returning the updated proposal."""
    cands = _retag_field_candidates(
        tags, proposed, field, mb_meta, itunes_meta, deezer_meta_arg,
        lastfm_meta,
    )
    label = _RETAG_LABEL[field]
    total = len(cands["groups"])
    while True:
        console.print(f"[bold]{_retag_letter(field)}) {label}[/bold] — pick a candidate:")
        for i, g in enumerate(cands["groups"], 1):
            src = ", ".join(g["providers"])
            if g["caveats"]:
                src += " — " + ", ".join(g["caveats"])
            console.print(
                f"  {i}) {escape(g['display'] or '—')}  "
                + escape(f"[{src}]")
            )
        console.print(
            f"[dim]  current: {escape(cands['current']) or '—'}[/dim]"
        )
        entry = _prompt(
            f"Option 1-{total} (0 = keep current) or type a value"
            if total
            else "No candidates — type a value (0 = keep current)",
            default="",
            type=str,
            show_default=False,
        ).strip()
        if entry == "":
            return proposed
        if entry == "0":
            value = tags.get(field)
            proposed[field] = value
            if field == "artist":
                proposed["artists"] = _split_multi_value(proposed["artist"]) or []
                proposed["artist"] = ", ".join(proposed["artists"])
            return proposed
        if entry.isdigit() and 1 <= int(entry) <= total:
            proposed[field] = cands["groups"][int(entry) - 1]["value"]
            if field == "artist":
                proposed["artists"] = _split_multi_value(proposed["artist"]) or []
                proposed["artist"] = ", ".join(proposed["artists"])
            return proposed
        if field in ("year", "track_num"):
            if entry.isdigit():
                proposed[field] = str(int(entry)) if field == "year" else int(entry)
                return proposed
            console.print("[yellow]⚠ Invalid value — enter a number or pick an option[/yellow]")
            continue
        proposed[field] = entry
        if field == "artist":
            proposed["artists"] = [entry]
        return proposed


@cli.command("duplicates")
@click.option("--yes", "auto_yes", is_flag=True, help="Auto-confirm deleting duplicate copies")
def duplicates(auto_yes: bool):
    """Scan the library for duplicate tracks and remove extras."""
    music_dir = _music_dir()
    dedupe(music_dir, auto_yes=auto_yes)


def _project_root() -> Path | None:
    pkg_dir = Path(__file__).resolve().parent
    for parent in [pkg_dir, *pkg_dir.parents]:
        if (parent / ".git").is_dir():
            return parent
    return None


@cli.command()
def update():
    """Pull latest code and reinstall dependencies."""
    root = _project_root()
    if root is None:
        console.print("[red]✖ Not a git repository — cannot update[/red]")
        return

    console.print("[bold]Pulling latest code...[/bold]")
    r = subprocess.run(["git", "pull"], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        console.print(f"[red]✖ git pull failed:\n{r.stderr}[/red]")
        return
    if "CONFLICT" in r.stdout or "CONFLICT" in r.stderr:
        console.print("[red]✖ Merge conflicts detected. Resolve them manually, then run update again.[/red]")
        return
    console.print(r.stdout)

    pipx_path = shutil.which("pipx")
    if pipx_path:
        name = "musiq"
        r = subprocess.run(
            [pipx_path, "ensurepath", "--quiet"], capture_output=True,
        )
        r = subprocess.run(
            [pipx_path, "upgrade", name],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            console.print(r.stdout)
            console.print("[green]✓ Update complete (pipx)[/green]")
            return
        if "not installed" not in r.stderr.lower():
            console.print(f"[red]✖ pipx upgrade failed:\n{r.stderr}[/red]")
            return

    console.print("[bold]Reinstalling dependencies...[/bold]")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", str(root)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        console.print(f"[red]✖ pip install failed:\n{r.stderr}[/red]")
        return
    console.print(r.stdout)

    console.print("[green]✓ Update complete[/green]")


if __name__ == "__main__":
    cli()
