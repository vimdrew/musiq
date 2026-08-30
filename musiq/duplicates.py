import re
from dataclasses import dataclass
from pathlib import Path

import click
import eyed3
from rich.table import Table

from musiq.display import console, make_progress
from musiq.history import get_all_downloads, remove_download_by_path
from musiq.spotify import _spotify_client
from musiq.tagger import read_id3_tags

_FEAT_RE = re.compile(
    r"\s*(?:\(|\[)?\s*(?:feat\.?|featuring|ft\.?)\b[^)\]]*(?:\)|\])?$",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"\(\s*[^)]*\s*\)\s*$")
_BARE_SUFFIX = re.compile(
    r"\s*[-\u2013\u2014]\s*(?:"
    r"remaster(?:ed)?|\d{4}\s+remaster(?:ed)?|\d{4}|live|remix|"
    r"deluxe(?: edition)?|radio edit|edit|acoustic(?: version)?|"
    r"instrumental|demo(?: version)?|single version|album version|bonus track"
    r")$",
    re.IGNORECASE,
)
_TRAILING_SEP_RE = re.compile(r"[\s\-–—]+$")

_GENERIC_ALBUMS = {"", "playlist", "unknown", "various artists", "single", "ep"}


@dataclass
class TrackFile:
    path: Path
    mtime: float
    title: str = ""
    artist: str = ""
    album: str = ""
    track_num: int = 0
    has_art: bool = False
    duration_sec: float = 0.0
    bitrate_kbps: float = 0.0
    size_bytes: int = 0
    score: float = 0.0


def _norm_key(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").strip()


def _strip_qualifiers(text: str) -> str:
    s = text.strip()
    for _ in range(10):
        cand = s
        cand = _FEAT_RE.sub(" ", cand)
        cand = _PAREN_RE.sub(" ", cand)
        cand = _BARE_SUFFIX.sub(" ", cand)
        cand = _TRAILING_SEP_RE.sub("", cand.strip())
        cand = cand.strip()
        if not cand:
            return s
        if cand == s:
            return cand
        s = cand
    return s


def _group_key(artist: str, title: str) -> str:
    a = _norm_key(_strip_qualifiers(artist))
    t = _norm_key(_strip_qualifiers(title))
    return f"{a}\n{t}"


def _score_file(r: TrackFile, official_duration_ms: int | None) -> float:
    score = 0.0
    album_key = _norm_key(r.album)
    if album_key and album_key not in _GENERIC_ALBUMS:
        score += 2.0
    if r.track_num and r.track_num > 0:
        score += 1.5
    if r.has_art:
        score += 1.5
    if official_duration_ms and r.duration_sec > 0:
        drift = abs(r.duration_sec - official_duration_ms / 1000)
        score += 3.0 * max(0.0, 1.0 - drift / 30)
    if r.bitrate_kbps:
        score += 1.5 * min(1.0, r.bitrate_kbps / 320)
    elif r.size_bytes:
        score += 1.5 * min(1.0, r.size_bytes / (8 * 1024 * 1024))
    return round(score, 3)


def _read_track(path: Path) -> TrackFile | None:
    try:
        tags = read_id3_tags(path)
        st = path.stat()
    except Exception:
        return None
    title = (tags.get("title") or "").strip()
    artist = (tags.get("artist") or "").strip()
    if not title or not artist:
        return None

    duration_sec = 0.0
    bitrate_kbps = 0.0
    try:
        audio = eyed3.load(str(path))
        if audio is not None and audio.info is not None:
            duration_sec = audio.info.time_secs or 0.0
            raw = getattr(audio.info, "bit_rate", 0)
            bitrate_kbps = raw[1] if isinstance(raw, tuple) else raw
            bitrate_kbps = bitrate_kbps or 0.0
    except Exception:
        pass

    return TrackFile(
        path=path,
        mtime=st.st_mtime,
        title=title,
        artist=artist,
        album=(tags.get("album") or "").strip(),
        track_num=int(tags.get("track_num") or 0),
        has_art=bool(tags.get("images")),
        duration_sec=float(duration_sec),
        bitrate_kbps=float(bitrate_kbps),
        size_bytes=int(st.st_size),
    )


def _human_size(n: int) -> str:
    if n <= 0:
        return "--"
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}M"
    return f"{n / 1024:.0f}K"


def _print_group(group: list[TrackFile], music_dir: Path) -> None:
    console.print(f"\n[bold]{group[0].title}[/bold] — [cyan]{group[0].artist}[/cyan]")
    table = Table(box=None)
    table.add_column("#", style="dim")
    table.add_column("File", style="white")
    table.add_column("Album", style="magenta")
    table.add_column("Duration", style="cyan")
    table.add_column("Bitrate", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Mark", style="bold")
    for i, r in enumerate(group, 1):
        rel = str(r.path.relative_to(music_dir))
        dur = f"{int(r.duration_sec // 60)}:{int(r.duration_sec % 60):02d}" if r.duration_sec else "--:--"
        bit = f"{r.bitrate_kbps:.0f}k" if r.bitrate_kbps else "--"
        mark = "[green]KEEP[/green]" if i == 1 else "[yellow]DELETE?[/yellow]"
        table.add_row(
            str(i),
            rel,
            r.album or "",
            dur,
            bit,
            _human_size(r.size_bytes),
            f"{r.score:.2f}",
            mark,
        )
    console.print(table)


def _official_duration_map(paths: set[str]) -> dict[str, int]:
    """Map absolute mp3 path -> official duration_ms, using history spotify_ids."""
    if not _spotify_client.configured:
        return {}
    try:
        rows = get_all_downloads()
    except Exception:
        return {}
    path_to_sid: dict[str, str] = {}
    for row in rows:
        sid, fp = row[0], row[4]
        if sid and fp:
            path_to_sid[str(Path(fp))] = sid
    result: dict[str, int] = {}
    cache: dict[str, int | None] = {}
    for p in paths:
        sid = path_to_sid.get(p)
        if not sid:
            continue
        if sid not in cache:
            cache[sid] = None
            try:
                data = _spotify_client.get_track(sid)
            except Exception:
                data = None
            if data and data.get("duration_ms"):
                cache[sid] = int(data["duration_ms"])
        if cache[sid]:
            result[p] = cache[sid]
    return result


def _delete_paths(paths: list[Path]) -> list[Path]:
    deleted: list[Path] = []
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            console.print(f"[red]✖ Could not delete: {p} ({exc})[/red]")
            continue
        try:
            remove_download_by_path(str(p))
        except Exception:
            pass
        deleted.append(p)
    return deleted


def dedupe(music_dir: Path, auto_yes: bool = False) -> int:
    """Scan music_dir for duplicate tracks, print groups, and delete extras."""
    files = sorted(p for p in music_dir.rglob("*.mp3") if p.is_file())
    if not files:
        console.print("[yellow]No MP3 files found in library[/yellow]")
        return 0

    records: list[TrackFile] = []
    with make_progress() as progress:
        task = progress.add_task("[cyan]Scanning library...", total=len(files))
        for p in files:
            rec = _read_track(p)
            if rec is not None:
                records.append(rec)
            progress.advance(task)

    raw: dict[str, list[TrackFile]] = {}
    for rec in records:
        raw.setdefault(_group_key(rec.artist, rec.title), []).append(rec)

    dup_paths = {
        str(r.path)
        for members in raw.values()
        if len(members) >= 2
        for r in members
    }
    durations = _official_duration_map(dup_paths)

    groups: list[list[TrackFile]] = []
    for members in raw.values():
        if len(members) < 2:
            continue
        for r in members:
            r.score = _score_file(r, durations.get(str(r.path)))
        members.sort(key=lambda r: (-r.score, r.mtime))
        groups.append(list(members))
    groups.sort(key=lambda g: (g[0].artist, g[0].title))

    if not groups:
        console.print("[green]No duplicates found[/green]")
        return 0

    total_deleted = 0
    for group in groups:
        _print_group(group, music_dir)
        losers = [r.path for r in group[1:]]
        if auto_yes:
            ok = True
        else:
            answer = click.prompt(
                f"Delete {len(losers)} duplicate(s) in this group? (y/n/skip)",
                default="n",
                show_default=False,
                type=str,
            )
            ok = answer.strip().lower().startswith("y")
        if ok:
            deleted = _delete_paths(losers)
            for p in deleted:
                console.print(f"[green]✓ Deleted:[/green] {p}")
            total_deleted += len(deleted)
        else:
            console.print("[dim]Skipped[/dim]")

    console.print(
        f"\n[bold]Duplicates scan complete.[/bold] {len(groups)} group(s), "
        f"[green]{total_deleted} file(s) deleted[/green]"
    )
    return total_deleted