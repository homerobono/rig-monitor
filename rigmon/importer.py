"""Backfill history from LibreHardwareMonitor's own CSV logs.

The log's first header row holds sensor ids and the second holds sensor names, which is
exactly what the resolver needs, so imported columns land on the same metric keys as live
data. Imports are written straight into the 1-minute rollup table (raw retention is short).
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import re
import time
from datetime import datetime
from pathlib import Path

from .lhm import Resolver, Sensor, detect_decimal_separator, parse_number
from .storage import Store

_ID_TYPE = {
    "temperature": "Temperature", "load": "Load", "control": "Control", "fan": "Fan",
    "power": "Power", "clock": "Clock", "voltage": "Voltage", "factor": "Factor",
    "smalldata": "SmallData", "throughput": "Throughput", "data": "Data",
    "level": "Level", "current": "Current", "frequency": "Frequency", "energy": "Energy",
}
_FILE_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_TS_FORMATS = ("%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")


def _sensors_from_header(ids: list[str], names: list[str]) -> list[Sensor]:
    out = []
    for sid, name in zip(ids, names):
        sid, name = sid.strip(), name.strip().strip('"')
        if not sid.startswith("/"):
            continue
        parts = sid.strip("/").split("/")
        stype = _ID_TYPE.get(parts[-2].lower(), parts[-2].title()) if len(parts) >= 2 else "Unknown"
        hardware = parts[0] if parts else ""
        out.append(Sensor(sid, stype, name, hardware, None, ""))
    return out


def _timestamp_parser(path: Path, sample: str):
    """LHM writes timestamps in the machine's locale; the filename date disambiguates."""
    hint = _FILE_DATE.search(path.name)
    candidates = list(_TS_FORMATS)
    if hint:
        y, m, d = (int(x) for x in hint.groups())
        for fmt in _TS_FORMATS:
            try:
                dt = datetime.strptime(sample, fmt)
            except ValueError:
                continue
            if (dt.year, dt.month, dt.day) == (y, m, d):
                candidates = [fmt]
                break

    def parse(text: str) -> float | None:
        for fmt in candidates:
            try:
                return time.mktime(datetime.strptime(text, fmt).timetuple())
            except ValueError:
                continue
        return None

    return parse


def _fingerprint(path: Path) -> str:
    st = path.stat()
    return hashlib.sha1(f"{path.name}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]


def import_csv(store: Store, path: Path, force: bool = False, flush_every: int = 20000) -> dict:
    key = f"import:{_fingerprint(path)}"
    if not force and store.get_meta(key):
        return {"file": path.name, "skipped": True, "rows": 0, "buckets": 0}

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            ids = next(reader)
            names = next(reader)
        except StopIteration:
            return {"file": path.name, "skipped": True, "rows": 0, "buckets": 0,
                    "error": "empty file"}

        sensors = _sensors_from_header(ids, names)
        bindings = Resolver().bind(sensors)
        col_of = {sid: i for i, sid in enumerate(s.strip() for s in ids)}
        cols = [(k, col_of[sid]) for k, sid in bindings.items() if sid in col_of]
        if not cols:
            return {"file": path.name, "skipped": True, "rows": 0, "buckets": 0,
                    "error": "no recognised sensor columns"}

        first = next(reader, None)
        if first is None:
            return {"file": path.name, "skipped": True, "rows": 0, "buckets": 0}
        parse_ts = _timestamp_parser(path, first[0].strip())

        # Sniff the locale from already-split fields joined by spaces: joining them back
        # with commas would make the CSV's own separators look like decimal commas.
        head = [first]
        for _ in range(19):
            row = next(reader, None)
            if row is None:
                break
            head.append(row)
        decimal = detect_decimal_separator(" ".join(" ".join(r[1:]) for r in head))

        acc: dict[tuple[str, int], list] = {}
        rows = written = 0

        def flush():
            nonlocal acc
            store.upsert_rollups(
                (k, b, v[0], v[1], v[2], v[3]) for (k, b), v in acc.items()
            )
            acc = {}

        for row in itertools.chain(head, reader):
            ts = parse_ts(row[0].strip()) if row else None
            if ts is None:
                continue
            bucket = int(ts) // 60 * 60
            rows += 1
            for k, idx in cols:
                if idx >= len(row):
                    continue
                val = parse_number(row[idx], decimal)
                if val is None:
                    continue
                slot = acc.get((k, bucket))
                if slot is None:
                    acc[(k, bucket)] = [val, 1, val, val]
                else:
                    slot[0] += val
                    slot[1] += 1
                    slot[2] = min(slot[2], val)
                    slot[3] = max(slot[3], val)
            if len(acc) >= flush_every:
                written += len(acc)
                flush()

        written += len(acc)
        flush()

    store.set_meta(key, int(time.time()))
    return {"file": path.name, "skipped": False, "rows": rows, "buckets": written,
            "metrics": len(cols)}


def import_paths(store: Store, paths: list[Path], force: bool = False):
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            found = sorted(p.rglob("*.csv"))
            if not found:
                yield {"file": str(p), "skipped": True, "rows": 0, "buckets": 0,
                       "error": "no .csv files found"}
            files.extend(found)
        elif p.is_file():
            files.append(p)
        else:
            yield {"file": str(p), "skipped": True, "rows": 0, "buckets": 0,
                   "error": "path does not exist"}
    for f in files:
        yield import_csv(store, f, force=force)
