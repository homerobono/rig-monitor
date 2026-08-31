import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from rigmon.importer import import_csv
from rigmon.storage import Store

IDS = ("," + ",".join([
    "/lpc/nct6687dr/0/control/0",
    "/lpc/nct6687dr/0/control/10",
    "/lpc/nct6687dr/0/fan/0",
    "/amdcpu/0/temperature/2",
    "/amdcpu/0/load/0",
    "/gpu-nvidia/0/temperature/2",
    "/gpu-nvidia/0/voltage/0",
]))
NAMES = ('Time,"CPU Fan","System Fan #1","CPU Fan","Core (Tctl/Tdie)","CPU Total",'
         '"GPU Hot Spot","GPU Core"')


def write_log(dirpath: Path, day: str, rows: int, decimal: str = ".") -> Path:
    """Build a log shaped like LibreHardwareMonitorLog-YYYY-MM-DD-N.csv."""
    path = dirpath / f"LibreHardwareMonitorLog-{day}-1.csv"
    y, m, d = day.split("-")
    lines = [IDS, NAMES]
    for i in range(rows):
        stamp = f"{m}/{d}/{y} 10:{i // 60:02d}:{i % 60:02d}"
        temp = 90.25 if i < 30 else 60.25
        # Integer-heavy columns next to fractional ones, exactly like the real logs.
        fields = ["45", "30", "1050", f"{temp}", "55.5", f"{temp - 2}", "1.05"]
        if decimal == ",":
            fields = [f'"{f.replace(".", ",")}"' for f in fields]
        lines.append(",".join([stamp] + fields))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestImporter(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.store = Store(self.root / "t.db", 2, 90, 5)
        self.day = datetime.fromtimestamp(time.time() - 86400).strftime("%Y-%m-%d")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def peaks(self, key="cpu.temp"):
        return self.store._r.execute(
            "SELECT n, total/n, hi FROM sample_1m s JOIN metric m ON m.id = s.metric_id "
            "WHERE m.key = ? ORDER BY bucket", (key,)).fetchall()

    def test_maps_columns_to_metric_keys(self):
        path = write_log(self.root, self.day, 120)
        result = import_csv(self.store, path)
        self.assertFalse(result["skipped"])
        self.assertEqual(result["rows"], 120)
        self.assertEqual(self.store.keys(),
                         sorted(["cpu.temp", "cpu.load", "fan.cpu", "fan.sys1",
                                 "gpu.temp_hotspot", "rpm.cpu"]))

    def test_values_land_in_minute_rollups(self):
        path = write_log(self.root, self.day, 120)
        import_csv(self.store, path)
        rows = self.peaks()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], 60)
        self.assertAlmostEqual(rows[0][2], 90.25)   # first minute peaked at 90.25
        self.assertAlmostEqual(rows[1][2], 60.25)

    def test_field_separators_are_not_read_as_decimal_commas(self):
        """A dot-decimal log full of integer columns must not be scaled by 10^n."""
        path = write_log(self.root, self.day, 120, decimal=".")
        import_csv(self.store, path)
        self.assertAlmostEqual(self.peaks()[0][2], 90.25)
        self.assertAlmostEqual(self.peaks("rpm.cpu")[0][2], 1050)
        self.assertAlmostEqual(self.peaks("fan.cpu")[0][2], 45)

    def test_comma_decimal_log_is_read_correctly(self):
        path = write_log(self.root, self.day, 120, decimal=",")
        import_csv(self.store, path)
        self.assertAlmostEqual(self.peaks()[0][2], 90.25)
        self.assertAlmostEqual(self.peaks("rpm.cpu")[0][2], 1050)

    def test_reimport_is_a_no_op(self):
        path = write_log(self.root, self.day, 60)
        import_csv(self.store, path)
        again = import_csv(self.store, path)
        self.assertTrue(again["skipped"])
        total = self.store._r.execute("SELECT sum(n) FROM sample_1m s JOIN metric m "
                                      "ON m.id = s.metric_id WHERE m.key='cpu.temp'").fetchone()[0]
        self.assertEqual(total, 60)

    def test_imported_history_answers_the_85_question(self):
        path = write_log(self.root, self.day, 120)
        import_csv(self.store, path)
        now = int(time.time())
        summary = self.store.summarize(["cpu.temp"], now - 3 * 86400, now, 85.0)
        self.assertAlmostEqual(summary["cpu.temp"]["max"], 90.2)
        self.assertGreater(summary["cpu.temp"]["seconds_above"], 0)

    def test_ignores_unrecognised_columns(self):
        path = write_log(self.root, self.day, 10)
        import_csv(self.store, path)
        self.assertNotIn("gpu.core_voltage", self.store.keys())


if __name__ == "__main__":
    unittest.main()
