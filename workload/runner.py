#!/usr/local/bin/python3
"""Provider-free AX runner and local benchmark command endpoint."""

import hmac
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

WORKSPACE = Path("/workspace")
WORKSPACE.mkdir(exist_ok=True)
JOBS = {}
NONCE = None
TOKEN = os.environ.get("BENCH_TOKEN", "")
TASK = yaml.safe_load(os.environ.get("AX_TASK_YAML", "{}")) or {}


def stop_job(job):
    p = job["process"]
    if p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=5)


def launch(argv, timeout=600):
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        raise ValueError("argv must be a nonempty string array")
    # Pipe reader retains only the last 64 KiB; no unbounded output files in RAM.
    p = subprocess.Popen(
        argv,
        cwd=WORKSPACE,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    key = uuid.uuid4().hex
    job = {
        "process": p,
        "output": b"",
        "started": time.time(),
        "exit_code": None,
        "timed_out": False,
    }
    JOBS[key] = job

    def read():
        while chunk := p.stdout.read1(4096):
            job["output"] = (job["output"] + chunk)[-65536:]
        job["exit_code"] = p.wait()

    def deadline():
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            job["timed_out"] = True
            stop_job(job)

    threading.Thread(target=read, daemon=True).start()
    threading.Thread(target=deadline, daemon=True).start()
    return key


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, value, status=200, content_type="application/json"):
        data = json.dumps(value).encode() if content_type == "application/json" else value.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return bool(TOKEN) and hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + TOKEN
        )

    def do_GET(self):
        if self.path.split("?")[0] in ["/healthz", "/readyz"]:
            return self.send({"ready": True})
        if self.path.startswith("/metadata/v1alpha1/ax/"):
            name = "AX_TASK_YAML" if self.path.endswith("/task") else "AX_WORKSPACES_YAML"
            return self.send(os.environ.get(name, ""), content_type="application/yaml")
        if not self.authorized():
            return self.send({"error": "unauthorized"}, 401)
        if self.path == "/state":
            return self.send(
                {
                    "nonce": NONCE,
                    "pid": os.getpid(),
                    "file": (WORKSPACE / "nonce").read_text()
                    if (WORKSPACE / "nonce").exists()
                    else None,
                }
            )
        if self.path.startswith("/jobs/"):
            job = JOBS.get(self.path.rsplit("/", 1)[1])
            if not job:
                return self.send({"error": "unknown job"}, 404)
            return self.send(
                {k: v for k, v in job.items() if k not in ["process", "output"]}
                | {
                    "output": job["output"].decode(errors="replace"),
                    "exit_code": job["exit_code"],
                }
            )
        return self.send({"error": "not found"}, 404)

    def do_POST(self):
        global NONCE
        if not self.authorized():
            return self.send({"error": "unauthorized"}, 401)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 65536:
                raise ValueError("request too large")
            data = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/jobs":
                return self.send(
                    {"id": launch(data["argv"], min(3600, int(data.get("timeout", 600))))}, 202
                )
            if self.path == "/cancel":
                for job in list(JOBS.values()):
                    stop_job(job)
                return self.send({"cancelled": True})
            if self.path == "/state":
                NONCE = data["nonce"]
                (WORKSPACE / "nonce").write_text(NONCE)
                return self.send({"nonce": NONCE})
            return self.send({"error": "not found"}, 404)
        except (ValueError, KeyError, OSError) as exc:
            return self.send({"error": str(exc)}, 400)


def shutdown(*_):
    for job in list(JOBS.values()):
        stop_job(job)
    raise SystemExit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
command = TASK.get("spec", {}).get("command")
if command:
    launch(command)
ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "80"))), Handler).serve_forever()
