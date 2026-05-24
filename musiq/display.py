from rich.console import Console
from rich.table import Table
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TimeElapsedColumn,
    TaskID,
)
from rich import box
from rich.text import Text

console = Console()


def make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    )


def summary_table(results: list[dict]) -> Table:
    table = Table(box=box.ROUNDED, title="Download Summary")
    table.add_column("Status", style="bold")
    table.add_column("Title", style="cyan")
    table.add_column("Artist", style="magenta")
    table.add_column("Duration", style="white")

    status_icon = {"downloaded": "OK", "skipped": "SKIP", "failed": "FAIL"}

    for r in results:
        st = r.get("status", "?")
        color = {"downloaded": "green", "skipped": "yellow", "failed": "red"}.get(st, "white")
        d = r.get("duration", 0) or 0
        dur_str = f"{int(d // 60)}:{int(d % 60):02d}" if d else "--:--"
        table.add_row(
            f"[{color}]{status_icon.get(st, st)}[/{color}]",
            r.get("title", "?"),
            r.get("artist", "?"),
            dur_str,
        )
    return table


def stats_table(s: dict) -> Table:
    table = Table(box=box.ROUNDED, title="musiq Performance Report")
    table.add_column("Stage", style="bold cyan")
    table.add_column("Avg (ms)", justify="right")
    table.add_column("Min (ms)", justify="right")
    table.add_column("Max (ms)", justify="right")

    stages = [
        ("Spotify scrape", "avg_spotify", "min_spotify", "max_spotify"),
        ("iTunes lookup", "avg_itunes", "min_itunes", "max_itunes"),
        ("YT Music search", "avg_search", "min_search", "max_search"),
        ("Download + convert", "avg_download", "min_download", "max_download"),
        ("ID3 tagging", "avg_tag", "min_tag", "max_tag"),
        ("Total per track", "avg_total", "min_total", "max_total"),
    ]

    for label, avg_k, min_k, max_k in stages:
        avg = s.get(avg_k, 0)
        mn = s.get(min_k, 0)
        mx = s.get(max_k, 0)
        table.add_row(
            label,
            f"{avg:,.0f}",
            f"{mn:,.0f}",
            f"{mx:,.0f}",
        )

    total = s.get("ok", 0) + s.get("failed", 0) + s.get("skipped", 0)
    table.add_section()
    table.add_row(
        Text(f"Tracks downloaded: {s.get('ok', 0)}", style="green"),
        Text(f"Failed: {s.get('failed', 0)}", style="red"),
        Text(f"Skipped: {s.get('skipped', 0)}", style="yellow"),
        Text(f"Total: {total}", style="bold"),
    )
    table.add_row(
        f"Avg file size: {s.get('avg_filesize', 0) / 1000:.1f} MB" if s.get("avg_filesize") else "Avg file size: --",
        f"Success rate: {s.get('success_pct', 0):.0f}%",
        f"Slowest: {s.get('slowest_title', '--')}",
        f"Fastest: {s.get('fastest_title', '--')}",
    )
    return table
