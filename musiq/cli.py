import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import click
from rich.table import Table

from musiq.display import console, summary_table, stats_table
from musiq.downloader import handle_url, handle_search_selection, process_track, _ytm_download
from musiq.duplicates import dedupe
from musiq.history import get_all_downloads, clear_downloads, get_stats, get_file_path
from musiq.itunes import search_itunes
from musiq.tagger import read_id3_tags, rewrite_id3_tags, fetch_cover_image
from musiq import navidrome
from musiq.spotify import resolve_playlist_tracks


@click.group()
def cli():
    pass


@cli.command()
@click.argument("url")
@click.option("--no-itunes", is_flag=True, help="Skip iTunes metadata lookup")
@click.option("--dry-run", is_flag=True, help="Show what would download without fetching")
@click.option("--force", is_flag=True, help="Re-download even if already in history")
@click.option("--workers", default=3, type=click.IntRange(1, 5), help="Parallel downloads for playlists (1-5)")
def download(url: str, no_itunes: bool, dry_run: bool, force: bool, workers: int):
    """Download a track, album, or playlist from a Spotify URL."""
    results = handle_url(url, no_itunes=no_itunes, dry_run=dry_run, force=force, workers=workers)

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
@click.option("--dry-run", is_flag=True, help="Show what would download without fetching")
@click.option("--force", is_flag=True, help="Re-download even if already in history")
def playlist(url: str, no_itunes: bool, dry_run: bool, force: bool):
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
        r = process_track(track, no_itunes=no_itunes, dry_run=False, force=force)
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
@click.option("--dry-run", is_flag=True, help="Show what would download without fetching")
@click.option("--force", is_flag=True, help="Re-download even if already in history")
def search(query: tuple[str, ...], no_itunes: bool, dry_run: bool, force: bool):
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

    result = handle_search_selection(selected, no_itunes=no_itunes, dry_run=dry_run, force=force)
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

    tags["title"] = click.prompt("Title", default=tags.get("title") or "", type=str)
    orig_artist = tags.get("artist") or ""
    new_artist = click.prompt("Artist", default=orig_artist, type=str)
    tags["artist"] = new_artist
    if new_artist != orig_artist:
        tags["artists"] = [new_artist]
    tags["album"] = click.prompt("Album", default=tags.get("album") or "", type=str)
    tags["track_num"] = click.prompt("Track number", default=tags.get("track_num") or 0, type=int)
    tags["disc_num"] = click.prompt("Disc number", default=tags.get("disc_num") or 0, type=int)
    tags["genre"] = click.prompt("Genre", default=tags.get("genre") or "", type=str)

    while True:
        year_raw = click.prompt("Year", default=tags.get("year") or "", type=str).strip()
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
