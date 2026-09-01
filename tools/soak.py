"""Run the whole stack under a realistic client load and watch it for drift.

    python tools/soak.py [--minutes 4] [--clients 3]

Starts the fake LibreHardwareMonitor, seeds a full 12-hour database so every query hits
the worst case, then hammers the server the way open dashboards do while sampling the
server process's memory, thread count and file descriptors. Answers "does it ramp?".
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
import time
import http.client
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rigmon.storage import Store  # noqa: E402

LHM_PORT = 8098
APP_PORT = 8099


def seed(db: Path, hours: int, metrics: int):
    store = Store(db, raw_retention_days=1, rollup_retention_days=30, step=1)
    keys = [f"metric.{i:02d}" for i in range(metrics)]
    ids = [store.metric_id(k) for k in keys]
    now = int(time.time()) // 60 * 60
    batch = []
    for ts in range(now - hours * 3600, now, 1):
        batch.extend((mid, ts, 40.0 + (ts % 97) * 0.5 + i * 0.1) for i, mid in enumerate(ids))
        if len(batch) >= 200_000:
            with store._w:
                store._w.executemany("INSERT OR REPLACE INTO sample VALUES (?,?,?)", batch)
            batch.clear()
    if batch:
        with store._w:
            store._w.executemany("INSERT OR REPLACE INTO sample VALUES (?,?,?)", batch)
    store.rollup(now)
    store.close()


def sample_process(pid: int) -> dict:
    def run(cmd):
        return subprocess.run(cmd, capture_output=True, text=True, shell=True).stdout
    rss = run(f"ps -o rss= -p {pid}").strip()
    threads = run(f"ps -M -p {pid} | tail -n +2 | wc -l").strip()
    fds = run(f"lsof -p {pid} 2>/dev/null | tail -n +2 | wc -l").strip()
    cpu = run(f"ps -o time= -p {pid}").strip()
    return {"rss_mb": int(rss or 0) / 1024, "threads": int(threads or 0),
            "fds": int(fds or 0), "cpu_time": cpu}


def cpu_seconds(pid: int) -> float:
    out = subprocess.run(f"ps -o time= -p {pid}", capture_output=True, text=True,
                         shell=True).stdout.strip()
    if not out:
        return 0.0
    parts = out.replace("-", ":").split(":")
    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[-3] * 3600 + parts[-2] * 60 + parts[-1]


def client(stop: threading.Event, errors: list, keepalive: bool):
    """One open dashboard on the 12-hour view.

    A browser reuses one connection; `keepalive=False` reconnects per request instead,
    which is the worst case for anything held per connection.
    """
    tick = 0
    conn = None
    while not stop.is_set():
        try:
            paths = ["/api/latest", "/api/status"]
            if tick % 15 == 0:
                paths += ["/api/series?range=43200&points=1100", "/api/summary?range=43200"]
            for path in paths:
                if conn is None:
                    conn = http.client.HTTPConnection("127.0.0.1", APP_PORT, timeout=20)
                conn.request("GET", path)
                conn.getresponse().read()
                if not keepalive:
                    conn.close()
                    conn = None
        except Exception as e:
            errors.append(repr(e))
            if conn:
                conn.close()
            conn = None
        tick += 1
        stop.wait(1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=4)
    ap.add_argument("--clients", type=int, default=3)
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--metrics", type=int, default=42)
    ap.add_argument("--churn", action="store_true",
                    help="reconnect on every request instead of reusing the connection")
    args = ap.parse_args()

    tmp = tempfile.TemporaryDirectory()
    db = Path(tmp.name) / "soak.db"
    print(f"seeding {args.hours} h of {args.metrics} metrics at 1 Hz ...")
    seed(db, args.hours, args.metrics)
    print(f"  {db.stat().st_size / 1e6:.0f} MB\n")

    cfg = Path(tmp.name) / "config.json"
    cfg.write_text(json.dumps({
        "lhm_url": f"http://127.0.0.1:{LHM_PORT}/data.json",
        "poll_interval": 1, "listen_host": "127.0.0.1", "listen_port": APP_PORT,
        "db_path": str(db), "raw_retention_days": 1, "rollup_retention_days": 30,
    }))

    lhm = subprocess.Popen([sys.executable, "tools/fake_lhm.py", "--port", str(LHM_PORT)],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    app = subprocess.Popen([sys.executable, "-m", "rigmon", "--config", str(cfg), "serve"],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(3)
    if app.poll() is not None:
        print("server died:", app.stderr.read().decode())
        return

    stop = threading.Event()
    errors: list = []
    threads = [threading.Thread(target=client, args=(stop, errors, not args.churn),
                                daemon=True) for _ in range(args.clients)]
    for t in threads:
        t.start()

    mode = "reconnect per request" if args.churn else "keep-alive"
    print(f"{args.clients} dashboards on the 12 h view ({mode}), sampling every 15 s")
    print(f"{'elapsed':>8} {'RSS MB':>8} {'threads':>8} {'fds':>6} {'CPU %':>7} {'errors':>7}")
    rows = []
    t_start = time.time()
    cpu_prev, wall_prev = cpu_seconds(app.pid), time.time()
    deadline = t_start + args.minutes * 60
    while time.time() < deadline:
        time.sleep(15)
        snap = sample_process(app.pid)
        now, cpu_now = time.time(), cpu_seconds(app.pid)
        pct = (cpu_now - cpu_prev) / (now - wall_prev) * 100
        cpu_prev, wall_prev = cpu_now, now
        rows.append((snap["rss_mb"], snap["threads"], snap["fds"], pct))
        print(f"{now - t_start:7.0f}s {snap['rss_mb']:8.1f} {snap['threads']:8d} "
              f"{snap['fds']:6d} {pct:7.1f} {len(errors):7d}")

    stop.set()
    time.sleep(1.5)
    app.terminate()
    lhm.terminate()

    if rows:
        first, last = rows[0], rows[-1]
        print(f"\n  RSS      {first[0]:.1f} MB -> {last[0]:.1f} MB  "
              f"(peak {max(r[0] for r in rows):.1f})")
        print(f"  threads  {first[1]} -> {last[1]}  (peak {max(r[1] for r in rows)})")
        print(f"  fds      {first[2]} -> {last[2]}  (peak {max(r[2] for r in rows)})")
        busy = [r[3] for r in rows[1:]]
        if busy:
            print(f"  CPU      mean {sum(busy) / len(busy):.1f}% of one core "
                  f"(peak {max(busy):.1f}%) with {args.clients} dashboards open")
    print(f"  errors   {len(errors)}" + (f"  {errors[:3]}" if errors else ""))
    tmp.cleanup()


if __name__ == "__main__":
    main()
