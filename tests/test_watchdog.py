import io
import threading
import unittest
from unittest.mock import Mock

from bench.common import GIB
from bench.runner import Experiment


class WatchdogTests(unittest.TestCase):
    def test_pressure_deletes_newest_without_snapshot(self):
        experiment = Experiment.__new__(Experiment)
        experiment.lock = threading.Lock()
        experiment.stop = threading.Event()
        experiment.abort = None
        experiment.context = {}
        experiment.samples = []
        experiment.raw = io.StringIO()
        experiment.event = Mock()
        experiment.limits = {"host_reserve": 4 * GIB, "memory_limit": 20 * GIB}
        experiment.backend = Mock(names=["older", "newest"])
        experiment.backend.delete.side_effect = lambda _: experiment.stop.set()
        experiment.collector = Mock()
        experiment.collector.sample.return_value = {
            "timestamp": 1,
            "host": {
                "available": 10 * GIB,
                "disk_free": 30 * GIB,
                "memory_pressure": "",
                "cpu_total": 10,
                "cpu_idle": 9,
            },
            "node": {"current": 17 * GIB, "swap": 0, "oom_kill": 0},
        }
        thread = threading.Thread(target=experiment.monitor, daemon=True)
        thread.start()
        thread.join(timeout=3)
        experiment.stop.set()
        self.assertFalse(thread.is_alive())
        self.assertEqual(experiment.abort, "node memory threshold reached")
        experiment.backend.delete.assert_called_once_with("newest")
        experiment.backend.suspend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
