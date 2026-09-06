"""
Tests for container-sourced agent provisioning and the hermetic image build.

Background
----------
``bootstrap.py`` used to answer a missing Hermes Agent by piping a remote
installer into a shell on the user's machine::

    curl -fsSL https://raw.githubusercontent.com/.../install.sh | bash

That is an unpinned, unauditable artifact executed with the invoking user's
privileges, and it leaves agent code on the host whether or not the operator
wanted anything installed there.

The behavior under test
-----------------------
The agent now comes out of the pinned ``hermes-agent`` **container image**:
the image is pulled, a container is created but never started, ``/opt/hermes``
is copied out with ``docker cp``, and the container is removed. The host
installer is reachable only through an explicit ``--agent-source host``.

The image build carries the same property: ``Dockerfile`` takes the agent from
``FROM ${HERMES_AGENT_IMAGE}`` and resolves every dependency at build time into
a root-owned venv, so a started container installs nothing.

Coverage
--------
1.  ``resolve_agent_source`` defaults to ``container`` and honours the env var
2.  ``--agent-source`` is a recognised flag with ``container`` as the default
3.  ``provision_agent`` does NOT reach the host installer by default
4.  ``provision_agent`` reaches it for the explicit ``host`` opt-in only
5.  ``main()`` provisions from the container image when no agent is found
6.  ``provision_agent_from_container`` runs pull/create/cp/rm and publishes the
    tree atomically
7.  ... removes the throwaway container even when extraction fails
8.  ... refuses an incomplete extraction (no ``run_agent.py``) rather than
    publishing a half-populated agent dir
9.  ... refuses to overwrite an existing non-empty target
10. ... errors with actionable guidance when no container runtime is usable
11. ``HERMES_AGENT_IMAGE`` selects the image; the default is the official tag
12. Dockerfile takes the agent from the agent image and bakes a root-owned venv
13. ``docker_init.bash`` skips its install path when the baked venv is present
14. ``docker-compose.yml`` threads the build args through
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        "HERMES_AGENT_IMAGE",
        "HERMES_WEBUI_AGENT_SOURCE",
        "HERMES_WEBUI_CONTAINER_CLI",
        "HERMES_WEBUI_AGENT_DIR",
        "HERMES_WEBUI_HOST",
        "HERMES_WEBUI_PORT",
        "HERMES_WEBUI_PYTHON",
        "HERMES_WEBUI_STATE_DIR",
        "HERMES_WEBUI_SERVER_CWD",
        "HERMES_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def bs():
    """Import bootstrap freshly each test to avoid module-level state bleed."""
    if "bootstrap" in sys.modules:
        del sys.modules["bootstrap"]
    import bootstrap

    return bootstrap


# ---------- source resolution ---------------------------------------------


class TestAgentSourceResolution:

    def test_default_is_the_container_image(self, bs, clean_env):
        assert bs.resolve_agent_source(None) == "container"

    def test_env_var_sets_the_source(self, bs, clean_env, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_AGENT_SOURCE", "host")
        assert bs.resolve_agent_source(None) == "host"

    def test_cli_value_wins_over_env(self, bs, clean_env, monkeypatch):
        monkeypatch.setenv("HERMES_WEBUI_AGENT_SOURCE", "host")
        assert bs.resolve_agent_source("container") == "container"

    def test_unknown_source_is_rejected(self, bs, clean_env):
        with pytest.raises(RuntimeError, match="Unknown agent source"):
            bs.resolve_agent_source("wget")

    def test_flag_is_recognised_and_defaults_to_none_sentinel(
        self, bs, clean_env, monkeypatch
    ):
        monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
        assert bs.parse_args().agent_source is None

        monkeypatch.setattr(
            sys, "argv", ["bootstrap.py", "--agent-source", "container"]
        )
        assert bs.parse_args().agent_source == "container"

    def test_flag_rejects_unknown_values(self, bs, clean_env, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--agent-source", "curl"])
        with pytest.raises(SystemExit):
            bs.parse_args()


# ---------- the host installer is not reachable by default ----------------


class TestHostInstallerIsOptIn:

    def test_default_provisioning_never_runs_the_host_installer(self, bs, clean_env):
        with patch.object(bs, "install_hermes_agent") as mock_install, patch.object(
            bs, "provision_agent_from_container"
        ) as mock_container:
            bs.provision_agent(bs.resolve_agent_source(None))

        mock_install.assert_not_called()
        mock_container.assert_called_once_with()

    def test_host_source_is_the_only_route_to_the_installer(self, bs, clean_env):
        with patch.object(bs, "install_hermes_agent") as mock_install, patch.object(
            bs, "provision_agent_from_container"
        ) as mock_container:
            bs.provision_agent("host")

        mock_install.assert_called_once_with()
        mock_container.assert_not_called()

    def test_none_source_fails_instead_of_provisioning(self, bs, clean_env):
        with patch.object(bs, "install_hermes_agent") as mock_install, patch.object(
            bs, "provision_agent_from_container"
        ) as mock_container, pytest.raises(RuntimeError, match="provisioning was disabled"):
            bs.provision_agent("none")

        mock_install.assert_not_called()
        mock_container.assert_not_called()

    def test_main_provisions_from_the_container_when_no_agent_is_found(
        self, bs, clean_env, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
        monkeypatch.setattr(bs, "ensure_supported_platform", lambda: None)
        monkeypatch.setattr(bs, "discover_agent_dir", lambda: None)
        monkeypatch.setattr(bs, "hermes_command_exists", lambda: False)

        # Stop main() right after provisioning: everything past this point is
        # the launcher path, which other tests already cover.
        def boom(*_args, **_kwargs):
            raise SystemExit(7)

        monkeypatch.setattr(bs, "discover_launcher_python", boom)

        with patch.object(bs, "install_hermes_agent") as mock_install, patch.object(
            bs, "provision_agent_from_container"
        ) as mock_container, pytest.raises(SystemExit):
            bs.main()

        mock_container.assert_called_once_with()
        mock_install.assert_not_called()


# ---------- extraction from the agent image -------------------------------


class _FakeDocker:
    """Records the CLI calls and fakes `docker cp` by writing the agent tree."""

    def __init__(self, *, populate=True, cp_fails=False):
        self.calls: list[list[str]] = []
        self.populate = populate
        self.cp_fails = cp_fails

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        verb = argv[1] if len(argv) > 1 else ""

        if verb == "image":  # `docker image inspect` — pretend it is absent
            return subprocess.CompletedProcess(argv, 1, "", "no such image")
        if verb == "create":
            return subprocess.CompletedProcess(argv, 0, "container-abc123\n", "")
        if verb == "cp":
            if self.cp_fails:
                raise subprocess.CalledProcessError(1, argv)
            dest = Path(argv[-1])
            dest.mkdir(parents=True, exist_ok=True)
            if self.populate:
                (dest / "run_agent.py").write_text("# agent\n", encoding="utf-8")
                (dest / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls if len(c) > 1]


class TestContainerExtraction:

    def test_pull_create_cp_rm_and_publishes_the_tree(
        self, bs, clean_env, monkeypatch, tmp_path
    ):
        target = tmp_path / "hermes-home" / "hermes-agent"
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        monkeypatch.setattr(bs, "container_cli", lambda: "/usr/bin/docker")
        fake = _FakeDocker()
        monkeypatch.setattr(bs.subprocess, "run", fake)

        assert bs.provision_agent_from_container() == target

        assert fake.verbs() == ["image", "pull", "create", "cp", "rm"]
        assert (target / "run_agent.py").exists()

        # The container is created, never started, and always removed.
        assert not any(c[1] == "start" for c in fake.calls if len(c) > 1)
        rm_call = next(c for c in fake.calls if len(c) > 1 and c[1] == "rm")
        assert rm_call[-1] == "container-abc123"

        # Nothing is left staged next to the published tree.
        assert [p.name for p in target.parent.iterdir()] == ["hermes-agent"]

    def test_local_image_is_not_re_pulled(self, bs, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        monkeypatch.setattr(bs, "container_cli", lambda: "/usr/bin/docker")

        fake = _FakeDocker()

        def with_local_image(argv, **kwargs):
            if len(argv) > 1 and argv[1] == "image":
                fake.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, "[{}]", "")
            return fake(argv, **kwargs)

        monkeypatch.setattr(bs.subprocess, "run", with_local_image)
        bs.provision_agent_from_container()

        assert "pull" not in fake.verbs()

    def test_failed_extraction_still_removes_the_container(
        self, bs, clean_env, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        monkeypatch.setattr(bs, "container_cli", lambda: "/usr/bin/docker")
        fake = _FakeDocker(cp_fails=True)
        monkeypatch.setattr(bs.subprocess, "run", fake)

        with pytest.raises(subprocess.CalledProcessError):
            bs.provision_agent_from_container()

        assert "rm" in fake.verbs()

    def test_incomplete_extraction_is_not_published(
        self, bs, clean_env, monkeypatch, tmp_path
    ):
        """An image without an agent tree must not leave a half-populated dir.

        ``discover_agent_dir`` accepts any directory containing
        ``run_agent.py``; publishing an incomplete extraction would make every
        later run pick up a broken agent instead of re-provisioning.
        """
        target = tmp_path / "hermes-home" / "hermes-agent"
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        monkeypatch.setattr(bs, "container_cli", lambda: "/usr/bin/docker")
        monkeypatch.setattr(bs.subprocess, "run", _FakeDocker(populate=False))

        with pytest.raises(RuntimeError, match="does not carry an agent source tree"):
            bs.provision_agent_from_container()

        assert not target.exists()
        assert list(target.parent.iterdir()) == []

    def test_existing_target_is_refused_not_overwritten(
        self, bs, clean_env, monkeypatch, tmp_path
    ):
        target = tmp_path / "hermes-home" / "hermes-agent"
        target.mkdir(parents=True)
        (target / "keep-me.txt").write_text("precious", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        monkeypatch.setattr(bs, "container_cli", lambda: "/usr/bin/docker")
        fake = _FakeDocker()
        monkeypatch.setattr(bs.subprocess, "run", fake)

        with pytest.raises(RuntimeError, match="Refusing to overwrite"):
            bs.provision_agent_from_container()

        assert (target / "keep-me.txt").read_text(encoding="utf-8") == "precious"
        assert fake.calls == []

    def test_missing_runtime_names_the_container_path_and_the_opt_in(
        self, bs, clean_env, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        monkeypatch.setattr(bs, "container_cli", lambda: None)

        with pytest.raises(RuntimeError) as excinfo:
            bs.provision_agent_from_container()

        message = str(excinfo.value)
        assert "docker-build" in message
        assert "--agent-source host" in message

    def test_image_defaults_to_the_official_tag_and_is_overridable(
        self, bs, clean_env, monkeypatch
    ):
        assert bs.agent_image() == "nousresearch/hermes-agent:latest"
        assert bs.AGENT_IMAGE_DEFAULT == "nousresearch/hermes-agent:latest"

        monkeypatch.setenv("HERMES_AGENT_IMAGE", "nousresearch/hermes-agent@sha256:beef")
        assert bs.agent_image() == "nousresearch/hermes-agent@sha256:beef"

    def test_the_configured_image_is_the_one_pulled(
        self, bs, clean_env, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        monkeypatch.setenv("HERMES_AGENT_IMAGE", "example.invalid/agent@sha256:cafe")
        monkeypatch.setattr(bs, "container_cli", lambda: "/usr/bin/docker")
        fake = _FakeDocker()
        monkeypatch.setattr(bs.subprocess, "run", fake)

        bs.provision_agent_from_container()

        pull = next(c for c in fake.calls if len(c) > 1 and c[1] == "pull")
        assert pull[-1] == "example.invalid/agent@sha256:cafe"

    def test_agent_dir_env_var_selects_the_extraction_target(
        self, bs, clean_env, monkeypatch, tmp_path
    ):
        target = tmp_path / "elsewhere" / "agent"
        monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", str(target))
        monkeypatch.setattr(bs, "container_cli", lambda: "/usr/bin/docker")
        monkeypatch.setattr(bs.subprocess, "run", _FakeDocker())

        assert bs.provision_agent_from_container() == target
        assert (target / "run_agent.py").exists()


# ---------- the image build carries the same guarantee --------------------


class TestHermeticImageBuild:
    """Source-level invariants for the build.

    These are deliberately structural: the runtime behaviour they stand for is
    proved by actually booting the image (``scripts/docker-build.sh --verify``
    and the docker-smoke workflow), which cannot run in a unit test.
    """

    def test_dockerfile_takes_the_agent_from_the_agent_image(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

        assert "ARG HERMES_AGENT_IMAGE=nousresearch/hermes-agent:latest" in dockerfile
        assert "FROM ${HERMES_AGENT_IMAGE} AS agent-image" in dockerfile
        assert "COPY --from=agent-src /opt/hermes-agent /opt/hermes-agent" in dockerfile
        # The agent's host installer must never be *executed* by the build.
        # Comments may mention it (they explain that it is not used), and uv's
        # own installer is a different, legitimate script — so look only at
        # executed lines and only for the agent's installer.
        executed = [
            line for line in dockerfile.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert not any(
            "hermes-agent" in line and "install.sh" in line for line in executed
        ), "the build must not run the agent's host installer"

    def test_dockerfile_bakes_a_root_owned_runtime(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

        assert "ENV HERMES_WEBUI_BAKED_VENV=/opt/hermes-webui/venv" in dockerfile
        assert "uv pip install --python \"$_py\" -r /apptoo/requirements.txt" in dockerfile
        assert 'uv pip install --python "$_py" -e "/opt/hermes-agent[${AGENT_EXTRAS}]"' in dockerfile
        # Non-writable by the runtime user is the property the build exists for.
        assert "chmod -R a+rX,go-w /opt/hermes-webui" in dockerfile
        # And the build must fail rather than deferring failure to first chat.
        assert "from run_agent import AIAgent" in dockerfile

    def test_docker_init_skips_installing_when_the_runtime_is_baked(self):
        init = (REPO_ROOT / "docker_init.bash").read_text(encoding="utf-8")

        assert 'HERMES_WEBUI_BAKED_VENV:-/opt/hermes-webui/venv' in init
        assert 'HERMES_WEBUI_DISABLE_BAKED_VENV' in init
        # The baked branch must run the server itself, never falling through
        # into the uv/pip install path below it.
        baked = init.index("Baked runtime detected")
        installs = init.index("== Installing uv and creating a new virtual environment")
        assert baked < installs
        assert init.index("Running hermes-webui (baked runtime)") < installs

    def test_docker_init_skips_the_ownership_walk_on_host_shares(self):
        """A bind-mounted ~/.hermes on Docker Desktop (Windows) is a v9fs share.

        chown cannot change ownership there, yet the recursive walk over a real
        agent home (node_modules, .venv, npm caches) stalled startup for many
        minutes. The walk must be skipped for share filesystems and on the
        explicit HERMES_SKIP_HOME_CHOWN=1 knob, while the parts of the home that
        live in the container's own layer are still aligned.
        """
        init = (REPO_ROOT / "docker_init.bash").read_text(encoding="utf-8")
        start = init.index("chown_home_hermeswebui()")
        body = init[start:init.index("\n}\n", start)]

        assert 'stat -f -c %T "$_home_mount"' in body
        for fstype in ("v9fs", "9p", "cifs"):
            assert fstype in body, f"{fstype} must be recognised as a host share"
        assert "HERMES_SKIP_HOME_CHOWN" in body
        assert "Skipping ownership walk" in body
        # The skip path still aligns the container-layer part of the home, and
        # the full walk (with its hermes-agent / .git prunes) stays as fallback.
        assert '-path "$_home_mount" -prune' in body
        assert '-path "/home/hermeswebui/.hermes/hermes-agent" -prune' in body

    def test_compose_threads_the_build_args_through(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        assert "HERMES_AGENT_IMAGE: ${HERMES_AGENT_IMAGE:-nousresearch/hermes-agent:latest}" in compose
        assert "AGENT_SOURCE: ${AGENT_SOURCE:-image}" in compose
        assert "BAKE_RUNTIME: ${BAKE_RUNTIME:-1}" in compose

    def test_container_build_entrypoints_exist_for_both_platforms(self):
        assert (REPO_ROOT / "scripts" / "docker-build.sh").is_file()
        assert (REPO_ROOT / "scripts" / "docker-build.ps1").is_file()
