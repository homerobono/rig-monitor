import unittest

from rigmon import lhm


class TestNumberParsing(unittest.TestCase):
    def test_dot_decimal(self):
        self.assertAlmostEqual(lhm.parse_number("52.8 °C"), 52.8)
        self.assertAlmostEqual(lhm.parse_number("1,234 RPM"), 1234)
        self.assertAlmostEqual(lhm.parse_number("1,234.5 RPM"), 1234.5)

    def test_comma_decimal(self):
        self.assertAlmostEqual(lhm.parse_number("52,8 °C", ","), 52.8)
        self.assertAlmostEqual(lhm.parse_number("1.234 RPM", ","), 1234)
        self.assertAlmostEqual(lhm.parse_number("1.234,5 RPM", ","), 1234.5)

    def test_signs_and_junk(self):
        self.assertAlmostEqual(lhm.parse_number("-1 FPS"), -1)
        self.assertIsNone(lhm.parse_number(""))
        self.assertIsNone(lhm.parse_number("n/a"))

    def test_unit(self):
        self.assertEqual(lhm.parse_unit("52.8 °C"), "°C")
        self.assertEqual(lhm.parse_unit("35 %"), "%")

    def test_locale_sniffing(self):
        en = '{"Value":"12.12 V"},{"Value":"52.8 °C"},{"Value":"1,234 RPM"}'
        pt = '{"Value":"12,12 V"},{"Value":"52,8 °C"},{"Value":"1.234 RPM"}'
        self.assertEqual(lhm.detect_decimal_separator(en), ".")
        self.assertEqual(lhm.detect_decimal_separator(pt), ",")


def _leaf(sid, name, value, stype=None):
    node = {"Text": name, "Value": value, "Min": value, "Max": value, "Children": []}
    if sid:
        node["SensorId"] = sid
    if stype:
        node["Type"] = stype
    return node


MODERN = {
    "Text": "Sensor", "Children": [{
        "Text": "DESKTOP", "Children": [
            {"Text": "AMD Ryzen 7 7800X3D", "Children": [
                {"Text": "Temperatures", "Children": [
                    _leaf("/amdcpu/0/temperature/2", "Core (Tctl/Tdie)", "62.5 °C", "Temperature"),
                ]},
                {"Text": "Load", "Children": [
                    _leaf("/amdcpu/0/load/0", "CPU Total", "41.2 %", "Load"),
                ]},
            ]},
            {"Text": "NVIDIA GeForce RTX 4070 Ti SUPER", "Children": [
                {"Text": "Temperatures", "Children": [
                    _leaf("/gpu-nvidia/0/temperature/0", "GPU Core", "48.0 °C", "Temperature"),
                    _leaf("/gpu-nvidia/0/temperature/2", "GPU Hot Spot", "59.5 °C", "Temperature"),
                ]},
                {"Text": "Controls", "Children": [
                    _leaf("/gpu-nvidia/0/control/1", "GPU Fan 1", "38 %", "Control"),
                ]},
            ]},
            {"Text": "Nuvoton NCT6687D", "Children": [
                {"Text": "Controls", "Children": [
                    _leaf("/lpc/nct6687dr/0/control/0", "CPU Fan", "45 %", "Control"),
                    _leaf("/lpc/nct6687dr/0/control/10", "System Fan #1", "30 %", "Control"),
                ]},
                {"Text": "Fans", "Children": [
                    _leaf("/lpc/nct6687dr/0/fan/0", "CPU Fan", "1050 RPM", "Fan"),
                ]},
            ]},
        ],
    }],
}


class TestFlatten(unittest.TestCase):
    def test_modern_payload(self):
        sensors = lhm.flatten(MODERN)
        by_id = {s.sensor_id: s for s in sensors}
        self.assertEqual(len(sensors), 8)
        self.assertAlmostEqual(by_id["/amdcpu/0/temperature/2"].value, 62.5)
        self.assertEqual(by_id["/amdcpu/0/temperature/2"].hardware, "AMD Ryzen 7 7800X3D")
        self.assertEqual(by_id["/lpc/nct6687dr/0/control/0"].type, "Control")
        self.assertEqual(by_id["/lpc/nct6687dr/0/fan/0"].type, "Fan")

    def test_legacy_payload_without_ids_or_types(self):
        """Older builds omit SensorId/Type; the category name has to carry the type."""
        legacy = {
            "Text": "Sensor", "Children": [{
                "Text": "PC", "Children": [{
                    "Text": "AMD Ryzen 7 7800X3D", "Children": [{
                        "Text": "Temperatures",
                        "Children": [_leaf(None, "Core (Tctl/Tdie)", "62.5 °C")],
                    }],
                }],
            }],
        }
        sensors = lhm.flatten(legacy)
        self.assertEqual(len(sensors), 1)
        self.assertEqual(sensors[0].type, "Temperature")
        self.assertEqual(sensors[0].hardware, "AMD Ryzen 7 7800X3D")
        # Name-based matching still binds it to the metric key.
        self.assertIn("cpu.temp", lhm.Resolver().bind(sensors))


class TestResolver(unittest.TestCase):
    def test_binds_expected_metrics(self):
        sensors = lhm.flatten(MODERN)
        values = lhm.Resolver().read(sensors)
        self.assertAlmostEqual(values["cpu.temp"], 62.5)
        self.assertAlmostEqual(values["gpu.temp_hotspot"], 59.5)
        self.assertAlmostEqual(values["fan.cpu"], 45)
        self.assertAlmostEqual(values["fan.sys1"], 30)
        self.assertAlmostEqual(values["rpm.cpu"], 1050)

    def test_control_and_fan_share_a_name_but_not_a_metric(self):
        sensors = lhm.flatten(MODERN)
        bindings = lhm.Resolver().bind(sensors)
        self.assertEqual(bindings["fan.cpu"], "/lpc/nct6687dr/0/control/0")
        self.assertEqual(bindings["rpm.cpu"], "/lpc/nct6687dr/0/fan/0")

    def test_renumbered_sensor_indices_still_bind(self):
        renumbered = {
            "Text": "Sensor", "Children": [{
                "Text": "PC", "Children": [{
                    "Text": "NVIDIA GeForce RTX 4070 Ti SUPER", "Children": [{
                        "Text": "Temperatures", "Children": [
                            _leaf("/gpu-nvidia/0/temperature/7", "GPU Hot Spot", "91.0 °C",
                                  "Temperature"),
                        ],
                    }],
                }],
            }],
        }
        values = lhm.Resolver().read(lhm.flatten(renumbered))
        self.assertAlmostEqual(values["gpu.temp_hotspot"], 91.0)

    def test_negative_fps_is_dropped(self):
        tree = {
            "Text": "Sensor", "Children": [{
                "Text": "PC", "Children": [{
                    "Text": "AMD Custom GPU 0405", "Children": [{
                        "Text": "Factors",
                        "Children": [_leaf("/gpu-amd/0/factor/0", "Fullscreen FPS", "-1 FPS",
                                           "Factor")],
                    }],
                }],
            }],
        }
        self.assertNotIn("sys.fps", lhm.Resolver().read(lhm.flatten(tree)))


if __name__ == "__main__":
    unittest.main()
