#!/usr/bin/env bash
#
# Build Hermes WebUI entirely inside containers.
#
# Nothing this script does touches the host beyond the Docker socket: no venv,
# no pip, no agent installer, no Node. The image build runs in BuildKit, the
# Hermes Agent comes out of the pinned hermes-agent container image, and both
# dependency sets are resolved at build time into a root-owned venv the runtime
# user cannot write to. See the "Baked runtime" block in the Dockerfile.
#
#   ./scripts/docker-build.sh                 build the image
#   ./scripts/docker-build.sh --pin           resolve the agent tag to a digest first
#   ./scripts/docker-build.sh --verify        build, then boot-smoke it in a container
#   ./scripts/docker-build.sh --test          build, then run pytest in a container
#   ./scripts/docker-build.sh --up            build, then `docker compose up -d`
#   ./scripts/docker-build.sh --lean          WebUI-only image (agent mounted at runtime)
#
# The Windows equivalent is scripts/docker-build.ps1.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Seed build-arg defaults from .env, which `docker compose build` reads on its
# own. Without this the two paths silently produce DIFFERENT images whenever an
# arg is set only there. Precedence stays: CLI flag > environment > .env >
# built-in default — a name already exported wins, so this never clobbers an
# explicit choice.
_env_default() {
  local name="$1"
  [[ -n "${!name+x}" ]] && return 0            # already set in the environment
  [[ -f "${REPO_ROOT}/.env" ]] || return 0
  local raw
  raw="$(grep -m1 "^${name}=" "${REPO_ROOT}/.env" 2>/dev/null || true)"
  [[ -n "$raw" ]] || return 0
  raw="${raw#*=}"
  raw="${raw%$'\r'}"
  raw="${raw%\"}"; raw="${raw#\"}"
  raw="${raw%\'}"; raw="${raw#\'}"
  printf -v "$name" '%s' "$raw"
}
for _arg_name in HERMES_AGENT_IMAGE AGENT_SOURCE AGENT_EXTRAS \
                 AGENT_PRUNE_NODE_MODULES BAKE_RUNTIME \
                 HERMES_WEBUI_IMAGE; do
  _env_default "$_arg_name"
done
unset _arg_name

# Matches the `image:` in docker-compose.yml so --verify, --test and --up all
# act on the same artifact. Deliberately not the published ghcr.io name.
IMAGE_TAG="${HERMES_WEBUI_IMAGE:-hermes-webui-doc:latest}"
AGENT_IMAGE="${HERMES_AGENT_IMAGE:-nousresearch/hermes-agent:latest}"
AGENT_SOURCE="${AGENT_SOURCE:-image}"
AGENT_EXTRAS="${AGENT_EXTRAS:-all}"
AGENT_PRUNE_NODE_MODULES="${AGENT_PRUNE_NODE_MODULES:-1}"
BAKE_RUNTIME="${BAKE_RUNTIME:-1}"

DO_PIN=0
DO_VERIFY=0
DO_TEST=0
DO_UP=0
DO_NO_CACHE=0
SMOKE_PORT="${HERMES_WEBUI_SMOKE_PORT:-8799}"

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pin) DO_PIN=1 ;;
    --verify) DO_VERIFY=1 ;;
    --test) DO_TEST=1 ;;
    --up) DO_UP=1 ;;
    --no-cache) DO_NO_CACHE=1 ;;
    --lean)
      # No agent baked in; the agent is supplied at runtime by a mounted
      # volume, the way docker-compose.two-container.yml does it. Point the
      # agent stage at a base image that is already local so nothing is pulled.
      AGENT_SOURCE=none
      AGENT_IMAGE=python:3.12-slim
      ;;
    --tag) IMAGE_TAG="$2"; shift ;;
    --agent-image) AGENT_IMAGE="$2"; shift ;;
    -h|--help) usage 0 ;;
    *) echo "[XX] Unknown argument: $1" >&2; usage 1 ;;
  esac
  shift
done

say() { echo "[docker-build] $*"; }
die() { echo "[docker-build] ERROR: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
docker version --format '{{.Server.Version}}' >/dev/null 2>&1 \
  || die "the Docker daemon is not reachable — start Docker and re-run"

# ── Pin the agent image by digest ───────────────────────────────────────────
# A tag is a moving target. Resolving it to a digest once, up front, means the
# build records exactly which agent artifact went in — and a rebuild either
# gets that same artifact or fails loudly.
if [[ "$DO_PIN" -eq 1 && "$AGENT_SOURCE" == "image" ]]; then
  say "Resolving $AGENT_IMAGE to a digest"
  docker pull "$AGENT_IMAGE" >/dev/null
  _digest="$(docker image inspect "$AGENT_IMAGE" \
    --format '{{ index .RepoDigests 0 }}' 2>/dev/null || true)"
  [[ -n "$_digest" ]] || die "could not resolve a digest for $AGENT_IMAGE"
  AGENT_IMAGE="$_digest"
  say "Pinned agent image: $AGENT_IMAGE"
fi

HERMES_VERSION="$(git describe --tags --always 2>/dev/null || echo unknown)"

build_args=(
  --build-arg "HERMES_AGENT_IMAGE=${AGENT_IMAGE}"
  --build-arg "AGENT_SOURCE=${AGENT_SOURCE}"
  --build-arg "AGENT_EXTRAS=${AGENT_EXTRAS}"
  --build-arg "AGENT_PRUNE_NODE_MODULES=${AGENT_PRUNE_NODE_MODULES}"
  --build-arg "BAKE_RUNTIME=${BAKE_RUNTIME}"
  --build-arg "HERMES_VERSION=${HERMES_VERSION}"
)
[[ "$DO_NO_CACHE" -eq 1 ]] && build_args+=(--no-cache)

say "Building ${IMAGE_TAG}"
say "  agent image  : ${AGENT_IMAGE}"
say "  agent source : ${AGENT_SOURCE} (extras: ${AGENT_EXTRAS})"
say "  baked runtime: ${BAKE_RUNTIME}"
say "  webui version: ${HERMES_VERSION}"

if docker buildx version >/dev/null 2>&1; then
  docker buildx build --load -t "$IMAGE_TAG" "${build_args[@]}" .
else
  docker build -t "$IMAGE_TAG" "${build_args[@]}" .
fi
say "Built ${IMAGE_TAG}"

# ── Containerised boot smoke ────────────────────────────────────────────────
if [[ "$DO_VERIFY" -eq 1 ]]; then
  container="hermes-webui-smoke-$$"
  cleanup_smoke() {
    local rc=$?
    docker logs "$container" 2>&1 | tail -80 || true
    docker rm -f "$container" >/dev/null 2>&1 || true
    return $rc
  }
  trap cleanup_smoke EXIT

  # No mounts: state lives in the container's own layer, so the smoke tests the
  # image and nothing about the host's directory ownership. The state dir is
  # passed explicitly because docker_init.bash requires it (compose sets it too).
  say "Boot smoke on 127.0.0.1:${SMOKE_PORT} with container-local state"
  docker run -d --name "$container" \
    -p "127.0.0.1:${SMOKE_PORT}:8787" \
    -e HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui \
    "$IMAGE_TAG" >/dev/null

  attempts=0
  # Probe from inside the container so the check does not depend on how the
  # host's Docker implementation publishes ports.
  until docker exec "$container" \
      curl --fail --silent --max-time 5 http://127.0.0.1:8787/health >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    [[ "$attempts" -ge 60 ]] && die "/health never answered (~5m)"
    if [[ "$(docker inspect -f '{{.State.Running}}' "$container")" != "true" ]]; then
      die "container exited before /health came up"
    fi
    sleep 5
  done
  say "/health answered after ${attempts} attempts"

  logs="$(docker logs "$container" 2>&1)"
  bad='EROFS|Read-only file system|Traceback|PermissionError|!! ERROR|!! Exiting script'
  if echo "$logs" | grep -E -i "$bad"; then
    die "startup logs contain a known-bad pattern (above)"
  fi

  # The security property the baked build exists for: the unprivileged runtime
  # user must not be able to rewrite its own dependencies or the agent's code,
  # and the container must not have installed anything at startup.
  if [[ "$BAKE_RUNTIME" == "1" ]]; then
    echo "$logs" | grep -q "Baked runtime detected" \
      || die "the container did not take the baked-runtime path"
    if echo "$logs" | grep -qE "uv pip install|Installing uv and creating"; then
      die "the container installed packages at startup — the runtime is not hermetic"
    fi
    say "startup installed nothing"

    if docker exec -u hermeswebui "$container" \
        sh -c 'test -w /opt/hermes-webui/venv' 2>/dev/null; then
      die "the baked venv is writable by hermeswebui — tamper resistance is lost"
    fi
    say "baked venv is not writable by the runtime user"

    if [[ "$AGENT_SOURCE" == "image" ]]; then
      if docker exec -u hermeswebui "$container" \
          sh -c 'test -w /opt/hermes-agent' 2>/dev/null; then
        die "the baked agent source is writable by hermeswebui"
      fi
      docker exec "$container" /opt/hermes-webui/venv/bin/python \
        -c 'from run_agent import AIAgent; print("agent import OK inside the container")' \
        || die "the agent baked from ${AGENT_IMAGE} does not import"
    fi
  fi

  trap - EXIT
  cleanup_smoke || true
  say "Boot smoke passed"
fi

# ── Containerised test run ──────────────────────────────────────────────────
if [[ "$DO_TEST" -eq 1 ]]; then
  # Extra pytest arguments come from PYTEST_ARGS, e.g. a CI-style slice:
  #   PYTEST_ARGS="--shard-id=0 --num-shards=8" ./scripts/docker-build.sh --test
  say "Running the test suite inside a container (dev deps stay in the container)"
  [[ -n "${PYTEST_ARGS:-}" ]] && say "  pytest args: ${PYTEST_ARGS}"
  docker run --rm \
    -v "${REPO_ROOT}:/src:ro" \
    -e "PYTEST_ARGS=${PYTEST_ARGS:-}" \
    -w /tmp/hermes-webui-tests \
    --entrypoint /bin/bash \
    "$IMAGE_TAG" -lc '
      set -euo pipefail
      cp -a /src/. /tmp/hermes-webui-tests/
      # The suite spawns a real server via `python3` from PATH (tests/conftest.py),
      # so the baked venv must be what PATH resolves to.
      export PATH=/opt/hermes-webui/venv/bin:$PATH
      python -m pip install --quiet -r requirements-dev.txt
      # shellcheck disable=SC2086  # PYTEST_ARGS is intentionally word-split
      HERMES_HOME=/tmp/hermes-test-home \
      HERMES_WEBUI_STATE_DIR=/tmp/hermes-test-state \
        python -m pytest -q -p no:cacheprovider ${PYTEST_ARGS:-}
    '
  say "Tests passed"
fi

# ── Bring the stack up ──────────────────────────────────────────────────────
if [[ "$DO_UP" -eq 1 ]]; then
  # Compose names the project after the directory. A second checkout with the
  # same basename (e.g. ~/dev/hermes-webui and ~/tmp/hermes-webui) therefore
  # shares the project name, and `up` from here would silently recreate the
  # other checkout's running containers. Refuse unless the operator picked a
  # distinct project name.
  # Ask compose for the effective project name: it already applies the `name:`
  # key in docker-compose.yml, COMPOSE_PROJECT_NAME, and a .env file, in the
  # right precedence. Fall back to compose's own default (the directory name).
  project="$(docker compose config --format json 2>/dev/null \
    | grep -m1 -o '"name": *"[^"]*"' | sed -E 's/.*"name": *"([^"]*)"/\1/' || true)"
  [[ -n "$project" ]] || project="$(basename "$REPO_ROOT" | tr '[:upper:]' '[:lower:]')"
  repo_cmp="$REPO_ROOT"
  command -v cygpath >/dev/null 2>&1 && repo_cmp="$(cygpath -w "$REPO_ROOT")"
  foreign_dir="$(docker ps -a --filter "label=com.docker.compose.project=${project}" \
      --format '{{.Label `com.docker.compose.project.working_dir`}}' 2>/dev/null \
    | sort -u | grep -v -x -F -e "$REPO_ROOT" -e "$repo_cmp" | head -n 1 || true)"
  if [[ -n "$foreign_dir" ]]; then
    die "compose project '${project}' already has containers from ${foreign_dir}." \
        "'docker compose up' from here would recreate them. Re-run with" \
        "COMPOSE_PROJECT_NAME=<distinct-name> to run this checkout side by side."
  fi

  say "Starting the stack (docker compose up -d, project ${project})"
  HERMES_AGENT_IMAGE="$AGENT_IMAGE" \
  AGENT_SOURCE="$AGENT_SOURCE" \
  AGENT_EXTRAS="$AGENT_EXTRAS" \
  BAKE_RUNTIME="$BAKE_RUNTIME" \
    docker compose up -d
  say "Open http://localhost:8787"
fi
