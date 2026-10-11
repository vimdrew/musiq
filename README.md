# musiq

Download music from Spotify to high-quality MP3s — no API key required.

Uses **yt-dlp** (YouTube Music) for audio, **eyeD3** for ID3 tagging, and
scrapes the **Spotify embed** + **iTunes Search API** for metadata.

## Requirements

- **ffmpeg** (required by yt-dlp for audio conversion)
- **Pillow** (required by `musiq import` for square cover-art cropping)

  ```
  # Arch
  sudo pacman -S ffmpeg

  # Ubuntu / Debian
  sudo apt install ffmpeg

  # macOS (Homebrew)
  brew install ffmpeg
  ```

## Configuration

The output folder (your music library) is set by the `MUSIC_DIR` line in a
repo-local `.env` file. A committed template lives at **`.env.example`** —
copy it once, then edit that line to move your library:

```bash
cp .env.example .env
```

```ini
MUSIC_DIR=~/Music
```

`.env` is git-ignored, so your personal path stays out of version control.
`MUSIC_DIR` is used by every command that writes to or reads from the
library (`download`, `import`, `organize`, `view`, `retag`, `fix`).
Resolution order, highest priority first:

1. `MUSIC_DIR` environment variable
2. the repo `.env` file
3. `~/.musiq/config`
4. `~/Music` (default)

Optional service settings go in `~/.musiq/config` (`KEY=VALUE` lines), same
place as the Spotify/Navidrome credentials:

```ini
LASTFM_API_KEY=your-lastfm-api-key
```

Last.fm is used by `musiq retag` as a second metadata source (title/artist
cross-check via autocorrect + genre tags). It can also be set as the
`LASTFM_API_KEY` env var, which takes priority. Without it, `musiq retag`
just skips Last.fm and uses iTunes.

Deezer (used by `musiq download` and `musiq retag`) needs no key — its public
API serves album/genre/year for the best artist+title hit. It sits alongside
iTunes: a confident Deezer hit (confidence ≥ 0.9, the same weak-match
threshold `musiq retag` uses) supplies album/genre/year and cover art, and
anything below that falls through to the existing iTunes resolution. Album
batches dedupe the per-album lookup and space requests to stay under Deezer's
rate limit.

MusicBrainz (also used by `musiq retag`) needs no key — a polite
User-Agent is enough. It resolves recordings by artist MBID + title instead
of string matching, so it's the source used to disambiguate album/year when
an artist/title is catalog-heavy (live sets, remixes, compilations all pull
up alongside the studio version). It only proposes data when it finds a
confident match: a release-group matching your current album tag, else a
clean `Album` release-group (no Live/Compilation/Remix secondary types),
earliest release first. Genre is left to iTunes unless MusicBrainz itself
carries one.

## Install

```bash
pip install -e .
```

## Usage

```bash
# Download a single track
musiq download "https://open.spotify.com/track/4iV5W9uYEdYUVa79Axb7Rh"

# Import an underground/leaked upload straight from a YouTube URL
musiq import "https://www.youtube.com/watch?v=apEMrIDUPz0"

# Download an entire album
musiq download "https://open.spotify.com/album/4m2880jivSbbyEGAKMJIT5"

# Download a playlist
musiq download "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"

# Skip iTunes metadata lookup
musiq download <url> --no-itunes

# Skip Deezer metadata lookup
musiq download <url> --no-deezer

# Dry-run (show what would download)
musiq download <url> --dry-run

# Control parallelism (default 3, max 5)
musiq download <url> --workers 4

# Force re-download (bypass history check)
musiq download <url> --force

# Search Spotify interactively
musiq search "bohemian rhapsody"

# Fetch metadata for a track and fix its ID3 tags
musiq retag "bohemian rhapsody"

# View a track's current ID3 metadata in a read-only table
musiq view "bohemian rhapsody"

# Scan the library for album-folder / tag mismatches (dry-run table);
# `organise` works as an alias. The rule is literal: folder name must
# equal the album tag — a "- Single" suffix mismatch counts too
musiq organize

# Move mismatched album folders to match their tags (one confirmation per
# folder); files that would overwrite an existing same-named file are
# skipped and reported, the rest of the folder still moves
musiq organize --apply

# Same, but show the moves up front and ask ONE confirmation for the
# whole batch (-y / --yes); decline and nothing is applied
musiq organize --apply -y

# Import a single track from a YouTube URL (interactive metadata)
musiq import "https://www.youtube.com/watch?v=apEMrIDUPz0"

# Show download history
musiq history

# Clear history
musiq history --clear

# Show performance stats
musiq stats

# List Navidrome tracks that have never been played (play count 0),
# present-only and pipe/redirect friendly; the count goes to stderr
musiq unplayed
musiq unplayed > unplayed.txt
musiq unplayed --out unplayed.txt
```

## How it works

### Download pipeline (per track)

1. **Scrape** Spotify embed → `__NEXT_DATA__` JSON (title, artist, album, track#)
2. **Fetch** iTunes Search API → genre, year, high-res artwork
3. **Search** YouTube Music via `ytmusicapi` for best matching song → video ID
4. **Download** video via yt-dlp → FFmpeg → MP3 320 kbps
5. **Tag** with eyeD3: title, artist, album, track#, year, genre, composer + cover art
6. **Save** to `~/Music/<Artist>/<Album>/<NN - Artist - Title.mp3>`
7. **Record** in SQLite history + performance log

### `musiq import` pipeline

1. **Resolve** YouTube video metadata (yt-dlp, no download)
2. **Parse** title heuristically to guess artist + song
3. **Collect** metadata interactively (artist, album, year, genre, composer…)
4. **Show** confirmation table; allow edits or artwork changes
5. **Check** history.db by video_id (authoritative dedupe), then file path
6. **Download** + convert via yt-dlp → FFmpeg → MP3 320 kbps
7. **Crop** cover art to square (Pillow) if needed
8. **Tag** with eyeD3 and record in history

### Fallback chain

If `__NEXT_DATA__` parsing fails (Spotify changes their embed format):

1. Logs a warning with the actual error
2. Falls back to direct YouTube Music search
3. Still tags with whatever metadata is available

The Spotify embed format does change occasionally — that's the normal
fragility of API-free scraping.

## Output

By default (with `MUSIC_DIR=~/Music` in `.env`):

```
~/Music/
  Daft Punk/
    Discovery/
      01 - Daft Punk - One More Time.mp3
      02 - Daft Punk - Aerodynamic.mp3
  ...
```

Change the `MUSIC_DIR` line in `.env` to write somewhere else.

## Project structure

```
musiq/
├── musiq/
│   ├── __init__.py
│   ├── cli.py          # click commands
│   ├── config.py       # repo .env / ~/.musiq/config resolution (MUSIC_DIR)
│   ├── spotify.py      # __NEXT_DATA__ scraper + Spotify search
│   ├── itunes.py       # iTunes Search API client
│   ├── deezer.py       # Deezer public API client (download + retag: album/genre/year)
│   ├── lastfm.py       # Last.fm API client (retag metadata source)
│   ├── musicbrainz.py  # MusicBrainz client (confirm_metadata + retag recording lookup)
│   ├── ytmusic.py      # ytmusicapi wrapper for YT Music search
│   ├── downloader.py   # yt-dlp wrapper + pipeline orchestrator
│   ├── folders.py      # album-folder / tag mismatch scan + rename/merge (organize)
│   ├── tagger.py       # eyeD3 ID3 + cover art writer
│   ├── history.py      # SQLite history + perf_log
│   ├── perf.py         # Timer + PerfRecord dataclass
│   └── display.py      # rich tables, progress bars
├── .env.example        # template — copy to .env and set MUSIC_DIR
├── pyproject.toml
└── README.md
```
