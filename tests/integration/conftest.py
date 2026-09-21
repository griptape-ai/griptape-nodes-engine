import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest  # type: ignore[reportMissingImports]

from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.engine import Engine, current_engine, reset_root_engine
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import RegisterLibraryFromFileRequest
from griptape_nodes.retained_mode.managers import config_manager as config_manager_module
from griptape_nodes.retained_mode.managers import secrets_manager as secrets_manager_module
from griptape_nodes.utils import install_file_url_support

# Install file:// URL support for httpx/requests in integration tests
install_file_url_support()


@pytest.fixture(autouse=True)
def _isolated_engine_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Boot each test against empty temp config/secrets and a clean registry.

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
def flow(engine: Engine) -> CreateFlowResultSuccess:
    """Fixture to create a flow for testing."""
    request = RegisterLibraryFromFileRequest(
        file_path="../griptape-nodes/libraries/griptape_nodes_library/griptape_nodes_library.json"
    )
    result = engine.handle_request(request)

    # Create a canvas (flow with no parents)
    request = CreateFlowRequest(parent_flow_name=None, flow_name="canvas")
    result = engine.handle_request(request)

    assert isinstance(result, CreateFlowResultSuccess)

    return result
