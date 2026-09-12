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

# Dry-run (show what would download)
musiq download <url> --dry-run

# Control parallelism (default 3, max 5)
musiq download <url> --workers 4

# Force re-download (bypass history check)
musiq download <url> --force

# Search Spotify interactively
musiq search "bohemian rhapsody"

# Import a single track from a YouTube URL (interactive metadata)
musiq import "https://www.youtube.com/watch?v=apEMrIDUPz0"

# Show download history
musiq history

# Clear history
musiq history --clear

# Show performance stats
musiq stats
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

```
~/Music/
  Daft Punk/
    Discovery/
      01 - Daft Punk - One More Time.mp3
      02 - Daft Punk - Aerodynamic.mp3
  ...
```

## Project structure

```
musiq/
├── musiq/
│   ├── __init__.py
│   ├── cli.py          # click commands
│   ├── spotify.py      # __NEXT_DATA__ scraper + Spotify search
│   ├── itunes.py       # iTunes Search API client
│   ├── ytmusic.py      # ytmusicapi wrapper for YT Music search
│   ├── downloader.py   # yt-dlp wrapper + pipeline orchestrator
│   ├── tagger.py       # eyeD3 ID3 + cover art writer
│   ├── history.py      # SQLite history + perf_log
│   ├── perf.py         # Timer + PerfRecord dataclass
│   └── display.py      # rich tables, progress bars
├── pyproject.toml
└── README.md
```
