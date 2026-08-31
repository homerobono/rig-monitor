"""LibreHardwareMonitor client: fetch /data.json, flatten it, and map sensors to metric keys."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import metrics

_NUMBER_HEAD = re.compile(r"^\s*([+-]?[\d][\d.,\u00a0 ]*)")
_DECIMAL_COMMA = re.compile(r"\d,\d{1,2}(?!\d)")
_DECIMAL_DOT = re.compile(r"\d\.\d{1,2}(?!\d)")

# LibreHardwareMonitor omits "Type" on older builds; the category node name identifies it.
_CATEGORY_TYPE = {
    "voltages": "Voltage",
    "currents": "Current",
    "powers": "Power",
    "clocks": "Clock",
    "temperatures": "Temperature",
    "load": "Load",
    "fans": "Fan",
    "controls": "Control",
    "data": "Data",
    "small data": "SmallData",
    "levels": "Level",
    "factors": "Factor",
    "throughput": "Throughput",
    "frequencies": "Frequency",
    "energy": "Energy",
    "noise level": "Noise",
}

# Used only when a build gives us no SensorId to match a hardware id prefix against.
_HW_ALIASES = {
    "/amdcpu/": r"amd ryzen|amd athlon|amd fx|amd eng",
    "/intelcpu/": r"intel core|intel xeon|intel pentium",
    "/gpu-nvidia/": r"nvidia|geforce|rtx |gtx ",
    "/gpu-amd/": r"radeon|amd custom gpu",
    "/lpc/": r"nct\d|it\d{4}|ite |nuvoton|fintek|smsc|winbond",
    "/ram/": r"generic memory|memory",
}


class LhmUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Sensor:
    sensor_id: str
    type: str
    name: str
    hardware: str
    value: float | None
    unit: str

    @property
    def display(self) -> str:
        return f"{self.hardware} › {self.name}"


def detect_decimal_separator(payload: str) -> str:
    """LHM formats numbers with the machine's locale, so sniff it from the whole payload.

    Voltage/temperature readings always carry fractional digits, which makes the majority
    vote reliable and avoids misreading "1.234 RPM" (pt-BR thousands) as 1.234.
    """
    commas = len(_DECIMAL_COMMA.findall(payload))
    dots = len(_DECIMAL_DOT.findall(payload))
    return "," if commas > dots else "."


def parse_number(text: str, decimal: str = ".") -> float | None:
    if not text:
        return None
    m = _NUMBER_HEAD.match(text)
    if not m:
        return None
    s = m.group(1).replace("\u00a0", "").replace(" ", "")
    grouping = "," if decimal == "." else "."
    s = s.replace(grouping, "")
    if decimal != ".":
        s = s.replace(decimal, ".")
    s = s.rstrip(".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_unit(text: str) -> str:
    if not text:
        return ""
    m = _NUMBER_HEAD.match(text)
    return text[m.end():].strip() if m else text.strip()


def _walk(node: dict, hardware_path: list[str], category: str, out: list, decimal: str):
    children = node.get("Children") or []
    text = (node.get("Text") or "").strip()

    if not children:
        raw = node.get("Value") or ""
        sid = node.get("SensorId") or ""
        if not raw and not sid:
            return
        stype = node.get("Type") or _CATEGORY_TYPE.get(category.lower(), category or "Unknown")
        hardware = hardware_path[-1] if hardware_path else ""
        if not sid:
            sid = "/".join(["", *(p.lower().replace(" ", "-") for p in hardware_path),
                            stype.lower(), text.lower().replace(" ", "-")])
        out.append(Sensor(sid, stype, text, hardware,
                          parse_number(raw, decimal), parse_unit(raw)))
        return

    # A node whose children are leaves is a category (Temperatures, Fans, ...);
    # anything else on the way down is a computer or a piece of hardware.
    is_category = text.lower() in _CATEGORY_TYPE
    if is_category:
        for child in children:
            _walk(child, hardware_path, text, out, decimal)
    else:
        path = hardware_path + [text] if text and text.lower() != "sensor" else hardware_path
        for child in children:
            _walk(child, path, "", out, decimal)


def flatten(root: dict, decimal: str = ".") -> list[Sensor]:
    out: list[Sensor] = []
    _walk(root, [], "", out, decimal)
    return out


def fetch(url: str, timeout: float = 5.0) -> list[Sensor]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise LhmUnavailable(f"{url} returned HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise LhmUnavailable(f"cannot reach {url}: {e}") from e

    decimal = detect_decimal_separator(payload)
    try:
        root = json.loads(payload)
    except json.JSONDecodeError as e:
        raise LhmUnavailable(f"{url} did not return JSON: {e}") from e
    sensors = flatten(root, decimal)
    if not sensors:
        raise LhmUnavailable(
            f"{url} responded but exposed no sensors - is LibreHardwareMonitor running as admin?"
        )
    return sensors


class Resolver:
    """Binds catalogue metric keys to concrete sensors, re-binding if hardware changes."""

    def __init__(self, extra: dict[str, str] | None = None, collect_all: bool = False):
        self.extra = extra or {}
        self.collect_all = collect_all
        self.bindings: dict[str, str] = {}
        self.unmatched: list[str] = []
        self._signature: tuple = ()

    def bind(self, sensors: list[Sensor]) -> dict[str, str]:
        signature = tuple(sorted(s.sensor_id for s in sensors))
        if signature == self._signature and self.bindings:
            return self.bindings

        by_id = {s.sensor_id: s for s in sensors}
        bindings: dict[str, str] = {}
        unmatched: list[str] = []

        for metric in metrics.CATALOG:
            sid = self._find(metric, sensors, by_id)
            if sid:
                bindings[metric.key] = sid
            else:
                unmatched.append(metric.key)

        for key, sid in self.extra.items():
            if sid in by_id:
                bindings[key] = sid

        if self.collect_all:
            for s in sensors:
                key = f"raw:{s.sensor_id}"
                bindings.setdefault(key, s.sensor_id)

        self.bindings = bindings
        self.unmatched = unmatched
        self._signature = signature
        return bindings

    @staticmethod
    def _find(metric, sensors: list[Sensor], by_id: dict[str, Sensor]) -> str | None:
        for match in metric.matches:
            wanted = match.name.casefold()
            alias = _HW_ALIASES.get(match.hw)
            for s in sensors:
                if s.type != match.type or s.name.strip().casefold() != wanted:
                    continue
                if s.sensor_id.startswith(match.hw):
                    return s.sensor_id
                if alias and re.search(alias, s.hardware, re.I):
                    return s.sensor_id
        for sid in metric.fallback_ids:
            if sid in by_id:
                return sid
        return None

    def read(self, sensors: list[Sensor]) -> dict[str, float]:
        bindings = self.bind(sensors)
        by_id = {s.sensor_id: s for s in sensors}
        out: dict[str, float] = {}
        for key, sid in bindings.items():
            s = by_id.get(sid)
            if s is None:
                continue
            value = metrics.sanitize(key, s.value)
            if value is not None:
                out[key] = value
        return out
