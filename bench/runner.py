"""Adaptive lifecycle experiment with continuous sampling and a safety watchdog."""

import hashlib
import json
import os
import shutil
import statistics
import threading
import time
import uuid
from pathlib import Path

from .backend import Backend
from .common import GIB, MIB, ROOT, STATE, kubectl, run, write_json
from .metrics import Collector
from .policy import next_count, safety_reason, stable
from .report import render


class SafetyStop(RuntimeError):
    pass


def finished_sweeps(status):
    completed = {(r["backend"], r["scenario"], r["trial"]) for r in status.get("completed", [])}
    return {
        (r["backend"], r["scenario"], r["trial"])
        for r in status.get("capacity_stops", [])
        if (r["backend"], r["scenario"], r["trial"]) in completed
        and r["reason"]
        in {
            "predicted resource threshold",
            "host memory pressure",
            "host RAM reserve reached",
            "node memory threshold reached",
            "host CPU exceeded 75% for 30 seconds",
            "disk reserve reached",
        }
    }


class Experiment:
    def __init__(self, args):
        self.args = args
        self.path = ROOT / "results" / args.name
        self.path.mkdir(parents=True, exist_ok=False)
        self.limits = json.loads((STATE / "ownership.json").read_text())
        self.context = {
            "backend": "none",
            "scenario": "none",
            "trial": 0,
            "count": 0,
            "phase": "setup",
            "window": "transition",
            "stable": False,
        }
        self.samples = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.abort = None
        self.backend = None
        self.thread = None
        self.completed = []
        self.stops = []
        self.skip_sweeps = set()
        continuation = None
        if getattr(args, "continue_from", None):
            previous = Path(args.continue_from).resolve()
            previous_status = json.loads((previous / "status.json").read_text())
            if previous_status["status"] == "running":
                raise RuntimeError("Cannot continue an active run")
            self.skip_sweeps = finished_sweeps(previous_status)
            continuation = {
                "source": str(previous),
                "prior_status": previous_status,
                "skipped_sweeps": sorted(self.skip_sweeps),
                "policy": "Completed sweeps preserved separately; incomplete trials restarted from count 1",
            }
        self.worst_cost = {}
        self.events = (self.path / "events.jsonl").open("w")
        self.raw = (self.path / "samples.jsonl").open("w")
        self.collector = Collector()
        if not self.collector.node:
            raise RuntimeError("Owned kind node is unavailable")
        initial = self.collector.sample()["node"]
        if initial["limit"] != self.limits["memory_limit"]:
            raise RuntimeError(
                "Effective node memory limit differs from the recorded safety ceiling"
            )
        self.limits["initial_oom_kill"] = initial.get("oom_kill", 0)
        self.metadata = {
            "started_at": time.time(),
            "continuation": continuation,
            "monitoring_deployment": json.loads(
                kubectl("get", "deployment", "prometheus", "-n", "otel-system", "-o", "json")
            )["spec"],
            "arguments": vars(args),
            "limits": self.limits,
            "versions": json.loads((ROOT / "config/versions.json").read_text()),
            "workload": json.loads((STATE / "workload-lock.json").read_text()),
            "compatibility_patch_sha256": hashlib.sha256(
                (ROOT / "patches/substrate-overlayfs-legacy.patch").read_bytes()
            ).hexdigest(),
            "host": run(["uname", "-a"]).strip(),
            "cpu_count": len(os.sched_getaffinity(0)),
            "sandbox_config": json.loads(
                kubectl("get", "sandboxconfig", "gvisor-default", "-o", "json")
            ),
            "worker_image": (STATE / "worker-image").read_text().strip(),
            "infrastructure_images": {
                p["metadata"]["namespace"] + "/" + p["metadata"]["name"]: [
                    {"container": c["name"], "image": c["image"], "image_id": c.get("imageID")}
                    for c in p.get("status", {}).get("containerStatuses", [])
                ]
                for p in json.loads(kubectl("get", "pods", "-A", "-o", "json"))["items"]
            },
            "limitations": [
                "AX limits enforced at worker cgroup, not actor template",
                "host baselines during setup may include compiler/image-build activity; node baselines exclude host build processes",
            ],
        }
        write_json(self.path / "metadata.json", self.metadata)
        shutil.copytree(STATE / "baselines", self.path / "installation-baselines")
        write_json(self.path / "status.json", {"status": "running"})

    def event(self, kind, **data):
        with self.lock:
            self.events.write(
                json.dumps({"timestamp": time.time(), "event": kind, **self.context, **data}) + "\n"
            )
            self.events.flush()
        print(
            f"{self.context['backend']}/{self.context['scenario']} "
            f"trial={self.context['trial']} n={self.context['count']} {kind}: "
            f"{data.get('phase', data.get('reason', data.get('name', '')))}",
            flush=True,
        )

    def check(self):
        if self.abort:
            raise SafetyStop(self.abort)

    def monitor(self):
        previous = None
        hot = 0
        missing = 0
        while not self.stop.is_set():
            start = time.monotonic()
            try:
                sample = self.collector.sample()
                if previous:
                    total = sample["host"]["cpu_total"] - previous["host"]["cpu_total"]
                    idle = sample["host"]["cpu_idle"] - previous["host"]["cpu_idle"]
                    sample["host"]["cpu_fraction"] = 1 - idle / total if total > 0 else 0
                    hot = (
                        hot + max(0, sample["timestamp"] - previous["timestamp"])
                        if sample["host"]["cpu_fraction"] > 0.75
                        else 0
                    )
                reason = safety_reason(sample, self.limits, previous, hot)
                with self.lock:
                    sample.update(self.context)
                    self.samples.append(sample)
                    self.raw.write(json.dumps(sample) + "\n")
                    self.raw.flush()
                expected = sample.get("count", 0)
                measured_state = sample.get("phase") in {
                    "idle",
                    "active",
                    "post-workload-idle",
                    "resumed",
                }
                missing = (
                    missing + 1 if measured_state and len(sample["sandboxes"]) != expected else 0
                )
                if missing >= 3:
                    reason = "readiness failure: sandbox count changed during measurement"
                previous = sample
                if reason and not self.abort:
                    self.abort = reason
                    self.event("safety-stop", reason=reason)
                    # Avoid checkpoint bursts under pressure. Delete the newest actor.
                    if self.backend and self.backend.names:
                        name = self.backend.names[-1]
                        backend = self.backend

                        def release_newest():
                            try:
                                backend.delete(name)
                            except Exception as exc:
                                if not self.stop.is_set():
                                    self.event("emergency-cleanup-error", error=str(exc))

                        threading.Thread(target=release_newest, daemon=True).start()
            except Exception as exc:
                self.abort = "collector failure: " + str(exc)
                self.event("collector-error", error=str(exc))
            self.stop.wait(max(0, 1 - (time.monotonic() - start)))

    def phase(self, name):
        self.check()
        with self.lock:
            self.context = {**self.context, "phase": name, "window": "settling", "stable": False}
        self.event("phase", phase=name)
        start = time.monotonic()
        offset = len(self.samples)
        settled = False
        window = 3 if self.args.smoke else 20
        timeout = 20 if self.args.smoke else 300
        while time.monotonic() - start < timeout:
            self.check()
            with self.lock:
                subset = self.samples[offset:]
            node = [s.get("node", {}).get("working_set") for s in subset]
            sandbox = [
                sum(
                    x["working_set"]
                    for x in s.get("sandboxes", [])
                    if x.get("working_set") is not None
                )
                for s in subset
            ]
            if stable(node, window) and stable(sandbox, window):
                settled = True
                break
            time.sleep(1)
        with self.lock:
            self.context = {**self.context, "window": "measurement", "stable": settled}
        self.event("measurement-start", settled=settled)
        end = time.monotonic() + (5 if self.args.smoke else self.args.sample_seconds)
        while time.monotonic() < end:
            self.check()
            time.sleep(1)
        with self.lock:
            self.context = {**self.context, "window": "transition"}
        self.event("measurement-end", settled=settled)
        return settled

    def transition(self, name):
        with self.lock:
            self.context = {**self.context, "phase": name, "window": "transition", "stable": False}
        self.event("transition", phase=name)

    def workload(self, scenario, names, duration=0):
        jobs = {
            n: self.backend.job(
                n,
                ["python", "/opt/bench/tasks.py", scenario, "--seconds", str(duration)],
                timeout=duration + 300,
            )
            for n in names
        }
        return jobs

    def await_jobs(self, jobs):
        results = {}
        for n, j in jobs.items():
            self.check()
            results[n] = self.backend.await_job(n, j, timeout=900)
        return results

    def cleanup_cohort(self, names):
        failures = []
        for name in reversed(names):
            try:
                self.backend.delete(name)
            except Exception as exc:
                failures.append(str(exc))
        self.backend.scale(0)
        if failures:
            raise RuntimeError("Cleanup incomplete: " + str(failures))

    def cohort(self, kind, scenario, trial, count):
        with self.lock:
            self.context.update(backend=kind, scenario=scenario, trial=trial, count=count)
        self.phase("baseline")
        self.backend.scale(count)
        self.phase("empty-workers")
        self.transition("create")
        if kind == "substrate":
            self.backend.template()
        names = []
        try:
            for i in range(count):
                self.check()
                name = f"b-{kind}-{scenario[:3]}-{trial}-{count}-{i}-{uuid.uuid4().hex[:5]}"
                names.append(name)
                self.backend.create(name)
            # Validate runtime and collect actual worker cgroup limits.
            sample = self.collector.sample()
            if len(sample["sandboxes"]) != count:
                raise RuntimeError(
                    f"Expected {count} gVisor cgroups, found {len(sample['sandboxes'])}"
                )
            workers = [
                p
                for p in sample["pods"]
                if p["namespace"] == "ax-ram-bench" and p["name"].startswith("bench-workers-")
            ]
            if len(workers) != count or any(p["limit"] != 2 * GIB for p in workers):
                raise RuntimeError(
                    "Worker cgroup count or effective memory limits differ from configuration"
                )
            if kind == "substrate" and any(p["limit"] != 1536 * MIB for p in sample["sandboxes"]):
                raise RuntimeError("Direct actor memory limit is not enforced in its cgroup")
            self.event(
                "runtime-verified",
                sandbox_cgroups=sample["sandboxes"],
                actor_states={n: self.backend.state(n) for n in names},
            )
            self.phase("idle")
            if scenario != "idle":
                if scenario == "developer":
                    self.transition("prepare")
                    self.await_jobs(self.workload("dev-prepare", names))
                jobs = self.workload(scenario, names, 50 if self.args.smoke else 480)
                self.phase("active")
                for n in names:
                    observed = self.backend.request(n, "/jobs/" + jobs[n])
                    self.event("active-workload-validation", name=n, result=observed)
                    if (
                        observed["exit_code"] not in [None, 0]
                        or '"cycle":' not in observed["output"]
                    ):
                        raise RuntimeError(
                            "Active workload did not complete a successful cycle: " + str(observed)
                        )
                    self.backend.request(n, "/cancel", {})
                # A completed validation cycle is required even when timed looping is cancelled.
                self.await_jobs(self.workload(scenario, names))
                self.phase("post-workload-idle")
            nonces = {n: uuid.uuid4().hex for n in names}
            for n in names:
                self.backend.request(n, "/state", {"nonce": nonces[n]})
            self.transition("suspend-transition")
            for n in names:
                self.check()
                self.backend.suspend(n)
            self.phase("suspended")
            self.backend.scale(0)
            self.phase("suspended-no-workers")
            self.transition("resume-transition")
            self.backend.scale(count)
            for n in names:
                self.check()
                self.backend.resume(n)
                state = self.backend.request(n, "/state")
                self.event("persistence-check", name=n, result=state, expected=nonces[n])
                if state["file"] != nonces[n]:
                    raise RuntimeError("Workspace persistence failed")
                if kind == "substrate" and state["nonce"] != nonces[n]:
                    raise RuntimeError("Full-state memory persistence failed")
                if kind == "ax" and state["nonce"] == nonces[n]:
                    raise RuntimeError(
                        "AX unexpectedly preserved RAM: investigate snapshot semantics"
                    )
            self.phase("resumed")
            if scenario != "idle":
                self.await_jobs(self.workload(scenario, names))
            self.completed.append(
                {"backend": kind, "scenario": scenario, "trial": trial, "count": count}
            )
        finally:
            self.transition("destroy")
            self.cleanup_cohort(names)
        self.phase("residual")

    def capacity(self, count):
        with self.lock:
            rs = [
                s
                for s in self.samples
                if all(
                    s.get(k) == self.context[k] for k in ["backend", "scenario", "trial", "count"]
                )
            ]
        current = rs[-1]
        baseline = [
            s["node"]["current"]
            for s in rs
            if s["phase"] == "baseline" and s["window"] == "measurement"
        ]
        peak = max(s["node"]["current"] for s in rs)
        cost = max(128 * MIB, (peak - statistics.mean(baseline)) / count)
        key = (self.context["backend"], self.context["scenario"])
        cost = self.worst_cost[key] = max(self.worst_cost.get(key, 0), cost)
        headroom = min(
            0.8 * self.limits["memory_limit"] - peak,
            current["host"]["available"] - self.limits["host_reserve"],
        )
        active = [s for s in rs if s["phase"] == "active" and s["window"] == "measurement"]
        cpu = max([s["host"].get("cpu_fraction", 0) for s in active] or [0])
        next_n = next_count(
            count,
            headroom,
            cost,
            cpu_headroom=max(0, 0.75 - cpu),
            cpu_per_sandbox=cpu / count if active else 0,
        )
        # Scheduling limit is a distinct stop condition, not a guessed RAM capacity.
        node = json.loads(kubectl("get", "nodes", "-o", "json"))["items"][0]
        pods = json.loads(kubectl("get", "pods", "-A", "-o", "json"))["items"]
        infra = sum(p.get("status", {}).get("phase") not in ["Succeeded", "Failed"] for p in pods)
        pod_capacity = max(0, int(node["status"]["allocatable"]["pods"]) - infra - 2)
        request_capacity = int(0.8 * self.limits["memory_limit"] // (256 * MIB))
        cpu_capacity = int(self.limits["cpus"] / 0.1)
        disk_capacity = int(max(0, current["host"]["disk_free"] - 10 * GIB) // (2 * cost))
        next_n = min(next_n, pod_capacity, request_capacity, cpu_capacity, disk_capacity)
        self.limits["disk_reserve"] = 10 * GIB + 2 * max(count, next_n) * cost
        return next_n, {
            "peak": peak,
            "cost": cost,
            "headroom": headroom,
            "cpu_fraction": cpu,
            "pod_capacity": pod_capacity,
            "request_capacity": request_capacity,
            "cpu_request_capacity": cpu_capacity,
            "disk_capacity": disk_capacity,
        }

    def execute(self):
        self.thread = threading.Thread(target=self.monitor, daemon=True)
        self.thread.start()
        status = "complete"
        error = None
        try:
            for kind in self.args.backends:
                self.backend = Backend(kind, self.event)
                self.backend.guard = self.check
                self.backend.initialize()
                for scenario in self.args.scenarios:
                    for trial in range(1, (1 if self.args.smoke else self.args.trials) + 1):
                        if (kind, scenario, trial) in self.skip_sweeps:
                            self.event(
                                "prior-sweep-preserved",
                                skipped_backend=kind,
                                skipped_scenario=scenario,
                                skipped_trial=trial,
                            )
                            continue
                        with self.lock:
                            self.samples = self.samples[-1:]
                        self.limits["disk_reserve"] = 10 * GIB + 2 * self.worst_cost.get(
                            (kind, scenario), 128 * MIB
                        )
                        count = 1
                        while True:
                            try:
                                self.cohort(kind, scenario, trial, count)
                            except SafetyStop as exc:
                                self.stops.append(
                                    {
                                        "backend": kind,
                                        "scenario": scenario,
                                        "trial": trial,
                                        "attempted_count": count,
                                        "reason": str(exc),
                                    }
                                )
                                if "OOM" in str(exc) or "collector failure" in str(exc):
                                    raise
                                # Cleanup has released workers; only proceed once pressure clears.
                                deadline = time.monotonic() + 120
                                quiet_seconds = 0
                                while time.monotonic() < deadline:
                                    with self.lock:
                                        latest = self.samples[-1] if self.samples else None
                                    clear = latest and safety_reason(latest, self.limits) is None
                                    clear = clear and latest["host"].get("cpu_fraction", 1) < 0.65
                                    quiet_seconds = quiet_seconds + 2 if clear else 0
                                    if quiet_seconds >= 10:
                                        break
                                    time.sleep(2)
                                else:
                                    raise
                                self.abort = None
                                break
                            write_json(
                                self.path / "status.json",
                                {
                                    "status": "running",
                                    "completed": self.completed,
                                    "capacity_stops": self.stops,
                                    "updated_at": time.time(),
                                },
                            )
                            render(self.path)
                            if self.args.smoke:
                                break
                            nxt, details = self.capacity(count)
                            self.event(
                                "capacity-decision", current=count, next_count=nxt, **details
                            )
                            if nxt <= count:
                                self.stops.append(
                                    {
                                        "backend": kind,
                                        "scenario": scenario,
                                        "trial": trial,
                                        "count": count,
                                        "reason": "predicted resource threshold",
                                        **details,
                                    }
                                )
                                break
                            with self.lock:
                                self.samples = self.samples[-1:]
                            count = nxt
                self.backend.close()
                self.backend = None
        except BaseException as exc:
            status = "stopped" if self.abort or isinstance(exc, KeyboardInterrupt) else "failed"
            error = str(exc)
            self.event("run-ended", reason=error)
        finally:
            if self.backend:
                for n in list(self.backend.names):
                    try:
                        self.backend.delete(n)
                    except Exception as exc:
                        self.event("cleanup-error", error=str(exc))
                try:
                    self.backend.scale(0)
                except Exception as exc:
                    self.event("cleanup-error", error=str(exc))
                self.backend.close()
            self.stop.set()
            self.thread.join(timeout=30)
            write_json(
                self.path / "status.json",
                {
                    "status": status,
                    "error": error,
                    "completed": self.completed,
                    "capacity_stops": self.stops,
                    "finished_at": time.time(),
                },
            )
            self.raw.close()
            self.events.close()
            render(self.path)
        if status != "complete":
            raise RuntimeError(error)
