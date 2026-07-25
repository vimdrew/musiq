import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.table import Table

from musiq.display import console, summary_table, stats_table
from musiq.downloader import handle_url, handle_search_selection
from musiq.history import get_all_downloads, clear_downloads, get_stats
from musiq.spotify import search_itunes


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
