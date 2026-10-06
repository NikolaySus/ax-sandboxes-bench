"""Generate portable CSV, Markdown and dependency-free SVG charts."""

import csv
import html
import json
from collections import defaultdict
from pathlib import Path

from .common import MIB, write_json
from .policy import summary


def flatten(s):
    row = {
        k: s.get(k)
        for k in ["timestamp", "backend", "scenario", "trial", "count", "phase", "window", "stable"]
    }
    for key in ["host", "node", "registry"]:
        for field in [
            "used",
            "available",
            "current",
            "working_set",
            "anon",
            "file",
            "swap",
            "cpu_usec",
        ]:
            if isinstance(s.get(key, {}).get(field), (float, int)):
                row[f"{key}_{field}"] = s[key][field]
    for metric in ["current", "working_set", "anon", "file"]:
        vals = [x[metric] for x in s.get("sandboxes", []) if x.get(metric) is not None]
        row[f"sandbox_{metric}"] = sum(vals) if vals else None
    groups = defaultdict(int)
    for p in s.get("pods", []):
        ns = p["namespace"]
        group = (
            "ax"
            if ns == "ax-system"
            else "workers"
            if ns == "ax-ram-bench" and "worker" in p["name"]
            else "instrumentation"
            if ns == "otel-system"
            else "substrate"
            if ns in ["ate-system", "podcertificate-controller-system"]
            else "other_pods"
        )
        if p.get("working_set") is not None:
            groups[group] += p["working_set"]
    row.update({f"{k}_working_set": v for k, v in groups.items()})
    row["observer_rss"] = s.get("observer_rss")
    row["sandbox_count_measured"] = len(s.get("sandboxes", []))
    return row


def render(directory):
    directory = Path(directory)
    results = []
    metrics = [
        "host_used",
        "node_current",
        "node_working_set",
        "sandbox_current",
        "sandbox_working_set",
        "ax_working_set",
        "substrate_working_set",
        "workers_working_set",
        "instrumentation_working_set",
        "registry_working_set",
        "observer_rss",
    ]
    fields = ["timestamp", "backend", "scenario", "trial", "count", "phase", "window", "stable"]
    fields += [
        f"{key}_{field}"
        for key in ["host", "node", "registry"]
        for field in [
            "used",
            "available",
            "current",
            "working_set",
            "anon",
            "file",
            "swap",
            "cpu_usec",
        ]
    ]
    fields += [f"sandbox_{field}" for field in ["current", "working_set", "anon", "file"]]
    fields += [
        f"{key}_working_set"
        for key in ["ax", "workers", "instrumentation", "substrate", "other_pods"]
    ]
    fields += ["observer_rss", "sandbox_count_measured"]
    groups = {}
    with (
        (directory / "samples.csv").open("w") as out,
        (directory / "samples.jsonl").open() as source,
    ):
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        for line in source:
            try:
                row = flatten(json.loads(line))
            except json.JSONDecodeError:
                # A report may be requested while the sampler is appending its last line.
                if not line.endswith("\n"):
                    break
                raise
            writer.writerow(row)
            transient = row.get("window") == "transition" and row.get("phase") in {
                "create",
                "prepare",
                "suspend-transition",
                "resume-transition",
                "destroy",
            }
            if row.get("window") != "measurement" and not transient:
                continue
            key = tuple(row.get(k) for k in ["backend", "scenario", "trial", "count", "phase"])
            group = groups.setdefault(key, {"stable": True, "metrics": {m: [] for m in metrics}})
            group["stable"] = group["stable"] and bool(row.get("stable"))
            for metric in metrics:
                group["metrics"][metric].append(row.get(metric))
    for key, group in sorted(groups.items(), key=lambda x: str(x[0])):
        item = dict(zip(["backend", "scenario", "trial", "count", "phase"], key))
        item["stable"] = group["stable"]
        item["phase_type"] = (
            "transient"
            if item["phase"]
            in {"create", "prepare", "suspend-transition", "resume-transition", "destroy"}
            else "steady"
        )
        item["metrics"] = {m: summary(group["metrics"][m]) for m in metrics}
        results.append(item)
    for item in results:
        baseline = next(
            (
                x
                for x in results
                if all(x[k] == item[k] for k in ["backend", "scenario", "trial", "count"])
                and x["phase"] == "baseline"
            ),
            None,
        )
        mean = item["metrics"]["node_working_set"]["mean"]
        base = baseline["metrics"]["node_working_set"]["mean"] if baseline else None
        item["incremental_node_bytes_per_sandbox"] = (
            (mean - base) / item["count"]
            if mean is not None and base is not None and item["count"]
            else None
        )
        peak = item["metrics"]["node_working_set"]["peak"]
        item["peak_incremental_node_bytes_per_sandbox"] = (
            (peak - base) / item["count"]
            if peak is not None and base is not None and item["count"]
            else None
        )
        previous = [
            x
            for x in results
            if all(x[k] == item[k] for k in ["backend", "scenario", "trial", "phase"])
            and x["count"] < item["count"]
        ]
        prev = max(previous, key=lambda x: x["count"]) if previous else None
        old = prev["metrics"]["node_working_set"]["mean"] if prev else None
        item["marginal_node_bytes_per_sandbox"] = (
            (mean - old) / (item["count"] - prev["count"])
            if old is not None and mean is not None
            else None
        )
    write_json(directory / "summary.json", results)
    status = (
        json.loads((directory / "status.json").read_text())
        if (directory / "status.json").exists()
        else {}
    )
    # Pool windows across trials for the concise table; per-trial records remain in JSON.
    pooled = {}
    for r in results:
        key = tuple(r[k] for k in ["backend", "scenario", "count", "phase"])
        group = pooled.setdefault(
            key, {"trials": [], "stable": True, "values": {m: [] for m in metrics}, "deltas": []}
        )
        group["trials"].append(r["trial"])
        group["stable"] = group["stable"] and r["stable"]
        original = groups[(r["backend"], r["scenario"], r["trial"], r["count"], r["phase"])]
        for metric in metrics:
            group["values"][metric].extend(original["metrics"][metric])
        group["deltas"].append(r["incremental_node_bytes_per_sandbox"])
    display = []
    for key, group in pooled.items():
        row = dict(zip(["backend", "scenario", "count", "phase"], key))
        row.update(
            trial=",".join(map(str, sorted(group["trials"]))),
            stable=group["stable"],
            metrics={m: summary(group["values"][m]) for m in metrics},
            incremental_node_bytes_per_sandbox=summary(group["deltas"])["mean"],
        )
        display.append(row)
    metadata_path = directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    smoke = metadata.get("arguments", {}).get("smoke", False)
    lines = [
        "# AX / Agent Substrate RAM benchmark",
        "",
        f"Run status: **{status.get('status', 'unknown')}**.",
        "",
        "All values below are measured MiB. Node working set includes runtime and infrastructure. "
        "Sandbox cgroups include gVisor sentry/gofer memory; they are nested in worker/node totals and must not be added to them.",
        "",
        "| API | Scenario | Trials | Count | State | Stable | Node mean | Node median | Node peak | Sandbox mean | Increment / sandbox |",
        "|---|---|---:|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    if metadata.get("continuation"):
        source = metadata["continuation"]["source"]
        lines[4:4] = [
            f"Continuation of `{source}`. Completed prior sweeps remain in that directory; interrupted trials restart from one sandbox. "
            "Monitoring configuration changed: use each new cohort's baseline for comparison. "
            "Installation baseline files are historical and predate this change.",
            "",
        ]

    def fmt(v):
        return f"{v / MIB:.1f}" if v is not None else "N/A"

    for r in display:
        m = r["metrics"]
        node = m["node_working_set"]
        lines.append(
            "| "
            + " | ".join(
                map(
                    str,
                    [
                        r["backend"],
                        r["scenario"],
                        r["trial"],
                        r["count"],
                        r["phase"],
                        r["stable"],
                        fmt(node["mean"]),
                        fmt(node["median"]),
                        fmt(node["peak"]),
                        fmt(m["sandbox_working_set"]["mean"]),
                        fmt(r["incremental_node_bytes_per_sandbox"]),
                    ],
                )
            )
            + " |"
        )
    lines += [
        "",
        "## Infrastructure baseline",
        "",
        "| API | Trial | AX/Redis MiB | Substrate/storage MiB | Monitoring MiB | Worker MiB | Node working set MiB |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        if r["phase"] == "baseline" and r["count"] == 1:
            m = r["metrics"]
            lines.append(
                "| "
                + " | ".join(
                    map(
                        str,
                        [
                            r["backend"],
                            r["trial"],
                            *[
                                fmt(m[k]["mean"])
                                for k in [
                                    "ax_working_set",
                                    "substrate_working_set",
                                    "instrumentation_working_set",
                                    "workers_working_set",
                                    "node_working_set",
                                ]
                            ],
                        ],
                    )
                )
                + " |"
            )
    base_dir = directory / "installation-baselines"
    if base_dir.exists():
        lines += [
            "",
            "Staged installation baselines (warm caches where noted in provenance):",
            "",
            "| Stage | Host used mean MiB | Node working set mean MiB | Node peak MiB |",
            "|---|---:|---:|---:|",
        ]
        for path in sorted(base_dir.glob("*.jsonl")):
            bs = [json.loads(x) for x in path.read_text().splitlines() if x]
            hs = summary([x["host"]["used"] for x in bs])
            ns = summary([x.get("node", {}).get("working_set") for x in bs])
            lines.append(
                f"| {path.stem} | {fmt(hs['mean'])} | {fmt(ns['mean'])} | {fmt(ns['peak'])} |"
            )
    lines += [
        "",
        "![Node working set](memory.svg)",
        "",
        "![Per-sandbox working set](sandbox.svg)",
        "",
        "## Budget interpretation",
        "",
        "Use fixed infrastructure + retained empty-worker costs + active-state marginal costs. "
        "Suspended RAM depends on whether workers remain provisioned. AX resumes workspace data from a golden snapshot; "
        "the direct Substrate path uses full snapshots. These are different persistence contracts.",
        "",
        "| API | Scenario / state | Conservative observed MiB / sandbox | With 30% headroom |",
        "|---|---|---:|---:|",
    ]
    if smoke:
        lines += [
            "",
            "**Smoke validation only: these short windows are not RAM budget recommendations.**",
        ]
    budgets = defaultdict(list)
    for r in results:
        if (
            not smoke
            and r["stable"]
            and r["phase"] not in ["baseline", "empty-workers", "residual"]
        ):
            v = r["peak_incremental_node_bytes_per_sandbox"]
            if v is not None:
                budgets[(r["backend"], r["scenario"], r["phase"])].append(v)
    for key, vals in sorted(budgets.items()):
        value = max(vals)
        lines.append(
            f"| {key[0]} | {key[1]} / {key[2]} | {fmt(value)} | {(fmt(value * 1.3) if value > 0 else 'insufficient signal')} |"
        )
    lines += [
        "",
        "## Limits and provenance",
        "",
        "- Peaks in this table are synchronized sampled peaks; raw JSON retains cgroup lifetime peaks separately.",
        "- Missing cgroups/metrics are unavailable, not zero. Host used RAM is MemTotal − MemAvailable; it is an availability estimate, not summed process RSS.",
        "- Baseline and marginal deltas can be noisy or negative because of caches and unrelated host activity. No host cache flushing is performed.",
        "- The largest completed count is a tested safe count under the recorded guards, not an absolute capacity.",
        "- AX task limits are not propagated by the pinned controller; worker and node cgroups enforce outer limits.",
        "- See metadata.json, events.jsonl, samples.jsonl, samples.csv and summary.json for provenance, raw data and failures.",
        "",
        "```json",
        json.dumps(
            {k: v for k, v in status.items() if k not in ["completed", "capacity_stops"]}, indent=2
        ),
        "```",
    ]
    lines += [
        "",
        "## Tested concurrency",
        "",
        "| API | Scenario | Trial | Largest completed count | Stop reason |",
        "|---|---|---:|---:|---|",
    ]
    counts = {}
    for entry in status.get("completed", []):
        key = tuple(entry[k] for k in ["backend", "scenario", "trial"])
        counts[key] = max(counts.get(key, 0), entry["count"])
    for key, count in sorted(counts.items()):
        reasons = [
            s["reason"]
            for s in status.get("capacity_stops", [])
            if tuple(s[k] for k in ["backend", "scenario", "trial"]) == key
        ]
        reason = (
            "; ".join(reasons)
            if reasons
            else ("smoke only" if smoke else "sweep still in progress / no threshold established")
        )
        lines.append(f"| {key[0]} | {key[1]} | {key[2]} | {count} | {reason} |")
    (directory / "report.md").write_text("\n".join(lines) + "\n")
    chart(directory, display)
    chart(directory, display, sandbox=True)
    return results


def chart(directory, results, sandbox=False):
    metric = "sandbox_working_set" if sandbox else "node_working_set"
    title = (
        "Mean gVisor working set per active sandbox (MiB); includes runtime overhead"
        if sandbox
        else "Mean node working set (MiB); includes infrastructure"
    )

    def value(row):
        total = row["metrics"][metric]["mean"]
        return total / row["count"] if sandbox else total

    points = [
        r
        for r in results
        if r["phase"]
        in ["idle", "active", "post-workload-idle", "suspended", "suspended-no-workers", "resumed"]
        and r["metrics"][metric]["mean"] is not None
    ]
    width = 1000
    height = max(180, 50 + len(points) * 23)
    maximum = max([value(r) for r in points] or [1]) or 1
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="white"/><g font-family="sans-serif" font-size="11"><text x="10" y="20">{title}</text>'
    ]
    for i, r in enumerate(points):
        y = 40 + i * 23
        v = value(r)
        w = 450 * v / maximum
        label = f"{r['backend']} {r['scenario']} t{r['trial']} n={r['count']} {r['phase']}"
        svg.append(
            f'<text x="10" y="{y + 12}">{html.escape(label)}</text><rect x="440" y="{y}" width="{w:.1f}" height="16" fill="#336699"/><text x="{445 + w:.1f}" y="{y + 12}">{v / MIB:.1f}</text>'
        )
    svg.append("</g></svg>")
    (directory / ("sandbox.svg" if sandbox else "memory.svg")).write_text("".join(svg))
