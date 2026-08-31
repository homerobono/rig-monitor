import tempfile
import time
import unittest
from pathlib import Path

from rigmon.storage import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "t.db", raw_retention_days=2,
                           rollup_retention_days=90, step=5)
        self.now = int(time.time()) // 60 * 60

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def fill(self, start, count, step=5, key="cpu.temp", fn=lambda i: 50.0 + i):
        for i in range(count):
            self.store.write(start + i * step, {key: fn(i)})


class TestQuery(StoreTest):
    def test_raw_bucketing(self):
        start = self.now - 300
        self.fill(start, 60)
        res = self.store.query(["cpu.temp"], start, self.now, max_points=6)
        self.assertEqual(res["source"], "raw")
        self.assertEqual(res["bucket"], 50)
        self.assertEqual(len(res["t"]), len(res["series"]["cpu.temp"]["avg"]))
        avg = [v for v in res["series"]["cpu.temp"]["avg"] if v is not None]
        mx = [v for v in res["series"]["cpu.temp"]["max"] if v is not None]
        self.assertTrue(all(a <= m for a, m in zip(avg, mx)))
        self.assertAlmostEqual(max(mx), 109.0)

    def test_gaps_become_null(self):
        self.fill(self.now - 600, 10)          # 50 s of data, then nothing
        res = self.store.query(["cpu.temp"], self.now - 600, self.now, max_points=20)
        values = res["series"]["cpu.temp"]["avg"]
        self.assertIsNotNone(values[0])
        self.assertIsNone(values[-1])

    def test_unknown_key_is_ignored(self):
        self.fill(self.now - 60, 12)
        res = self.store.query(["cpu.temp", "nope"], self.now - 60, self.now, 10)
        self.assertIn("cpu.temp", res["series"])
        self.assertNotIn("nope", res["series"])


class TestRollup(StoreTest):
    def test_rollup_preserves_average_and_peak(self):
        start = self.now - 600
        self.fill(start, 120)                 # 10 minutes at 5 s
        self.store.rollup(self.now)
        rows = self.store._r.execute(
            "SELECT bucket, total/n, hi, n FROM sample_1m ORDER BY bucket").fetchall()
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(n == 12 for _, _, _, n in rows))
        self.assertAlmostEqual(rows[0][1], 50 + 5.5)      # mean of 50..61
        self.assertAlmostEqual(rows[0][2], 61.0)          # peak of the first minute

    def test_rollup_is_not_double_counted(self):
        self.fill(self.now - 600, 120)
        self.store.rollup(self.now)
        self.store.rollup(self.now)
        total_n = self.store._r.execute("SELECT sum(n) FROM sample_1m").fetchone()[0]
        self.assertEqual(total_n, 120)

    def test_long_range_reads_from_rollups(self):
        start = self.now - 6 * 3600
        self.fill(start, 720, step=30)
        self.store.rollup(self.now)
        res = self.store.query(["cpu.temp"], start, self.now, max_points=60)
        self.assertIn(res["source"], ("rollup", "mixed"))
        self.assertGreaterEqual(res["bucket"], 60)
        self.assertTrue(any(v is not None for v in res["series"]["cpu.temp"]["avg"]))


class TestSummary(StoreTest):
    def test_detects_episodes_above_threshold(self):
        base = self.now - 600
        # two separate spikes over 85, five minutes apart
        for i in range(120):
            temp = 70.0
            if 10 <= i < 16:
                temp = 88.0
            if 90 <= i < 94:
                temp = 91.5
            self.store.write(base + i * 5, {"gpu.temp_hotspot": temp})
        res = self.store.summarize(["gpu.temp_hotspot"], base, self.now, 85.0)
        s = res["gpu.temp_hotspot"]
        self.assertEqual(s["episode_count"], 2)
        self.assertAlmostEqual(s["max"], 91.5)
        self.assertEqual(s["seconds_above"], 10 * 5)
        self.assertAlmostEqual(s["episodes"][0]["peak"], 88.0)

    def test_quiet_window_reports_nothing(self):
        self.fill(self.now - 300, 60, fn=lambda i: 60.0)
        res = self.store.summarize(["cpu.temp"], self.now - 300, self.now, 85.0)
        self.assertEqual(res["cpu.temp"]["seconds_above"], 0)
        self.assertEqual(res["cpu.temp"]["episode_count"], 0)


class TestRetention(StoreTest):
    def test_prune_drops_old_raw_but_keeps_rollups(self):
        old = self.now - 5 * 86400
        self.fill(old, 60)
        self.fill(self.now - 300, 60)
        self.store.rollup(self.now)
        raw_removed, agg_removed = self.store.prune(self.now)
        self.assertEqual(raw_removed, 60)
        self.assertEqual(agg_removed, 0)
        self.assertIsNotNone(
            self.store._r.execute("SELECT count(*) FROM sample_1m").fetchone()[0])

    def test_coverage_spans_both_tables(self):
        self.fill(self.now - 300, 60)
        self.store.rollup(self.now)
        cov = self.store.coverage()
        self.assertLessEqual(cov["first"], self.now - 295)
        self.assertGreaterEqual(cov["last"], self.now - 300)


if __name__ == "__main__":
    unittest.main()
