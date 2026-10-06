#!/usr/bin/env python3
"""Validate the exact built runner image without relying on a model or cluster."""

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

image = (Path(__file__).resolve().parents[1] / ".state/workload-image").read_text().strip()
name = "ax-bench-check-" + uuid.uuid4().hex[:8]
token = uuid.uuid4().hex


def run(*args):
    return subprocess.check_output(args, text=True).strip()


try:
    run(
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--label",
        "ax-ram-bench=owned",
        "-p",
        "127.0.0.1::80",
        "-e",
        "BENCH_TOKEN=" + token,
        image,
    )
    info = json.loads(run("docker", "inspect", name))[0]
    port = info["NetworkSettings"]["Ports"]["80/tcp"][0]["HostPort"]

    def request(path, data=None):
        req = urllib.request.Request(
            "http://127.0.0.1:" + port + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.load(response)

    for _ in range(30):
        try:
            if request("/readyz")["ready"]:
                break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.2)
    else:
        raise RuntimeError("Runner readiness timeout")
    job = request("/jobs", {"argv": ["python", "-c", 'print("x"*100000);print("sentinel")']})["id"]
    for _ in range(50):
        result = request("/jobs/" + job)
        if result["exit_code"] is not None:
            break
        time.sleep(0.1)
    assert result["exit_code"] == 0, result
    assert len(result["output"].encode()) <= 65536
    assert result["output"].endswith("sentinel\n")
    job = request("/jobs", {"argv": ["python", "-c", "import time;time.sleep(600)"]})["id"]
    request("/cancel", {})
    for _ in range(50):
        result = request("/jobs/" + job)
        if result["exit_code"] is not None:
            break
        time.sleep(0.1)
    assert result["exit_code"] is not None and result["exit_code"] != 0
    print("PASS: readiness, bounded/drained output, exit status, process-group cancellation")
finally:
    subprocess.run(["docker", "stop", "--time", "5", name], check=False, stdout=subprocess.DEVNULL)
