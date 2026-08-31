"""Configuration loading for rig-monitor."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DEFAULT_CONFIG_PATH = ROOT / "config.json"


@dataclass
class Config:
    lhm_url: str = "http://127.0.0.1:8085/data.json"
    poll_interval: int = 1
    listen_host: str = "0.0.0.0"
    listen_port: int = 8600
    db_path: str = "data/rigmon.db"

    # Raw only has to outlive the longest selectable range (12 h); everything older is
    # served from the 1-minute rollups.
    raw_retention_days: float = 1.0
    rollup_retention_days: float = 30.0

    temp_alert_c: float = 85.0

    # Record every sensor LibreHardwareMonitor exposes, not just the curated set.
    collect_all: bool = False
    # Extra sensor ids to record even when collect_all is off, as {"key": "/nvme/0/temperature/0"}.
    extra_metrics: dict = field(default_factory=dict)

    def __post_init__(self):
        self.poll_interval = max(1, int(self.poll_interval))

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else ROOT / p


def load(path: str | os.PathLike | None = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = Config()
    if cfg_path.exists():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        known = {f for f in asdict(cfg)}
        unknown = set(raw) - known
        if unknown:
            raise SystemExit(
                f"Unknown option(s) in {cfg_path}: {', '.join(sorted(unknown))}"
            )
        for k, v in raw.items():
            setattr(cfg, k, v)
    return cfg


def write_example(path: Path) -> None:
    path.write_text(json.dumps(asdict(Config()), indent=2) + "\n", encoding="utf-8")
