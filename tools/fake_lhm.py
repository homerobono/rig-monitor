"""A stand-in for LibreHardwareMonitor's web server, for developing away from the rig.

Emits a /data.json shaped like the real thing (7800X3D + RTX 4070 Ti SUPER + NCT6687D)
with a simulated workload so temperatures, fan speeds and load actually correlate.

    python tools/fake_lhm.py --port 8085 [--locale pt] [--speed 60]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START = time.time()
SPEED = 1.0
DECIMAL = "."


def _fmt(value: float, unit: str, digits: int = 1) -> str:
    text = f"{value:.{digits}f}"
    if DECIMAL == ",":
        text = text.replace(".", ",")
    return f"{text} {unit}"


class Rig:
    """Load follows a slow duty cycle; temperature lags load; fans lag temperature."""

    def __init__(self):
        self.cpu_t = 40.0
        self.gpu_t = 35.0
        self.hot_t = 40.0

    def state(self):
        t = (time.time() - START) * SPEED
        phase = math.sin(t / 420) * 0.5 + 0.5
        burst = 1.0 if math.sin(t / 95) > 0.72 else 0.0
        gpu_load = min(100.0, max(2.0, phase * 88 + burst * 25 + random.gauss(0, 3)))
        cpu_load = min(100.0, max(1.0, phase * 55 + burst * 40 + random.gauss(0, 4)))

        cpu_target = min(89.0, 36 + cpu_load * 0.48 + burst * 9)
        gpu_target = 32 + gpu_load * 0.37
        for attr, target, rate in (
            ("cpu_t", cpu_target, 0.35),
            ("gpu_t", gpu_target, 0.12),
            ("hot_t", gpu_target + 10 + gpu_load * 0.09, 0.16),
        ):
            cur = getattr(self, attr)
            setattr(self, attr, cur + (target - cur) * rate + random.gauss(0, 0.25))

        def curve(temp, lo, hi, floor, ceiling):
            x = (temp - lo) / (hi - lo)
            return round(min(ceiling, max(floor, floor + x * (ceiling - floor))))

        cpu_fan = curve(self.cpu_t, 40, 85, 25, 100)
        sys_fan = curve(max(self.cpu_t, self.gpu_t), 35, 80, 20, 90)
        gpu_fan = 0 if self.gpu_t < 52 else curve(self.gpu_t, 52, 83, 30, 100)
        return {
            "cpu_load": cpu_load, "gpu_load": gpu_load,
            "cpu_t": self.cpu_t, "gpu_t": self.gpu_t, "hot_t": self.hot_t,
            "cpu_fan": cpu_fan, "pump": min(100, cpu_fan + 12), "sys_fan": sys_fan,
            "gpu_fan": gpu_fan,
            "cpu_w": 25 + cpu_load * 0.85, "gpu_w": 30 + gpu_load * 2.6,
            "cpu_mhz": 3400 + cpu_load * 17, "gpu_mhz": 900 + gpu_load * 17,
            "ram": 38 + phase * 22,
            "fps": (60 + gpu_load) if gpu_load > 25 else -1,
        }


RIG = Rig()


def _sensor(sid, stype, name, value, unit, digits=1):
    return {"id": abs(hash(sid)) % 100000, "Text": name, "Type": stype, "SensorId": sid,
            "Min": _fmt(value, unit, digits), "Value": _fmt(value, unit, digits),
            "Max": _fmt(value, unit, digits), "ImageURL": "", "Children": []}


def _group(name, sensors):
    return {"id": abs(hash(name)) % 100000, "Text": name, "Min": "", "Value": "", "Max": "",
            "ImageURL": "", "Children": sensors}


def build_tree() -> dict:
    s = RIG.state()
    rpm = lambda pct: pct * 21 + (120 if pct else 0)

    mobo = {
        "Text": "MSI MAG B650 TOMAHAWK WIFI", "Min": "", "Value": "", "Max": "",
        "ImageURL": "", "id": 2,
        "Children": [{
            "Text": "Nuvoton NCT6687D", "Min": "", "Value": "", "Max": "", "ImageURL": "",
            "id": 3,
            "Children": [
                _group("Fans", [
                    _sensor("/lpc/nct6687dr/0/fan/0", "Fan", "CPU Fan", rpm(s["cpu_fan"]), "RPM", 0),
                    _sensor("/lpc/nct6687dr/0/fan/1", "Fan", "Pump Fan #1", rpm(s["pump"]), "RPM", 0),
                    _sensor("/lpc/nct6687dr/0/fan/10", "Fan", "System Fan #1", rpm(s["sys_fan"]), "RPM", 0),
                    _sensor("/lpc/nct6687dr/0/fan/11", "Fan", "System Fan #2", rpm(s["sys_fan"] + 4), "RPM", 0),
                    _sensor("/lpc/nct6687dr/0/fan/12", "Fan", "System Fan #3", rpm(s["sys_fan"] - 3), "RPM", 0),
                    _sensor("/lpc/nct6687dr/0/fan/13", "Fan", "System Fan #4", rpm(s["sys_fan"]), "RPM", 0),
                ]),
                _group("Controls", [
                    _sensor("/lpc/nct6687dr/0/control/0", "Control", "CPU Fan", s["cpu_fan"], "%"),
                    _sensor("/lpc/nct6687dr/0/control/1", "Control", "Pump Fan", s["pump"], "%"),
                    _sensor("/lpc/nct6687dr/0/control/10", "Control", "System Fan #1", s["sys_fan"], "%"),
                    _sensor("/lpc/nct6687dr/0/control/11", "Control", "System Fan #2", s["sys_fan"] + 4, "%"),
                    _sensor("/lpc/nct6687dr/0/control/12", "Control", "System Fan #3", s["sys_fan"] - 3, "%"),
                    _sensor("/lpc/nct6687dr/0/control/13", "Control", "System Fan #4", s["sys_fan"], "%"),
                ]),
                _group("Voltages", [
                    _sensor("/lpc/nct6687dr/0/voltage/0", "Voltage", "+12V", 12.12, "V", 2),
                    _sensor("/lpc/nct6687dr/0/voltage/5", "Voltage", "Vcore", 1.184, "V", 3),
                ]),
            ],
        }],
    }

    cpu = {
        "Text": "AMD Ryzen 7 7800X3D", "Min": "", "Value": "", "Max": "", "ImageURL": "", "id": 10,
        "Children": [
            _group("Temperatures", [
                _sensor("/amdcpu/0/temperature/2", "Temperature", "Core (Tctl/Tdie)", s["cpu_t"], "°C"),
            ]),
            _group("Load", [
                _sensor("/amdcpu/0/load/0", "Load", "CPU Total", s["cpu_load"], "%"),
            ]),
            _group("Powers", [
                _sensor("/amdcpu/0/power/0", "Power", "Package", s["cpu_w"], "W"),
            ]),
            _group("Clocks", [
                _sensor("/amdcpu/0/clock/1", "Clock", "Cores (Average)", s["cpu_mhz"], "MHz", 0),
            ]),
        ],
    }

    gpu = {
        "Text": "NVIDIA GeForce RTX 4070 Ti SUPER", "Min": "", "Value": "", "Max": "",
        "ImageURL": "", "id": 20,
        "Children": [
            _group("Temperatures", [
                _sensor("/gpu-nvidia/0/temperature/0", "Temperature", "GPU Core", s["gpu_t"], "°C"),
                _sensor("/gpu-nvidia/0/temperature/2", "Temperature", "GPU Hot Spot", s["hot_t"], "°C"),
            ]),
            _group("Load", [
                _sensor("/gpu-nvidia/0/load/0", "Load", "GPU Core", s["gpu_load"], "%"),
            ]),
            _group("Controls", [
                _sensor("/gpu-nvidia/0/control/1", "Control", "GPU Fan 1", s["gpu_fan"], "%"),
                _sensor("/gpu-nvidia/0/control/2", "Control", "GPU Fan 2", s["gpu_fan"], "%"),
            ]),
            _group("Fans", [
                _sensor("/gpu-nvidia/0/fan/1", "Fan", "GPU Fan 1", rpm(s["gpu_fan"]) * 1.4, "RPM", 0),
                _sensor("/gpu-nvidia/0/fan/2", "Fan", "GPU Fan 2", rpm(s["gpu_fan"]) * 1.4, "RPM", 0),
            ]),
            _group("Powers", [
                _sensor("/gpu-nvidia/0/power/0", "Power", "GPU Package", s["gpu_w"], "W"),
            ]),
            _group("Clocks", [
                _sensor("/gpu-nvidia/0/clock/0", "Clock", "GPU Core", s["gpu_mhz"], "MHz", 0),
            ]),
        ],
    }

    igpu = {
        "Text": "AMD Custom GPU 0405", "Min": "", "Value": "", "Max": "", "ImageURL": "", "id": 30,
        "Children": [_group("Factors", [
            _sensor("/gpu-amd/0/factor/0", "Factor", "Fullscreen FPS", s["fps"], "FPS", 0),
        ])],
    }

    ram = {
        "Text": "Generic Memory", "Min": "", "Value": "", "Max": "", "ImageURL": "", "id": 40,
        "Children": [_group("Load", [
            _sensor("/ram/load/0", "Load", "Memory", s["ram"], "%"),
        ])],
    }

    return {"id": 0, "Text": "Sensor", "Min": "", "Value": "", "Max": "", "ImageURL": "",
            "Children": [{"id": 1, "Text": "DESKTOP-RIG", "Min": "", "Value": "", "Max": "",
                          "ImageURL": "", "Children": [mobo, cpu, gpu, igpu, ram]}]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.rstrip("/") not in ("/data.json", "", "/index.html"):
            self.send_error(404)
            return
        body = json.dumps(build_tree()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    global SPEED, DECIMAL
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8085)
    ap.add_argument("--speed", type=float, default=1.0, help="simulation time multiplier")
    ap.add_argument("--locale", choices=("en", "pt"), default="en",
                    help="pt emits comma decimal separators, like a pt-BR Windows install")
    args = ap.parse_args()
    SPEED = args.speed
    DECIMAL = "," if args.locale == "pt" else "."
    print(f"fake LibreHardwareMonitor on http://127.0.0.1:{args.port}/data.json "
          f"(speed x{SPEED}, decimal '{DECIMAL}')")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
