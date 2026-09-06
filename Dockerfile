# ─────────────────────────────────────────────────────────────────────────────
# Hermes WebUI — hermetic, container-only build.
#
# Two properties this file is responsible for:
#
#   1. The Hermes Agent enters this image ONLY from the official agent
#      CONTAINER IMAGE below. Nothing here (and nothing in bootstrap.py) ever
#      runs the agent's host installer (`curl … install.sh | bash`), so the
#      agent's supply chain is exactly one auditable, digest-pinnable artifact.
#
#   2. Every dependency the server imports — the WebUI's own and the agent's —
#      is resolved at BUILD time, inside the builder, into a root-owned venv
#      the runtime user cannot write to. A started container needs no network
#      and installs nothing, so the code that runs is exactly the code that
#      was reviewed at build time.
#
# Build args (see scripts/docker-build.sh / scripts/docker-build.ps1):
#   HERMES_AGENT_IMAGE        agent image to take the agent from. Pin it:
#                             nousresearch/hermes-agent@sha256:<digest>
#   AGENT_SOURCE              image (default) | none
#                             `none` builds a WebUI-only image whose agent is
#                             supplied at runtime by a mounted volume, the way
#                             docker-compose.two-container.yml does it. Pair it
#                             with HERMES_AGENT_IMAGE=python:3.12-slim so the
#                             agent image is never fetched at all.
#   AGENT_EXTRAS              agent extras to install (default: all)
#   AGENT_PRUNE_NODE_MODULES  1 (default) drops the agent's node_modules — the
#                             WebUI imports the agent as a Python library and
#                             never runs its JS tooling. 0 keeps the tree whole.
#   BAKE_RUNTIME              1 (default) bakes the venv. 0 falls back to the
#                             legacy install-at-container-start path.
# ─────────────────────────────────────────────────────────────────────────────
ARG HERMES_AGENT_IMAGE=nousresearch/hermes-agent:latest
ARG AGENT_SOURCE=image

# The agent image itself. Referenced only by the alias below, so BuildKit never
# fetches it when AGENT_SOURCE=none selects the empty stage instead.
FROM ${HERMES_AGENT_IMAGE} AS agent-image

# The "no agent baked in" alternative — an empty source tree.
FROM python:3.12-slim AS agent-none
RUN mkdir -p /opt/hermes

# One of the two above, chosen by AGENT_SOURCE.
FROM agent-${AGENT_SOURCE} AS agent-picked

# ── Agent source, pruned ────────────────────────────────────────────────────
# Pruning happens in its own stage so the discarded trees never reach a layer
# of the final image. The exclusion set mirrors docker_init.bash's runtime
# staging copy (egg-info / build / dist / __pycache__ / .git / .playwright)
# and adds node_modules, which is ~376 MB of JS the Python server never loads.
FROM python:3.12-slim AS agent-src
ARG AGENT_PRUNE_NODE_MODULES=1
COPY --from=agent-picked /opt/hermes /opt/hermes-agent
RUN set -eu; \
    cd /opt/hermes-agent; \
    rm -rf .git .playwright build dist .pytest_cache; \
    find . -name '*.egg-info' -prune -exec rm -rf {} + ; \
    find . -type d -name '__pycache__' -prune -exec rm -rf {} + ; \
    if [ "${AGENT_PRUNE_NODE_MODULES}" = "1" ]; then \
        find . -type d -name 'node_modules' -prune -exec rm -rf {} + ; \
    fi; \
    echo "-- agent source staged: $(du -sh /opt/hermes-agent | cut -f1)"

FROM python:3.12-slim AS runtime

LABEL maintainer="nesquena"
LABEL description="Hermes Web UI — browser interface for Hermes Agent"

# Install system packages
ENV DEBIAN_FRONTEND=noninteractive

# Make use of apt-cacher-ng if available
RUN if [ "A${BUILD_APT_PROXY:-}" != "A" ]; then \
        echo "Using APT proxy: ${BUILD_APT_PROXY}"; \
        printf 'Acquire::http::Proxy "%s";\n' "$BUILD_APT_PROXY" > /etc/apt/apt.conf.d/01proxy; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

RUN apt-get update -y --fix-missing --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    apt-utils \
    locales \
    ca-certificates \
    curl \
    rsync \
    openssh-client \
    git \
    xz-utils \
    && apt-get upgrade -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── SQLite upgrade ──────────────────────────────────────────────────────────
# The python:3.12-slim base ships SQLite 3.46.1 (Debian Trixie), which is
# vulnerable to the WAL-reset corruption bug discovered March 2026.
# https://sqlite.org/wal.html#walresetbug
#
# Debian has not backported the fix, so we compile from the amalgamation.
# Installs to /usr/local/lib (registered in ld.so.conf.d for arm64 priority).
# Build tools are purged after compilation to keep the image lean.
# Build args are for forward version bumps only (3.54+, etc.).
# When bumping SQLITE_VERSION, recompute the SHA-256 from the official
# download and update SQLITE_SHA256 accordingly.
ARG SQLITE_VERSION=3530000
ARG SQLITE_YEAR=2026
ARG SQLITE_SHA256=851e9b38192fe2ceaa65e0baa665e7fa06230c3d9bd1a6a9662d02380d73365a
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc make libc6-dev \
    && cd /tmp \
    && curl -fsSL "https://sqlite.org/${SQLITE_YEAR}/sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
       -o sqlite.tar.gz \
    && echo "${SQLITE_SHA256}  sqlite.tar.gz" | sha256sum -c - \
    && tar xzf sqlite.tar.gz \
    && cd "sqlite-autoconf-${SQLITE_VERSION}" \
    && CPPFLAGS="-DSQLITE_SECURE_DELETE" ./configure --prefix=/usr/local --disable-static --disable-readline \
       --enable-fts5 --enable-fts4 --enable-rtree \
    && make -j"$(nproc)" \
    && make install \
    && echo "/usr/local/lib" > /etc/ld.so.conf.d/000-usr-local-lib.conf \
    && /sbin/ldconfig \
    && cd / && rm -rf /tmp/sqlite* \
    && apt-get purge -y gcc make libc6-dev \
    && apt-get autoremove -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && python3 -c "\
import sqlite3; \
v = sqlite3.sqlite_version; \
assert tuple(int(x) for x in v.split('.')) >= (3, 51, 3), \
    f'SQLite {v} still vulnerable'; \
c = sqlite3.connect(':memory:'); \
assert c.execute('PRAGMA secure_delete').fetchone()[0] == 1, \
    'SQLITE_SECURE_DELETE not compiled in (deleted rows would remain recoverable)'; \
c.execute('CREATE VIRTUAL TABLE _fts5_build_check USING fts5(x)'); \
c.execute('DROP TABLE _fts5_build_check'); \
c.close()"

# Optional GPU user-space acceleration libraries for users who pass through
# host GPU devices. The default image remains CPU-only.
ARG INSTALL_GPU_LIBS=0
RUN if [ "$INSTALL_GPU_LIBS" = "1" ]; then \
        apt-get update -y --fix-missing --no-install-recommends \
        && apt-get install -y --no-install-recommends \
            libva2 \
            vainfo \
            mesa-va-drivers \
        && if apt-cache show intel-media-va-driver-non-free >/dev/null 2>&1; then \
            apt-get install -y --no-install-recommends intel-media-va-driver-non-free; \
        else \
            echo "intel-media-va-driver-non-free is not available from the configured Debian repositories; skipping Intel non-free VA-API driver."; \
        fi \
        && apt-get clean \
        && rm -rf /var/lib/apt/lists/*; \
    else \
        echo "Skipping optional GPU user-space acceleration libraries (INSTALL_GPU_LIBS=0)."; \
    fi

# UTF-8
RUN localedef -i en_US -c -f UTF-8 -A /usr/share/locale/locale.alias en_US.UTF-8
ENV LANG=en_US.utf8
ENV LC_ALL=C

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /apptoo

# Create the unprivileged runtime user. The entrypoint starts as root only for
# UID/GID alignment and filesystem preparation, then execs the server as this user.
RUN groupadd -g 1024 hermeswebui \
    && useradd -u 1024 -d /home/hermeswebui -g hermeswebui -G users -s /bin/bash -m hermeswebui \
    && mkdir -p /app /uv_cache /workspace \
    && chown -R hermeswebui:hermeswebui /home/hermeswebui /app /uv_cache /workspace \
    && chmod 0755 /home/hermeswebui \
    && chmod 1777 /app /uv_cache /workspace

COPY --chmod=555 docker_init.bash /hermeswebui_init.bash

RUN touch /.within_container

# Remove APT proxy configuration and clean up APT downloaded files
RUN rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/01proxy \
    && apt-get clean

USER root

# Pre-install uv system-wide so the container doesn't need internet access at runtime.
# Installing as root places uv in /usr/local/bin, available to all users.
# The init script will skip the download when uv is already on PATH.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

COPY --chown=root:root . /apptoo

# Bake the git version tag into the image so the settings badge works even
# when .git is not present (it is excluded by .dockerignore).
# CI passes: --build-arg HERMES_VERSION=$(git describe --tags --always)
# Local builds that omit the arg get "unknown" as the fallback.
ARG HERMES_VERSION=unknown
RUN echo "__version__ = '${HERMES_VERSION}'" > /apptoo/api/_version.py

# ── Baked runtime ───────────────────────────────────────────────────────────
# The agent's source and every Python dependency (WebUI + agent) are resolved
# here, at build time, into /opt/hermes-webui/venv.
#
# Both trees stay root-owned and non-writable by hermeswebui. That is the
# security property this build exists for: the unprivileged process that
# handles requests cannot rewrite its own interpreter, its dependencies, or the
# agent's code, and it never reaches a package index at run time. docker_init.bash
# detects the baked venv and skips its whole install path — see the
# "Baked runtime" branch there.
ARG BAKE_RUNTIME=1
ARG AGENT_EXTRAS=all
ENV HERMES_WEBUI_BAKED_VENV=/opt/hermes-webui/venv
ENV HERMES_WEBUI_BAKED_AGENT_DIR=/opt/hermes-agent

COPY --from=agent-src /opt/hermes-agent /opt/hermes-agent

RUN set -eu; \
    if [ "${BAKE_RUNTIME}" != "1" ]; then \
        echo "== BAKE_RUNTIME=0 — no baked venv; the container installs at startup (legacy path)"; \
        echo "== The agent source stays at /opt/hermes-agent; docker_init.bash installs from it."; \
        chown -R root:root /opt/hermes-agent 2>/dev/null || true; \
        chmod -R a+rX,go-w /opt/hermes-agent 2>/dev/null || true; \
        exit 0; \
    fi; \
    export UV_CACHE_DIR=/tmp/uv-build-cache; \
    export UV_LINK_MODE=copy; \
    _py="${HERMES_WEBUI_BAKED_VENV}/bin/python"; \
    echo "== Creating the baked virtual environment at ${HERMES_WEBUI_BAKED_VENV}"; \
    uv venv --python "$(command -v python3)" "${HERMES_WEBUI_BAKED_VENV}"; \
    uv pip install --python "$_py" --no-cache pip setuptools wheel; \
    echo "== Installing hermes-webui dependencies"; \
    uv pip install --python "$_py" -r /apptoo/requirements.txt; \
    echo "== Installing the Hindsight memory provider client"; \
    uv pip install --python "$_py" "hindsight-client>=0.4.22"; \
    if [ -f /opt/hermes-agent/pyproject.toml ]; then \
        echo "== Installing the Hermes Agent from the agent container image"; \
        uv pip install --python "$_py" -e "/opt/hermes-agent[${AGENT_EXTRAS}]"; \
    else \
        echo "!! No agent source in this build (AGENT_SOURCE=none)."; \
        echo "!! Mount an agent source volume at /home/hermeswebui/.hermes/hermes-agent,"; \
        echo "!! as docker-compose.two-container.yml does, or the WebUI starts with"; \
        echo "!! reduced functionality."; \
        rm -rf /opt/hermes-agent; \
    fi; \
    rm -rf /tmp/uv-build-cache; \
    chown -R root:root /opt/hermes-webui; \
    chmod -R a+rX,go-w /opt/hermes-webui; \
    if [ -d /opt/hermes-agent ]; then \
        chown -R root:root /opt/hermes-agent; \
        chmod -R a+rX,go-w /opt/hermes-agent; \
    fi

# Fail the build — not the first user request — when the baked runtime cannot
# import what the server needs. This mirrors bootstrap.py's
# _python_can_run_webui_and_agent() probe: a green build means chat works, not
# just that the image assembled. Skipped when nothing was baked.
RUN set -eu; \
    _py="${HERMES_WEBUI_BAKED_VENV}/bin/python"; \
    if [ ! -x "$_py" ]; then \
        echo "-- No baked venv to verify (BAKE_RUNTIME=0)"; \
        exit 0; \
    fi; \
    "$_py" -c "import yaml, cryptography; print('-- webui deps OK')"; \
    if [ -f /opt/hermes-agent/pyproject.toml ]; then \
        "$_py" -c "from run_agent import AIAgent; print('-- hermes-agent import OK')"; \
    fi

# Default to binding all interfaces (required for container networking)
ENV HERMES_WEBUI_HOST=0.0.0.0
ENV HERMES_WEBUI_PORT=8787

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=8s --start-period=10s --retries=3 \
  CMD bash /apptoo/scripts/lib/health_probe.sh localhost 8787 /health 2 >/dev/null || exit 1

# docker_init.bash performs root-only bind-mount setup, then drops to hermeswebui
# before starting the WebUI server. The production image does not ship sudo.
USER root
CMD ["/hermeswebui_init.bash"]

