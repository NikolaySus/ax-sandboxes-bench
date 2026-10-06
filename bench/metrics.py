"""Read physical host cgroups without installing a monitoring stack."""

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .common import NODE, STATE, kubectl, run


def pairs(path):
    try:
        return {
            p[0].rstrip(":"): int(p[1])
            for line in Path(path).read_text().splitlines()
            if len(p := line.split()) >= 2 and p[1].isdigit()
        }
    except (OSError, ValueError):
        return {}


def number(path):
    try:
        return int(Path(path).read_text())
    except (OSError, ValueError):
        return None


def cgroup(path):
    path = Path(path)
    stat = pairs(path / "memory.stat")
    current = number(path / "memory.current")
    events = pairs(path / "memory.events")
    rss = []
    pss = []
    try:
        for pid in (path / "cgroup.procs").read_text().split():
            status = pairs(f"/proc/{pid}/status")
            rollup = pairs(f"/proc/{pid}/smaps_rollup")
            if "VmRSS" in status:
                rss.append(status["VmRSS"] * 1024)
            if "Pss" in rollup:
                pss.append(rollup["Pss"] * 1024)
    except OSError:
        pass
    return dict(
        process_rss_sum=sum(rss) if rss else None,
        process_pss_sum=sum(pss) if pss else None,
        current=current,
        working_set=max(0, current - stat.get("inactive_file", 0)) if current is not None else None,
        peak=number(path / "memory.peak"),
        anon=stat.get("anon"),
        file=stat.get("file"),
        kernel=stat.get("kernel"),
        swap=number(path / "memory.swap.current"),
        limit=number(path / "memory.max"),
        cpu_usec=pairs(path / "cpu.stat").get("usage_usec"),
        oom_kill=events.get("oom_kill"),
        path=str(path),
    )


def host():
    m = pairs("/proc/meminfo")
    cpu = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    disk = os.statvfs(STATE)
    pressure = Path("/proc/pressure/memory").read_text()
    return dict(
        total=m["MemTotal"] * 1024,
        available=m["MemAvailable"] * 1024,
        used=(m["MemTotal"] - m["MemAvailable"]) * 1024,
        free=m["MemFree"] * 1024,
        cached=m["Cached"] * 1024,
        swap_used=(m["SwapTotal"] - m["SwapFree"]) * 1024,
        cpu_total=sum(cpu),
        cpu_idle=cpu[3] + cpu[4],
        disk_free=disk.f_bavail * disk.f_frsize,
        memory_pressure=pressure,
    )


def container_path(name):
    try:
        pid = int(run(["docker", "inspect", "-f", "{{.State.Pid}}", name], timeout=10))
        line = Path(f"/proc/{pid}/cgroup").read_text().split("0::", 1)[1].strip()
        path = Path("/sys/fs/cgroup") / line.lstrip("/")
        # systemd moves kind's PID 1 into init.scope; account at the Docker parent.
        while path.name != "sys" and path != Path("/sys/fs/cgroup"):
            if path.name.startswith("docker-") and path.name.endswith(".scope"):
                return path
            if len(path.name) == 64 and all(c in "0123456789abcdef" for c in path.name):
                return path
            path = path.parent
        raise RuntimeError("Cannot resolve Docker accounting cgroup")
    except (OSError, RuntimeError, ValueError, IndexError):
        return None


class Collector:
    def __init__(self):
        self.node = container_path(NODE)
        self.registry = container_path("ax-ram-bench-registry")
        self.pods = {}
        self.refresh_at = 0
        self.refreshing = False
        self.discovery_error = None

    def sample(self):
        stamp = time.time()
        sample = dict(
            timestamp=stamp,
            host=host(),
            pods=[],
            sandboxes=[],
            observer_rss=pairs("/proc/self/status").get("VmRSS", 0) * 1024,
        )
        if self.registry:
            sample["registry"] = cgroup(self.registry)
        if not self.node:
            return sample
        sample["node"] = cgroup(self.node)
        if stamp >= self.refresh_at and not self.refreshing:
            self.refreshing = True
            self.refresh_at = stamp + 10

            def refresh():
                try:
                    items = json.loads(kubectl("get", "pods", "-A", "-o", "json", timeout=10))[
                        "items"
                    ]
                    self.pods = {
                        p["metadata"]["uid"]: (p["metadata"]["namespace"], p["metadata"]["name"])
                        for p in items
                    }
                    self.discovery_error = None
                except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                    self.discovery_error = str(exc)
                finally:
                    self.refreshing = False

            threading.Thread(target=refresh, daemon=True).start()
        if self.discovery_error:
            sample["discovery_error"] = self.discovery_error
        # Pod parents and nested sandbox leaves are distinct views, never summed together.
        for path in self.node.rglob("memory.current"):
            parent = path.parent
            match = re.search(r"pod([0-9a-f_-]{36})", parent.name)
            if match:
                uid = match[1].replace("_", "-")
                ns, name = self.pods.get(uid, ("unknown", uid))
                sample["pods"].append(dict(cgroup(parent), uid=uid, namespace=ns, name=name))
            if parent.name in ["pause", "_pause"] or parent.name.endswith("-pause"):
                match = re.search(r"pod([0-9a-f_-]{36})", str(parent))
                uid = match[1].replace("_", "-") if match else None
                ns, name = self.pods.get(uid, ("unknown", uid))
                sample["sandboxes"].append(
                    dict(cgroup(parent), pod_uid=uid, namespace=ns, pod_name=name)
                )
        sample["collection_seconds"] = time.time() - stamp
        return sample
