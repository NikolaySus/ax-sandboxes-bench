import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state"
NODE = "ax-ram-bench-control-plane"
NS = "ax-ram-bench"
GIB = 1024**3
MIB = 1024**2


def run(args, *, cwd=ROOT, data=None, timeout=600, check=True, env=None):
    p = subprocess.run(
        [str(a) for a in args],
        cwd=cwd,
        input=data,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
    )
    if check and p.returncode:
        raise RuntimeError(f"{args}: {p.stderr[-6000:]} {p.stdout[-2000:]}")
    return p.stdout


def kubectl(*args, **kwargs):
    return run(["kubectl", "--context=kind-ax-ram-bench", *args], **kwargs)


def apply(obj):
    return kubectl("apply", "-f", "-", data=json.dumps(obj))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


def wait_for(fn, timeout=300, interval=2):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = fn()
            if last:
                return last
        except (RuntimeError, OSError, ValueError) as exc:
            last = str(exc)
        time.sleep(interval)
    raise TimeoutError(f"Condition not reached: {last}")
