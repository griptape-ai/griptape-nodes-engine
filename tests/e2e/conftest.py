"""Shared configuration and helpers for the end-to-end suite."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import pytest

import griptape_nodes.retained_mode.managers.config_manager as config_manager_module
import griptape_nodes.retained_mode.managers.secrets_manager as secrets_manager_module
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.engine import current_engine, reset_root_engine
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine


@pytest.fixture(autouse=True)
def _isolated_engine_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Boot each test against empty temp config/secrets and a clean registry.

    ``SecretsManager`` installs ``ENV_VAR_PATH``'s contents into ``os.environ`` at init;
    without this isolation a subprocess spawned via ``os.environ.copy()`` inherits the
    developer's real ``GT_CLOUD_BUCKET_ID`` and runs in Griptape Cloud mode, whose
    background threads segfault the child at interpreter shutdown.

    Dropping the root engine forces the managers to re-initialize against the patched paths
    and gives each test a fresh object and workflow registry. ``LibraryRegistry`` keeps its
    state in ``ClassVar`` dicts that the engine does not own, so it is reset explicitly.
    """
    reset_root_engine()
    LibraryRegistry._clear()

    for key in list(os.environ):
        if key.startswith(("GT_CLOUD_", "GTN_CONFIG_")):
            monkeypatch.delenv(key, raising=False)
    try:
        temp_workspace_path = tmp_path / "workspace"
        temp_config_path = tmp_path / "griptape_nodes_config.json"
        temp_config_path.write_text(json.dumps({"workspace_directory": str(temp_workspace_path)}, indent=2))
        temp_env_path = tmp_path / ".env"
        temp_env_path.write_text("")
        monkeypatch.setattr(config_manager_module, "USER_CONFIG_PATH", temp_config_path)
        monkeypatch.setattr(secrets_manager_module, "ENV_VAR_PATH", temp_env_path)
        yield
    finally:
        reset_root_engine()
        LibraryRegistry._clear()


@pytest.fixture
def engine() -> Engine:
    """Provide the engine for this test, building it on first use."""
    return current_engine()


@pytest.fixture
def engine_subprocess_env() -> Callable[..., dict[str, str]]:
    """Return a factory that builds the env for an engine workflow subprocess.

    With ``_isolated_engine_env`` active ``os.environ`` carries no real secrets, so a plain
    copy is already hermetic. The factory just supplies the ``GT_CLOUD_API_KEY`` placeholder
    the bootstrap requires and applies any overrides (e.g. ``XDG_CONFIG_HOME``).
    """

    def _build(**overrides: str) -> dict[str, str]:
        env = os.environ.copy()
        # Engine bootstrap requires GT_CLOUD_API_KEY to be set; the value never leaves the
        # subprocess so a placeholder is fine.
        env.setdefault("GT_CLOUD_API_KEY", "fake-test-key-for-bootstrap")
        env.update(overrides)
        return env

    return _build


@pytest.fixture
def materialize_library() -> Callable[..., Path]:
    """Return a factory that copies a fixture library into a temp dir for registration.

    Rewrites the fixture JSON's ``engine_version`` to the running engine version so
    ``IncompatibleEngineVersionCheck`` never marks the library UNUSABLE on a version bump,
    optionally overrides the library ``name`` (needed when two tests register the same
    fixture under distinct names), copies the node file plus any ``extra_files`` the library
    references (workflow files, for instance), and returns the written
    ``griptape_nodes_library.json`` path.
    """

    def _materialize(
        target_dir: Path,
        *,
        template: Path,
        node_file: Path,
        name: str | None = None,
        extra_files: Sequence[Path] | None = None,
    ) -> Path:
        target_dir.mkdir(parents=True, exist_ok=True)
        schema = json.loads(template.read_text())
        if name is not None:
            schema["name"] = name
        schema["metadata"]["engine_version"] = engine_version
        library_json = target_dir / "griptape_nodes_library.json"
        library_json.write_text(json.dumps(schema, indent=2))
        (target_dir / node_file.name).write_text(node_file.read_text())
        for extra_file in extra_files or ():
            (target_dir / extra_file.name).write_text(extra_file.read_text())
        return library_json

    return _materialize


@pytest.fixture
def write_isolated_config() -> Callable[..., None]:
    """Return a factory that writes an XDG-style engine config registering the fixture library.

    Used by subprocess tests to point the child engine at an isolated workspace and have it
    auto-register the materialized library on initialization.
    """

    def _write(config_root: Path, *, workspace: Path, library_path: Path) -> None:
        config_dir = config_root / "griptape_nodes"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "griptape_nodes_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "workspace_directory": str(workspace),
                    "log_level": "WARNING",
                    "app_events": {
                        "on_app_initialization_complete": {
                            "libraries_to_register": [str(library_path)],
                        },
                    },
                }
            )
        )

    return _write


@pytest.fixture
def create_node(engine: Engine) -> Callable[..., str]:
    """Return a helper that creates a node from the given library and asserts success."""

    def _create(
        node_type: str, node_name: str, flow_name: str, *, library_name: str, metadata: dict | None = None
    ) -> str:
        result = engine.handle_request(
            CreateNodeRequest(
                node_type=node_type,
                specific_library_name=library_name,
                node_name=node_name,
                override_parent_flow_name=flow_name,
                metadata=metadata,
            )
        )
        assert isinstance(result, CreateNodeResultSuccess), result
        return result.node_name

    return _create


@pytest.fixture
def connect(engine: Engine) -> Callable[..., None]:
    """Return a helper that connects two node parameters and asserts success."""

    def _connect(source_node: str, source_param: str, target_node: str, target_param: str) -> None:
        result = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=source_node,
                source_parameter_name=source_param,
                target_node_name=target_node,
                target_parameter_name=target_param,
            )
        )
        assert isinstance(result, CreateConnectionResultSuccess), result

    return _connect


@pytest.fixture
def record_published(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[Any]]:
    """Return a helper that taps the event bus and records every published payload of a type.

    What the editor shows comes off this stream, so a claim about what the artist saw is a claim
    about published events, not about the engine's internal state. Each call returns its own list,
    filled as matching payloads are published; `extract` picks what to keep from each one.

    Both spellings are tapped, because which one an event takes is an implementation detail of the
    code that emits it: the scheduler's error path is async and uses `aput_event`, the flow
    manager's cleanup is not and uses `put_event`. A tap on one of them silently records half the
    stream.
    """
    event_manager = engine.event_manager
    taps: list[Callable[[object], None]] = []
    real_put_event = event_manager.put_event
    real_aput_event = event_manager.aput_event

    def _note(event: object) -> None:
        for tap in taps:
            tap(event)

    def _collect(event: object) -> None:
        _note(event)
        real_put_event(event)

    async def _acollect(event: object) -> None:
        _note(event)
        await real_aput_event(event)

    monkeypatch.setattr(event_manager, "put_event", _collect)
    monkeypatch.setattr(event_manager, "aput_event", _acollect)

    def _record(payload_type: type, extract: Callable[[Any], Any] = lambda payload: payload) -> list[Any]:
        recorded: list[Any] = []

        def _tap(event: object) -> None:
            payload = getattr(getattr(event, "wrapped_event", None), "payload", event)
            if isinstance(payload, payload_type):
                recorded.append(extract(payload))

        taps.append(_tap)
        return recorded

    return _record


@pytest.fixture(params=["sequential", "parallel"])
def execution_mode(request: pytest.FixtureRequest, engine: Engine) -> Iterator[str]:
    """Run a test under both schedulers, restoring the engine's configuration afterwards."""
    config_manager = engine.config_manager
    previous_mode = config_manager.get_config_value("workflow_execution_mode")
    previous_max = config_manager.get_config_value("max_nodes_in_parallel")
    config_manager.set_config_value("workflow_execution_mode", request.param)
    config_manager.set_config_value("max_nodes_in_parallel", 4)
    try:
        yield request.param
    finally:
        config_manager.set_config_value("workflow_execution_mode", previous_mode)
        config_manager.set_config_value("max_nodes_in_parallel", previous_max)
