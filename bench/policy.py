"""Pure measurement and resource-admission policy, shared by runner and tests."""

import math
import statistics

from .common import GIB, MIB


def summary(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"samples": 0, "mean": None, "median": None, "peak": None}
    return dict(
        samples=len(values),
        mean=statistics.mean(values),
        median=statistics.median(values),
        peak=max(values),
    )


def stable(values, window=20):
    if len(values) < 3 * window or any(v is None for v in values[-3 * window :]):
        return False
    windows = [
        values[-3 * window + i * window : -3 * window + (i + 1) * window or None] for i in range(3)
    ]
    for metric in [statistics.mean, statistics.median]:
        results = [metric(w) for w in windows]
        if max(results) - min(results) > max(16 * MIB, 0.05 * max(results)):
            return False
    return True


def next_count(current, headroom, per_sandbox, cpu_headroom=None, cpu_per_sandbox=0):
    proposed = next(
        (n for n in [1, 2, 5, 10] if n > current), max(current + 1, math.ceil(current * 1.5))
    )
    admitted = int(max(0, headroom) // max(1, per_sandbox * 1.5))
    if cpu_headroom is not None and cpu_per_sandbox > 0:
        admitted = min(admitted, int(max(0, cpu_headroom) // (cpu_per_sandbox * 1.5)))
    return min(proposed, current + admitted)


def safety_reason(sample, limits, previous=None, cpu_hot_seconds=0):
    h = sample["host"]
    node = sample.get("node", {})
    if h["available"] < limits["host_reserve"]:
        return "host RAM reserve reached"
    if node.get("current", 0) > 0.8 * limits["memory_limit"]:
        return "node memory threshold reached"
    if h["disk_free"] < limits.get("disk_reserve", 10 * GIB):
        return "disk reserve reached"
    if node.get("swap", 0):
        return "benchmark node used swap"
    if node.get("oom_kill", 0) > limits.get("initial_oom_kill", 0):
        return "node OOM event"
    for pod in sample.get("pods", []):
        if pod.get("namespace") == "otel-system" and pod.get("name", "").startswith("prometheus-"):
            if pod.get("limit") and pod.get("working_set", 0) > 0.8 * pod["limit"]:
                return "Prometheus memory threshold reached"
    if cpu_hot_seconds >= 30:
        return "host CPU exceeded 75% for 30 seconds"
    if previous:
        for kind in ["some", "full"]:
            line = next(
                (
                    entry
                    for entry in h["memory_pressure"].splitlines()
                    if entry.startswith(kind + " ")
                ),
                "",
            )
            fields = dict(x.split("=") for x in line.split()[1:])
            if float(fields.get("avg10", 0)) > (5 if kind == "some" else 1):
                return "host memory pressure"
    return None
