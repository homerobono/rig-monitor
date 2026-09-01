"""Background poller: reads LibreHardwareMonitor on a fixed cadence and stores samples."""

from __future__ import annotations

import logging
import threading
import time

from . import lhm
from .config import Config
from .storage import Store

log = logging.getLogger("rigmon.collector")

ROLLUP_EVERY = 60
PRUNE_EVERY = 900
MAX_BACKOFF = 30


class Collector(threading.Thread):
    def __init__(self, cfg: Config, store: Store):
        super().__init__(name="collector", daemon=True)
        self.cfg = cfg
        self.store = store
        self.resolver = lhm.Resolver(cfg.extra_metrics, cfg.collect_all)
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.latest: dict[str, dict] = {}
        self.sensors: list[lhm.Sensor] = []
        self.ok = False
        self.last_ok: int | None = None
        self.last_error: str | None = None
        self.samples_written = 0
        self.polls = 0

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        with self._lock:
            return {
                "ok": self.ok,
                "last_ok": self.last_ok,
                "last_error": self.last_error,
                "poll_interval": self.cfg.poll_interval,
                "polls": self.polls,
                "samples_written": self.samples_written,
                "bound_metrics": len(self.resolver.bindings),
                "unmatched_metrics": list(self.resolver.unmatched),
                "sensor_count": len(self.sensors),
                "source": self.cfg.lhm_url,
            }

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return dict(self.latest)

    def sensor_list(self) -> list[dict]:
        with self._lock:
            sensors = list(self.sensors)
            bound = {sid: key for key, sid in self.resolver.bindings.items()}
        return [
            {"id": s.sensor_id, "type": s.type, "name": s.name, "hardware": s.hardware,
             "value": s.value, "unit": s.unit, "metric": bound.get(s.sensor_id)}
            for s in sensors
        ]

    def poll_once(self) -> int:
        sensors = lhm.fetch(self.cfg.lhm_url)
        values = self.resolver.read(sensors)
        ts = int(time.time())
        self.store.write(ts, values)
        with self._lock:
            self.sensors = sensors
            self.latest = {k: {"ts": ts, "value": v} for k, v in values.items()}
            self.ok = True
            self.last_ok = ts
            self.last_error = None
            self.polls += 1
            self.samples_written += len(values)
        return len(values)

    def run(self) -> None:
        interval = max(1, int(self.cfg.poll_interval))
        failures = 0
        next_rollup = time.time() + ROLLUP_EVERY
        next_prune = time.time() + 30

        while not self._stop.is_set():
            started = time.time()
            try:
                n = self.poll_once()
                if failures:
                    log.info("LibreHardwareMonitor reachable again (%d metrics)", n)
                failures = 0
            except lhm.LhmUnavailable as e:
                failures += 1
                with self._lock:
                    self.ok = False
                    self.last_error = str(e)
                if failures in (1, 5) or failures % 60 == 0:
                    log.warning("collector: %s", e)
            except Exception as e:  # keep the thread alive whatever happens
                failures += 1
                with self._lock:
                    self.ok = False
                    self.last_error = f"{type(e).__name__}: {e}"
                # Rate limited: a fault that repeats every second must not fill the disk
                # with tracebacks.
                if failures in (1, 5) or failures % 60 == 0:
                    log.exception("collector: unexpected failure")

            now = time.time()
            # Each deadline moves before the work runs, so a persistent failure retries
            # on the normal schedule instead of spinning.
            if now >= next_rollup:
                next_rollup = now + ROLLUP_EVERY
                try:
                    self.store.rollup(int(now))
                except Exception:
                    log.exception("collector: rollup failed")
            if now >= next_prune:
                next_prune = now + PRUNE_EVERY
                try:
                    removed_raw, removed_agg = self.store.prune(int(now))
                    if removed_raw or removed_agg:
                        log.info("pruned %d raw / %d rollup rows", removed_raw, removed_agg)
                except Exception:
                    log.exception("collector: prune failed")

            delay = interval if not failures else min(MAX_BACKOFF, interval * 2 ** min(failures, 4))
            elapsed = time.time() - started
            # Idle at least as long as the poll itself took. If the rig is loaded enough
            # that LibreHardwareMonitor answers slowly, this keeps us from issuing
            # back-to-back requests and never lets us use more than half of it.
            floor = min(elapsed, MAX_BACKOFF)
            self._stop.wait(max(0.05, floor, delay - elapsed))
