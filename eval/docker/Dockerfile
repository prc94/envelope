# Pinned Linux trial image (task 10.1): one container per trial on the `docker` target.
# Python 3.12, the shells the Linux lane's task suite declares (bash/sh/zsh/fish), git, and both
# harnesses under evaluation. Versions are pinned so a job's fingerprint (the image digest) is
# stable between builds; bump them here deliberately, not by rebuilding against "latest".
#
# Build:  docker build -t envelope-eval-linux:2026-09-28 -f eval/docker/Dockerfile eval/docker
# Base:   python:3.12-slim (Debian trixie); zsh and fish come from Debian's repos, Node.js 22 LTS
#         from NodeSource (needed for both harnesses; Debian trixie's own nodejs is older).

FROM python:3.12-slim

ARG QWEN_CODE_VERSION=0.24.5
ARG OPENCODE_VERSION=1.18.32
ARG NODE_MAJOR=22

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash zsh fish git ca-certificates curl gnupg \
    && install -d /usr/share/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && npm install -g "@qwen-code/qwen-code@${QWEN_CODE_VERSION}" "opencode-ai@${OPENCODE_VERSION}" \
    && chmod +x /usr/lib/node_modules/@qwen-code/qwen-code/vendor/ripgrep/*/rg \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.npm /tmp/*

# A non-root trial user; the runner creates one workspace per trial under its home. Envelope
# itself needs no install here: `envelope.pyz` is uploaded per job (build_variant, task 9.4 wiring).
RUN useradd --create-home --shell /bin/bash trial
USER trial
WORKDIR /home/trial

RUN qwen --version && opencode --version
