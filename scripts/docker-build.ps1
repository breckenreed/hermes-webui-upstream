<#
.SYNOPSIS
  Build Hermes WebUI entirely inside containers (Windows / PowerShell).

.DESCRIPTION
  Nothing this script does touches the host beyond the Docker socket: no venv,
  no pip, no agent installer, no Node. The image build runs in BuildKit, the
  Hermes Agent comes out of the pinned hermes-agent container image, and both
  dependency sets are resolved at build time into a root-owned venv the runtime
  user cannot write to. See the "Baked runtime" block in the Dockerfile.

  The POSIX equivalent is scripts/docker-build.sh.

.EXAMPLE
  .\scripts\docker-build.ps1
  Build the image.

.EXAMPLE
  .\scripts\docker-build.ps1 -Pin -Verify
  Pin the agent image to a digest, build, then boot-smoke it in a container.

.EXAMPLE
  .\scripts\docker-build.ps1 -Up
  Build, then bring the stack up on http://localhost:8787.
#>
[CmdletBinding()]
param(
    # Resolve the agent tag to an immutable digest before building.
    [switch]$Pin,
    # After building, boot the image in a throwaway container and check it.
    [switch]$Verify,
    # After building, run the test suite inside a container.
    [switch]$Test,
    # After building, `docker compose up -d`.
    [switch]$Up,
    [switch]$NoCache,
    # WebUI-only image; the agent is supplied at runtime by a mounted volume.
    [switch]$Lean,
    [string]$Tag,
    [string]$AgentImage,
    [int]$SmokePort = 8799
)

# Deliberately NOT 'Stop': Windows PowerShell 5.1 wraps every stderr line a
# native executable writes (docker/buildx stream their progress there) in an
# ErrorRecord, and under 'Stop' the first such line terminates the script even
# though the command is succeeding. Every native call below is gated on
# $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Say([string]$Message) { Write-Host "[docker-build] $Message" }
function Die([string]$Message) {
    Write-Host "[docker-build] ERROR: $Message" -ForegroundColor Red
    exit 1
}

# PowerShell 5.1 delivers a native command's stderr as ErrorRecords whose
# ToString() is the exception type name when the line was blank. Reduce every
# pipeline object to the text it carries.
function ConvertTo-PlainLine($Item) {
    if ($Item -is [System.Management.Automation.ErrorRecord]) {
        return [string]$Item.Exception.Message
    }
    return [string]$Item
}

# Run a native command whose progress goes to stderr, printing it as plain
# text rather than as PowerShell error records. Returns the exit code.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host (ConvertTo-PlainLine $_) }
    return $LASTEXITCODE
}

# Both streams of a container's log as one plain string.
function Get-ContainerLogs([string]$Name) {
    $lines = & docker logs $Name 2>&1 | ForEach-Object { ConvertTo-PlainLine $_ }
    return ($lines -join "`n")
}

# Read one KEY=value out of .env, which `docker compose build` reads on its own.
# Without this the script and compose silently produce DIFFERENT images whenever
# a build arg is set only there.
function Get-DotEnvValue([string]$Name) {
    $envPath = Join-Path $RepoRoot '.env'
    if (-not (Test-Path $envPath)) { return $null }
    foreach ($line in Get-Content $envPath) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=(.*)$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

# Precedence: CLI parameter > environment > .env > built-in default.
function Get-Default([string]$Value, [string]$EnvName, [string]$Fallback) {
    if (-not [string]::IsNullOrWhiteSpace($Value)) { return $Value }
    $fromEnv = [Environment]::GetEnvironmentVariable($EnvName)
    if (-not [string]::IsNullOrWhiteSpace($fromEnv)) { return $fromEnv }
    $fromDotEnv = Get-DotEnvValue $EnvName
    if (-not [string]::IsNullOrWhiteSpace($fromDotEnv)) { return $fromDotEnv }
    return $Fallback
}

# Matches the `image:` in docker-compose.yml so -Verify, -Test and -Up all act
# on the same artifact. Deliberately not the published ghcr.io name.
$ImageTag                = Get-Default $Tag 'HERMES_WEBUI_IMAGE' 'hermes-webui-doc:latest'
$ResolvedAgentImage      = Get-Default $AgentImage 'HERMES_AGENT_IMAGE' 'nousresearch/hermes-agent:latest'
$AgentSource             = Get-Default '' 'AGENT_SOURCE' 'image'
$AgentExtras             = Get-Default '' 'AGENT_EXTRAS' 'all'
$AgentPruneNodeModules   = Get-Default '' 'AGENT_PRUNE_NODE_MODULES' '1'
$BakeRuntime             = Get-Default '' 'BAKE_RUNTIME' '1'

if ($Lean) {
    # No agent baked in. Point the agent stage at a base image that is already
    # local so the 1.2 GB agent image is never fetched.
    $AgentSource = 'none'
    $ResolvedAgentImage = 'python:3.12-slim'
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Die 'docker is not on PATH'
}
& docker version --format '{{.Server.Version}}' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Die 'the Docker daemon is not reachable - start Docker Desktop and re-run' }

# ── Pin the agent image by digest ───────────────────────────────────────────
# A tag is a moving target. Resolving it to a digest once, up front, means the
# build records exactly which agent artifact went in - and a rebuild either
# gets that same artifact or fails loudly.
if ($Pin -and $AgentSource -eq 'image') {
    Say "Resolving $ResolvedAgentImage to a digest"
    $rc = Invoke-Native 'docker' @('pull', $ResolvedAgentImage)
    if ($rc -ne 0) { Die "could not pull $ResolvedAgentImage" }
    $digest = & docker image inspect $ResolvedAgentImage --format '{{ index .RepoDigests 0 }}' 2>$null
    if ([string]::IsNullOrWhiteSpace($digest)) { Die "could not resolve a digest for $ResolvedAgentImage" }
    $ResolvedAgentImage = $digest.Trim()
    Say "Pinned agent image: $ResolvedAgentImage"
}

$HermesVersion = 'unknown'
try {
    $described = git describe --tags --always 2>$null
    if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($described)) {
        $HermesVersion = $described.Trim()
    }
} catch { }

$buildArgs = @(
    '--build-arg', "HERMES_AGENT_IMAGE=$ResolvedAgentImage",
    '--build-arg', "AGENT_SOURCE=$AgentSource",
    '--build-arg', "AGENT_EXTRAS=$AgentExtras",
    '--build-arg', "AGENT_PRUNE_NODE_MODULES=$AgentPruneNodeModules",
    '--build-arg', "BAKE_RUNTIME=$BakeRuntime",
    '--build-arg', "HERMES_VERSION=$HermesVersion"
)
if ($NoCache) { $buildArgs += '--no-cache' }

Say "Building $ImageTag"
Say "  agent image  : $ResolvedAgentImage"
Say "  agent source : $AgentSource (extras: $AgentExtras)"
Say "  baked runtime: $BakeRuntime"
Say "  webui version: $HermesVersion"

& docker buildx version 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    $rc = Invoke-Native 'docker' (@('buildx', 'build', '--load', '-t', $ImageTag) + $buildArgs + @('.'))
} else {
    $rc = Invoke-Native 'docker' (@('build', '-t', $ImageTag) + $buildArgs + @('.'))
}
if ($rc -ne 0) { Die "image build failed (exit $rc)" }
Say "Built $ImageTag"

# ── Containerised boot smoke ────────────────────────────────────────────────
if ($Verify) {
    $container = "hermes-webui-smoke-$PID"

    $smokeFailed = $null
    try {
        # No mounts: state lives in the container's own layer, so the smoke tests
        # the image and nothing about the host's directory ownership. The state
        # dir is passed explicitly because docker_init.bash requires it.
        Say "Boot smoke on 127.0.0.1:$SmokePort with container-local state"
        $rc = Invoke-Native 'docker' @(
            'run', '-d', '--name', $container,
            '-p', "127.0.0.1:${SmokePort}:8787",
            '-e', 'HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui',
            $ImageTag
        )
        if ($rc -ne 0) { throw "docker run failed (exit $rc)" }

        $healthy = $false
        for ($attempt = 1; $attempt -le 60; $attempt++) {
            $running = & docker inspect -f '{{.State.Running}}' $container 2>$null
            if ($running -ne 'true') { throw 'container exited before /health came up' }
            try {
                $response = Invoke-WebRequest -Uri "http://127.0.0.1:$SmokePort/health" `
                    -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
                if ($response.StatusCode -eq 200) {
                    $healthy = $true
                    Say "/health answered after $attempt attempts"
                    break
                }
            } catch { }
            Start-Sleep -Seconds 5
        }
        if (-not $healthy) { throw '/health never answered (~5m)' }

        $logs = Get-ContainerLogs $container
        $bad = 'EROFS|Read-only file system|Traceback|PermissionError|!! ERROR|!! Exiting script'
        if ($logs -match $bad) {
            Write-Host ($logs -split "`n" | Select-String -Pattern $bad | Out-String)
            throw 'startup logs contain a known-bad pattern (above)'
        }

        # The security property the baked build exists for: the unprivileged
        # runtime user must not be able to rewrite its own dependencies or the
        # agent's code, and the container must not have installed anything at
        # startup.
        if ($BakeRuntime -eq '1') {
            if ($logs -notmatch 'Baked runtime detected') {
                throw 'the container did not take the baked-runtime path'
            }
            if ($logs -match 'uv pip install|Installing uv and creating') {
                throw 'the container installed packages at startup - the runtime is not hermetic'
            }
            Say 'startup installed nothing'

            & docker exec -u hermeswebui $container sh -c 'test -w /opt/hermes-webui/venv' 2>$null
            if ($LASTEXITCODE -eq 0) {
                throw 'the baked venv is writable by hermeswebui - tamper resistance is lost'
            }
            Say 'baked venv is not writable by the runtime user'

            if ($AgentSource -eq 'image') {
                & docker exec -u hermeswebui $container sh -c 'test -w /opt/hermes-agent' 2>$null
                if ($LASTEXITCODE -eq 0) {
                    throw 'the baked agent source is writable by hermeswebui'
                }
                # No double quotes inside the snippet: PowerShell 5.1 strips
                # embedded quotes when handing arguments to native executables.
                $probe = "from run_agent import AIAgent; print('agent import OK inside the container')"
                & docker exec $container /opt/hermes-webui/venv/bin/python -c $probe
                if ($LASTEXITCODE -ne 0) {
                    throw "the agent baked from $ResolvedAgentImage does not import"
                }
            }
        }
        Say 'Boot smoke passed'
    } catch {
        $smokeFailed = $_
        # Tail only: docker_init.bash's rsync -av lists every synced file, which
        # would bury the failure reason.
        $tail = (Get-ContainerLogs $container) -split "`n" | Select-Object -Last 60
        Write-Host ($tail -join "`n")
    } finally {
        & docker rm -f $container 2>$null | Out-Null
    }
    if ($smokeFailed) { Die $smokeFailed }
}

# ── Containerised test run ──────────────────────────────────────────────────
if ($Test) {
    # Extra pytest arguments come from PYTEST_ARGS, e.g. a CI-style slice:
    #   $env:PYTEST_ARGS = "--shard-id=0 --num-shards=8"; .\scripts\docker-build.ps1 -Test
    $pytestArgs = [Environment]::GetEnvironmentVariable('PYTEST_ARGS')
    if ($null -eq $pytestArgs) { $pytestArgs = '' }
    Say 'Running the test suite inside a container (dev deps stay in the container)'
    if (-not [string]::IsNullOrWhiteSpace($pytestArgs)) { Say "  pytest args: $pytestArgs" }
    $script = @'
set -euo pipefail
cp -a /src/. /tmp/hermes-webui-tests/
# The suite spawns a real server via `python3` from PATH (tests/conftest.py),
# so the baked venv must be what PATH resolves to.
export PATH=/opt/hermes-webui/venv/bin:$PATH
python -m pip install --quiet -r requirements-dev.txt
HERMES_HOME=/tmp/hermes-test-home \
HERMES_WEBUI_STATE_DIR=/tmp/hermes-test-state \
  python -m pytest -q -p no:cacheprovider ${PYTEST_ARGS:-}
'@
    $rc = Invoke-Native 'docker' @(
        'run', '--rm',
        '-v', "${RepoRoot}:/src:ro",
        '-e', "PYTEST_ARGS=$pytestArgs",
        '-w', '/tmp/hermes-webui-tests',
        '--entrypoint', '/bin/bash',
        $ImageTag, '-lc', $script
    )
    if ($rc -ne 0) { Die "tests failed (exit $rc)" }
    Say 'Tests passed'
}

# ── Bring the stack up ──────────────────────────────────────────────────────
if ($Up) {
    # Compose names the project after the directory. A second checkout with the
    # same basename (e.g. DEV\hermes-webui and DEV-CLAUDE\hermes-webui) therefore
    # shares the project name, and `up` from here would silently recreate the
    # other checkout's running containers. Refuse unless the operator picked a
    # distinct project name.
    # Ask compose for the effective project name: it already applies the
    # `name:` key in docker-compose.yml, COMPOSE_PROJECT_NAME, and a .env file,
    # in the right precedence. Fall back to compose's default (directory name).
    $project = ''
    try {
        $cfg = (& docker compose config --format json 2>$null | Out-String | ConvertFrom-Json)
        if ($null -ne $cfg -and -not [string]::IsNullOrWhiteSpace($cfg.name)) { $project = $cfg.name }
    } catch { }
    if ([string]::IsNullOrWhiteSpace($project)) { $project = (Split-Path -Leaf $RepoRoot).ToLower() }
    # Backtick raw string inside the Go template: no double quotes, which
    # PowerShell 5.1 would strip on the way to the native executable.
    $dirs = & docker ps -a --filter "label=com.docker.compose.project=$project" `
        --format '{{.Label `com.docker.compose.project.working_dir`}}' 2>$null
    $here = $RepoRoot.TrimEnd('\')
    $foreign = @($dirs | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_) -and ($_.TrimEnd('\') -ne $here)
    } | Select-Object -Unique)
    if ($foreign.Count -gt 0) {
        Die ("compose project '$project' already has containers from $($foreign[0]). " +
             "'docker compose up' from here would recreate them. Re-run with " +
             '$env:COMPOSE_PROJECT_NAME = "<distinct-name>" to run this checkout side by side.')
    }

    Say "Starting the stack (docker compose up -d, project $project)"
    $env:HERMES_AGENT_IMAGE = $ResolvedAgentImage
    $env:AGENT_SOURCE = $AgentSource
    $env:AGENT_EXTRAS = $AgentExtras
    $env:BAKE_RUNTIME = $BakeRuntime
    $rc = Invoke-Native 'docker' @('compose', 'up', '-d')
    if ($rc -ne 0) { Die "docker compose up failed (exit $rc)" }
    Say 'Open http://localhost:8787'
}
