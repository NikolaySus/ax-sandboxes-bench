#!/usr/bin/env python3
"""Build a results-only PDF from measured cohorts."""

import json
import shutil
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/pdf/measured-results"
DATA = OUT / "data"
TMP = ROOT / "tmp/pdfs/measured-results"
for path in [OUT, DATA, TMP]:
    path.mkdir(parents=True, exist_ok=True)
MIB = 2**20
blue, teal, gray = "#2563a6", "#13867e", "#8594a5"
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "text.color": "#25364a",
        "axes.labelcolor": "#25364a",
    }
)


def load(path):
    return json.loads(path.read_text())


def key(r):
    return (r["backend"], r["scenario"], r["trial"], r["count"])


runs = {}
for name in ["research", "research-resumed", "validation"]:
    dest = DATA / name
    dest.mkdir(exist_ok=True)
    for file in ["summary.json", "metadata.json", "status.json"]:
        shutil.copy2(ROOT / "results" / name / file, dest / file)
    rows = load(dest / "summary.json")
    # Require complete measurement windows including post-destruction residual.
    eligible = {
        key(r)
        for r in rows
        if r["phase"] == "residual"
        and r["metrics"]["node_working_set"]["samples"] >= (5 if name == "validation" else 60)
    }
    runs[name] = [r for r in rows if key(r) in eligible]
shutil.copytree(
    ROOT / "results/research/installation-baselines", DATA / "baselines", dirs_exist_ok=True
)
meta = load(DATA / "research/metadata.json")


def get(run, scenario, trial, count, phase, backend="ax"):
    return next(
        r for r in runs[run] if key(r) == (backend, scenario, trial, count) and r["phase"] == phase
    )


def metric(r, name="node_working_set", stat="mean"):
    v = r["metrics"][name][stat]
    return None if v is None else v / MIB


def saveplot(name, fig):
    fig.tight_layout()
    file = TMP / (name + ".png")
    fig.savefig(file, dpi=190, bbox_inches="tight")
    fig.savefig(OUT / (name + ".svg"), bbox_inches="tight")
    plt.close(fig)
    return file


stages = []
for file, label in [
    ("kubernetes-only", "Kubernetes"),
    ("substrate-installed", "+ Substrate"),
    ("ax-installed", "+ AX (full stack)"),
]:
    rows = [
        json.loads(line) for line in (DATA / "baselines" / f"{file}.jsonl").read_text().splitlines()
    ]
    stages.append(
        [
            label,
            statistics.mean(r["node"]["working_set"] for r in rows) / MIB,
            max(r["node"]["working_set"] for r in rows) / MIB,
            statistics.mean(r["host"]["used"] for r in rows) / MIB,
        ]
    )
fig, ax = plt.subplots(figsize=(7.1, 2.45))
ax.barh([r[0] for r in stages][::-1], [r[1] for r in stages][::-1], color=[blue, teal, gray])
for i, r in enumerate(stages[::-1]):
    ax.text(r[1] + 20, i, f"{r[1]:,.0f}", va="center")
ax.set_xlim(0, 1900)
ax.set_xlabel("Mean node working set (MiB)")
baseline_plot = saveplot("infrastructure", fig)

fig, ax = plt.subplots(figsize=(7.1, 2.9))
for run, trial, label, color in [
    ("research", 1, "Configuration A / trial 1", blue),
    ("research-resumed", 2, "Configuration B / trial 2", teal),
]:
    rows = sorted(
        [
            r
            for r in runs[run]
            if r["scenario"] == "idle" and r["trial"] == trial and r["phase"] == "idle"
        ],
        key=lambda r: r["count"],
    )
    ax.plot(
        [r["count"] for r in rows],
        [r["incremental_node_bytes_per_sandbox"] / MIB for r in rows],
        "o-",
        label=label,
        color=color,
    )
ax.set_xlabel("Concurrent sandboxes")
ax.set_ylabel("Incremental node MiB / sandbox")
ax.set_ylim(bottom=0)
ax.grid(alpha=0.18)
ax.legend(frameon=False, fontsize=9)
idle_plot = saveplot("idle-scaling", fig)

fig, ax = plt.subplots(figsize=(7.1, 2.9))
docrows = sorted(
    [
        r
        for r in runs["research-resumed"]
        if r["scenario"] == "documents" and r["trial"] == 2 and r["phase"] == "active"
    ],
    key=lambda r: r["count"],
)
ax.plot(
    [r["count"] for r in docrows],
    [r["incremental_node_bytes_per_sandbox"] / MIB for r in docrows],
    "o-",
    color=blue,
    label="Incremental node working set",
)
ax.plot(
    [r["count"] for r in docrows],
    [metric(r, "sandbox_working_set") / r["count"] for r in docrows],
    "s--",
    color=teal,
    label="Sandbox cgroup working set",
)
ax.set_xlabel("Concurrent document workloads")
ax.set_ylabel("Mean MiB / sandbox")
ax.set_ylim(0, 300)
ax.grid(alpha=0.18)
ax.legend(frameon=False, fontsize=9)
doc_plot = saveplot("document-scaling", fig)

vrows = []
for scenario, phase, label in [
    ("idle", "idle", "Idle"),
    ("documents", "active", "Documents"),
    ("developer", "active", "Developer"),
]:
    for backend in ["ax", "substrate"]:
        r = get("validation", scenario, 1, 1, phase, backend)
        vrows.append(
            [
                label,
                backend,
                *[metric(r, "sandbox_working_set", s) for s in ["mean", "median", "peak"]],
            ]
        )
fig, ax = plt.subplots(figsize=(7.1, 2.65))
x = np.arange(3)
for j, (backend, color, label) in enumerate(
    [("ax", blue, "AX"), ("substrate", teal, "Direct Substrate")]
):
    rows = [r for r in vrows if r[1] == backend]
    pos = x + (j - 0.5) * 0.34
    ax.bar(pos, [r[2] for r in rows], 0.32, color=color, label=label)
    ax.scatter(pos, [r[4] for r in rows], marker="_", s=140, color="#182535", zorder=4)
ax.set_xticks(x, ["Idle", "Documents", "Developer"])
ax.set_ylabel("Sandbox working set (MiB)")
ax.set_ylim(0, 210)
ax.legend(frameon=False, fontsize=9)
ax.text(
    0.99,
    0.97,
    "Black marks: sampled peaks",
    transform=ax.transAxes,
    ha="right",
    va="top",
    fontsize=8,
)
validation_plot = saveplot("workload-validation", fig)

styles = getSampleStyleSheet()
styles.add(
    ParagraphStyle(
        name="TitleCustom",
        fontName="Helvetica-Bold",
        fontSize=25,
        leading=29,
        textColor=colors.HexColor("#173957"),
        spaceAfter=14,
    )
)
styles.add(
    ParagraphStyle(
        name="Deck", fontSize=11, leading=16, textColor=colors.HexColor("#536779"), spaceAfter=12
    )
)
styles["BodyText"].fontSize = 9.5
styles["BodyText"].leading = 14
styles["BodyText"].spaceAfter = 8
styles["Heading1"].fontSize = 18
styles["Heading1"].leading = 22
styles["Heading1"].textColor = colors.HexColor("#173957")
styles["Heading1"].spaceAfter = 12
styles["Heading2"].fontSize = 12
styles["Heading2"].leading = 16
styles["Heading2"].spaceBefore = 12
styles.add(
    ParagraphStyle(
        name="NoteCustom",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#536779"),
        spaceAfter=7,
    )
)
story = []


def p(text, style="BodyText"):
    story.append(Paragraph(text, styles[style]))


def title(text):
    p(text, "Heading1")


def table(rows, widths):
    data = [[Paragraph(str(c), styles["NoteCustom"]) for c in row] for row in rows]
    t = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6edf4")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f8fa")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#afc1d1")),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 8))


def picture(file):
    from PIL import Image as PI

    with PI.open(file) as im:
        w, h = im.size
    story.append(Image(str(file), width=499, height=499 * h / w))
    story.append(Spacer(1, 8))


def page():
    story.append(PageBreak())


def f(v):
    return "N/A" if v is None else f"{v / MIB:,.1f}"


p("Sandbox RAM measurements", "TitleCustom")
p("Google AX + Agent Substrate | gVisor | Python 3.13", "Deck")
p(
    "Measurement date: 27 September 2026. Local single-node Kubernetes on a 31.26 GiB Linux host.",
    "NoteCustom",
)
p(
    "<b>Measured at scale:</b> a newly created idle sandbox adds about <b>64-65 MiB</b> of node working set; a resumed idle sandbox adds <b>70-71 MiB</b>. The document workload adds about <b>205 MiB</b> per sandbox at concurrency 17. These values include the incremental worker and infrastructure costs observed in each cohort."
)
table(
    [
        ["Evidence", "API / scale", "Measurement window"],
        ["Idle lifecycle", "AX / up to 53 sandboxes", "60 samples per state"],
        ["Document workload", "AX / up to 17 sandboxes", "60 samples per state"],
        [
            "Developer + API comparison",
            "AX and direct Substrate / 1 sandbox",
            "5-sample validation windows",
        ],
    ],
    [155, 174, 170],
)
p(
    "The sandbox contains a versioned python:3.13-slim workload image with pandas, openpyxl and python-docx preinstalled. No LLM inference, provider integration or Deep Agents runtime is included."
)
title("Warm infrastructure baseline")
picture(baseline_plot)
table(
    [["Cumulative stage", "Node mean MiB", "Node peak MiB", "Host used MiB"]]
    + [[r[0], *[f"{v:,.1f}" for v in r[1:]]] for r in stages],
    [175, 108, 108, 108],
)
p(
    "Baseline stages use 60 samples after stabilization. Images and caches remain warm. The full-stack mean is 1.53 GiB; accumulated runtime infrastructure memory is higher in long-lived experiments. Stage differences are not pure component attribution.",
    "NoteCustom",
)
page()
title("Idle sandbox scaling")
p(
    "Two separately configured series converge near 65 MiB per idle sandbox. Configuration A uses a 512 MiB Prometheus limit; configuration B uses 2 GiB and bounded retention. Series are shown separately, with each cohort compared against its own baseline."
)
picture(idle_plot)
p("Lifecycle at 53 sandboxes - configuration A, trial 1", "Heading2")
phases = [
    ("idle", "New idle"),
    ("resumed", "Resumed"),
    ("suspended", "Suspended / workers retained"),
    ("suspended-no-workers", "Suspended / workers removed"),
]
table(
    [["State", "Node mean MiB", "Sandbox mean / actor", "Node delta / actor", "Peak delta / actor"]]
    + [
        [
            label,
            f(get("research", "idle", 1, 53, phase)["metrics"]["node_working_set"]["mean"]),
            f(
                (
                    get("research", "idle", 1, 53, phase)["metrics"]["sandbox_working_set"]["mean"]
                    or 0
                )
                / 53
            )
            if phase in ["idle", "resumed"]
            else "N/A",
            f(get("research", "idle", 1, 53, phase)["incremental_node_bytes_per_sandbox"]),
            f(get("research", "idle", 1, 53, phase)["peak_incremental_node_bytes_per_sandbox"]),
        ]
        for phase, label in phases
    ],
    [153, 85, 87, 87, 87],
)
p("Cross-check at 37 sandboxes - configuration B, trial 2", "Heading2")
rr = [get("research-resumed", "idle", 2, 37, phase) for phase, _ in phases]
p(
    "Mean incremental working set per sandbox: "
    + "; ".join(
        f"{label.lower()} <b>{r['incremental_node_bytes_per_sandbox'] / MIB:.1f} MiB</b>"
        for (_, label), r in zip(phases, rr)
    )
    + "."
)
p(
    "Suspended actors have no live sandbox cgroup. N/A means the cgroup is absent, not that the complete service consumes zero memory. Worker removal reduces retained costs. The largest observed count is a tested configuration, not a universal capacity guarantee.",
    "NoteCustom",
)
page()
title("Python and document workload")
p(
    "The workload processes 10,000 rows with pandas, performs filesystem operations, and generates and validates XLSX and DOCX files. Repeated cycles remain active throughout sampling. The chart shows configuration B, trial 2."
)
picture(doc_plot)
p("Active workload at selected concurrency levels", "Heading2")
chosen = [r for r in docrows if r["count"] in [1, 5, 10, 17]]
table(
    [["Count", "Node mean MiB", "Sandbox / actor: mean", "Median", "Peak", "Node delta / actor"]]
    + [
        [
            r["count"],
            f(r["metrics"]["node_working_set"]["mean"]),
            *[
                f(r["metrics"]["sandbox_working_set"][s] / r["count"])
                for s in ["mean", "median", "peak"]
            ],
            f(r["incremental_node_bytes_per_sandbox"]),
        ]
        for r in chosen
    ],
    [44, 93, 108, 65, 65, 124],
)
p(
    "Median and peak columns are the aggregate sandbox working set divided by count, not the median or maximum of individual actors. Peaks are synchronized sampled peaks.",
    "NoteCustom",
)
p("At 17 simultaneous workloads", "Heading2")
doc = get("research-resumed", "documents", 2, 17, "active")
p(
    f"Total node working set averages <b>{metric(doc) / 1024:.2f} GiB</b>. Sandbox cgroups average <b>{metric(doc, 'sandbox_working_set') / 17:.1f} MiB per actor</b>; the baseline-adjusted node cost is <b>{doc['incremental_node_bytes_per_sandbox'] / MIB:.1f} MiB per actor</b>. The difference captures costs outside the actor cgroup."
)
p(
    "After suspension, the same cohort adds <b>18.2 MiB per actor</b> with workers retained and <b>0.3 MiB per actor</b> with workers removed. Such small baseline deltas are cache-sensitive and should not be treated as guaranteed zero-cost storage."
)
p(
    "This series stopped scaling at a predicted resource threshold; observed host CPU reached about 70%. This is a workload-and-policy limit, not a demonstrated RAM-only maximum.",
    "NoteCustom",
)
page()
title("Single-sandbox workload comparison")
p(
    "These separate validation measurements use five samples per state. They verify execution and persistence across both APIs and characterize the selected workloads; they are not substitutes for concurrency measurements."
)
picture(validation_plot)
table(
    [["Workload", "API", "Mean MiB", "Median MiB", "Peak MiB"]]
    + [[r[0], "AX" if r[1] == "ax" else "Substrate", *[f"{v:.1f}" for v in r[2:]]] for r in vrows],
    [120, 100, 93, 93, 93],
)
p("Developer workload", "Heading2")
p(
    "Each sandbox clones pinned FastAPI sources, creates a virtual environment, installs hash-locked dependencies from a local wheelhouse, runs selected tests and builds a wheel. The chart covers the repeated test/build phase. Clone and dependency installation are recorded separately as preparation transitions."
)
p(
    "The active developer sandbox peaks at 122 MiB through AX and 144 MiB through direct Substrate; the corresponding sampled incremental node peaks are about 240 MiB. Snapshot, cache and storage retention can raise the total lifecycle cost. The test is representative of a small Python project, not all developer activity."
)
p("Persistence semantics", "Heading2")
p(
    "AX preserves workspace files and restores process memory from a golden image. Direct Substrate full snapshots preserve files and the in-memory nonce. Both paths passed these persistence checks. Their memory figures do not represent identical snapshot semantics."
)
page()
title("Accounting and practical interpretation")
p("Shared infrastructure is a separate budget", "Heading2")
basecases = [
    ("A / idle, 53", get("research", "idle", 1, 53, "baseline")),
    ("B / idle, 37", get("research-resumed", "idle", 2, 37, "baseline")),
    ("B / documents, 17", get("research-resumed", "documents", 2, 17, "baseline")),
]
table(
    [["Cohort baseline", "AX / Redis", "Substrate / storage", "Monitoring", "Total node"]]
    + [
        [
            label,
            *[
                f(r["metrics"][k]["mean"])
                for k in [
                    "ax_working_set",
                    "substrate_working_set",
                    "instrumentation_working_set",
                    "node_working_set",
                ]
            ],
        ]
        for label, r in basecases
    ],
    [143, 80, 100, 86, 90],
)
p(
    "Values are mean MiB with no cohort sandboxes active. Total node memory also includes Kubernetes, kernel/runtime charges and other pods; it is not the sum of the three listed components. These warm baselines range from 2.77 to 4.69 GiB.",
    "NoteCustom",
)
p("Planning allowances grounded in these measurements", "Heading2")
table(
    [
        ["State", "Observed at scale", "Illustrative allowance"],
        ["Idle / resumed", "64-71 MiB incremental / actor", "128-200 MiB / actor"],
        ["Active documents", "205 MiB incremental / actor at n=17", "256-512 MiB / actor"],
        [
            "Small developer workload",
            "About 240 MiB sampled node delta peak; single actor",
            "512 MiB-1 GiB / actor",
        ],
    ],
    [110, 235, 154],
)
p(
    "Allowances are engineering estimates, not measured upper bounds. Add shared infrastructure based on the warm baseline for the intended configuration, then reserve host headroom. Data size, native libraries, parallelism and snapshot/cache retention can change the budget."
)
p("Method and scope", "Heading2")
p(
    "Working set = cgroup memory.current minus inactive_file. Charged memory includes reclaimable file cache and may be considerably higher. Node, worker and sandbox counters are nested and must not be added together. Host used RAM is MemTotal - MemAvailable. Baseline deltas can be negative because of shared cache and infrastructure drift."
)
p(
    "Research samples are collected every second after three 20-second windows agree within 5% or 16 MiB, with a five-minute stabilization limit. Plotted series use complete lifecycle cohorts including a residual window; interrupted cleanup cohorts are excluded. Tables retain sampled means, medians and peaks rather than claiming per-process RSS."
)
p(
    "The node ceiling is 20.65 GiB / 12 CPUs; host reserve is 7.82 GiB. AX workers are limited to 2 GiB / one CPU. Direct Substrate actors additionally have a 1536 MiB limit. AX does not propagate actor limits at this revision. A documented Linux 6.5 overlayfs compatibility patch is applied; gVisor remains the only sandbox runtime.",
    "NoteCustom",
)
p("Sources and reproduction", "Heading2")
p(
    "Source directories: results/research, results/research-resumed and results/validation. Source summaries, metadata and baseline samples are copied under output/pdf/measured-results/data. Raw time series remain in their source directories. SVG charts accompany this PDF.",
    "NoteCustom",
)
p(
    f"AX {meta['versions']['ax'][:12]} | Substrate {meta['versions']['substrate'][:12]} | Python 3.13 slim. Upstream: github.com/google/ax; github.com/agent-substrate/substrate.<br/>Regenerate: uv run --no-project --with reportlab --with matplotlib --with pypdf scripts/pdf-measured-results.py",
    "NoteCustom",
)


def footer(c, doc):
    c.setStrokeColor(colors.HexColor("#d8e1ea"))
    c.line(48, 39, 547, 39)
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.HexColor("#536779"))
    c.drawString(48, 26, "AX + Agent Substrate | Measured RAM consumption")
    c.drawRightString(547, 26, str(doc.page))


file = OUT / "ax-sandbox-ram-measured-results.pdf"
SimpleDocTemplate(
    str(file),
    pagesize=A4,
    rightMargin=48,
    leftMargin=48,
    topMargin=43,
    bottomMargin=53,
    title="AX + Agent Substrate: Measured RAM Consumption",
    author="Local benchmark",
).build(story, onFirstPage=footer, onLaterPages=footer)
reader = PdfReader(file)
print(file, len(reader.pages), "pages")
for i, pdfpage in enumerate(reader.pages):
    print(i + 1, len(pdfpage.extract_text()), "characters")
(DATA / "selection.json").write_text(
    json.dumps({run: sorted({key(r) for r in rows}) for run, rows in runs.items()}, indent=2)
)
