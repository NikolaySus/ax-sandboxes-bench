import unittest

from bench.common import GIB
from bench.policy import safety_reason
from bench.runner import finished_sweeps


class ContinuationTests(unittest.TestCase):
    def test_preserve_resource_stop_but_retry_oom_trial(self):
        status = {
            "completed": [dict(backend="ax", scenario="idle", trial=t) for t in [1, 2]],
            "capacity_stops": [
                dict(backend="ax", scenario="idle", trial=1, reason="host memory pressure"),
                dict(backend="ax", scenario="idle", trial=2, reason="node OOM event"),
            ],
        }
        self.assertEqual(finished_sweeps(status), {("ax", "idle", 1)})

    def test_monitoring_headroom_stops_before_container_oom(self):
        sample = {
            "host": {"available": 10 * GIB, "disk_free": 30 * GIB},
            "node": {"current": 3 * GIB},
            "pods": [
                {
                    "namespace": "otel-system",
                    "name": "prometheus-test",
                    "limit": 2 * GIB,
                    "working_set": 1.7 * GIB,
                }
            ],
        }
        self.assertEqual(
            safety_reason(sample, {"host_reserve": 4 * GIB, "memory_limit": 20 * GIB}),
            "Prometheus memory threshold reached",
        )
