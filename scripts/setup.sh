#!/usr/bin/env bash
source "$(dirname "$0")/env.sh"
cd "$BENCH_ROOT"
mkdir -p .state/tools .state/bin .state/logs .state/sources
if [[ ! -x .state/tools/go/bin/go ]]; then
  curl -fsSL https://go.dev/dl/go1.27.1.linux-amd64.tar.gz -o .state/tools/go.tar.gz
  echo '63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445  .state/tools/go.tar.gz' | sha256sum -c -
  tar -C .state/tools -xzf .state/tools/go.tar.gz
  rm .state/tools/go.tar.gz
fi
for entry in 'ax https://github.com/google/ax.git d0bc38bcf90bb2ad9c012ff1be9d68ff05347ba9' 'substrate https://github.com/agent-substrate/substrate.git 672533541dbfcd29084e4de2475267088bda3651'; do
  read -r name url ref <<< "$entry"
  if [[ ! -d .state/sources/$name ]]; then
    git clone "$url" ".state/sources/$name"
    git -C ".state/sources/$name" checkout "$ref"
  fi
  [[ $(git -C ".state/sources/$name" rev-parse HEAD) == "$ref" ]] || { echo "Unexpected $name revision" >&2; exit 1; }
done
if [[ ! -x .state/bin/kind ]]; then go install sigs.k8s.io/kind@v0.33.0; cp "$GOPATH/bin/kind" .state/bin/; fi
if [[ ! -x .state/bin/ko ]]; then go install github.com/google/ko@v0.18.0; cp "$GOPATH/bin/ko" .state/bin/; fi
# Versioned compatibility patch: fsconfig lowerdir+ arrived after this host kernel.
if git -C .state/sources/substrate apply --check "$BENCH_ROOT/patches/substrate-overlayfs-legacy.patch" 2>/dev/null; then
  git -C .state/sources/substrate apply "$BENCH_ROOT/patches/substrate-overlayfs-legacy.patch"
elif ! git -C .state/sources/substrate apply --reverse --check "$BENCH_ROOT/patches/substrate-overlayfs-legacy.patch" 2>/dev/null; then
  echo 'Substrate compatibility patch conflicts with local source changes' >&2; exit 1
fi
exec python3 -m bench.setup "$@"
