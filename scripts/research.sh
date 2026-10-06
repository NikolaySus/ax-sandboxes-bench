#!/usr/bin/env bash
# Run the complete research matrix; pass a unique result-directory name.
source "$(dirname "$0")/env.sh"
cd "$BENCH_ROOT"
exec python3 -m bench.cli run --name "${1:-research-$(date +%Y%m%d-%H%M%S)}"
