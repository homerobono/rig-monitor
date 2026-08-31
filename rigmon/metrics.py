"""Metric catalogue: the stable keys we store, and how to find them in LibreHardwareMonitor.

Sensor matching is done by (hardware id prefix, sensor type, sensor name) rather than by
the numeric sensor index, because LibreHardwareMonitor renumbers indices when the set of
detected sensors changes. Raw ids are kept only as a fallback.

Defaults are tuned for: Ryzen 7 7800X3D / RTX 4070 Ti SUPER ROG Strix / Nuvoton NCT6687D.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Catppuccin Mocha - muted, non-saturated, readable on a dark background.
C = {
    "red": "#f38ba8",
    "maroon": "#eba0ac",
    "peach": "#fab387",
    "yellow": "#f9e2af",
    "green": "#a6e3a1",
    "teal": "#94e2d5",
    "sky": "#89dceb",
    "sapphire": "#74c7ec",
    "blue": "#89b4fa",
    "lavender": "#b4befe",
    "mauve": "#cba6f7",
    "pink": "#f5c2e7",
    "flamingo": "#f2cdcd",
    "rosewater": "#f5e0dc",
    "subtext": "#a6adc8",
    "overlay": "#7f849c",
}


@dataclass(frozen=True)
class Match:
    """One way of locating a sensor. `hw` is matched against the sensor id prefix."""

    hw: str
    type: str
    name: str


@dataclass
class Metric:
    key: str
    label: str
    unit: str
    group: str
    color: str
    matches: tuple[Match, ...]
    fallback_ids: tuple[str, ...] = ()
    default_on: bool = True
    short: str = ""

    def __post_init__(self):
        if not self.short:
            self.short = self.label


def _t(hw, name):
    return Match(hw, "Temperature", name)


def _l(hw, name):
    return Match(hw, "Load", name)


def _c(hw, name):
    return Match(hw, "Control", name)


def _f(hw, name):
    return Match(hw, "Fan", name)


CPU = "/amdcpu/"
GPU = "/gpu-nvidia/"
LPC = "/lpc/"
RAM = "/ram/"
IGPU = "/gpu-amd/"


CATALOG: list[Metric] = [
    # ---------------------------------------------------------------- temperatures
    Metric("cpu.temp", "CPU (Tctl/Tdie)", "°C", "temp", C["red"],
           (_t(CPU, "Core (Tctl/Tdie)"), _t(CPU, "CPU Package")),
           fallback_ids=("/amdcpu/0/temperature/2",), short="CPU"),
    Metric("cpu.temp_ccd1", "CPU CCD1", "°C", "temp", C["maroon"],
           (_t(CPU, "CCD1 (Tdie)"),),
           fallback_ids=("/amdcpu/0/temperature/3",), short="CCD1"),
    Metric("gpu.temp", "GPU Core", "°C", "temp", C["blue"],
           (_t(GPU, "GPU Core"),),
           fallback_ids=("/gpu-nvidia/0/temperature/0",), short="GPU"),
    Metric("gpu.temp_hotspot", "GPU Hot Spot", "°C", "temp", C["mauve"],
           (_t(GPU, "GPU Hot Spot"),),
           fallback_ids=("/gpu-nvidia/0/temperature/2",), short="Hot Spot"),
    Metric("gpu.temp_vram", "GPU Mem Junction", "°C", "temp", C["lavender"],
           (_t(GPU, "GPU Memory Junction"),),
           fallback_ids=("/gpu-nvidia/0/temperature/3",), short="VRAM"),
    Metric("board.temp_vrm", "VRM MOS", "°C", "temp", C["peach"],
           (_t(LPC, "VRM MOS"),),
           fallback_ids=("/lpc/nct6687dr/0/temperature/2",), short="VRM"),
    Metric("board.temp_sys", "Motherboard", "°C", "temp", C["subtext"],
           (_t(LPC, "System"),),
           fallback_ids=("/lpc/nct6687dr/0/temperature/1",), short="Board"),
    Metric("board.temp_chipset", "Chipset", "°C", "temp", C["overlay"],
           (_t(LPC, "Chipset"),),
           fallback_ids=("/lpc/nct6687dr/0/temperature/3",),
           default_on=False, short="Chipset"),
    Metric("board.temp_socket", "CPU Socket (board)", "°C", "temp", C["flamingo"],
           (_t(LPC, "CPU Core"),),
           fallback_ids=("/lpc/nct6687dr/0/temperature/0",),
           default_on=False, short="Socket"),

    # ----------------------------------------------------------------- utilisation
    Metric("cpu.load", "CPU Total", "%", "load", C["red"],
           (_l(CPU, "CPU Total"),),
           fallback_ids=("/amdcpu/0/load/0",), short="CPU"),
    Metric("cpu.load_max", "CPU Hottest Core", "%", "load", C["maroon"],
           (_l(CPU, "CPU Core Max"),),
           fallback_ids=("/amdcpu/0/load/1",), default_on=False, short="CPU max core"),
    Metric("gpu.load", "GPU Core", "%", "load", C["blue"],
           (_l(GPU, "GPU Core"),),
           fallback_ids=("/gpu-nvidia/0/load/0",), short="GPU"),
    Metric("gpu.load_memctl", "GPU Mem Controller", "%", "load", C["sapphire"],
           (_l(GPU, "GPU Memory Controller"),),
           fallback_ids=("/gpu-nvidia/0/load/1",), default_on=False, short="GPU memctl"),
    Metric("ram.load", "RAM", "%", "load", C["green"],
           (_l(RAM, "Memory"),), short="RAM"),
    Metric("gpu.vram_load", "VRAM", "%", "load", C["teal"],
           (_l(GPU, "GPU Memory"),), default_on=False, short="VRAM"),

    # ------------------------------------------------------------------- fans in %
    Metric("fan.cpu", "CPU Fan", "%", "fan", C["red"],
           (_c(LPC, "CPU Fan"),),
           fallback_ids=("/lpc/nct6687dr/0/control/0",), short="CPU"),
    Metric("fan.pump", "Pump", "%", "fan", C["pink"],
           (_c(LPC, "Pump Fan"), _c(LPC, "Pump Fan #1")),
           fallback_ids=("/lpc/nct6687dr/0/control/1",), short="Pump"),
    Metric("fan.sys1", "System Fan #1", "%", "fan", C["green"],
           (_c(LPC, "System Fan #1"),),
           fallback_ids=("/lpc/nct6687dr/0/control/10",), short="Sys #1"),
    Metric("fan.sys2", "System Fan #2", "%", "fan", C["teal"],
           (_c(LPC, "System Fan #2"),),
           fallback_ids=("/lpc/nct6687dr/0/control/11",), short="Sys #2"),
    Metric("fan.sys3", "System Fan #3", "%", "fan", C["sky"],
           (_c(LPC, "System Fan #3"),),
           fallback_ids=("/lpc/nct6687dr/0/control/12",), short="Sys #3"),
    Metric("fan.sys4", "System Fan #4", "%", "fan", C["sapphire"],
           (_c(LPC, "System Fan #4"),),
           fallback_ids=("/lpc/nct6687dr/0/control/13",), short="Sys #4"),
    Metric("fan.sys5", "System Fan #5", "%", "fan", C["lavender"],
           (_c(LPC, "System Fan #5"),),
           fallback_ids=("/lpc/nct6687dr/0/control/14",), short="Sys #5"),
    Metric("fan.sys6", "System Fan #6", "%", "fan", C["mauve"],
           (_c(LPC, "System Fan #6"),),
           fallback_ids=("/lpc/nct6687dr/0/control/15",), short="Sys #6"),
    Metric("fan.ezconnect", "EZ-Connect Fan", "%", "fan", C["yellow"],
           (_c(LPC, "EZ-Connect Fan"),),
           fallback_ids=("/lpc/nct6687dr/0/control/3",), short="EZ-Connect"),
    Metric("fan.chipset", "Chipset Fan", "%", "fan", C["overlay"],
           (_c(LPC, "Chipset Fan"),),
           fallback_ids=("/lpc/nct6687dr/0/control/2",), default_on=False, short="Chipset"),
    Metric("fan.gpu1", "GPU Fan 1", "%", "fan", C["peach"],
           (_c(GPU, "GPU Fan 1"),),
           fallback_ids=("/gpu-nvidia/0/control/1",), short="GPU #1"),
    Metric("fan.gpu2", "GPU Fan 2", "%", "fan", C["flamingo"],
           (_c(GPU, "GPU Fan 2"),),
           fallback_ids=("/gpu-nvidia/0/control/2",), short="GPU #2"),

    # ------------------------------------------------------------------ fans in RPM
    Metric("rpm.cpu", "CPU Fan", "RPM", "rpm", C["red"], (_f(LPC, "CPU Fan"),), short="CPU"),
    Metric("rpm.pump", "Pump", "RPM", "rpm", C["pink"],
           (_f(LPC, "Pump Fan"), _f(LPC, "Pump Fan #1")), short="Pump"),
    Metric("rpm.sys1", "System Fan #1", "RPM", "rpm", C["green"], (_f(LPC, "System Fan #1"),), short="Sys #1"),
    Metric("rpm.sys2", "System Fan #2", "RPM", "rpm", C["teal"], (_f(LPC, "System Fan #2"),), short="Sys #2"),
    Metric("rpm.sys3", "System Fan #3", "RPM", "rpm", C["sky"], (_f(LPC, "System Fan #3"),), short="Sys #3"),
    Metric("rpm.sys4", "System Fan #4", "RPM", "rpm", C["sapphire"], (_f(LPC, "System Fan #4"),), short="Sys #4"),
    Metric("rpm.sys5", "System Fan #5", "RPM", "rpm", C["lavender"], (_f(LPC, "System Fan #5"),), short="Sys #5"),
    Metric("rpm.sys6", "System Fan #6", "RPM", "rpm", C["mauve"], (_f(LPC, "System Fan #6"),), short="Sys #6"),
    Metric("rpm.ezconnect", "EZ-Connect Fan", "RPM", "rpm", C["yellow"], (_f(LPC, "EZ-Connect Fan"),), short="EZ-Connect"),
    Metric("rpm.gpu1", "GPU Fan 1", "RPM", "rpm", C["peach"], (_f(GPU, "GPU Fan 1"),), short="GPU #1"),
    Metric("rpm.gpu2", "GPU Fan 2", "RPM", "rpm", C["flamingo"], (_f(GPU, "GPU Fan 2"),), short="GPU #2"),

    # ----------------------------------------------------------------------- power
    Metric("cpu.power", "CPU Package", "W", "power", C["red"],
           (Match(CPU, "Power", "Package"),),
           fallback_ids=("/amdcpu/0/power/0",), short="CPU"),
    Metric("gpu.power", "GPU Package", "W", "power", C["blue"],
           (Match(GPU, "Power", "GPU Package"),),
           fallback_ids=("/gpu-nvidia/0/power/0",), short="GPU"),

    # ---------------------------------------------------------------------- clocks
    Metric("cpu.clock", "CPU Cores (avg)", "MHz", "clock", C["red"],
           (Match(CPU, "Clock", "Cores (Average)"),), default_on=False, short="CPU"),
    Metric("gpu.clock", "GPU Core", "MHz", "clock", C["blue"],
           (Match(GPU, "Clock", "GPU Core"),), default_on=False, short="GPU"),

    # ------------------------------------------------------------------------ misc
    Metric("sys.fps", "Fullscreen FPS", "FPS", "misc", C["yellow"],
           (Match(IGPU, "Factor", "Fullscreen FPS"),
            Match(GPU, "Factor", "Fullscreen FPS")), short="FPS"),
]

BY_KEY: dict[str, Metric] = {m.key: m for m in CATALOG}

# Sensors whose value is meaningless when negative / zero-filled by the driver.
_DROP_NEGATIVE = {"sys.fps"}

GROUPS = {
    "temp": {"label": "Temperature", "unit": "°C"},
    "load": {"label": "Utilisation", "unit": "%"},
    "fan": {"label": "Fan speed", "unit": "%"},
    "rpm": {"label": "Fan speed", "unit": "RPM"},
    "power": {"label": "Power", "unit": "W"},
    "clock": {"label": "Clock", "unit": "MHz"},
    "misc": {"label": "Misc", "unit": ""},
}


def sanitize(key: str, value: float | None) -> float | None:
    if value is None:
        return None
    if key in _DROP_NEGATIVE and value < 0:
        return None
    return value


def temp_keys() -> list[str]:
    return [m.key for m in CATALOG if m.group == "temp"]
