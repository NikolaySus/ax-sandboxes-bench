"""Owned, resumable installation. Upstream sources remain at their pinned commits."""

import argparse
import json
import os
import subprocess
import time

from .common import GIB, NODE, ROOT, STATE, kubectl, run, write_json
from .metrics import Collector, host
from .policy import stable

NODE_IMAGE = (
    "kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
)
REGISTRY = "ax-ram-bench-registry"


def logged(args, name, cwd=ROOT):
    print(f"{name}: starting", flush=True)
    with (STATE / "logs" / f"{name}.log").open("a") as f:
        subprocess.run(
            [str(x) for x in args], cwd=cwd, stdout=f, stderr=subprocess.STDOUT, check=True
        )
    print(f"{name}: complete", flush=True)


def baseline(name, seconds=60):
    path = STATE / "baselines" / f"{name}.jsonl"
    if path.exists() and len(path.read_text().splitlines()) >= seconds:
        return
    path.parent.mkdir(exist_ok=True)
    collector = Collector()
    observations = []
    settled = False
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        start = time.monotonic()
        sample = collector.sample()
        observations.append(sample.get("node", {}).get("working_set", sample["host"]["used"]))
        if stable(observations):
            settled = True
            break
        time.sleep(max(0, 1 - (time.monotonic() - start)))
    with path.open("w") as f:
        for _ in range(seconds):
            start = time.monotonic()
            f.write(json.dumps({**collector.sample(), "baseline_stable": settled}) + "\n")
            f.flush()
            time.sleep(max(0, 1 - (time.monotonic() - start)))


def setup_cluster():
    h = host()
    if h["disk_free"] < 20 * GIB:
        raise RuntimeError(
            "Setup requires at least 20 GiB free disk; no unrelated data will be removed."
        )
    reserve = max(4 * GIB, h["total"] * 0.25)
    ceiling = int(min(h["available"] - reserve, h["total"] * 0.70))
    if ceiling < 6 * GIB:
        raise RuntimeError("Insufficient available RAM after host reserve")
    cpus = max(1, len(os.sched_getaffinity(0)) * 0.75)
    clusters = run(["kind", "get", "clusters"]).splitlines()
    owned = STATE / "ownership.json"
    if "ax-ram-bench" in clusters:
        if not owned.exists():
            raise RuntimeError("Existing cluster has no benchmark ownership record")
        configure_proxy()
        return
    if (
        run(["docker", "ps", "-aq", "--filter", f"name=^{REGISTRY}$"]).strip()
        and not owned.exists()
    ):
        raise RuntimeError("Registry name is already occupied")
    baseline("host-only")
    config = """kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
featureGates:
  ClusterTrustBundle: true
  ClusterTrustBundleProjection: true
  PodCertificateRequest: true
runtimeConfig:
  certificates.k8s.io/v1beta1: "true"
networking:
  ipFamily: ipv4
kubeadmConfigPatches:
- |
  kind: KubeletConfiguration
  serializeImagePulls: false
  maxParallelImagePulls: 2
  evictionHard:
    memory.available: "1Gi"
    nodefs.available: "5%"
    imagefs.available: "5%"
"""
    (STATE / "kind.yaml").write_text(config)
    write_json(
        owned,
        dict(
            cluster="ax-ram-bench",
            registry=REGISTRY,
            memory_limit=ceiling,
            cpus=cpus,
            host_reserve=reserve,
            created_at=time.time(),
            node_image=NODE_IMAGE,
        ),
    )
    if not run(["docker", "ps", "-aq", "--filter", f"name=^{REGISTRY}$"]).strip():
        run(
            [
                "docker",
                "run",
                "-d",
                "--restart=unless-stopped",
                "--label",
                "ax-ram-bench=owned",
                "-p",
                "127.0.0.1:5001:5000",
                "--name",
                REGISTRY,
                json.loads((ROOT / "config/versions.json").read_text())["registry_image"],
            ]
        )
    logged(
        [
            "kind",
            "create",
            "cluster",
            "--name",
            "ax-ram-bench",
            "--image",
            NODE_IMAGE,
            "--config",
            STATE / "kind.yaml",
            "--kubeconfig",
            STATE / "kubeconfig",
        ],
        "cluster",
    )
    run(
        [
            "docker",
            "update",
            "--memory",
            str(ceiling),
            "--memory-swap",
            str(ceiling),
            "--cpus",
            str(cpus),
            NODE,
        ]
    )
    run(["docker", "network", "connect", "kind", REGISTRY])
    run(["docker", "exec", NODE, "sysctl", "net.ipv4.conf.all.proxy_arp=1"])
    target = "/etc/containerd/certs.d/localhost:5001"
    run(["docker", "exec", NODE, "mkdir", "-p", target])
    run(
        ["docker", "exec", "-i", NODE, "sh", "-c", f"cat > {target}/hosts.toml"],
        data=f'[host."http://{REGISTRY}:5000"]\n',
    )
    configure_proxy()
    baseline("kubernetes-only")


def configure_proxy():
    from urllib.parse import urlsplit

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return
    parsed = urlsplit(proxy)
    if parsed.hostname not in ["127.0.0.1", "localhost"]:
        return
    network = json.loads(run(["docker", "network", "inspect", "kind"]))[0]
    gateway = next(x["Gateway"] for x in network["IPAM"]["Config"] if ":" not in x["Gateway"])
    import socket

    try:
        with socket.create_connection((gateway, 18081), timeout=1):
            pass
    except OSError:
        log = (STATE / "logs/proxy-relay.log").open("a")
        process = subprocess.Popen(
            [
                "python3",
                "-m",
                "bench.proxy_relay",
                gateway,
                parsed.hostname,
                str(parsed.port or 80),
            ],
            cwd=ROOT,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        (STATE / "proxy-relay.pid").write_text(str(process.pid))
    address = f"http://{gateway}:18081"
    bypass = (
        "localhost,127.0.0.1,::1,.svc,.cluster.local,10.96.0.0/12,10.244.0.0/16,172.18.0.0/16,"
        + REGISTRY
    )
    content = (
        '[Service]\nEnvironment="HTTP_PROXY='
        + address
        + '" "HTTPS_PROXY='
        + address
        + '" "NO_PROXY='
        + bypass
        + '"\n'
    )
    destination = "/etc/systemd/system/containerd.service.d/bench-proxy.conf"
    existing = run(["docker", "exec", NODE, "cat", destination], check=False)
    if existing == content:
        write_json(STATE / "proxy.json", {"address": address, "bypass": bypass})
        return
    run(["docker", "exec", NODE, "mkdir", "-p", "/etc/systemd/system/containerd.service.d"])
    run(
        [
            "docker",
            "exec",
            "-i",
            NODE,
            "sh",
            "-c",
            "cat > /etc/systemd/system/containerd.service.d/bench-proxy.conf",
        ],
        data=content,
    )
    run(["docker", "exec", NODE, "systemctl", "daemon-reload"])
    run(["docker", "exec", NODE, "systemctl", "restart", "containerd"])
    write_json(STATE / "proxy.json", {"address": address, "bypass": bypass})


def setup_substrate():
    src = STATE / "sources/substrate"
    marker = STATE / "substrate-installed"
    if marker.exists():
        configure_monitoring()
        return
    logged(
        ["go", "build", "-o", STATE / "bin/ate-setup", "./cmd/ate-setup"],
        "build-substrate-setup",
        src,
    )
    logged(
        ["go", "build", "-o", STATE / "bin/kubectl-ate", "./cmd/kubectl-ate"],
        "build-substrate-cli",
        src,
    )
    logged(
        [
            STATE / "bin/ate-setup",
            "--kind",
            "--context",
            "kind-ax-ram-bench",
            "--rollout-timeout",
            "15m",
            "deploy",
            "ate-system",
        ],
        "substrate",
        src,
    )
    ds = json.loads(kubectl("get", "ds", "-n", "ate-system", "-o", "json"))["items"]
    for d in ds:
        if d["metadata"]["name"].startswith("atelet"):
            containers = d["spec"]["template"]["spec"]["containers"]
            for c in containers:
                c["args"] = [
                    a.replace("kind-registry:5000", REGISTRY + ":5000") for a in c.get("args", [])
                ]
            if (STATE / "proxy.json").exists():
                proxy = json.loads((STATE / "proxy.json").read_text())
                for c in containers:
                    c.setdefault("env", []).extend(
                        [
                            {"name": "HTTPS_PROXY", "value": proxy["address"]},
                            {"name": "HTTP_PROXY", "value": proxy["address"]},
                            {"name": "NO_PROXY", "value": proxy["bypass"]},
                        ]
                    )
            kubectl(
                "patch",
                "ds",
                d["metadata"]["name"],
                "-n",
                "ate-system",
                "--type=merge",
                "-p",
                json.dumps({"spec": {"template": {"spec": {"containers": containers}}}}),
            )
    kubectl(
        "wait",
        "--for=condition=complete",
        "job/rustfs-bucket-init",
        "-n",
        "ate-system",
        "--timeout=600s",
    )
    # Keep upstream observability installed and account for it explicitly.
    configure_monitoring()
    baseline("substrate-installed")
    marker.touch()


def configure_monitoring():
    kubectl(
        "patch",
        "deployment",
        "prometheus",
        "-n",
        "otel-system",
        "--type=strategic",
        "--patch-file",
        str(ROOT / "config/prometheus-patch.json"),
    )
    kubectl("rollout", "status", "deployment/prometheus", "-n", "otel-system", "--timeout=180s")


def setup_ax():
    if (STATE / "ax-installed").exists():
        return
    src = STATE / "sources/ax"
    logged(["go", "build", "-o", STATE / "bin/ax", "./cmd/ax"], "build-ax-cli", src)
    versions = json.loads((ROOT / "config/versions.json").read_text())
    redis = (
        (src / "deploy/redis.yaml")
        .read_text()
        .replace("image: redis:7-alpine", "image: " + versions["redis_image"])
    )
    kubectl("apply", "-f", "-", data=redis)
    for name in ["ax-controller", "ax-server"]:
        resolved = run(
            ["ko", "resolve", "-f", src / f"deploy/{name}.yaml"],
            cwd=src,
            timeout=1800,
            env={**os.environ, "KO_DEFAULTBASEIMAGE": versions["ax_base"]},
        )
        resolved = resolved.replace(
            "gs://snapshot-substrate-test-ax-substrate/ate-env/", "gs://ate-snapshots/ax-ram-bench/"
        )
        (STATE / f"{name}.yaml").write_text(resolved)
        kubectl("apply", "-f", str(STATE / f"{name}.yaml"))
        kubectl("rollout", "status", f"deployment/{name}", "-n", "ax-system", "--timeout=300s")
    baseline("ax-installed")
    (STATE / "ax-installed").touch()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["cluster", "substrate", "ax", "all"], default="all")
    args = parser.parse_args()
    setup_cluster()
    if args.stage == "cluster":
        return
    setup_substrate()
    if args.stage == "substrate":
        return
    setup_ax()
    write_json(
        STATE / "installed.json",
        dict(
            versions=json.loads((ROOT / "config/versions.json").read_text()),
            ownership=json.loads((STATE / "ownership.json").read_text()),
            pods=json.loads(kubectl("get", "pods", "-A", "-o", "json")),
        ),
    )
    print("Infrastructure installed. Run scripts/build-workload.sh next.", flush=True)


if __name__ == "__main__":
    main()
