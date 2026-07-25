import time
from dataclasses import dataclass


@dataclass
class PerfRecord:
    spotify_id: str = ""
    title: str = ""
    artist: str = ""
    t_spotify_ms: int = 0
    t_itunes_ms: int = 0
    t_search_ms: int = 0
    t_download_ms: int = 0
    t_tag_ms: int = 0
    t_total_ms: int = 0
    filesize_kb: int = 0
    bitrate_kbps: int = 0
    source: str = "spotify_embed"
    status: str = "ok"


class Timer:
    __slots__ = ("start", "elapsed_ms")

    def __init__(self):
        self.start: float = 0.0
        self.elapsed_ms: int = 0

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args):
        self.elapsed_ms = int((time.perf_counter() - self.start) * 1000)

    def stop(self) -> int:
        self.elapsed_ms = int((time.perf_counter() - self.start) * 1000)
        return self.elapsed_ms
