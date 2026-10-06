import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bench.metrics import cgroup, container_path
from bench.report import render


class AccountingTests(unittest.TestCase):
    def test_cgroup_working_set_and_missing_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "memory.current").write_text("1000")
            (p / "memory.stat").write_text("inactive_file 300\nanon 500\nfile 400\n")
            (p / "memory.max").write_text("max")
            result = cgroup(p)
            self.assertEqual(result["working_set"], 700)
            self.assertIsNone(result["limit"])
            self.assertIsNone(result["peak"])
            (p / "memory.stat").write_text("inactive_file 1200\n")
            self.assertEqual(cgroup(p)["working_set"], 0)

    def test_kind_init_scope_resolves_outer_container(self):
        with (
            patch("bench.metrics.run", return_value="123"),
            patch.object(
                Path, "read_text", return_value="0::/system.slice/docker-abc.scope/init.scope\n"
            ),
        ):
            self.assertEqual(
                str(container_path("node")), "/sys/fs/cgroup/system.slice/docker-abc.scope"
            )

    def test_report_preserves_negative_delta_and_peak(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            rows = []
            for phase, values in [("baseline", [100, 100]), ("idle", [80, 90])]:
                for value in values:
                    rows.append(
                        {
                            "backend": "ax",
                            "scenario": "idle",
                            "trial": 1,
                            "count": 2,
                            "phase": phase,
                            "window": "measurement",
                            "stable": True,
                            "node": {"working_set": value},
                            "sandboxes": [],
                            "pods": [],
                        }
                    )
            (p / "samples.jsonl").write_text("\n".join(map(json.dumps, rows)))
            result = render(p)
            idle = next(r for r in result if r["phase"] == "idle")
            self.assertEqual(idle["incremental_node_bytes_per_sandbox"], -7.5)
            self.assertEqual(idle["metrics"]["node_working_set"]["peak"], 90)
            self.assertTrue((p / "memory.svg").exists())


if __name__ == "__main__":
    unittest.main()
