"""Lifecycle adapters: all actor state changes go through AX/Substrate."""

import json
import secrets
import subprocess
import time
import urllib.error
import urllib.request

from .common import NODE, NS, STATE, apply, kubectl, run, wait_for, write_json


def ate(*args, **kwargs):
    return run(["kubectl-ate", "--context", "kind-ax-ram-bench", *args], **kwargs)


class Backend:
    def __init__(self, kind, event=lambda *a, **k: None):
        self.kind = kind
        self.event = event
        self.forward = None
        self.ax_forward = None
        self.image = (STATE / "workload-image").read_text().strip()
        token = STATE / "token"
        if not token.exists():
            token.write_text(secrets.token_hex(24))
            token.chmod(0o600)
        self.token = token.read_text().strip()
        self.names = []
        self.worker_count = 0
        self.guard = lambda: None

    def initialize(self):
        apply(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": NS, "labels": {"ax-ram-bench": "owned"}},
            }
        )
        ate("create", "atespace", NS, check=False)
        existing = json.loads(ate("get", "actors", "-a", NS, "-o", "json"))
        if existing.get("actors"):
            raise RuntimeError(
                "Benchmark actors remain from an earlier run; run scripts/cleanup.sh first"
            )
        if not (STATE / "worker-image").exists():
            image = (
                run(
                    ["ko", "build", "./cmd/ateom-gvisor"],
                    cwd=STATE / "sources/substrate",
                    timeout=1800,
                )
                .strip()
                .splitlines()[-1]
            )
            (STATE / "worker-image").write_text(image + "\n")
        run(["docker", "exec", NODE, "mkdir", "-p", "/opt/bench-assets"])
        if not (STATE / "assets-installed").exists():
            run(
                ["docker", "cp", str(STATE / "assets") + "/.", NODE + ":/opt/bench-assets"],
                timeout=300,
            )
            (STATE / "assets-installed").touch()
        apply(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "bench-assets", "namespace": NS},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "bench-assets"}},
                    "template": {
                        "metadata": {"labels": {"app": "bench-assets"}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "http",
                                    "image": self.image,
                                    "command": [
                                        "python",
                                        "-m",
                                        "http.server",
                                        "80",
                                        "--directory",
                                        "/assets",
                                    ],
                                    "resources": {
                                        "requests": {"memory": "32Mi", "cpu": "10m"},
                                        "limits": {"memory": "128Mi", "cpu": "1"},
                                    },
                                    "volumeMounts": [
                                        {"name": "assets", "mountPath": "/assets", "readOnly": True}
                                    ],
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "assets",
                                    "hostPath": {"path": "/opt/bench-assets", "type": "Directory"},
                                }
                            ],
                        },
                    },
                },
            }
        )
        apply(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "bench-assets", "namespace": NS},
                "spec": {
                    "selector": {"app": "bench-assets"},
                    "ports": [{"port": 80, "targetPort": 80}],
                },
            }
        )
        kubectl("rollout", "status", "deployment/bench-assets", "-n", NS, "--timeout=180s")
        self.scale(0)
        log = (STATE / "logs/router-forward.log").open("a")
        self.forward = subprocess.Popen(
            [
                "kubectl",
                "--context=kind-ax-ram-bench",
                "-n",
                "ate-system",
                "port-forward",
                "svc/atenet-router",
                "18080:80",
                "--address=127.0.0.1",
            ],
            stdout=log,
            stderr=log,
        )
        time.sleep(2)
        if self.forward.poll() is not None:
            raise RuntimeError("Router port-forward failed; see router-forward.log")
        if self.kind == "ax":
            self.ax_forward = subprocess.Popen(
                [
                    "kubectl",
                    "--context=kind-ax-ram-bench",
                    "-n",
                    "ax-system",
                    "port-forward",
                    "svc/ax-server",
                    "18082:8080",
                    "--address=127.0.0.1",
                ],
                stdout=log,
                stderr=log,
            )
            time.sleep(1)
            if self.ax_forward.poll() is not None:
                raise RuntimeError("AX port-forward failed")

    def scale(self, n):
        if n > self.worker_count + 1:
            for count in range(self.worker_count + 1, n + 1):
                self.guard()
                self.scale(count)
            return
        if n > 0:
            self.guard()
        apply(
            {
                "apiVersion": "ate.dev/v1alpha1",
                "kind": "WorkerPool",
                "metadata": {"name": "bench-workers", "namespace": NS},
                "spec": {
                    "replicas": n,
                    "sandboxClass": "gvisor",
                    "workerImage": (STATE / "worker-image").read_text().strip(),
                    "template": {
                        "resources": {
                            "requests": {"memory": "256Mi", "cpu": "100m"},
                            "limits": {"memory": "2Gi", "cpu": "1"},
                        }
                    },
                },
            }
        )

        def ready():
            obj = json.loads(kubectl("get", "workerpool", "bench-workers", "-n", NS, "-o", "json"))
            status = obj.get("status", {})
            return status.get("readyReplicas", 0) == n and status.get("replicas", 0) == n

        wait_for(ready, timeout=300)
        self.worker_count = n
        self.event("workers-scaled", count=n)

    def template(self):
        obj = {
            "metadata": {"name": "bench-full", "atespace": NS},
            "containers": [
                {
                    "name": "guest",
                    "image": self.image,
                    "command": ["/usr/local/bin/ax-task-runner"],
                    "env": [{"name": "BENCH_TOKEN", "value": self.token}],
                    "readyz": {"httpGet": {"path": "/readyz", "port": 80}},
                    "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
                }
            ],
            "volumes": [{"name": "workspace", "durableDir": {}}],
            "resources": {
                "limits": [
                    {"name": "cpu", "quantity": "1"},
                    {"name": "memory", "quantity": "1536Mi"},
                ]
            },
            "snapshotsConfig": {
                "onPause": "SNAPSHOT_CONTENT_SCOPE_FULL",
                "onCommit": "SNAPSHOT_CONTENT_SCOPE_FULL",
                "storageLocation": "gs://ate-snapshots/ax-ram-bench/direct/",
            },
            "sandboxConfig": {
                "sandboxClass": "SANDBOX_CLASS_GVISOR",
                "configName": "gvisor-default",
            },
        }
        path = STATE / "direct-template.json"
        write_json(path, obj)
        # Existing immutable templates are only reused if created by this owned setup.
        raw = ate(
            "get", "actor-template", "bench-full", "-a", NS, "-o", "json", check=False
        ).strip()
        templates = json.loads(raw).get("actorTemplates", []) if raw else []
        if templates:
            old = templates[0]
            keys = ["containers", "volumes", "resources", "snapshotsConfig", "sandboxConfig"]
            failed = old.get("status", {}).get("goldenSnapshotStatus", {}).get("errorMessage")
            if failed or any(old.get(key) != obj[key] for key in keys):
                ate("delete", "actor-template", "bench-full", "-a", NS)
                templates = []
        if not templates:
            ate("create", "actor-template", "-f", path)

        def golden_ready():
            obj = json.loads(ate("get", "actor-template", "bench-full", "-a", NS, "-o", "json"))
            status = obj["actorTemplates"][0].get("status", {}).get("goldenSnapshotStatus", {})
            if status.get("errorMessage"):
                raise AssertionError("Golden snapshot failed: " + status["errorMessage"])
            return status.get("goldenSnapshot", {}).get("snapshotUri")

        wait_for(golden_ready, timeout=300)

    def create(self, name):
        self.names.append(name)
        if self.kind == "ax":
            obj = {
                "apiVersion": "ax.io/v1alpha1",
                "kind": "Task",
                "metadata": {"name": name, "atespace": NS},
                "spec": {
                    "image": self.image,
                    "env": [{"name": "BENCH_TOKEN", "value": self.token}],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1536Mi"},
                    },
                },
            }
            path = STATE / f"{name}.json"
            write_json(path, obj)
            run(
                [
                    "ax",
                    "--server",
                    "127.0.0.1:18082",
                    "--context",
                    "kind-ax-ram-bench",
                    "apply",
                    "-f",
                    path,
                ],
                timeout=300,
            )
        else:
            ate("create", "actor", name, "--template", "bench-full", "-a", NS)
        self.resume(name)
        self.event("created", name=name, state=self.state(name))

    def state(self, name):
        return json.loads(ate("get", "actor", name, "-a", NS, "-o", "json"))["actors"][0]

    def resume(self, name):
        if self.kind == "ax":
            run(
                [
                    "ax",
                    "--server",
                    "127.0.0.1:18082",
                    "--context",
                    "kind-ax-ram-bench",
                    "resume",
                    "task",
                    name,
                    "-a",
                    NS,
                ],
                timeout=300,
            )
        else:
            ate("resume", "actor", name, "-a", NS, timeout=300)
        wait_for(lambda: self.request(name, "/readyz").get("ready"), timeout=300)

    def suspend(self, name):
        if self.kind == "ax":
            run(
                [
                    "ax",
                    "--server",
                    "127.0.0.1:18082",
                    "--context",
                    "kind-ax-ram-bench",
                    "suspend",
                    "task",
                    name,
                    "-a",
                    NS,
                ],
                timeout=300,
            )
        else:
            ate("suspend", "actor", name, "-a", NS, timeout=300)
        wait_for(lambda: "ACTOR_STATE_SUSPENDED" in json.dumps(self.state(name)), timeout=300)
        self.event("suspended", name=name, state=self.state(name))

    def delete(self, name):
        try:
            if self.kind == "ax":
                run(
                    [
                        "ax",
                        "--server",
                        "127.0.0.1:18082",
                        "--context",
                        "kind-ax-ram-bench",
                        "delete",
                        "task",
                        name,
                        "-a",
                        NS,
                    ],
                    timeout=330,
                )
            else:
                ate("delete", "actor", name, "-a", NS, "--any-state", timeout=300)
        except RuntimeError as exc:
            if "not found" not in str(exc).lower() and "notfound" not in str(exc).lower():
                raise
        if name in self.names:
            self.names.remove(name)

    def request(self, name, path, data=None):
        req = urllib.request.Request(
            "http://127.0.0.1:18080" + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "ate-target-actor": NS + "/" + name,
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.load(resp)
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{name} {path}: {exc}") from exc

    def job(self, name, argv, timeout=600):
        return self.request(name, "/jobs", {"argv": argv, "timeout": timeout})["id"]

    def await_job(self, name, job, timeout=600):
        def done():
            value = self.request(name, "/jobs/" + job)
            return value if value.get("exit_code") is not None else None

        result = wait_for(done, timeout=timeout)
        self.event("job-complete", name=name, job=job, result=result)
        if result["exit_code"] != 0:
            raise RuntimeError(f"Workload failed: {result}")
        return result

    def close(self):
        for process in [self.forward, self.ax_forward]:
            if process:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
