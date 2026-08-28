import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import click
from rich.table import Table

from musiq.display import console, summary_table, stats_table
from musiq.downloader import handle_url, handle_search_selection, _ytm_download
from musiq.history import get_all_downloads, clear_downloads, get_stats
from musiq.itunes import search_itunes
from musiq.tagger import read_id3_tags, rewrite_id3_tags


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


@cli.command("fix")
@click.argument("query", nargs=-1, required=True)
def fix(query: tuple[str, ...]):
    """Find a track in the local library and re-download its audio from a YouTube URL."""
    q = " ".join(query)
    console.print(f"[bold]Searching library for:[/bold] {q}")

    music_dir = Path.home() / "Music"
    if not music_dir.is_dir():
        console.print(f"[red]✖ Music library not found: {music_dir}[/red]")
        raise SystemExit(1)

    def rel_str(p: Path) -> str:
        return str(p.relative_to(music_dir).with_suffix(""))

    def track_parts(p: Path) -> tuple[str, str, str]:
        rel = p.relative_to(music_dir).parts
        artist = rel[0] if len(rel) > 1 else ""
        album = rel[1] if len(rel) > 2 else ""
        return artist, album, p.stem

    def normalize(s: str) -> str:
        out = []
        for ch in s.lower():
            if ch.isalnum() or ch in " -":
                out.append(ch)
            elif ch == ".":
                out.append(" ")
        return "".join(out)

    files = sorted(p for p in music_dir.rglob("*.mp3") if p.is_file())
    needle = normalize(q)
    hits = [p for p in files if needle in normalize(rel_str(p))]
    if not hits:
        parts = {p: track_parts(p) for p in files}
        candidates = set()
        for idx in range(3):
            by_component = {}
            for p, (a, b, c) in parts.items():
                comp = normalize((a, b, c)[idx])
                if comp and comp not in by_component:
                    by_component[comp] = p
            for m in difflib.get_close_matches(needle, list(by_component), n=len(by_component), cutoff=0.6):
                candidates.add(by_component[m])

        def best_score(p: Path) -> float:
            return max(
                difflib.SequenceMatcher(None, needle, normalize(comp)).ratio()
                for comp in parts[p]
                if comp
            )

        hits = sorted(candidates, key=lambda p: (-best_score(p), rel_str(p)))

    if not hits:
        console.print("[red]No matching tracks found in ~/Music[/red]")
        return

    if len(hits) > 1:
        table = Table(box=None)
        table.add_column("#", style="dim")
        table.add_column("Artist", style="magenta")
        table.add_column("Album", style="white")
        table.add_column("Song", style="cyan")
        for i, p in enumerate(hits[:10], 1):
            artist, album, song = track_parts(p)
            table.add_row(str(i), artist, album, song)
        console.print(table)

        choice = click.prompt(
            "Select a track to fix",
            type=click.IntRange(1, min(len(hits), 10)),
        )
        filepath = hits[choice - 1]
    else:
        filepath = hits[0]
    artist, album, song = track_parts(filepath)
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
