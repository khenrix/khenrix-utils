FROM jdxcode/mise:2026.9.10@sha256:993f61861001b816fd30f70c33a5421e85c2f310aafc1514084975e70ad0b24f AS mise

FROM debian:trixie-slim@sha256:7792b1f7702a86946cd518db72b6a407302c3e9bc1635634368b878189e8221c

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

ENV MISE_DATA_DIR=/opt/mise \
    MISE_CACHE_DIR=/opt/mise/cache \
    MISE_LOCKED=1 \
    MISE_USE_VERSIONS_HOST=false \
    AGY_CLI_DISABLE_AUTO_UPDATE=true
WORKDIR /opt/fanout
COPY mise.toml mise.lock relay.py /opt/fanout/
COPY --from=fanout_transfer container_transfer.py container_transfer_support.py /opt/fanout/
RUN mise install \
    && for tool in python claude codex agy; do \
         ln -s "$(mise which "$tool")" "/usr/local/bin/$tool"; \
       done \
    && ln -s "$(mise which python)" /usr/local/bin/python3 \
    && test "$(mise --version | head -n 1 | cut -d' ' -f1)" = "2026.9.10" \
    && install -d -o 65532 -g 65532 -m 0700 \
         /workspace /session /session/.config /session/.codex /session/tmp /run/fanout

ENV HOME=/session \
    XDG_CONFIG_HOME=/session/.config \
    CODEX_HOME=/session/.codex \
    CLAUDE_CODE_TMPDIR=/session/tmp \
    TMPDIR=/session/tmp \
    ANTHROPIC_BASE_URL=http://127.0.0.1:43187 \
    OPENAI_BASE_URL=http://127.0.0.1:43187 \
    GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:43187 \
    ANTHROPIC_API_KEY=disposable-placeholder \
    OPENAI_API_KEY=disposable-placeholder \
    GEMINI_API_KEY=disposable-placeholder
USER 65532:65532
WORKDIR /workspace
ENTRYPOINT ["python", "/opt/fanout/relay.py"]
CMD ["bridge", "--port", "43187", "--max-request-bytes", "1048576", "--max-response-bytes", "8388608"]
