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
    && mise exec -- /opt/venvs/harbor-0.20.0/bin/python controller/test_maka_harbor_judge_compat.py "$package_root" \
    && mise exec -- /opt/venvs/harbor-0.20.0/bin/python controller/test_undici_h2_close_compat.py "$package_root" \
    && mise exec -- node controller/provider-request-contract.mjs "$package_root" /tmp/provider-request.json \
    && mise exec -- python controller/assert-provider-body.py /tmp/provider-request.json \
    && rm /tmp/provider-request.json

LABEL org.opencontainers.image.title="Maka audit lab controller" \
      org.opencontainers.image.version="0.2.0-dev.62.20260928" \
      org.opencontainers.image.revision="2f32205528a4436783c800715261d4b6e286023d" \
      io.maka.eval.compatibility-overlay="relay-root-virtiofs-v2-eval-openai-onboarding-v2" \
      io.maka.eval.relay-agent-sha256="6214ed402d2d8345b20b625739b9a08905116bfdd944775938d5d78054033621" \
      io.maka.eval.model-fetcher-sha256="427672c03fead122a387d4f690d85ec2d800fe7e7a9e70d02f8fbe648d413dd7" \
      io.maka.eval.maka-subject-sha256="f47c1278ed25434f2a1a012d26ed285eb930e737acbc182cbdcda6b914887775" \
      io.maka.judge.compatibility-overlay="relay-root-virtiofs-v2-eval-openai-onboarding-v2-harbor-judge-sol61-v1-undici-h2-close-v1" \
      io.maka.judge.runtime-variant="harbor-judge-sol61-h2-v1" \
      io.maka.judge.model-fetcher-sha256="7afdf60203915621ea87e85f786a07e0dbc6a4a8332f5f8034486594f0e2bfa5" \
      io.maka.judge.model-metadata-sha256="9996eeb39bd34a89b0161ce4acaf036c2b2429cd13157b52a63dabe9189501b7" \
      io.maka.judge.undici-h2-base-sha256="611003bb876a218e5bd652877766e8dbfef6e6d54274241fe31b4ccb5e3895e7" \
      io.maka.judge.undici-h2-sha256="521fef59ccb84909a3c68d52c15e972f61836e0d386093d5d4f4e64ba7d85087" \
      io.maka.eval.terminal-bench-revision="d49e28f1e4ddd13d289e85a5f312a66750951932"

ENTRYPOINT ["/usr/bin/tini", "--", "/opt/maka-lab/controller/entrypoint.sh"]
CMD ["maka", "--version"]
