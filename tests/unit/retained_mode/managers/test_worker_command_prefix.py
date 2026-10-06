"""Starting a library worker through worker.command_prefix.

The prefix here runs a probe script instead of any environment tool. The probe records the words it
was given and the environment it was started with, then exits without starting the engine, which is
all a real tool's command line and environment have to be checked against. The unit-test conftest
clears the GTN_* variables these hooks read, so every value comes from the test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.managers.external_environment import LIBRARY_WORKER_REQUESTS_ENV_VAR
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARY_PROVISIONED_BY_KEY,
    WORKER_COMMAND_PREFIX_KEY,
)
from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from pathlib import Path

_SESSION = "sess-abc"
_ORCHESTRATOR_ID = "orchestrator-id"
_LIBRARY = "Foo Library"
# What an environment tool leaves behind when it prepares the orchestrator's environment. The name
# is one real tool's; nothing in the engine knows it.
_RESOLVE_VARIABLE = "REZ_USED_RESOLVE"

_PROBE_SOURCE = """
import json
import os
import sys

output_path = sys.argv[1]
with open(output_path, "w", encoding="utf-8") as output:
    json.dump({"argv": sys.argv[2:], "env": dict(os.environ)}, output)
"""

# The variables spawn_worker sets by name on top of the baseline. The static server URL is absent
# because the test has no static server.
_ENGINE_SET_VARIABLES = {"GTN_ENGINE_ID", "GTN_ORCHESTRATOR_ENGINE_ID", "PYTHONUNBUFFERED"}


def _worker_manager(config_values: dict[str, Any], baseline: dict[str, str]) -> WorkerManager:
    engine = MagicMock()
    engine.get_session_id.return_value = _SESSION

    def get_config_value(key: str, default: Any = None, cast_type: Any = None) -> Any:
        # The engine reads library.provisioned_by through its section (read_provisioned_by).
        if key == "library" and LIBRARY_PROVISIONED_BY_KEY in config_values:
            return {"provisioned_by": config_values[LIBRARY_PROVISIONED_BY_KEY]}
        return config_values.get(key, default if cast_type is None else cast_type(default))

    engine.config_manager.get_config_value.side_effect = get_config_value
    engine.project_manager.get_pre_project_environ.return_value = baseline
    engine.engine_identity_manager.active_engine_id = _ORCHESTRATOR_ID
    engine.library_manager.environment.execution_site_packages.return_value = None
    manager = WorkerManager(engine=engine, event_manager=MagicMock())
    manager._orchestrator_static_server_base_url = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return manager


async def _spawn(manager: WorkerManager) -> MagicMock:
    """Run the spawn path with process creation captured instead of performed."""
    with patch("asyncio.create_subprocess_exec", return_value=MagicMock()) as create:
        await manager._spawn_when_session_ready(_LIBRARY)
    return create


def _run_captured(create: MagicMock) -> dict[str, Any]:
    """Start what the spawn would have started, and return what the probe recorded."""
    args = list(create.call_args.args)
    env = create.call_args.kwargs["env"]
    subprocess.run(args, env=env, check=True, timeout=60)  # noqa: S603 (the test's own probe)
    output_path = args[2]
    with open(output_path, encoding="utf-8") as output:  # noqa: PTH123
        return json.load(output)


@pytest.fixture
def probe(tmp_path: Path) -> Path:
    """The probe script the prefix runs in place of an environment tool."""
    script = tmp_path / "probe.py"
    script.write_text(_PROBE_SOURCE, encoding="utf-8")
    return script


def _probe_prefix(probe: Path, tmp_path: Path) -> list[str]:
    return [
        sys.executable,
        str(probe),
        str(tmp_path / "probe_output.json"),
        "env",
        "engine=={engine_version}",
        "{library_request}",
        "--name={library_name}",
        "--",
    ]


class TestPrefixedWorkerCommand:
    @pytest.mark.asyncio
    async def test_the_prefix_runs_in_front_of_the_worker_command_with_placeholders_filled(
        self, probe: Path, tmp_path: Path
    ) -> None:
        manager = _worker_manager(
            {WORKER_COMMAND_PREFIX_KEY: _probe_prefix(probe, tmp_path), LIBRARY_PROVISIONED_BY_KEY: "environment"},
            baseline={
                "PATH": "/usr/bin:/bin",
                LIBRARY_WORKER_REQUESTS_ENV_VAR: f"{_LIBRARY}=lib_foo==1.4.2 .gpu_build-cu128",
            },
        )

        create = await _spawn(manager)
        recorded = _run_captured(create)

        assert recorded["argv"] == [
            "env",
            f"engine=={engine_version}",
            "lib_foo==1.4.2",
            ".gpu_build-cu128",
            f"--name={_LIBRARY}",
            "--",
            sys.executable,
            "-m",
            "griptape_nodes_app",
            "engine",
            "--session-id",
            _SESSION,
            "--library-name",
            _LIBRARY,
        ]

    @pytest.mark.asyncio
    async def test_environment_mode_never_starts_a_worker_with_no_request(self, probe: Path, tmp_path: Path) -> None:
        manager = _worker_manager(
            {WORKER_COMMAND_PREFIX_KEY: _probe_prefix(probe, tmp_path), LIBRARY_PROVISIONED_BY_KEY: "environment"},
            baseline={LIBRARY_WORKER_REQUESTS_ENV_VAR: "Other Library=lib_other==1.0.0"},
        )
        manager.expect_worker(_LIBRARY)

        create = await _spawn(manager)

        create.assert_not_called()
        reason = manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert LIBRARY_WORKER_REQUESTS_ENV_VAR in reason
        # Anything waiting to run the library is released with that reason rather than left hanging.
        assert manager.has_settled(_LIBRARY)

    @pytest.mark.asyncio
    async def test_a_project_template_cannot_change_a_worker_request(
        self, probe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A project template writes os.environ; the worker request comes from the startup environment.
        monkeypatch.setenv(LIBRARY_WORKER_REQUESTS_ENV_VAR, f"{_LIBRARY}=lib_foo==9.9.9")
        manager = _worker_manager(
            {WORKER_COMMAND_PREFIX_KEY: _probe_prefix(probe, tmp_path), LIBRARY_PROVISIONED_BY_KEY: "environment"},
            baseline={LIBRARY_WORKER_REQUESTS_ENV_VAR: f"{_LIBRARY}=lib_foo==1.4.2"},
        )

        recorded = _run_captured(await _spawn(manager))

        assert "lib_foo==1.4.2" in recorded["argv"]
        assert "lib_foo==9.9.9" not in recorded["argv"]

    @pytest.mark.asyncio
    async def test_engine_mode_starts_a_worker_with_no_request_unprefixed(self, probe: Path, tmp_path: Path) -> None:
        manager = _worker_manager({WORKER_COMMAND_PREFIX_KEY: _probe_prefix(probe, tmp_path)}, baseline={})

        create = await _spawn(manager)

        assert list(create.call_args.args)[:2] == [sys.executable, "-m"]

    @pytest.mark.asyncio
    async def test_no_prefix_leaves_the_worker_command_unchanged(self) -> None:
        manager = _worker_manager({}, baseline={})

        create = await _spawn(manager)

        assert list(create.call_args.args) == [
            sys.executable,
            "-m",
            "griptape_nodes_app",
            "engine",
            "--session-id",
            _SESSION,
            "--library-name",
            _LIBRARY,
        ]


class TestWorkerEnvironment:
    """The worker's environment is the baseline plus variables the engine sets by name, nothing else."""

    @pytest.mark.asyncio
    async def test_only_named_variables_are_added_to_the_baseline(
        self, probe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        baseline = {
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": "/launcher/python",
            _RESOLVE_VARIABLE: "launch-1 engine-2",
            LIBRARY_WORKER_REQUESTS_ENV_VAR: f"{_LIBRARY}=lib_foo==1.4.2",
        }
        # Set after startup, as an environment tool or a project would, so it is in the live
        # environment but not in the baseline. Copying a family of variables by name pattern is
        # how parent resolve state used to leak into a worker.
        monkeypatch.setenv(_RESOLVE_VARIABLE, "live-value")
        monkeypatch.setenv("ENV_TOOL_CONTEXT_FILE", str(tmp_path / "live-context"))
        manager = _worker_manager(
            {WORKER_COMMAND_PREFIX_KEY: _probe_prefix(probe, tmp_path), LIBRARY_PROVISIONED_BY_KEY: "environment"},
            baseline=baseline,
        )

        create = await _spawn(manager)
        env = create.call_args.kwargs["env"]

        assert set(env) - set(baseline) == _ENGINE_SET_VARIABLES
        assert {name: env[name] for name in baseline} == baseline

        recorded = _run_captured(create)
        assert recorded["env"][_RESOLVE_VARIABLE] == "launch-1 engine-2"
        assert "ENV_TOOL_CONTEXT_FILE" not in recorded["env"]
        # With no virtual environment to put first, the launcher's PYTHONPATH reaches the prefix as-is.
        assert recorded["env"]["PYTHONPATH"] == "/launcher/python"
