"""Recapture staged warm baselines without rebuilding or deleting the stack."""

import json
import time

from .backend import ate
from .common import STATE, kubectl, wait_for, write_json
from .setup import baseline

NAMESPACES = {
    "ate-system",
    "otel-system",
    "podcertificate-controller-system",
    "ax-system",
    "ax-ram-bench",
}


def capture():
    actors = json.loads(ate("get", "actors", "-a", "ax-ram-bench", "-o", "json"))
    if actors.get("actors"):
        raise RuntimeError("Baseline capture requires no benchmark actors")
    workerpools = json.loads(kubectl("get", "workerpools", "-A", "-o", "json"))["items"]
    if any(p["spec"]["replicas"] for p in workerpools):
        raise RuntimeError("Baseline capture requires zero workers")
    items = json.loads(kubectl("get", "deployments,statefulsets,daemonsets", "-A", "-o", "json"))[
        "items"
    ]
    objects = [o for o in items if o["metadata"]["namespace"] in NAMESPACES]
    write_json(STATE / "baseline-restore.json", objects)

    def change(obj, restore=False):
        ns = obj["metadata"]["namespace"]
        kind = obj["kind"]
        name = obj["metadata"]["name"]
        if kind == "DaemonSet":
            selector = obj["spec"]["template"]["spec"].get("nodeSelector", {})
            # JSON merge null removes the temporary selector while preserving original keys.
            selector = {**selector, "ax-bench-baseline": None if restore else "disabled"}
            kubectl(
                "patch",
                kind,
                name,
                "-n",
                ns,
                "--type=merge",
                "-p",
                json.dumps({"spec": {"template": {"spec": {"nodeSelector": selector}}}}),
            )
        else:
            kubectl(
                "scale",
                kind + "/" + name,
                "-n",
                ns,
                "--replicas=" + str(obj["spec"].get("replicas", 1) if restore else 0),
            )

    def zero():
        pods = json.loads(kubectl("get", "pods", "-A", "-o", "json"))["items"]
        return not any(
            p["metadata"]["namespace"] in NAMESPACES
            and p.get("status", {}).get("phase") not in ["Succeeded", "Failed"]
            for p in pods
        )

    def ready(objects):
        for o in objects:
            kind = o["kind"]
            ns = o["metadata"]["namespace"]
            name = o["metadata"]["name"]
            if kind in ["Deployment", "StatefulSet", "DaemonSet"]:
                kubectl("rollout", "status", kind + "/" + name, "-n", ns, "--timeout=300s")

    def record(name):
        (STATE / "baselines" / f"{name}.jsonl").unlink(missing_ok=True)
        baseline(name)

    try:
        for o in objects:
            change(o)
        wait_for(zero, timeout=180)
        record("kubernetes-only")
        substrate = [
            o for o in objects if o["metadata"]["namespace"] not in ["ax-system", "ax-ram-bench"]
        ]
        for o in substrate:
            change(o, True)
        ready(substrate)
        record("substrate-installed")
        ax = [o for o in objects if o["metadata"]["namespace"] == "ax-system"]
        for o in ax:
            change(o, True)
        ready(ax)
        record("ax-installed")
    finally:
        for o in objects:
            change(o, True)
    write_json(
        STATE / "baselines/provenance.json",
        {
            "captured_at": time.time(),
            "type": "warm staged baseline",
            "method": "owned infrastructure scaled down and restored; images and caches retained",
        },
    )


if __name__ == "__main__":
    capture()
