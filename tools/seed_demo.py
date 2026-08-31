"""Fill the database with plausible history so the dashboard can be exercised offline.

    python tools/seed_demo.py --hours 30

Purely a development aid - it writes the same metric keys the collector would.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rigmon import config as config_mod  # noqa: E402
from rigmon.storage import Store  # noqa: E402

SYS_FANS = ["fan.sys1", "fan.sys2", "fan.sys3", "fan.sys4", "fan.sys5", "fan.sys6"]


def simulate(hours: float, step: int):
    now = int(time.time())
    start = now - int(hours * 3600)
    cpu_t, gpu_t, hot_t, vram_t, vrm_t, board_t = 40, 35, 42, 38, 34, 31

    for ts in range(start, now + 1, step):
        h = (ts % 86400) / 3600.0
        # Long gaming sessions in the evening, idle overnight, a stress run each afternoon.
        session = 0.15
        if 14 <= h < 16:
            session = 0.98
        elif 19 <= h < 24:
            session = 0.55 + 0.45 * math.sin((h - 19) / 5 * math.pi)
        elif 9 <= h < 13:
            session = 0.35
        session = max(0.05, min(1.0, session + random.gauss(0, 0.02)))
        burst = 1.0 if random.random() < 0.01 else 0.0

        gpu_load = min(100, max(1, session * 92 + burst * 8 + random.gauss(0, 2.5)))
        cpu_load = min(100, max(1, session * 58 + burst * 30 + random.gauss(0, 3)))

        # Coefficients chosen so the ranges look like a real 7800X3D / 4070 Ti SUPER:
        # CPU peaks in the high 80s under an all-core load, GPU core stays near 70 and
        # the hot spot pushes past 85 only during the sustained afternoon stress run.
        targets = {
            "cpu_t": 36 + cpu_load * 0.47 + burst * 9,
            "gpu_t": 32 + gpu_load * 0.37,
            "hot_t": 32 + gpu_load * 0.37 + 10 + gpu_load * 0.09,
            "vram_t": 35 + gpu_load * 0.36,
            "vrm_t": 31 + cpu_load * 0.22,
            "board_t": 29 + (cpu_load + gpu_load) * 0.07,
        }
        cpu_t = min(89.0, cpu_t + (targets["cpu_t"] - cpu_t) * 0.3 + random.gauss(0, 0.35))
        gpu_t += (targets["gpu_t"] - gpu_t) * 0.12 + random.gauss(0, 0.12)
        hot_t += (targets["hot_t"] - hot_t) * 0.15 + random.gauss(0, 0.18)
        vram_t += (targets["vram_t"] - vram_t) * 0.10 + random.gauss(0, 0.1)
        vrm_t += (targets["vrm_t"] - vrm_t) * 0.07 + random.gauss(0, 0.08)
        board_t += (targets["board_t"] - board_t) * 0.04 + random.gauss(0, 0.05)

        def curve(temp, lo, hi, floor, ceiling):
            x = max(0.0, min(1.0, (temp - lo) / (hi - lo)))
            return round(floor + x * (ceiling - floor))

        cpu_fan = curve(cpu_t, 40, 85, 25, 100)
        sys_fan = curve(board_t, 30, 50, 20, 90)
        gpu_fan = 0 if gpu_t < 52 else curve(gpu_t, 52, 83, 30, 100)

        values = {
            "cpu.temp": cpu_t, "cpu.temp_ccd1": cpu_t + 2.4,
            "gpu.temp": gpu_t, "gpu.temp_hotspot": hot_t, "gpu.temp_vram": vram_t,
            "board.temp_vrm": vrm_t, "board.temp_sys": board_t,
            "board.temp_chipset": board_t + 8, "board.temp_socket": cpu_t - 1,
            "cpu.load": cpu_load, "cpu.load_max": min(100, cpu_load * 1.6),
            "gpu.load": gpu_load, "gpu.load_memctl": gpu_load * 0.6,
            "ram.load": 38 + session * 24, "gpu.vram_load": 20 + gpu_load * 0.5,
            "fan.cpu": cpu_fan, "fan.pump": min(100, cpu_fan + 12),
            "fan.gpu1": gpu_fan, "fan.gpu2": gpu_fan,
            "fan.ezconnect": min(100, sys_fan + 10), "fan.chipset": 0,
            "cpu.power": 25 + cpu_load * 0.9, "gpu.power": 30 + gpu_load * 2.6,
            "cpu.clock": 3400 + cpu_load * 17, "gpu.clock": 900 + gpu_load * 17,
            "rpm.cpu": cpu_fan * 21 + 120, "rpm.gpu1": gpu_fan * 29,
        }
        for i, key in enumerate(SYS_FANS):
            values[key] = max(0, min(100, sys_fan + (i - 2) * 3))
            values[key.replace("fan.", "rpm.")] = values[key] * 20 + (110 if values[key] else 0)
        if gpu_load > 25:
            values["sys.fps"] = 55 + gpu_load * 0.9

        yield ts, values


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=30)
    ap.add_argument("--step", type=int, default=None, help="seconds between samples")
    ap.add_argument("--config")
    args = ap.parse_args()

    cfg = config_mod.load(args.config)
    step = args.step or cfg.poll_interval
    store = Store(cfg.db_file, cfg.raw_retention_days, cfg.rollup_retention_days, step)

    now = int(time.time())
    raw_floor = now - store.raw_retention
    buckets: dict[tuple[str, int], list] = {}
    raw_rows = 0

    for ts, values in simulate(args.hours, step):
        if ts >= raw_floor:
            store.write(ts, values)
            raw_rows += 1
        bucket = ts // 60 * 60
        for k, v in values.items():
            slot = buckets.get((k, bucket))
            if slot is None:
                buckets[(k, bucket)] = [v, 1, v, v]
            else:
                slot[0] += v
                slot[1] += 1
                slot[2] = min(slot[2], v)
                slot[3] = max(slot[3], v)

    store.upsert_rollups((k, b, v[0], v[1], v[2], v[3]) for (k, b), v in buckets.items())
    store.set_meta("rollup_watermark", now // 60 * 60)
    print(f"seeded {args.hours}h: {raw_rows:,} raw timestamps, {len(buckets):,} minute buckets, "
          f"db now {store.db_size_bytes()/1e6:.1f} MB")


if __name__ == "__main__":
    main()
