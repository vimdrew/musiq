import sqlite3
import threading
from pathlib import Path

DB_DIR = Path.home() / ".musiq"
DB_PATH = DB_DIR / "history.db"

_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            spotify_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            album TEXT,
            file_path TEXT,
            downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS perf_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            spotify_id      TEXT,
            title           TEXT,
            artist          TEXT,
            ts              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            t_spotify_ms    INTEGER,
            t_itunes_ms     INTEGER,
            t_search_ms     INTEGER,
            t_download_ms   INTEGER,
            t_tag_ms        INTEGER,
            t_total_ms      INTEGER,
            filesize_kb     INTEGER,
            bitrate_kbps    INTEGER,
            source          TEXT,
            status          TEXT
        )
    """)
    conn.commit()
    return conn


def track_exists(spotify_id: str) -> bool:
    with _lock:
        conn = get_conn()
        cur = conn.execute("SELECT 1 FROM downloads WHERE spotify_id = ?", (spotify_id,))
        exists = cur.fetchone() is not None
        conn.close()
    return exists


def add_download(spotify_id: str, title: str, artist: str, album: str | None, file_path: str | None):
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO downloads "
            "(spotify_id, title, artist, album, file_path, downloaded_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (spotify_id, title, artist, album, file_path),
        )
        conn.commit()
        conn.close()


def insert_perf(rec):
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO perf_log "
            "(spotify_id, title, artist, t_spotify_ms, t_itunes_ms, "
            "t_search_ms, t_download_ms, t_tag_ms, t_total_ms, "
            "filesize_kb, bitrate_kbps, source, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.spotify_id, rec.title, rec.artist,
                rec.t_spotify_ms, rec.t_itunes_ms,
                rec.t_search_ms, rec.t_download_ms, rec.t_tag_ms,
                rec.t_total_ms, rec.filesize_kb, rec.bitrate_kbps,
                rec.source, rec.status,
            ),
        )
        conn.commit()
        conn.close()


def get_all_downloads():
    conn = get_conn()
    cur = conn.execute(
        "SELECT spotify_id, title, artist, album, file_path, downloaded_at "
        "FROM downloads ORDER BY downloaded_at DESC"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def clear_downloads():
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM downloads")
        conn.execute("DELETE FROM perf_log")
        conn.commit()
        conn.close()


def get_stats():
    conn = get_conn()
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_perf_log_t_total ON perf_log(t_total_ms)"
    )
    cur = conn.execute("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'ok') AS ok_count,
            COUNT(*) FILTER (WHERE status = 'failed') AS fail_count,
            COUNT(*) FILTER (WHERE status = 'skipped') AS skip_count,
            AVG(t_spotify_ms) AS avg_spotify,
            AVG(t_itunes_ms) AS avg_itunes,
            AVG(t_search_ms) AS avg_search,
            AVG(t_download_ms) AS avg_download,
            AVG(t_tag_ms) AS avg_tag,
            AVG(t_total_ms) AS avg_total,
            MIN(t_total_ms) AS min_total,
            MAX(t_total_ms) AS max_total,
            AVG(filesize_kb) AS avg_filesize,
            AVG(bitrate_kbps) AS avg_bitrate,
            MIN(t_spotify_ms) AS min_spotify,
            MAX(t_spotify_ms) AS max_spotify,
            MIN(t_itunes_ms) AS min_itunes,
            MAX(t_itunes_ms) AS max_itunes,
            MIN(t_search_ms) AS min_search,
            MAX(t_search_ms) AS max_search,
            MIN(t_download_ms) AS min_download,
            MAX(t_download_ms) AS max_download,
            MIN(t_tag_ms) AS min_tag,
            MAX(t_tag_ms) AS max_tag,
            CASE COUNT(*) WHEN 0 THEN 0 ELSE
                CAST(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS REAL) / COUNT(*) * 100
            END AS success_pct
        FROM perf_log
    """)
    row = cur.fetchone()

    slowest_title = ""
    fastest_title = ""
    cur = conn.execute(
        "SELECT title, t_total_ms FROM perf_log WHERE status = 'ok' "
        "ORDER BY t_total_ms DESC LIMIT 1"
    )
    r = cur.fetchone()
    if r:
        slowest_title = r[0]
    cur = conn.execute(
        "SELECT title, t_total_ms FROM perf_log WHERE status = 'ok' "
        "ORDER BY t_total_ms ASC LIMIT 1"
    )
    r = cur.fetchone()
    if r:
        fastest_title = r[0]
    conn.close()

    return {
        "ok": row[0], "failed": row[1], "skipped": row[2],
        "avg_spotify": row[3] or 0, "avg_itunes": row[4] or 0,
        "avg_search": row[5] or 0, "avg_download": row[6] or 0,
        "avg_tag": row[7] or 0, "avg_total": row[8] or 0,
        "min_total": row[9] or 0, "max_total": row[10] or 0,
        "avg_filesize": row[11] or 0, "avg_bitrate": row[12] or 0,
        "min_spotify": row[13] or 0, "max_spotify": row[14] or 0,
        "min_itunes": row[15] or 0, "max_itunes": row[16] or 0,
        "min_search": row[17] or 0, "max_search": row[18] or 0,
        "min_download": row[19] or 0, "max_download": row[20] or 0,
        "min_tag": row[21] or 0, "max_tag": row[22] or 0,
        "success_pct": row[23] or 0,
        "slowest_title": slowest_title,
        "fastest_title": fastest_title,
    }
