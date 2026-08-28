"""Shared fixtures for unit tests."""

import json
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

from griptape_nodes.retained_mode.engine import Engine, current_engine, reset_root_engine


@pytest.fixture(autouse=True)
def isolate_user_config() -> Generator[Path, None, None]:
    """Isolate the user config file during tests to prevent pollution of the real config."""
    import griptape_nodes.retained_mode.managers.config_manager as config_manager_module

    # Drop the root engine so managers re-initialize against the patched config below.
    reset_root_engine()

    # Create a temporary directory for the test config
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_config_path = Path(temp_dir) / "griptape_nodes_config.json"

        # Initialize with an empty config
        temp_config_path.write_text(json.dumps({}, indent=2))

        # Patch the USER_CONFIG_PATH constant to point to our temp file
        with patch.object(config_manager_module, "USER_CONFIG_PATH", temp_config_path):
            yield temp_config_path

            # Drop it again so the next test doesn't inherit this one's object graph.
            reset_root_engine()


@pytest.fixture
def engine() -> Engine:
    """Provide the engine for this test, building it on first use."""
    return current_engine()
