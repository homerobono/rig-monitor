"""Command line entry point: `python -m rigmon [serve|discover|import|check]`."""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import time
from pathlib import Path

from . import config as config_mod
from . import lhm, metrics
from .collector import Collector
from .server import serve
from .storage import Store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.0.1", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _open_store(cfg) -> Store:
    return Store(cfg.db_file, cfg.raw_retention_days, cfg.rollup_retention_days,
                 cfg.poll_interval)


def cmd_serve(args, cfg) -> int:
    store = _open_store(cfg)
    collector = Collector(cfg, store)
    collector.start()

    httpd = serve(cfg, store, collector)
    host = cfg.listen_host
    shown = _lan_ip() if host in ("0.0.0.0", "::") else host
    log = logging.getLogger("rigmon")
    log.info("polling      %s every %ds", cfg.lhm_url, cfg.poll_interval)
    log.info("database     %s", cfg.db_file)
    log.info("dashboard    http://%s:%d/  (also http://localhost:%d/)",
             shown, cfg.listen_port, cfg.listen_port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        collector.stop()
        httpd.server_close()
    return 0


def cmd_discover(args, cfg) -> int:
    try:
        sensors = lhm.fetch(cfg.lhm_url)
    except lhm.LhmUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    resolver = lhm.Resolver(cfg.extra_metrics, cfg.collect_all)
    bindings = resolver.bind(sensors)
    bound = {sid: key for key, sid in bindings.items()}

    width = max((len(s.sensor_id) for s in sensors), default=20)
    current_hw = None
    for s in sensors:
        if s.hardware != current_hw:
            current_hw = s.hardware
            print(f"\n=== {current_hw}")
        mark = f"  -> {bound[s.sensor_id]}" if s.sensor_id in bound else ""
        value = "-" if s.value is None else f"{s.value:g}"
        print(f"  {s.sensor_id:<{width}}  {s.type:<12} {s.name:<28} {value:>10} {s.unit:<5}{mark}")

    print(f"\n{len(sensors)} sensors, {len(bindings)} mapped to metrics.")
    if resolver.unmatched:
        print("unmapped metric keys: " + ", ".join(resolver.unmatched))
    return 0


def cmd_check(args, cfg) -> int:
    print(f"LibreHardwareMonitor : {cfg.lhm_url}")
    try:
        t0 = time.time()
        sensors = lhm.fetch(cfg.lhm_url)
        dt = (time.time() - t0) * 1000
    except lhm.LhmUnavailable as e:
        print(f"  FAILED  {e}")
        print("\n  Open LibreHardwareMonitor -> Options -> Remote Web Server -> Run,")
        print("  make sure the port matches, and run it as Administrator.")
        return 2

    resolver = lhm.Resolver(cfg.extra_metrics, cfg.collect_all)
    values = resolver.read(sensors)
    print(f"  OK      {len(sensors)} sensors in {dt:.0f} ms, {len(values)} metrics mapped")
    for group in ("temp", "load", "fan"):
        items = [(m.label, values[m.key], m.unit) for m in metrics.CATALOG
                 if m.group == group and m.key in values]
        if items:
            print(f"  {metrics.GROUPS[group]['label']:<12} " +
                  ", ".join(f"{n} {v:g}{u}" for n, v, u in items[:8]))
    if resolver.unmatched:
        print(f"  missing : {', '.join(resolver.unmatched)}")
    store = _open_store(cfg)
    cov = store.coverage()
    print(f"\nDatabase             : {cfg.db_file} ({store.db_size_bytes()/1e6:.1f} MB)")
    if cov["first"]:
        span = (cov["last"] - cov["first"]) / 3600
        print(f"  history : {time.strftime('%Y-%m-%d %H:%M', time.localtime(cov['first']))}"
              f" -> {time.strftime('%Y-%m-%d %H:%M', time.localtime(cov['last']))}"
              f"  ({span:.1f} h)")
    else:
        print("  history : empty")
    return 0


def cmd_import(args, cfg) -> int:
    from .importer import import_paths

    store = _open_store(cfg)
    paths = [Path(p) for p in args.paths]
    totals = {"files": 0, "skipped": 0, "rows": 0, "buckets": 0}
    for result in import_paths(store, paths, force=args.force):
        totals["files"] += 1
        if result.get("skipped"):
            totals["skipped"] += 1
            if result.get("error"):
                print(f"  skip {result['file']}: {result['error']}")
        else:
            totals["rows"] += result["rows"]
            totals["buckets"] += result["buckets"]
        if totals["files"] % 25 == 0:
            print(f"  {totals['files']} files, {totals['rows']:,} rows ...")
    print(f"imported {totals['files'] - totals['skipped']} files "
          f"({totals['skipped']} skipped), {totals['rows']:,} rows -> "
          f"{totals['buckets']:,} minute buckets")
    cov = store.coverage()
    if cov["first"]:
        print(f"history now spans "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(cov['first']))} -> "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(cov['last']))}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rigmon", description=__doc__)
    parser.add_argument("-c", "--config", help="path to config.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="run the collector and dashboard (default)")
    p.add_argument("--port", type=int)
    p.add_argument("--host")
    p.add_argument("--lhm-url")
    p.add_argument("--interval", type=int)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("discover", help="list every sensor LibreHardwareMonitor exposes")
    p.add_argument("--lhm-url")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("check", help="verify the data source and show a snapshot")
    p.add_argument("--lhm-url")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("import", help="backfill from LibreHardwareMonitor CSV logs")
    p.add_argument("paths", nargs="+", help="CSV files or directories to scan")
    p.add_argument("--force", action="store_true", help="re-import already-seen files")
    p.set_defaults(func=cmd_import)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = config_mod.load(args.config)

    for attr, field in (("port", "listen_port"), ("host", "listen_host"),
                        ("lhm_url", "lhm_url"), ("interval", "poll_interval")):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(cfg, field, value)

    func = getattr(args, "func", cmd_serve)
    return func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
