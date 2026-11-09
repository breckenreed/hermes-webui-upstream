# Update runbook: Hermes Agent + WebUI (container build, Windows)

For a Claude Code session. The hard merge is already done on branch
`deploy/containers-and-mobile`, so this job is one fast-forward and two
rebuilds: about 10 short tool calls. Sonnet or Haiku at low effort is enough.

## Rules (these keep it cheap)

- Do the steps below in order. No plan mode, subagents, web search, or side fixes.
- Do not merge `upstream/master` or chase a newer WebUI than this branch.
- Do not run the test suite: about 25k tests, 55 minutes, and ~20 known
  environment-only failures. The branch was tested before it was pushed.
- Never read or print whole: `api/gateway_chat.py`, `static/ui.js`,
  `static/i18n.js`, `CHANGELOG.md`, `docs/docker.md`, build logs, or unfiltered
  `docker logs`. Use `grep -n` and `tail -15`. Never print any `.env`.
- Long commands write to a log and run in the background. Wait for the
  completion notice; do not poll.
- If a step fails: `tail -15` its log and apply the matching trap below once.
  If it fails again, stop and report the step, those 15 lines, and what you
  tried. Do not explore.

Commands are for the Bash tool (Git Bash on Windows). Shell state does not
persist between calls, so write the real paths into each command.

## Steps

**1. Find the deployment.** If `docker info` fails, ask the user to start Docker Desktop.

```bash
docker compose ls -a
docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
```

`REPO` is the folder of the project running `hermes-webui-doc`. `AGENT_DIR` is
the folder of the project running `hermes-agent`, if there is one. If `REPO`
can't be told from this, ask the user for that one path.

**2. Take rollback points.** Use `C=hermes-agent H=/opt/data` when that container
is running, otherwise `C=hermes-webui-doc H=/home/hermeswebui/.hermes`.

```bash
docker tag hermes-webui-doc:latest hermes-webui-doc:rollback
MSYS_NO_PATHCONV=1 docker exec C python3 -c "import sqlite3;d=sqlite3.connect('H/state.db.pre-update');sqlite3.connect('H/state.db').backup(d);print('state backed up')"
[ -f REPO/docker-compose.override.yml ] && cp REPO/docker-compose.override.yml REPO/../docker-compose.override.yml.bak
```

**3. Check that a fast-forward is safe.**

```bash
cd REPO && git status --porcelain | head -10
cd REPO && git fetch https://github.com/breckenreed/hermes-webui-upstream.git deploy/containers-and-mobile && git merge-base --is-ancestor HEAD FETCH_HEAD && echo FF_OK || { echo DIVERGED; git log --oneline FETCH_HEAD..HEAD | head -20; }
```

- `DIVERGED`, or local changes other than the override file: stop and ask the user.
- Only `docker-compose.override.yml` changed: `git restore` it. Step 2 saved a copy.

**4. Fast-forward and restore the machine-local override.**

```bash
cd REPO && git merge --ff-only FETCH_HEAD && git log -1 --format='%h %s'
cd REPO && { [ -f docker-compose.override.yml ] || [ ! -f ../docker-compose.override.yml.bak ] || cp ../docker-compose.override.yml.bak docker-compose.override.yml; } && docker compose config -q && echo COMPOSE_OK
```

**5. Update the agent container.** Skip if there is no `AGENT_DIR`. Run in the background.

```bash
cd AGENT_DIR && { docker compose pull --ignore-buildable; docker compose build --pull; MSYS_NO_PATHCONV=1 docker exec hermes-agent rm -f /opt/data/gateway.sock; docker compose up -d; } > ../hermes-agent-update.log 2>&1; echo "exit=$?"
```

**6. Rebuild the WebUI with its bundled agent.** 5-15 minutes, in the background.
`-Pin` resolves the newest agent image; `-Verify` boot-smokes it on port 8799
before `-Up` replaces the running container.

```bash
cd REPO && powershell -ExecutionPolicy Bypass -File scripts/docker-build.ps1 -Pin -Verify -Up > ../hermes-webui-build.log 2>&1; echo "exit=$?"; grep -E 'Pinned|Boot smoke passed|Open http' ../hermes-webui-build.log
```

**7. Verify once.**

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
MSYS_NO_PATHCONV=1 docker exec hermes-webui-doc /opt/hermes-webui/venv/bin/hermes --version 2>&1 | sed -n 1p
docker exec hermes-agent hermes --version 2>&1 | sed -n 1p
```

Expected: the WebUI turns `healthy` within about a minute (check once more if it
says `starting`), and both versions read `Hermes Agent v0.23.0` or newer. Skip
the last command if there is no `hermes-agent` container.

**8. Report in at most 5 lines:** agent old → new, WebUI commit, container
health, anything skipped. Ask the user to send one test chat, because the
WebUI login is theirs. Then stop.

## Traps

| Symptom | Fix |
|---|---|
| Fast-forward removed `docker-compose.override.yml` | Normal when coming from an older commit; step 4 restores it. |
| `set HERMES_VAULT_DIR in .env` | The override needs that key in `REPO/.env`. Ask the user for the path. |
| Agent version did not change | `HERMES_AGENT_IMAGE` in `.env` is pinned. Rerun step 6 with `-AgentImage nousresearch/hermes-agent:latest`. |
| Agent log says `unsafe_control_path ... gateway.sock`, health stays down | Leftover socket from the older version. Remove it as in step 5 and wait 2 minutes for the respawn backoff. |
| `pull access denied for hermes-webui-doc` | The build failed. Read the log tail. |
| Port 8799 in use during `-Verify` | Add `-SmokePort 8798`. |
| Chat fails on a missing `${VAR}` from `config.yaml` | The variable lost its passthrough. Add it under `environment:` in the override, then `docker compose up -d`. |
| Page looks broken after the update | Hard reload (`Ctrl+F5`). |
| First agent start logs a config migration | Normal; Hermes writes its own backup first. |

Rollback: `docker tag hermes-webui-doc:rollback hermes-webui-doc:latest`, then
`docker compose up -d` in `REPO`. `state.db.pre-update` is the matching database.
