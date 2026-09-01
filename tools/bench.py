"""Measure the cost of the pieces that run continuously on the gaming rig.

    python tools/bench.py [--hours 12] [--metrics 42]

Builds a worst-case database (every metric, 1 Hz, for the full selectable range) and
times the queries the dashboard issues, plus the per-poll collect path.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import statistics
import tempfile
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rigmon import lhm  # noqa: E402
from rigmon.server import RANGES  # noqa: E402
from rigmon.storage import Store  # noqa: E402


def rss_mb() -> float:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / 1e6 if sys.platform == "darwin" else raw / 1e3


def timeit(fn, repeat=5):
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - t0) * 1000)
        del result
    return statistics.median(samples), max(samples)


def build_db(path: Path, hours: float, n_metrics: int, step: int) -> Store:
    store = Store(path, raw_retention_days=1, rollup_retention_days=30, step=step)
    keys = [f"metric.{i:02d}" for i in range(n_metrics)]
    ids = [store.metric_id(k) for k in keys]
    now = int(time.time()) // 60 * 60
    start = now - int(hours * 3600)

    t0 = time.perf_counter()
    batch = []
    conn = store._w
    for ts in range(start, now, step):
        v = 40.0 + (ts % 97) * 0.5
        batch.extend((mid, ts, v + i * 0.1) for i, mid in enumerate(ids))
        if len(batch) >= 200_000:
            with conn:
                conn.executemany("INSERT OR REPLACE INTO sample VALUES (?,?,?)", batch)
            batch.clear()
    if batch:
        with conn:
            conn.executemany("INSERT OR REPLACE INTO sample VALUES (?,?,?)", batch)
    write_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    store.rollup(now)
    rollup_s = time.perf_counter() - t0

    rows = conn.execute("SELECT count(*) FROM sample").fetchone()[0]
    aggs = conn.execute("SELECT count(*) FROM sample_1m").fetchone()[0]
    print(f"  built {rows:,} raw + {aggs:,} rollup rows in {write_s:.1f}s "
          f"(rollup pass {rollup_s * 1000:.0f} ms)")
    print(f"  on disk: {store.db_size_bytes() / 1e6:.0f} MB")
    return store, keys, now


def synthetic_payload(n_sensors: int) -> str:
    """A data.json the size of the real rig's (163 sensors)."""
    def leaf(i):
        return {"id": i, "Text": f"Sensor {i}", "Type": "Temperature",
                "SensorId": f"/hw/0/temperature/{i}", "Min": "40.0 °C",
                "Value": f"{40 + i % 50}.5 °C", "Max": "90.0 °C",
                "ImageURL": "", "Children": []}
    groups = [{"Text": "Temperatures", "Min": "", "Value": "", "Max": "", "ImageURL": "",
               "Children": [leaf(i) for i in range(n_sensors)]}]
    return json.dumps({"id": 0, "Text": "Sensor", "Children": [
        {"id": 1, "Text": "PC", "Children": [
            {"id": 2, "Text": "Hardware", "Children": groups}]}]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12)
    ap.add_argument("--metrics", type=int, default=42)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--sensors", type=int, default=163)
    args = ap.parse_args()

    print(f"\nWorst case: {args.metrics} metrics at {args.step} Hz for {args.hours} h "
          f"(the longest selectable range)\n")
    tmp = tempfile.TemporaryDirectory()
    store, keys, now = build_db(Path(tmp.name) / "b.db", args.hours, args.metrics, args.step)
    print(f"  RSS after build: {rss_mb():.0f} MB\n")

    print("Series query (what every chart refresh costs, all metrics in one request):")
    for r in RANGES:
        span = r["seconds"]
        med, worst = timeit(lambda: store.query(keys, now - span, now, 1100), repeat=3)
        res = store.query(keys, now - span, now, 1100)
        size = len(json.dumps(res, separators=(",", ":")))
        print(f"  {r['label']:>9}  {med:7.1f} ms (max {worst:6.1f})  "
              f"source={res['source']:<7} bucket={res['bucket']:>4}s  "
              f"points={len(res['t']):>4}  json={size / 1024:5.0f} KB")

    print("\nSummary query (85 °C excursion scan, every 15 s):")
    print("  'quiet' = nothing crossed the threshold, the normal case.")
    print("  'hot'   = above the threshold for the entire window, the pathological case.")
    for r in RANGES:
        span = r["seconds"]
        quiet, _ = timeit(lambda: store.summarize(keys, now - span, now, 999.0), repeat=3)
        hot, worst = timeit(lambda: store.summarize(keys, now - span, now, 85.0), repeat=3)
        print(f"  {r['label']:>9}  quiet {quiet:6.1f} ms   hot {hot:7.1f} ms (max {worst:6.1f})")

    print("\nStatus/latest endpoints (every second):")
    med, _ = timeit(lambda: store.coverage(ttl=0), repeat=20)
    print(f"  coverage()      {med:7.2f} ms")
    med, _ = timeit(lambda: store.db_size_bytes(), repeat=20)
    print(f"  db_size_bytes() {med:7.2f} ms")

    print("\nCollector per-poll work (excludes LibreHardwareMonitor's own time):")
    payload = synthetic_payload(args.sensors)
    print(f"  payload {len(payload) / 1024:.0f} KB, {args.sensors} sensors")
    med, _ = timeit(lambda: lhm.detect_decimal_separator(payload), repeat=20)
    print(f"  locale sniff    {med:7.2f} ms")
    med, _ = timeit(lambda: lhm.flatten(json.loads(payload)), repeat=20)
    print(f"  parse + flatten {med:7.2f} ms")
    sensors = lhm.flatten(json.loads(payload))
    med, _ = timeit(lambda: store.write(int(time.time()), {k: 50.0 for k in keys}), repeat=20)
    print(f"  sqlite write    {med:7.2f} ms")

    print("\nMaintenance:")
    med, _ = timeit(lambda: store.rollup(now), repeat=3)
    print(f"  rollup()        {med:7.2f} ms")
    # Steady state: one prune cycle's worth of expired rows, which is what actually runs.
    t0 = time.perf_counter()
    store.prune(now - args.hours * 3600 + store.raw_retention + 900)
    print(f"  prune() steady state (15 min of expiry): "
          f"{(time.perf_counter() - t0) * 1000:.0f} ms")
    t0 = time.perf_counter()
    removed = store.prune(now + 2 * 86400)   # force everything to be expired at once
    prune_ms = (time.perf_counter() - t0) * 1000
    print(f"  prune() worst case (whole DB expired at once): {prune_ms:.0f} ms, "
          f"{removed[0]:,} raw rows")
    print(f"  WAL after prune: "
          f"{os.path.getsize(str(store.path) + '-wal') / 1e6 if os.path.exists(str(store.path) + '-wal') else 0:.0f} MB")

    gc.collect()
    print(f"\n  peak RSS for this whole benchmark: {rss_mb():.0f} MB")
    store.close()
    tmp.cleanup()


if __name__ == "__main__":
    main()
