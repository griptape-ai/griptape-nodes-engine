from pathlib import Path

import pytest

from griptape_nodes.retained_mode.events.config_events import (
    GetConfigValueRequest,
    GetConfigValueResultSuccess,
    GetWorkspaceRequest,
    GetWorkspaceResultSuccess,
    SetConfigValueRequest,
)
from griptape_nodes.retained_mode.events.secrets_events import (
    SetSecretValueRequest,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


class TestConfigEvents:
    @pytest.mark.xfail(strict=True, reason="Drifted due to not running on CI - see #5237")
    def test_get_config_value(self) -> None:
        GriptapeNodes.handle_request(SetSecretValueRequest(key="SECRET_KEY", value="secret foo"))
        GriptapeNodes.handle_request(SetConfigValueRequest(category_and_key="nodes.foo.bar", value="$SECRET_KEY"))
        result = GriptapeNodes.handle_request(GetConfigValueRequest(category_and_key="nodes.foo.bar"))

        assert isinstance(result, GetConfigValueResultSuccess)
        assert result.value == "secret foo"

    def test_get_workspace_returns_absolute_path(self) -> None:
        result = GriptapeNodes.handle_request(GetWorkspaceRequest())

        assert isinstance(result, GetWorkspaceResultSuccess)
        assert result.workspace_path
        # Path is absolute, with `~` expanded and symlinks resolved (Path.resolve()).
        assert Path(result.workspace_path).is_absolute()
        assert "~" not in result.workspace_path
