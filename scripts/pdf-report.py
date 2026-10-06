#!/usr/bin/env python3
"""Export a reproducible interim PDF from completed benchmark summaries.
Run: uv run --with reportlab --with matplotlib --with pypdf scripts/pdf-report.py
"""

import json
import shutil
import statistics
from datetime import datetime, timezone
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
OUT = ROOT / "output/pdf"
DATA = OUT / "data"
TMP = ROOT / "tmp/pdfs"
for p in (OUT, DATA, TMP):
    p.mkdir(parents=True, exist_ok=True)
for name in ("research", "validation"):
    dest = DATA / name
    dest.mkdir(exist_ok=True)
    for file in ("summary.json", "metadata.json", "status.json"):
        shutil.copy2(ROOT / "results" / name / file, dest / file)
shutil.copytree(
    ROOT / "results/research/installation-baselines", DATA / "baselines", dirs_exist_ok=True
)


def load(path):
    return json.loads(path.read_text())


research = load(DATA / "research/summary.json")
validation = load(DATA / "validation/summary.json")
status = load(DATA / "research/status.json")
meta = load(DATA / "research/metadata.json")
cutoff = datetime.fromtimestamp(
    status.get("updated_at", meta["started_at"]), timezone.utc
).strftime("%Y-%m-%d %H:%M UTC")
created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
MIB = 2**20
completed = {
    (x["backend"], x["scenario"], x["trial"], x["count"]) for x in status.get("completed", [])
}
research = [
    r for r in research if (r["backend"], r["scenario"], r["trial"], r["count"]) in completed
]
(DATA / "export.json").write_text(
    json.dumps(
        {
            "exported_at": created,
            "research_cutoff": cutoff,
            "completed_cohorts": list(map(list, sorted(completed))),
        },
        indent=2,
    )
)
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.facecolor": "white",
        "axes.labelcolor": "#25364a",
        "text.color": "#25364a",
    }
)
blue, teal = "#2563a6", "#13867e"


def plotfile(name, fig):
    file = TMP / (name + ".png")
    fig.savefig(file, dpi=190, bbox_inches="tight")
    fig.savefig(OUT / (name + ".svg"), bbox_inches="tight")
    plt.close(fig)
    return file


stages = []
for key, label in [
    ("kubernetes-only", "Kubernetes"),
    ("substrate-installed", "+ Substrate"),
    ("ax-installed", "+ AX (full stack)"),
]:
    rows = [json.loads(s) for s in (DATA / "baselines" / f"{key}.jsonl").read_text().splitlines()]
    stages.append(
        (
            label,
            statistics.mean(r["node"]["working_set"] for r in rows) / MIB,
            max(r["node"]["working_set"] for r in rows) / MIB,
            statistics.mean(r["host"]["used"] for r in rows) / MIB,
        )
    )
fig, ax = plt.subplots(figsize=(7.1, 2.7))
ax.barh([r[0] for r in stages][::-1], [r[1] for r in stages][::-1], color=[blue, teal, "#8b9bad"])
for i, r in enumerate(stages[::-1]):
    ax.text(r[1] + 18, i, f"{r[1]:,.0f}", va="center", fontsize=10)
ax.set_xlim(0, max(r[1] for r in stages) * 1.2)
ax.set_xlabel("Node working set (MiB), mean of 60 samples")
fig.tight_layout()
baseline_plot = plotfile("infrastructure-baseline", fig)

vrows = []
for scenario, phase, label in [
    ("idle", "idle", "Idle"),
    ("documents", "active", "Documents"),
    ("developer", "active", "Developer"),
]:
    for backend in ["ax", "substrate"]:
        r = next(
            x
            for x in validation
            if x["backend"] == backend and x["scenario"] == scenario and x["phase"] == phase
        )
        m = r["metrics"]["sandbox_working_set"]
        vrows.append((label, backend, m["mean"] / MIB, m["median"] / MIB, m["peak"] / MIB))
fig, ax = plt.subplots(figsize=(7.1, 2.9))
x = np.arange(3)
for j, (backend, color, label) in enumerate(
    [("ax", blue, "AX"), ("substrate", teal, "Direct Substrate")]
):
    rows = [r for r in vrows if r[1] == backend]
    vals = [r[2] for r in rows]
    pos = x + (j - 0.5) * 0.34
    ax.bar(pos, vals, 0.32, label=label, color=color)
    ax.scatter(pos, [r[4] for r in rows], marker="_", s=160, color="#182535", zorder=4)
ax.set_xticks(x, ["Idle", "Documents", "Developer"])
ax.set_ylabel("Sandbox working set (MiB)")
ax.set_ylim(0, 205)
ax.legend(frameon=False, loc="upper left")
ax.text(
    0.99,
    0.97,
    "Black marks: sampled peaks",
    transform=ax.transAxes,
    ha="right",
    va="top",
    fontsize=8,
)
fig.tight_layout()
workload_plot = plotfile("validation-workloads", fig)

# A single completed cohort keeps lifecycle attribution easy to inspect.
cohort = sorted(completed)[0]
rrows = [r for r in research if (r["backend"], r["scenario"], r["trial"], r["count"]) == cohort]
phase_names = [
    ("baseline", "Baseline"),
    ("empty-workers", "Empty worker"),
    ("idle", "Idle"),
    ("suspended", "Suspended"),
    ("suspended-no-workers", "No workers"),
    ("resumed", "Resumed"),
    ("residual", "After destroy"),
]
rr = [next(r for r in rrows if r["phase"] == p) for p, _ in phase_names]
fig, ax = plt.subplots(figsize=(7.1, 2.9))
means = [r["metrics"]["node_working_set"]["mean"] / MIB for r in rr]
ax.plot(range(len(rr)), means, "o-", color=blue, lw=2)
ax.fill_between(
    range(len(rr)),
    [r["metrics"]["node_working_set"]["median"] / MIB for r in rr],
    [r["metrics"]["node_working_set"]["peak"] / MIB for r in rr],
    color=blue,
    alpha=0.15,
    label="Median to sampled peak",
)
ax.set_xticks(range(len(rr)), [label.replace(" ", "\n") for _, label in phase_names], fontsize=8)
ax.set_ylabel("Total node working set (MiB)")
ax.legend(frameon=False, fontsize=8)
ax.grid(axis="y", alpha=0.18)
fig.tight_layout()
lifecycle_plot = plotfile("research-lifecycle", fig)

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


p("Sandbox RAM benchmark", "TitleCustom")
p("Google AX + Agent Substrate | gVisor | local single-node Kubernetes", "Deck")
p(
    f"<b>INTERIM REPORT</b> - exported {created}<br/>Research summary cutoff: {cutoff}. The adaptive sweep is still running.",
    "Deck",
)
p(
    "<b>Current finding:</b> the clean, warm infrastructure baseline is approximately "
    + f"{stages[-1][1] / 1024:.2f}"
    + " GiB of node working set. Sandbox lifecycle and document/developer workloads pass validation. A final per-user RAM budget and maximum safe concurrency are not yet established."
)
p(
    "The workload image is based on Python 3.13 slim with preinstalled pandas, openpyxl and python-docx. No LLM provider, inference or Deep Agents runtime runs in the sandbox."
)
title("Infrastructure before sandboxes")
picture(baseline_plot)
table(
    [["Warm stage", "Node mean MiB", "Node peak MiB", "Host used MiB"]]
    + [[r[0], f"{r[1]:,.1f}", f"{r[2]:,.1f}", f"{r[3]:,.1f}"] for r in stages],
    [175, 108, 108, 108],
)
p(
    "Each stage uses 60 one-second samples after stabilization. Infrastructure is scaled down and restored on the same node; images and caches remain warm. These are cumulative node totals, not independent components to add. Host used = MemTotal - MemAvailable.",
    "NoteCustom",
)
page()
title("Workload validation: one sandbox")
p(
    "<b>Smoke evidence only.</b> All six API/scenario combinations completed create, execute, suspend, resume and destroy. These five-sample windows validate behavior; they do not establish production sizing."
)
picture(workload_plot)
table(
    [["Workload", "API", "Mean MiB", "Median MiB", "Peak MiB"]]
    + [[r[0], "AX" if r[1] == "ax" else "Substrate", *[f"{v:.1f}" for v in r[2:]]] for r in vrows],
    [120, 100, 93, 93, 93],
)
p(
    "<b>Documents:</b> deterministic 10,000-row pandas processing, filesystem operations, XLSX and DOCX generation and read-back checks."
)
p(
    "<b>Developer:</b> clone pinned FastAPI sources, create a virtual environment, install locked dependencies from a local wheelhouse, execute selected tests and build a wheel. The plotted active phase covers repeated tests/builds; clone and installation are recorded separately as preparation transitions."
)
p(
    "Sandbox working set includes gVisor sentry/gofer memory. It is nested inside worker and node totals. The smaller developer value is specific to this workload and sampled phase; it does not imply that developer workloads generally need less RAM.",
    "NoteCustom",
)
p(
    "All 10 benchmark unit tests pass. The final workload image also passed readiness, bounded command-output handling, exit-status and process-group cancellation checks.",
    "NoteCustom",
)
page()
title("Research lifecycle: preliminary results")
p(
    f"Completed cohort shown: <b>{cohort[0].upper()} / {cohort[1]}, trial {cohort[2]}, {cohort[3]} sandbox</b>. Each row has a stabilized 60-sample measurement window. No concurrency threshold has yet been established."
)
picture(lifecycle_plot)
table(
    [["State", "Node mean", "Node median", "Node peak", "Delta / sandbox"]]
    + [
        [
            label,
            *[f(r["metrics"]["node_working_set"][s]) for s in ["mean", "median", "peak"]],
            f(r["incremental_node_bytes_per_sandbox"]),
        ]
        for (_, label), r in zip(phase_names, rr)
    ],
    [135, 88, 88, 88, 100],
)
p(
    "All table values are MiB. Delta per sandbox = (state node mean - that cohort baseline node mean) / sandbox count. Deltas include worker, cache and infrastructure effects; they are not isolated process RSS.",
    "NoteCustom",
)
p(
    "<b>Suspension is not zero total RAM.</b> The sandbox cgroup disappears, but retained workers, snapshot storage and caches can remain charged. In this cohort, suspended node memory exceeds the initial idle state; this is not evidence of a live suspended Python process."
)
p(
    "<b>Persistence contracts differ:</b> AX preserves workspace files and restarts process memory from a golden image. Direct Substrate full snapshots preserve both files and the in-memory nonce. Both paths passed resume validation."
)
page()
title("Method, limits and reproducibility")
p("Measurement design", "Heading2")
p(
    "The research matrix covers both API paths, three scenarios and three trials. Concurrency progresses 1, 2, 5, 10 and then approximately 1.5x, subject to observed RAM/CPU cost and a 50% predictive margin. Each state must stabilize over three 20-second windows, followed by 60 samples. A five-minute timeout marks a window unstable rather than hiding it."
)
p("Resource guards", "Heading2")
limits = meta["limits"]
p(
    f"The node has a {limits['memory_limit'] / 2**30:.2f} GiB memory ceiling and {limits['cpus']:g} CPU quota. Scaling stops at 80% of that ceiling, insufficient host reserve ({limits['host_reserve'] / 2**30:.2f} GiB), sustained CPU pressure, swap, OOM, memory pressure or low disk space. The reported capacity will be the largest tested safe count under these guards, not an absolute maximum."
)
p("Accounting and comparison limits", "Heading2")
p(
    "Host /proc and cgroup-v2 counters provide charged memory, working set, anonymous/file/kernel memory, CPU, swap and OOM observations. Working set is current memory minus inactive file cache. Readable process RSS/PSS is supplementary. Node, worker and sandbox counters are nested: do not add them together."
)
p(
    "AX/Redis, Substrate/storage and monitoring are accounted separately in raw summaries. AX at the pinned revision does not propagate actor limits: worker limits enforce 2 GiB / one CPU. Direct actors also have a 1536 MiB / one CPU limit. API-path differences are therefore not a pure estimate of AX overhead."
)
p(
    "A Linux 6.5 overlayfs compatibility patch is required on this host; the sandbox runtime remains gVisor. Image digests, patch hash, dependency hashes and effective limits are recorded in metadata. Smoke and research image digests are recorded separately."
)
p("Evidence and rerun", "Heading2")
p(
    "Source records: <b>results/validation/</b> and <b>results/research/</b> contain raw samples.jsonl, samples.csv, events, metadata, summaries and reports. This PDF preserves its source summaries and baseline samples in <b>output/pdf/data/</b>. Charts are also supplied as SVG files alongside the PDF."
)
p(
    "Setup: scripts/setup.sh; scripts/build-workload.sh<br/>Research: scripts/research.sh &lt;unique-run-name&gt;<br/>Cleanup: scripts/cleanup.sh [--stack | --cluster]<br/>Refresh PDF: uv run --with reportlab --with matplotlib --with pypdf scripts/pdf-report.py",
    "NoteCustom",
)
p(
    "Local methodology: README.md and patches/README.md. Upstream projects: github.com/google/ax and github.com/agent-substrate/substrate.",
    "NoteCustom",
)
p(
    f"Pinned AX: {meta['versions']['ax'][:12]} | Substrate: {meta['versions']['substrate'][:12]} | Workload: {meta['versions']['workload_version']}",
    "NoteCustom",
)


def footer(c, doc):
    c.setStrokeColor(colors.HexColor("#d8e1ea"))
    c.line(48, 39, 547, 39)
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.HexColor("#536779"))
    c.drawString(48, 26, "AX + Agent Substrate | Interim RAM benchmark")
    c.drawRightString(547, 26, f"{doc.page}")


file = OUT / "ax-sandbox-ram-report.pdf"
SimpleDocTemplate(
    str(file),
    pagesize=A4,
    rightMargin=48,
    leftMargin=48,
    topMargin=43,
    bottomMargin=53,
    title="AX + Agent Substrate: Interim RAM Benchmark",
    author="Local benchmark",
).build(story, onFirstPage=footer, onLaterPages=footer)
reader = PdfReader(file)
print(f"{file}: {len(reader.pages)} pages")
for i, pdf_page in enumerate(reader.pages):
    print(i + 1, len(pdf_page.extract_text()), "characters")
