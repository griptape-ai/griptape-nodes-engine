"""Unit tests for SubprocessWebSocketBaseMixin's connection preconditions."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.bootstrap.utils import subprocess_websocket_base
from griptape_nodes.bootstrap.utils.subprocess_websocket_base import (
    SubprocessWebSocketBaseMixin,
    SubprocessWebSocketUnavailableError,
)


class _Mixin(SubprocessWebSocketBaseMixin):
    def __init__(self) -> None:
        self._init_websocket_base("session-1")


def _patch_settings(*, relay_config: object, api_key: str | None) -> tuple:
    griptape_nodes = MagicMock()
    griptape_nodes.ConfigManager.return_value.get_config_value.return_value = relay_config
    griptape_nodes.SecretsManager.return_value.get_secret.return_value = api_key
    client_cls = MagicMock()
    client_cls.return_value.connect = AsyncMock()
    return (
        patch.object(subprocess_websocket_base, "GriptapeNodes", griptape_nodes),
        patch.object(subprocess_websocket_base, "Client", client_cls),
        client_cls,
    )


class TestStartWebSocketClient:
    @pytest.mark.asyncio
    async def test_raises_without_api_key_and_opens_no_client(self) -> None:
        gn_patch, client_patch, client_cls = _patch_settings(relay_config=None, api_key=None)
        with gn_patch, client_patch, pytest.raises(SubprocessWebSocketUnavailableError, match="GT_CLOUD_API_KEY"):
            await _Mixin()._start_websocket_client()

        client_cls.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("relay_enabled", [False, "false", "off", "0", "maybe", " true ", 2, None])
    async def test_raises_with_relay_disabled_and_opens_no_client(self, relay_enabled: object) -> None:
        gn_patch, client_patch, client_cls = _patch_settings(relay_config={"enabled": relay_enabled}, api_key="gt-key")
        with gn_patch, client_patch, pytest.raises(SubprocessWebSocketUnavailableError, match="relay transport"):
            await _Mixin()._start_websocket_client()

        client_cls.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("relay_config", [None, {}, [], {"enabled": True}, {"enabled": "On"}, {"enabled": 1.0}])
    async def test_connects_with_api_key_when_relay_not_disabled(self, relay_config: object) -> None:
        gn_patch, client_patch, client_cls = _patch_settings(relay_config=relay_config, api_key="gt-key")
        mixin = _Mixin()
        with gn_patch, client_patch:
            await mixin._start_websocket_client()

        client_cls.assert_called_once_with(api_key="gt-key")
        client_cls.return_value.connect.assert_awaited_once()
        assert mixin._ws_client is client_cls.return_value
