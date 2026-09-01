"""SQLite time-series storage: raw samples plus 1-minute rollups.

Raw samples are kept for a short window at full resolution; every completed minute is
folded into `sample_1m` (sum/count/min/max) which is what long ranges are drawn from.
"""

from __future__ import annotations

import math
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

MINUTE = 60

# Read connections are pooled rather than made per thread. Four is plenty: queries are
# milliseconds long and the HTTP server only has a handful of clients.
READERS = 4

# Past roughly this many raw samples per metric the detail is finer than the chart can
# draw and reading it only burns CPU: a 12 h window at 1 Hz is 1.8M raw rows against
# 30k rollup rows. Keeps long ranges cheap on a machine that is busy gaming.
RAW_SCAN_SAMPLES = 7200

# One prune must never take a long write lock or balloon the WAL, so expired rows go in
# batches instead of a single multi-million-row DELETE.
PRUNE_CHUNK = 100_000


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
    # busy_timeout first: every reader also asserts journal_mode, and that briefly wants
    # a lock the collector may be holding.
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    # Pin the page cache instead of inheriting a default, so total memory is a number
    # we can state: at most (READERS + 1) x 2 MB of cache for the whole process.
    conn.execute("PRAGMA cache_size=-2000")
    # Let the WAL shrink back after a checkpoint instead of staying at its high water mark.
    conn.execute("PRAGMA journal_size_limit=16777216")
    return conn


def _runs(buckets: list[tuple[int, float]], gap: int = MINUTE):
    """Collapse flagged minute buckets into contiguous (start, end, peak) windows."""
    runs: list[list] = []
    for bucket, value in buckets:
        if runs and bucket - runs[-1][1] <= gap:
            runs[-1][1] = bucket + MINUTE
            runs[-1][2] = max(runs[-1][2], value)
        else:
            runs.append([bucket, bucket + MINUTE, value])
    return [tuple(r) for r in runs]


class Store:
    def __init__(self, path: Path, raw_retention_days: float = 2.0,
                 rollup_retention_days: float = 90.0, step: int = 5):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_retention = int(raw_retention_days * 86400)
        self.rollup_retention = int(rollup_retention_days * 86400)
        self.step = max(1, int(step))
        self._write_lock = threading.Lock()
        self._w = _connect(self.path)
        self._pool: queue.LifoQueue = queue.LifoQueue(maxsize=READERS)
        self._ids: dict[str, int] = {}
        self._init_schema()
        self._load_ids()

    # ------------------------------------------------------------------ plumbing
    def _init_schema(self) -> None:
        with self._write_lock, self._w:
            self._w.executescript(
                """
                CREATE TABLE IF NOT EXISTS metric (
                    id  INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sample (
                    metric_id INTEGER NOT NULL,
                    ts        INTEGER NOT NULL,
                    value     REAL    NOT NULL,
                    PRIMARY KEY (metric_id, ts)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS sample_1m (
                    metric_id INTEGER NOT NULL,
                    bucket    INTEGER NOT NULL,
                    total     REAL    NOT NULL,
                    n         INTEGER NOT NULL,
                    lo        REAL    NOT NULL,
                    hi        REAL    NOT NULL,
                    PRIMARY KEY (metric_id, bucket)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS meta (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _reader(self):
        """Borrow a read connection from a small fixed pool.

        Giving every HTTP worker thread its own handle looked fine until a client that
        reconnects per request churns through threads: each new one pays for a cold
        SQLite page cache and memory climbs with the connection count. A pool bounds
        both, and hands back a cache that is already warm.
        """
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            conn = _connect(self.path)
        try:
            yield conn
        finally:
            try:
                self._pool.put_nowait(conn)
            except queue.Full:
                conn.close()

    def _load_ids(self) -> None:
        self._ids = {k: i for i, k in self._w.execute("SELECT id, key FROM metric")}

    def metric_id(self, key: str) -> int:
        mid = self._ids.get(key)
        if mid is None:
            with self._write_lock, self._w:
                self._w.execute("INSERT OR IGNORE INTO metric(key) VALUES (?)", (key,))
            mid = self._w.execute("SELECT id FROM metric WHERE key=?", (key,)).fetchone()[0]
            self._ids[key] = mid
        return mid

    def keys(self) -> list[str]:
        # copy() is a single atomic step, so the collector adding a metric mid-call
        # cannot raise "dictionary changed size during iteration".
        return sorted(self._ids.copy())

    def known(self, key: str) -> bool:
        return key in self._ids

    def get_meta(self, k: str, default=None):
        with self._reader() as db:
            row = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set_meta(self, k: str, v) -> None:
        with self._write_lock, self._w:
            self._w.execute(
                "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, str(v)),
            )

    # -------------------------------------------------------------------- writes
    def write(self, ts: int, values: dict[str, float]) -> None:
        if not values:
            return
        rows = [(self.metric_id(k), int(ts), float(v)) for k, v in values.items()]
        with self._write_lock, self._w:
            self._w.executemany(
                "INSERT OR REPLACE INTO sample(metric_id, ts, value) VALUES (?,?,?)", rows
            )

    def upsert_rollups(self, rows) -> int:
        """rows: iterable of (key, bucket, total, n, lo, hi). Used by the CSV importer."""
        payload = [(self.metric_id(k), int(b), float(tot), int(n), float(lo), float(hi))
                   for k, b, tot, n, lo, hi in rows]
        if not payload:
            return 0
        with self._write_lock, self._w:
            self._w.executemany(
                """
                INSERT INTO sample_1m(metric_id, bucket, total, n, lo, hi)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(metric_id, bucket) DO UPDATE SET
                    total = total + excluded.total,
                    n     = n + excluded.n,
                    lo    = min(lo, excluded.lo),
                    hi    = max(hi, excluded.hi)
                """,
                payload,
            )
        return len(payload)

    # --------------------------------------------------------------- maintenance
    def rollup(self, now: int | None = None) -> int:
        """Fold every completed minute since the watermark into sample_1m."""
        now = int(now or time.time())
        cutoff = (now // MINUTE) * MINUTE
        mark = self.get_meta("rollup_watermark")
        if mark is None:
            row = self._w.execute("SELECT min(ts) FROM sample").fetchone()
            start = (int(row[0]) // MINUTE) * MINUTE if row and row[0] is not None else cutoff
        else:
            start = int(mark)
        if start >= cutoff:
            return 0
        with self._write_lock, self._w:
            cur = self._w.execute(
                """
                INSERT INTO sample_1m(metric_id, bucket, total, n, lo, hi)
                SELECT metric_id, (ts/60)*60, sum(value), count(*), min(value), max(value)
                FROM sample WHERE ts >= ? AND ts < ?
                GROUP BY metric_id, (ts/60)*60
                ON CONFLICT(metric_id, bucket) DO UPDATE SET
                    total = excluded.total, n = excluded.n,
                    lo = excluded.lo, hi = excluded.hi
                """,
                (start, cutoff),
            )
            written = cur.rowcount
            self._w.execute(
                "INSERT INTO meta(k,v) VALUES('rollup_watermark',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(cutoff),),
            )
        return max(written, 0)

    def _prune_table(self, table: str, tcol: str, cutoff: int) -> int:
        """Delete expired rows in bounded batches, releasing the lock between each."""
        removed = 0
        while True:
            with self._write_lock, self._w:
                n = self._w.execute(
                    f"DELETE FROM {table} WHERE ({tcol}, metric_id) IN "
                    f"(SELECT {tcol}, metric_id FROM {table} WHERE {tcol} < ? LIMIT ?)",
                    (cutoff, PRUNE_CHUNK)).rowcount
            removed += max(n, 0)
            if n < PRUNE_CHUNK:
                return removed

    def prune(self, now: int | None = None) -> tuple[int, int]:
        now = int(now or time.time())
        raw = self._prune_table("sample", "ts", now - self.raw_retention)
        agg = self._prune_table("sample_1m", "bucket", now - self.rollup_retention)
        return raw, agg

    def close(self) -> None:
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                break
        with self._write_lock:
            self._w.close()

    def db_size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    # -------------------------------------------------------------------- reads
    def _edges(self, db, table: str, tcol: str, mid: int) -> tuple:
        """First and last timestamp for one metric - an index seek, not a table scan."""
        sql = f"SELECT {tcol} FROM {table} WHERE metric_id=? ORDER BY {tcol}"
        lo = db.execute(sql + " LIMIT 1", (mid,)).fetchone()
        hi = db.execute(sql + " DESC LIMIT 1", (mid,)).fetchone()
        return (lo[0] if lo else None, hi[0] if hi else None)

    def coverage(self, ttl: int = 5) -> dict:
        """Extent of stored history. Polled once a second by every open dashboard, so it
        reads a single metric's index edges and caches the answer briefly."""
        now = time.time()
        cached = getattr(self, "_coverage_cache", None)
        if cached and now - cached[0] < ttl:
            return cached[1]

        with self._reader() as db:
            row = db.execute("SELECT min(id) FROM metric").fetchone()
            mid = row[0] if row else None
            if mid is None:
                result = {"first": None, "last": None, "raw_first": None, "raw_last": None}
            else:
                raw = self._edges(db, "sample", "ts", mid)
                agg = self._edges(db, "sample_1m", "bucket", mid)
                starts = [v for v in (raw[0], agg[0]) if v is not None]
                ends = [v for v in (raw[1], agg[1]) if v is not None]
                result = {
                    "first": min(starts) if starts else None,
                    "last": max(ends) if ends else None,
                    "raw_first": raw[0],
                    "raw_last": raw[1],
                }
        self._coverage_cache = (now, result)
        return result

    def latest(self, keys: list[str] | None = None, max_age: int = 900) -> dict[str, dict]:
        """Most recent raw value per metric."""
        now = int(time.time())
        sql = (
            "SELECT m.key, s.ts, s.value FROM metric m "
            "JOIN sample s ON s.metric_id = m.id AND s.ts = ("
            "  SELECT max(ts) FROM sample WHERE metric_id = m.id AND ts >= ?)"
        )
        params: list = [now - max_age]
        if keys:
            sql += f" WHERE m.key IN ({','.join('?' * len(keys))})"
            params += keys
        with self._reader() as db:
            return {k: {"ts": ts, "value": v} for k, ts, v in db.execute(sql, params)}

    def pick_bucket(self, t0: int, t1: int, max_points: int) -> int:
        span = max(1, t1 - t0)
        raw_bucket = max(self.step, math.ceil(span / max(1, max_points)))
        if raw_bucket <= MINUTE:
            # snap to a whole number of poll intervals for stable bucket edges
            return max(self.step, (raw_bucket // self.step) * self.step)
        return int(math.ceil(raw_bucket / MINUTE) * MINUTE)

    def query(self, keys: list[str], t0: int, t1: int, max_points: int = 900,
              aggs: tuple[str, ...] = ("avg", "max")) -> dict:
        t0, t1 = int(t0), int(t1)
        keys = [k for k in keys if k in self._ids]
        bucket = self.pick_bucket(t0, t1, max_points)
        now = int(time.time())
        raw_floor = now - self.raw_retention

        if not keys:
            return {"from": t0, "to": t1, "bucket": bucket, "source": "none",
                    "t": [], "series": {}}

        ids = {self._ids[k]: k for k in keys}
        id_list = ",".join(str(i) for i in ids)

        use_raw = (bucket < MINUTE and t0 >= raw_floor
                   and (t1 - t0) / self.step <= RAW_SCAN_SAMPLES)
        if not use_raw:
            # Rollup rows are one minute wide, so the output grid has to be too.
            bucket = int(math.ceil(max(bucket, MINUTE) / MINUTE) * MINUTE)

        grid_start = (t0 // bucket) * bucket
        n_points = int((t1 - grid_start) // bucket) + 1
        n_points = max(0, min(n_points, max_points * 4))
        t_axis = [grid_start + i * bucket for i in range(n_points)]
        idx = {t: i for i, t in enumerate(t_axis)}

        acc = {k: {"sum": [0.0] * n_points, "n": [0] * n_points,
                   "lo": [None] * n_points, "hi": [None] * n_points} for k in keys}

        def merge(key, b, total, n, lo, hi):
            i = idx.get((b // bucket) * bucket)
            if i is None or not n:
                return
            a = acc[key]
            a["sum"][i] += total
            a["n"][i] += n
            a["lo"][i] = lo if a["lo"][i] is None else min(a["lo"][i], lo)
            a["hi"][i] = hi if a["hi"][i] is None else max(a["hi"][i], hi)

        source = "raw" if use_raw else "rollup"

        with self._reader() as db:
            if use_raw:
                sql = (f"SELECT metric_id, (ts/{bucket})*{bucket}, sum(value), count(*), "
                       f"min(value), max(value) FROM sample "
                       f"WHERE metric_id IN ({id_list}) AND ts >= ? AND ts <= ? "
                       f"GROUP BY metric_id, (ts/{bucket})*{bucket}")
                for mid, b, total, n, lo, hi in db.execute(sql, (grid_start, t1)):
                    merge(ids[mid], b, total, n, lo, hi)
            else:
                sql = (f"SELECT metric_id, (bucket/{bucket})*{bucket}, sum(total), sum(n), "
                       f"min(lo), max(hi) FROM sample_1m "
                       f"WHERE metric_id IN ({id_list}) AND bucket >= ? AND bucket <= ? "
                       f"GROUP BY metric_id, (bucket/{bucket})*{bucket}")
                for mid, b, total, n, lo, hi in db.execute(sql, (grid_start, t1)):
                    merge(ids[mid], b, total, n, lo, hi)

                # The current minute has not been rolled up yet; take its tail from raw.
                mark = db.execute(
                    "SELECT v FROM meta WHERE k='rollup_watermark'").fetchone()
                tail = max(grid_start, int(mark[0]) if mark else 0, raw_floor)
                if tail <= t1:
                    sql = (f"SELECT metric_id, (ts/{bucket})*{bucket}, sum(value), "
                           f"count(*), min(value), max(value) FROM sample "
                           f"WHERE metric_id IN ({id_list}) AND ts >= ? AND ts <= ? "
                           f"GROUP BY metric_id, (ts/{bucket})*{bucket}")
                    rows = list(db.execute(sql, (tail, t1)))
                    if rows:
                        source = "mixed"
                    for mid, b, total, n, lo, hi in rows:
                        merge(ids[mid], b, total, n, lo, hi)

        series = {}
        for k, a in acc.items():
            out = {}
            if "avg" in aggs:
                out["avg"] = [round(a["sum"][i] / a["n"][i], 2) if a["n"][i] else None
                              for i in range(n_points)]
            if "max" in aggs:
                out["max"] = [round(v, 2) if v is not None else None for v in a["hi"]]
            if "min" in aggs:
                out["min"] = [round(v, 2) if v is not None else None for v in a["lo"]]
            series[k] = out

        return {"from": t0, "to": t1, "bucket": bucket, "source": source,
                "t": t_axis, "series": series}

    def series_points(self, key: str, t0: int, t1: int):
        """Ordered (ts, value, peak) rows, using raw data when it covers the range."""
        mid = self._ids.get(key)
        if mid is None:
            return [], MINUTE
        now = int(time.time())
        with self._reader() as db:
            if t0 >= now - self.raw_retention:
                rows = db.execute(
                    "SELECT ts, value, value FROM sample "
                    "WHERE metric_id=? AND ts BETWEEN ? AND ? ORDER BY ts",
                    (mid, t0, t1)).fetchall()
                return rows, self.step
            rows = db.execute(
                "SELECT bucket, total/n, hi FROM sample_1m "
                "WHERE metric_id=? AND bucket BETWEEN ? AND ? ORDER BY bucket",
                (mid, t0, t1)).fetchall()
        return rows, MINUTE

    def summarize(self, keys: list[str], t0: int, t1: int, threshold: float,
                  max_gap: int = 120) -> dict:
        """Per-metric min/avg/max plus contiguous episodes above `threshold`.

        Two phases, because scanning every raw sample of a 12-hour window on a machine
        that is busy gaming is not acceptable. Statistics come from the rollups, which
        carry exact sum/count/min/max, and raw samples are only read back inside the
        minutes the rollups already flagged as hot - normally none at all. Timing of an
        excursion therefore stays second-accurate at a fraction of the cost.
        """
        t0, t1 = int(t0), int(t1)
        out = {}
        with self._reader() as db:
            mark = db.execute("SELECT v FROM meta WHERE k='rollup_watermark'").fetchone()
            # [t0, split) is rolled up, [split, t1] is still only in raw samples.
            split = min(max(int(mark[0]) if mark else 0, t0), t1)

            for key in keys:
                mid = self._ids.get(key)
                if mid is None:
                    continue
                summary = self._summarize_one(db, mid, t0, t1, split, threshold, max_gap)
                if summary is None and split > t0:
                    summary = self._summarize_one(db, mid, t0, t1, t0, threshold, max_gap)
                if summary is None and split < t1:
                    # Rollup-only history, e.g. anything the CSV importer brought in.
                    summary = self._summarize_one(db, mid, t0, t1, t1, threshold, max_gap)
                if summary is not None:
                    out[key] = summary
        return out

    def _summarize_one(self, db, mid: int, t0: int, t1: int, split: int,
                       threshold: float, max_gap: int) -> dict | None:
        raw_floor = int(time.time()) - self.raw_retention
        lo = hi = peak_ts = None
        total = 0.0
        n = 0
        exact = True                              # every excursion timed from raw samples
        hits: list[tuple[int, float, int]] = []   # (timestamp, value, seconds covered)

        if split > t0:
            row = db.execute(
                "SELECT min(lo), sum(total), sum(n) FROM sample_1m "
                "WHERE metric_id=? AND bucket>=? AND bucket<?", (mid, t0, split)).fetchone()
            if row and row[2]:
                lo, total, n = row[0], row[1], row[2]
                # A lone max() lets SQLite hand back the bare column from that same row.
                hi, peak_ts = db.execute(
                    "SELECT max(hi), bucket FROM sample_1m "
                    "WHERE metric_id=? AND bucket>=? AND bucket<?",
                    (mid, t0, split)).fetchone()
                # That timestamp is only the minute the peak fell in; find the second.
                second = db.execute(
                    "SELECT ts FROM sample WHERE metric_id=? AND ts>=? AND ts<? "
                    "AND value>=? ORDER BY ts LIMIT 1",
                    (mid, peak_ts, peak_ts + MINUTE, hi)).fetchone()
                if second:
                    peak_ts = second[0]

            hot = db.execute(
                "SELECT bucket, hi FROM sample_1m "
                "WHERE metric_id=? AND bucket>=? AND bucket<? AND hi>=? ORDER BY bucket",
                (mid, t0, split, threshold)).fetchall()
            for start, end, peak in _runs(hot):
                refined = db.execute(
                    "SELECT ts, value FROM sample WHERE metric_id=? "
                    "AND ts>=? AND ts<? AND value>=? ORDER BY ts",
                    (mid, start, end, threshold)).fetchall() if start >= raw_floor else []
                if refined:
                    hits.extend((ts, value, self.step) for ts, value in refined)
                else:
                    # Raw pruned, or history that only ever arrived as rollups (CSV
                    # import): one minute is the best resolution available.
                    hits.append((start, peak, end - start))
                    exact = False

        if t1 >= split:
            row = db.execute(
                "SELECT min(value), sum(value), count(*) FROM sample "
                "WHERE metric_id=? AND ts>=? AND ts<=?", (mid, split, t1)).fetchone()
            if row and row[2]:
                lo = row[0] if lo is None else min(lo, row[0])
                total += row[1]
                n += row[2]
                peak, at = db.execute(
                    "SELECT max(value), ts FROM sample "
                    "WHERE metric_id=? AND ts>=? AND ts<=?", (mid, split, t1)).fetchone()
                if hi is None or peak > hi:
                    hi, peak_ts = peak, at
                hits.extend(
                    (ts, value, self.step) for ts, value in db.execute(
                        "SELECT ts, value FROM sample WHERE metric_id=? "
                        "AND ts>=? AND ts<=? AND value>=? ORDER BY ts",
                        (mid, split, t1, threshold)))

        if not n:
            return None

        episodes = []
        seconds_above = 0
        start = last = last_step = None
        ep_peak = 0.0
        for ts, value, step in hits:
            seconds_above += step
            if start is None or ts - last > max_gap:
                if start is not None:
                    episodes.append({"start": start, "end": last + last_step,
                                     "peak": round(ep_peak, 1)})
                start, ep_peak = ts, value
            ep_peak = max(ep_peak, value)
            last, last_step = ts, step
        if start is not None:
            episodes.append({"start": start, "end": last + last_step,
                             "peak": round(ep_peak, 1)})

        return {
            "min": round(lo, 1),
            "avg": round(total / n, 1),
            "max": round(hi, 1),
            "peak_ts": peak_ts,
            "seconds_above": seconds_above,
            "episodes": episodes[-50:],
            "episode_count": len(episodes),
            "samples": n,
            "resolution": self.step if exact else MINUTE,
        }
