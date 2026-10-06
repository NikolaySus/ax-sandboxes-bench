# AX + Agent Substrate RAM benchmark

A Linux/cgroup-v2 benchmark for persistent gVisor sandboxes on a dedicated single-node kind cluster. It installs Google AX, Agent Substrate, their local storage/control-plane dependencies, and gVisor. No LLM provider, credentials, inference, or Deep Agents runtime is used.

Git contains source code, tests, configuration, dependency locks, patches and documentation. Installed components, credentials, raw measurements and generated reports stay local. Links below to `results/` or `output/` refer to locally generated files and are not included in the remote repository.

## Run

The bootstrap requires working Docker, Git, curl, Python 3, tar and kubectl. Go, kind and ko are installed into `.state/`; it does not modify your normal kubeconfig. Initial builds and downloads need at least 20 GiB free disk. This repository targets Linux amd64.

```bash
scripts/setup.sh                 # local infrastructure; resumes completed stages
scripts/build-workload.sh        # locked Python 3.13 workload, wheels and Git mirror
scripts/bench preflight
scripts/bench run --smoke --name smoke
scripts/bench baselines           # optional clean, warm staged infrastructure comparison
scripts/research.sh research      # full adaptive matrix, three trials
scripts/bench report results/research
scripts/cleanup.sh               # benchmark actors/templates/snapshots/workers
scripts/cleanup.sh --stack       # also AX/Substrate and benchmark assets
scripts/cleanup.sh --cluster     # also owned kind node and registry
```

The research default is both API paths × three scenarios × three trials, with adaptive concurrency and 60-second measurement windows after stabilization. Allow many hours; large idle-sandbox capacities can make the full matrix run overnight or longer. `--backends ax` or `--scenarios documents` narrows the matrix; `--trials` and `--sample-seconds` are explicit overrides recorded in metadata. Smoke runs use one sandbox and short windows; they are validation, not budgeting evidence. Only one run may execute at a time.

A run writes `samples.jsonl`, `samples.csv`, `events.jsonl`, `metadata.json`, `summary.json`, `status.json`, `report.md` and `memory.svg` under `results/<name>/`. Partial results survive failure. The report can be regenerated without a running cluster. Raw data and downloaded sources are ignored by Git.

For the research run started during implementation, follow `.state/logs/research.log` and `results/research/status.json`. Its PID is in `.state/research.pid`; `kill -INT "$(cat .state/research.pid)"` requests an orderly stop with sandbox cleanup and a partial report. The report is refreshed after each completed concurrency level. `results/validation/report.md` contains the completed six-scenario smoke validation.

## What runs

The Linux 6.5 host requires the documented [overlayfs compatibility patch](patches/README.md), applied automatically during setup. The worker image still runs gVisor exclusively.

The versioned OCI image derives from `python:3.13-slim`, pinned to its resolved digest. pandas, openpyxl, python-docx and PyYAML are preinstalled from hash-locked requirements. The small HTTP runner imports no document libraries while idle and stays alive between commands. It exposes readiness/metadata endpoints and authenticated benchmark jobs through Substrate's router. Host ports bind loopback; this is a local experiment, not a production command service.

- **Idle:** explicitly activate a newly created actor/task and leave the runner idle. A merely registered, never-activated actor is not counted as an active idle sandbox.
- **Documents:** deterministic Python and filesystem work over 10,000 rows, pandas aggregation, XLSX/DOCX generation and read-back validation. Active and post-workload idle states are separate.
- **Developer:** clone FastAPI commit `bb8c2a64981d3e806575a445115f29eddf014c77`, create a venv, install locked dependencies, run three offline test modules and build a wheel. Downloads come from the local prebuilt mirror/wheel service; installation and cloning still happen inside each sandbox.
- **Suspend/resume:** check files and an in-memory nonce. AX uses data-only snapshots and golden-image restart; direct Substrate uses full snapshots. Measure suspended actors with empty workers retained and with the pool scaled to zero, then resume and validate again.

Workload images, scripts, dependency hashes, infrastructure pins and effective host limits are recorded. `config/versions.json` pins AX and the exact Substrate revision its Go module declares. Upstream gVisor assets are checksum-pinned. Workload commands execute through the actor router, never through Kubernetes exec.

## Interpreting RAM

The physical host collector reads `/proc/meminfo` and the outer Docker node cgroup, then pod parents and gVisor `_pause` cgroup leaves. It reports current charged RAM, approximate working set (`current - inactive_file`), anonymous/file/kernel memory, CPU time, swap and OOM events. Where readable, process RSS/PSS is supplementary; it is not an independent addition to cgroup memory.

**Do not add nested totals:** actor cgroups are inside workers, which are inside the node. gVisor actor memory includes sentry/gofer overhead. The report separates AX/Redis, Substrate/storage, monitoring, worker pods and sandbox totals. Host `MemTotal - MemAvailable` is a pressure-oriented estimate, not process RSS. Shared page cache, snapshots and allocator retention can make baseline deltas noisy or negative. No cache dropping or swap manipulation is performed on the host.

Samples are taken every second, including transitions. Stabilization requires three 20-second windows whose means and medians differ by at most 5% or 16 MiB. A five-minute timeout produces an explicitly unstable result. Cyclic work remains active throughout the measurement; command output records completed cycle boundaries. Samples, missing metrics and lifetime cgroup peaks remain in raw JSON. Table peaks are synchronized sampled peaks.

Concurrency follows 1 → 2 → 5 → 10 → approximately 1.5×, constrained by observed RAM/CPU demand and a 50% predictive margin. Host reserve is max(4 GiB, 25% RAM), node ceiling is at most 70% host RAM, and the watchdog stops at 80% of that ceiling, host reserve infringement, swap, OOM, pressure, sustained high CPU or low disk space. Worker requests are 256 MiB/100m; outer limits are 2 GiB/one CPU. AX at this revision does not propagate task limits into actor templates; this limitation is explicitly reported. Direct actors are capped at 1536 MiB/one CPU. Because AX does not propagate the same actor limits, differences between API paths are not a pure measure of AX overhead; the separate infrastructure cgroups provide that attribution.

The result is the largest **tested safe** count for this machine, workload and policy, not a universal maximum. Capacity planning should include fixed infrastructure, retained worker overhead, state-specific incremental memory and operational headroom. There is no assumed zero-RAM suspended state.

## Development and diagnostics

```bash
python3 -m unittest discover -s tests -v
source scripts/env.sh
kubectl get pods -A
```

Setup logs live in `.state/logs/`. If the host uses a loopback HTTP proxy, setup starts a relay on the private kind bridge and configures the node to bypass it for local traffic. Cleanup removes the owned relay when deleting the cluster. No existing clusters or unrelated Docker images are pruned.

Sources: [Google AX](https://github.com/google/ax), [Agent Substrate](https://github.com/agent-substrate/substrate), [gVisor](https://github.com/google/gvisor).

## Initial PDF report

[Download the interim report](output/pdf/ax-sandbox-ram-report.pdf). It contains three plots, baseline and workload tables, and the first completed research lifecycle. The full sweep remains in progress; the PDF is a dated snapshot, not final capacity guidance. To export a new snapshot:

```bash
uv run --with reportlab --with matplotlib --with pypdf scripts/pdf-report.py
```

Source summaries are preserved under `output/pdf/data/`; editable SVG plots sit alongside the PDF.

## Continuation after the monitoring OOM

The original research run stopped because Prometheus exceeded its upstream 512 MiB limit. `config/prometheus-patch.json` now sets a 2 GiB limit, a 1536 MiB Go memory target and two-hour/2 GB retention. Setup reapplies this configuration. A working-set guard stops the benchmark at 80% of the Prometheus limit. The replacement pod starts with fresh ephemeral monitoring storage; raw benchmark measurements remain in the results directories.

The continuation preserves AX idle trial 1 in `results/research/` and restarts the incomplete trial 2 at count 1. It then executes the remaining trials and scenarios. New cohorts collect their own baselines; old installation baselines are historical, and configurations must not be pooled blindly.

```bash
scripts/bench run --name research-resumed --continue-from results/research
```

Current continuation: `results/research-resumed/`, log `.state/logs/research-resumed.log`, PID `.state/research.pid`. The initial PDF remains a dated snapshot.

## Measured-results PDF

[Download the measured-results report](output/pdf/measured-results/ax-sandbox-ram-measured-results.pdf). It combines the existing AX idle and document concurrency measurements with separate single-sandbox validation results. Monitoring configurations are distinguished; only cohorts with a full residual measurement window are selected.

```bash
uv run --no-project --with reportlab --with matplotlib --with pypdf scripts/pdf-measured-results.py
```
