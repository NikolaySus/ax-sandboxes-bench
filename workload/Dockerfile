ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /opt/bench/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/bench/requirements.lock
COPY runner.py /usr/local/bin/ax-task-runner
COPY tasks.py dev-requirements.lock /opt/bench/
RUN chmod +x /usr/local/bin/ax-task-runner && mkdir -p /workspace
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/ax-task-runner"]
