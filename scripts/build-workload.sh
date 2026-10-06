#!/usr/bin/env bash
source "$(dirname "$0")/env.sh"
cd "$BENCH_ROOT"
mkdir -p .state/assets/wheels .state/logs
if [[ ! -f .state/base-image ]]; then
  base=$(python3 -c 'import json; print(json.load(open("config/versions.json"))["python_base"])')
  docker pull "$base"
  echo "$base" > .state/base-image
fi
base=$(cat .state/base-image)
docker build --build-arg "BASE_IMAGE=$base" -t localhost:5001/ax-bench-workload:v1 workload
docker push localhost:5001/ax-bench-workload:v1
docker image inspect localhost:5001/ax-bench-workload:v1 --format '{{index .RepoDigests 0}}' > .state/workload-image
if [[ ! -d .state/assets/fastapi.git ]]; then
  git clone --bare --single-branch --branch 0.115.6 https://github.com/fastapi/fastapi.git .state/assets/fastapi.git
  git -C .state/assets/fastapi.git update-server-info
fi
docker run --rm --user "$(id -u):$(id -g)" -v "$BENCH_ROOT/workload:/locks:ro" -v "$BENCH_ROOT/.state/assets/wheels:/wheels" "$base" python -m pip download --require-hashes -r /locks/dev-requirements.lock -d /wheels
python3 - <<'PY'
from pathlib import Path
import hashlib,json
p=Path('.state/assets')
(p/'wheels.json').write_text(json.dumps(sorted(x.name for x in (p/'wheels').glob('*.whl'))))
lock={'base_image':Path('.state/base-image').read_text().strip(),'workload_image':Path('.state/workload-image').read_text().strip(),'fastapi_commit':'bb8c2a64981d3e806575a445115f29eddf014c77','files':{str(x):hashlib.sha256(x.read_bytes()).hexdigest() for x in Path('workload').glob('*') if x.is_file()}}
Path('.state/workload-lock.json').write_text(json.dumps(lock,indent=2))
PY
