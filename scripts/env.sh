#!/usr/bin/env bash
set -euo pipefail
BENCH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export BENCH_ROOT
export PATH="$BENCH_ROOT/.state/bin:$BENCH_ROOT/.state/tools/go/bin:$PATH"
export GOPATH="$BENCH_ROOT/.state/go"
export GOCACHE="$BENCH_ROOT/.state/go-cache"
export GOMAXPROCS=4
export GOFLAGS='-p=4'
export KUBECONFIG="$BENCH_ROOT/.state/kubeconfig"
export KIND_CLUSTER_NAME=ax-ram-bench
export KUBECTL_CONTEXT=kind-ax-ram-bench
export KO_DOCKER_REPO=localhost:5001
export KO_DEFAULTPLATFORMS=linux/amd64
export NO_DEV_ENV=true
