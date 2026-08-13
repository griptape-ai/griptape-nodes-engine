"""Shared configuration and helpers for the end-to-end suite."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import griptape_nodes.retained_mode.managers.config_manager as config_manager_module
import griptape_nodes.retained_mode.managers.secrets_manager as secrets_manager_module
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.engine import reset_root_engine
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence


@pytest.fixture(autouse=True)
def _isolated_engine_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Boot each test against empty temp config/secrets and a clean registry.

    ``SecretsManager`` installs ``ENV_VAR_PATH``'s contents into ``os.environ`` at init;
    without this isolation a subprocess spawned via ``os.environ.copy()`` inherits the
    developer's real ``GT_CLOUD_BUCKET_ID`` and runs in Griptape Cloud mode, whose
    background threads segfault the child at interpreter shutdown.

    Dropping the root engine forces the managers to re-initialize against the patched paths
    and gives each test a fresh object registry. ``LibraryRegistry`` keeps its state in
    ``ClassVar`` dicts that the engine does not own, so it is reset explicitly.
    """
    reset_root_engine()
    LibraryRegistry._clear()

    for key in list(os.environ):
        if key.startswith(("GT_CLOUD_", "GTN_CONFIG_")):
            monkeypatch.delenv(key, raising=False)
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_config_path = Path(temp_dir) / "griptape_nodes_config.json"
            temp_config_path.write_text(json.dumps({}, indent=2))
            temp_env_path = Path(temp_dir) / ".env"
            temp_env_path.write_text("")
            with (
                patch.object(config_manager_module, "USER_CONFIG_PATH", temp_config_path),
                patch.object(secrets_manager_module, "ENV_VAR_PATH", temp_env_path),
            ):
                yield
    finally:
        reset_root_engine()
        LibraryRegistry._clear()


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
def create_node() -> Callable[..., str]:
    """Return a helper that creates a node from the given library and asserts success."""

    def _create(node_type: str, node_name: str, flow_name: str, *, library_name: str) -> str:
        result = GriptapeNodes.handle_request(
            CreateNodeRequest(
                node_type=node_type,
                specific_library_name=library_name,
                node_name=node_name,
                override_parent_flow_name=flow_name,
            )
        )
        assert isinstance(result, CreateNodeResultSuccess), result
        return result.node_name

    return _create


@pytest.fixture
def connect() -> Callable[..., None]:
    """Return a helper that connects two node parameters and asserts success."""

    def _connect(source_node: str, source_param: str, target_node: str, target_param: str) -> None:
        result = GriptapeNodes.handle_request(
            CreateConnectionRequest(
                source_node_name=source_node,
                source_parameter_name=source_param,
                target_node_name=target_node,
                target_parameter_name=target_param,
            )
        )
        assert isinstance(result, CreateConnectionResultSuccess), result

    return _connect
