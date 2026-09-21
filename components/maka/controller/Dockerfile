# syntax=docker/dockerfile:1.7
FROM --platform=linux/amd64 jdxcode/mise:2026.9.10@sha256:993f61861001b816fd30f70c33a5421e85c2f310aafc1514084975e70ad0b24f

USER root
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       tini=0.19.0-3+b8 \
    && test "$(dpkg-query -W -f='${Version}' tini)" = 0.19.0-3+b8 \
    && rm -rf /var/lib/apt/lists/*
RUN command -v flock >/dev/null

WORKDIR /opt/maka-lab
COPY mise.toml mise.lock ./
COPY .mise/locks ./.mise/locks
RUN mise trust /opt/maka-lab/mise.toml && mise install --locked

COPY requirements ./requirements
RUN mise exec -- uv venv --python 3.12.14 /opt/venvs/harbor-0.20.0 \
    && mise exec -- uv pip sync --python /opt/venvs/harbor-0.20.0/bin/python --require-hashes requirements/harbor.lock \
    && mise exec -- uv venv --python 3.12.14 /opt/venvs/pier-0.3.0 \
    && mise exec -- uv pip sync --python /opt/venvs/pier-0.3.0/bin/python --require-hashes requirements/pier.lock \
    && mkdir -p /root/.docker/cli-plugins \
    && ln -s "$(mise which docker-compose)" /root/.docker/cli-plugins/docker-compose \
    && ln -s "$(mise which docker-cli-plugin-docker-buildx)" /root/.docker/cli-plugins/docker-buildx \
    && test "$(mise exec -- docker buildx version | awk '{print $2}')" = v0.37.1

COPY controller ./controller
COPY config ./config
COPY fixtures/pier-tasks ./fixtures/pier-tasks
RUN mise exec -- python controller/test_secret_broker.py \
    && package_json="$(find /mise/installs -type f -path '*/node_modules/maka-agent/package.json' -print -quit)" \
    && test -n "$package_json" \
    && package_root="$(dirname "$package_json")" \
    && /opt/venvs/harbor-0.20.0/bin/python controller/test_maka_compat.py "$package_root" \
    && mise exec -- /opt/venvs/harbor-0.20.0/bin/python controller/test_maka_hosted_onboarding_compat.py "$package_root" \
    && mise exec -- node controller/provider-request-contract.mjs "$package_root" /tmp/provider-request.json \
    && mise exec -- python controller/assert-provider-body.py /tmp/provider-request.json \
    && rm /tmp/provider-request.json

LABEL org.opencontainers.image.title="Maka audit lab controller" \
      org.opencontainers.image.version="0.2.0-dev.44.20260920" \
      org.opencontainers.image.revision="feb9cf22fa216ce499860ad7cbcd59c32d28aa97" \
      io.maka.eval.compatibility-overlay="relay-root-virtiofs-v2-eval-openai-onboarding-v2" \
      io.maka.eval.relay-agent-sha256="8761f73c2940365ca8a5861a9057a62f0ea2de6276b393512e50f72cb66e3bd0" \
      io.maka.eval.model-fetcher-sha256="82d4fdb90a8c3794a745f2e39410f19970c7a8afc679e14589244992ac50e56c" \
      io.maka.eval.maka-subject-sha256="2f39e06a20291b7d2759d5bd9d54e912c3d32817c15f74140ef9cf62b51ec8f3" \
      io.maka.eval.terminal-bench-revision="d49e28f1e4ddd13d289e85a5f312a66750951932"

ENTRYPOINT ["/usr/bin/tini", "--", "/opt/maka-lab/controller/entrypoint.sh"]
CMD ["maka", "--version"]
