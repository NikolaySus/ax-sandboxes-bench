import argparse
import json
import os
import signal
import time

from .common import NS, STATE, kubectl, run
from .metrics import host
from .report import render


def cleanup(args):
    from .backend import ate

    if not (STATE / "ownership.json").exists():
        raise RuntimeError("No benchmark ownership record")
    errors = []
    # Query real resources, so interrupted runs do not leave unrecorded actors behind.
    raw = ate("get", "actors", "-a", NS, "-o", "json", check=False)
    if raw:
        obj = json.loads(raw)
        items = obj if isinstance(obj, list) else obj.get("actors", obj.get("items", []))
        for actor in items:
            name = actor["metadata"]["name"]
            if name.startswith("b-ax-"):
                try:
                    run(
                        ["ax", "--context", "kind-ax-ram-bench", "delete", "task", name, "-a", NS],
                        timeout=330,
                    )
                except Exception as exc:
                    errors.append(str(exc))
            elif name.startswith("b-"):
                try:
                    ate("delete", "actor", name, "-a", NS, "--any-state")
                except Exception as exc:
                    errors.append(str(exc))
    raw = ate("get", "actor-templates", "-a", NS, "-o", "json", check=False)
    if raw:
        obj = json.loads(raw)
        items = obj if isinstance(obj, list) else obj.get("actorTemplates", obj.get("items", []))
        for template in items:
            name = template["metadata"]["name"]
            if name == "bench-full" or name.startswith("b-"):
                try:
                    ate("delete", "actor-template", name, "-a", NS)
                except Exception as exc:
                    errors.append(str(exc))
    kubectl("delete", "workerpool", "bench-workers", "-n", NS, "--ignore-not-found", check=False)
    if args.stack or args.cluster:
        kubectl("delete", "namespace", "ax-system", NS, "--ignore-not-found", check=False)
        run(
            [
                STATE / "bin/ate-setup",
                "--kind",
                "--context",
                "kind-ax-ram-bench",
                "delete",
                "ate-system",
            ],
            cwd=STATE / "sources/substrate",
            check=False,
        )
        for marker in ["ax-installed", "substrate-installed", "assets-installed", "installed.json"]:
            (STATE / marker).unlink(missing_ok=True)
    if args.cluster:
        run(["kind", "delete", "cluster", "--name", "ax-ram-bench"])
        run(["docker", "rm", "-f", "ax-ram-bench-registry"])
        pid = STATE / "proxy-relay.pid"
        if pid.exists():
            number = int(pid.read_text())
            try:
                if "bench.proxy_relay" in open(f"/proc/{number}/cmdline").read():
                    os.kill(number, signal.SIGTERM)
            except (FileNotFoundError, ProcessLookupError):
                pass
            pid.unlink()
        (STATE / "ownership.json").unlink()
    if errors:
        raise RuntimeError("Cleanup errors: " + str(errors))


def main():
    p = argparse.ArgumentParser(description="Local AX/Substrate RAM benchmark")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("baselines", help="Recapture warm infrastructure baselines with no actors")
    r = sub.add_parser("run")
    r.add_argument("--name", default=time.strftime("%Y%m%d-%H%M%S"))
    r.add_argument(
        "--continue-from",
        help="Prior result directory; skip finished sweeps and restart interrupted trials",
    )
    r.add_argument(
        "--backends", nargs="+", choices=["ax", "substrate"], default=["ax", "substrate"]
    )
    r.add_argument(
        "--scenarios",
        nargs="+",
        choices=["idle", "documents", "developer"],
        default=["idle", "documents", "developer"],
    )
    r.add_argument("--trials", type=int, default=3)
    r.add_argument("--sample-seconds", type=int, default=60)
    r.add_argument(
        "--smoke",
        action="store_true",
        help="One sandbox/trial, short windows; not research results",
    )
    report = sub.add_parser("report")
    report.add_argument("directory")
    c = sub.add_parser("cleanup")
    c.add_argument("--stack", action="store_true")
    c.add_argument("--cluster", action="store_true")
    a = p.parse_args()
    if a.command == "preflight":
        print(json.dumps(host(), indent=2))
    elif a.command == "report":
        render(a.directory)
    else:
        if a.command == "run" and (a.trials < 1 or a.sample_seconds < 1):
            p.error("trials and sample-seconds must be positive")
        import fcntl

        with (STATE / "run.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if a.command == "cleanup":
                cleanup(a)
            elif a.command == "baselines":
                from .baselines import capture

                capture()
            else:
                from .runner import Experiment

                Experiment(a).execute()


if __name__ == "__main__":
    main()
