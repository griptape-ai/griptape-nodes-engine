"""Tests for `install_connection_reset_handler`, which quiets peer resets on closing sockets."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from griptape_nodes.utils import async_utils
from griptape_nodes.utils.async_utils import install_connection_reset_handler

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


class _ResetTransport:
    """Stands in for the Windows proactor transport whose close callback hits a peer RST."""

    def _call_connection_lost(self, exc: BaseException | None) -> None:  # noqa: ARG002
        msg = "An existing connection was forcibly closed by the remote host"
        raise ConnectionResetError(10054, msg)


def _run_callback(callback: Callable[..., None], *args: Any, install: bool) -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        if install:
            install_connection_reset_handler(loop)
        loop.call_soon(callback, *args)
        await asyncio.sleep(0)

    asyncio.run(main())


def _asyncio_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "asyncio" and record.levelno >= logging.ERROR]


class TestInstallConnectionResetHandler:
    def test_default_handler_logs_peer_reset_as_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """The reported symptom, for contrast: without the handler asyncio logs an ERROR traceback."""
        caplog.set_level(logging.DEBUG)

        _run_callback(_ResetTransport()._call_connection_lost, None, install=False)

        assert len(_asyncio_errors(caplog)) == 1

    def test_peer_reset_on_closing_transport_is_logged_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        # Set on the module's own logger: engine startup can pin a level on the "griptape_nodes" parent.
        caplog.set_level(logging.DEBUG, logger=async_utils.__name__)

        _run_callback(_ResetTransport()._call_connection_lost, None, install=True)

        assert _asyncio_errors(caplog) == []
        reset_records = [record for record in caplog.records if "Peer reset" in record.getMessage()]
        assert len(reset_records) == 1
        assert reset_records[0].levelno == logging.DEBUG
        assert reset_records[0].exc_info is None

    def test_other_callback_errors_still_log_as_error(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)

        def broken() -> None:
            msg = "real bug"
            raise ValueError(msg)

        _run_callback(broken, install=True)

        assert len(_asyncio_errors(caplog)) == 1

    def test_reset_from_another_callback_still_logs_as_error(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)

        def unrelated() -> None:
            raise ConnectionResetError

        _run_callback(unrelated, install=True)

        assert len(_asyncio_errors(caplog)) == 1

    def test_reset_hiding_a_protocol_error_still_logs_as_error(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)

        class _BrokenProtocolTransport:
            def _call_connection_lost(self, exc: BaseException | None) -> None:  # noqa: ARG002
                try:
                    msg = "protocol bug"
                    raise ValueError(msg)
                finally:
                    raise ConnectionResetError

        _run_callback(_BrokenProtocolTransport()._call_connection_lost, None, install=True)

        assert len(_asyncio_errors(caplog)) == 1

    def test_delegates_to_the_previously_installed_handler(self) -> None:
        seen: list[str] = []

        async def main() -> None:
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda _loop, context: seen.append(context["message"]))
            install_connection_reset_handler(loop)
            loop.call_exception_handler({"message": "something else"})

        asyncio.run(main())

        assert seen == ["something else"]
