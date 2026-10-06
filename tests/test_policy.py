import unittest

from bench.common import GIB, MIB
from bench.policy import next_count, safety_reason, stable, summary
from bench.report import flatten


class PolicyTests(unittest.TestCase):
    def test_missing_is_not_zero(self):
        self.assertEqual(summary([None, None])["mean"], None)
        self.assertEqual(summary([1, None, 3])["mean"], 2)

    def test_stability(self):
        self.assertTrue(stable([100 * MIB] * 60))
        self.assertFalse(stable([100 * MIB] * 40 + [200 * MIB] * 20))
        self.assertFalse(stable([None] * 60))
        self.assertFalse(stable([100] * 59))

    def test_resource_limited_progression(self):
        self.assertEqual(next_count(1, 10 * GIB, GIB), 2)
        self.assertEqual(next_count(2, 2 * GIB, GIB), 3)
        self.assertEqual(next_count(5, GIB, GIB), 5)
        self.assertEqual(next_count(2, 20 * GIB, GIB, 0.1, 0.2), 2)

    def test_pressure(self):
        limits = {"host_reserve": 4 * GIB, "memory_limit": 20 * GIB}
        sample = {
            "host": {"available": 10 * GIB, "disk_free": 30 * GIB, "memory_pressure": ""},
            "node": {"current": 3 * GIB, "swap": 0, "oom_kill": 0},
        }
        self.assertIsNone(safety_reason(sample, limits))
        sample["node"]["swap"] = 4096
        self.assertIn("swap", safety_reason(sample, limits))
        sample["node"]["swap"] = 0
        sample["node"]["current"] = 17 * GIB
        self.assertIn("node memory", safety_reason(sample, limits))

    def test_nested_cgroups_not_added(self):
        row = flatten(
            {
                "node": {"working_set": 1000},
                "pods": [
                    {"namespace": "ax-ram-bench", "name": "bench-workers-1", "working_set": 500}
                ],
                "sandboxes": [{"working_set": 200}],
            }
        )
        self.assertEqual(row["node_working_set"], 1000)
        self.assertEqual(row["workers_working_set"], 500)
        self.assertEqual(row["sandbox_working_set"], 200)

    def test_unavailable_sandbox(self):
        self.assertIsNone(flatten({"sandboxes": []})["sandbox_working_set"])


if __name__ == "__main__":
    unittest.main()
