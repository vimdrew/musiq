"""Library folder integrity: detect and fix album-folder-vs-tag mismatches."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mutagen.id3 import ID3, ID3NoHeaderError

from musiq.display import console
from musiq.downloader import sanitize

AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".ogg", ".opus"}


@dataclass
class FolderIssue:
    artist_folder: str
    album_folder: str
    tag_album: str
    target_folder: str
    files: list[Path] = field(repr=False, default_factory=list)
    category: str = ""  # "rename" | "merge" | "collision" | "mixed"


def _read_id3_tags(filepath: Path) -> tuple[str, str]:
    """Read album and title tags from an audio file (ID3 only)."""
    try:
        tags = ID3(str(filepath))
    except (ID3NoHeaderError, Exception):
        return "", ""
    album = str(tags.get("TALB", "")).strip()
    title = str(tags.get("TIT2", "")).strip()
    return album, title


def scan_library(music_dir: Path) -> list[FolderIssue]:
    """Walk the library and return album folders whose name doesn't match the album tag.

    The rule is literal: for every album folder the folder name must equal
    the current album tag on the files inside it.  If it doesn't, that's a
    mismatch — the folder is renamed to match the tag.  No special case is
    made for singles (``- Single`` folders, tag == track title, etc.); a
    ``- Single`` suffix mismatch is just an ordinary mismatch.

    Handles the standard ``~/Music/<Artist>/<Album>/<files>`` layout only.
    Files at artist root or nested deeper than two levels are skipped.
    Files whose album tag differs from siblings in the same folder are
    flagged as ``mixed`` and excluded from automatic fixing — there is no
    single correct target to rename to in that case.

    When the target album folder already exists but a same-named file is
    NOT already inside it, the issue is classified as ``merge`` — the
    file(s) can be moved into the existing folder.  Only when a target
    file with the same name already exists is a ``collision`` reported.
    """
    issues: list[FolderIssue] = []

    if not music_dir.is_dir():
        return issues

    for artist_dir in sorted(p for p in music_dir.iterdir() if p.is_dir()):
        if artist_dir.name.startswith(".") or artist_dir.name in ("Books", "Audio"):
            continue

        # Group files by (artist_folder, album_folder) within this artist dir.
        # Values are (album_tag, title_tag) per file.
        album_groups: dict[str, dict[str, list[tuple[Path, str]]]] = {}

        for ext in AUDIO_EXTS:
            for audio_file in artist_dir.rglob(f"*{ext}"):
                if not audio_file.is_file():
                    continue
                # Only accept standard depth: artist_dir/album_dir/file
                try:
                    rel = audio_file.relative_to(artist_dir)
                except ValueError:
                    continue
                if len(rel.parts) != 2:
                    # Nested deeper (audiobooks) or at artist root — skip
                    continue

                album_folder = rel.parts[0]
                album_tag, title_tag = _read_id3_tags(audio_file)
                if not album_tag:
                    continue

                groups = album_groups.setdefault(album_folder, {})
                groups.setdefault(album_tag, []).append((audio_file, title_tag))

        for album_folder, tag_entries in sorted(album_groups.items()):
            tags = list(tag_entries.keys())

            if len(tags) > 1:
                # Ambiguous: files inside one folder disagree on album tag.
                dominant = max(tags, key=lambda t: len(tag_entries[t]))
                all_files = [f for entries in tag_entries.values() for f, _ in entries]
                issues.append(FolderIssue(
                    artist_folder=artist_dir.name,
                    album_folder=album_folder,
                    tag_album=dominant,
                    target_folder=sanitize(dominant),
                    files=all_files,
                    category="mixed",
                ))
                continue

            tag_album = tags[0]
            entries = tag_entries[tag_album]
            files = [f for f, _ in entries]
            target = sanitize(tag_album)

            # Already correct — the folder name matches what sanitize() would produce.
            if album_folder == target:
                continue

            # Collision check first — does a target file already exist?
            target_dir = artist_dir / target
            if target_dir.exists() and target_dir.is_dir() and target != album_folder:
                # A same-named file already inside the target → real conflict.
                existing_names = {f.name for f in target_dir.iterdir() if f.is_file()}
                if any(f.name in existing_names for f in files):
                    category = "collision"
                else:
                    # Folder exists but the file doesn't — safe to move into it.
                    category = "merge"
            else:
                category = "rename"

            issues.append(FolderIssue(
                artist_folder=artist_dir.name,
                album_folder=album_folder,
                tag_album=tag_album,
                target_folder=target,
                files=files,
                category=category,
            ))

    return issues


def rename_folder(issue: FolderIssue, *, dry_run: bool = True) -> str:
    """Rename an album folder to match its album tag, or merge its files
    into an existing target folder.

    Applies to ``rename``, ``merge`` and ``collision`` issues alike: when
    the target folder already exists, every source file is moved in unless
    a same-named file is already there.  Only those colliding files are
    skipped (and reported) — the rest of the folder still moves.

    Returns one of: ``"pending"``, ``"renamed"``, ``"merged"``,
    ``"collision"``, ``"skipped"``, ``"failed"``.
    """
    if issue.category not in ("rename", "merge", "collision"):
        return "skipped"

    if dry_run:
        return "pending"

    artist_path = Path.home() / "Music" / issue.artist_folder
    source = artist_path / issue.album_folder
    target = artist_path / issue.target_folder

    if target.exists() and target.is_dir():
        # Target folder exists — move in every file unless a same-named file
        # is already there (the true conflict).  Colliding files are skipped
        # and reported, the others still move.
        existing_names = {f.name for f in target.iterdir() if f.is_file()}
        to_move = [f for f in issue.files if f.name not in existing_names]
        blocking = [f for f in issue.files if f.name in existing_names]

        for f in blocking:
            console.print(
                f"[yellow]⚠ Collision — '{f.name}' already exists, "
                f"skipping:[/yellow] {target}"
            )

        if not source.is_dir():
            console.print(f"[red]✖ Source folder not found:[/red] {source}")
            return "failed"

        if not to_move:
            return "collision"

        try:
            for f in to_move:
                f.rename(target / f.name)
            try:
                source.rmdir()
            except OSError:
                pass
            if blocking:
                console.print(
                    f"[yellow]⚠ Partially moved — {len(blocking)} file(s) "
                    f"skipped:[/yellow] {source} → {target}"
                )
                return "collision"
            console.print(f"[green]✓ Merged:[/green] {source} → {target}")
            return "merged"
        except Exception as exc:
            console.print(f"[red]✖ Failed to merge {source}: {exc}[/red]")
            return "failed"

    if not source.is_dir():
        console.print(f"[red]✖ Source folder not found:[/red] {source}")
        return "failed"

    try:
        source.rename(target)
        console.print(f"[green]✓ Renamed:[/green] {source} → {target}")
        return "renamed"
    except Exception as exc:
        console.print(f"[red]✖ Failed to rename {source}: {exc}[/red]")
        return "failed"
