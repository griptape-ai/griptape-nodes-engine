"""Tests for the socket helper the static and MCP servers bind their ports with."""

from __future__ import annotations

import logging
import socket

import pytest

from griptape_nodes.servers import _ADDRESS_IN_USE_ERRNOS, bind_free_socket

LOOPBACK = "127.0.0.1"


def _free_port() -> int:
    """Return a port the OS just handed out, then release it.

    Picking the port this way rather than hardcoding one keeps the test safe under
    ``pytest -n auto``, where a fixed port would collide with another worker.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def test_falls_back_to_a_free_port_when_the_requested_one_is_taken() -> None:
    """A second server asking for a port another one holds must not be given that port.

    Two servers that both believe they own the port advertise the same URL while only one
    of them answers, so whichever files the other wrote are served as 404s.
    """
    port = _free_port()

    first = bind_free_socket(LOOPBACK, port)
    try:
        first.listen()
        second = bind_free_socket(LOOPBACK, port)
        try:
            assert second.getsockname()[1] != first.getsockname()[1], (
                "bind_free_socket handed a second server the port the first one is listening on"
            )
        finally:
            second.close()
    finally:
        first.close()


def test_assigns_a_port_when_asked_for_zero() -> None:
    """Port 0 means 'any free port', which the caller reads back off the socket."""
    sock = bind_free_socket(LOOPBACK, 0)
    try:
        assert sock.getsockname()[1] != 0
    finally:
        sock.close()


def test_keeps_its_port_across_a_restart_after_serving() -> None:
    """A server restarting on the port it just served must get that port back.

    The engine's MCP server is documented as reachable at a fixed port, so falling back to an
    OS-assigned one after a restart silently breaks every external client pointed at it. A
    closed connection leaves the port in TIME_WAIT, which is the state a restart has to survive.
    """
    first = bind_free_socket(LOOPBACK, 0)
    port = first.getsockname()[1]
    first.listen()

    # Serve one connection and close it, so the port is left in TIME_WAIT rather than simply idle.
    client = socket.create_connection((LOOPBACK, port))
    served, _ = first.accept()
    served.close()
    client.close()
    first.close()

    restarted = bind_free_socket(LOOPBACK, port)
    try:
        assert restarted.getsockname()[1] == port, (
            "bind_free_socket could not rebind the port it had just served, so a restart would "
            "move the server to an OS-assigned port"
        )
    finally:
        restarted.close()


def test_warns_with_the_requested_and_assigned_ports_when_it_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fallback has to say so, or a server quietly answers on a port nobody was told about."""
    port = _free_port()

    first = bind_free_socket(LOOPBACK, port)
    try:
        first.listen()
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            second = bind_free_socket(LOOPBACK, port)
        try:
            assert str(port) in caplog.text, "the warning does not say which port was asked for"
            assert str(second.getsockname()[1]) in caplog.text, (
                "the warning does not say which port the server ended up on"
            )
        finally:
            second.close()
    finally:
        first.close()


def test_raises_when_the_bind_fails_for_a_reason_other_than_the_port_being_taken() -> None:
    """An address the machine does not hold is a misconfiguration, not a busy port.

    Falling back to an OS-assigned port there would start the server on the wrong interface
    and report success, so the error has to reach the caller.
    """
    unassignable_host = "192.0.2.1"  # TEST-NET-1, never assigned to a real interface.

    with pytest.raises(OSError) as raised:  # noqa: PT011
        bind_free_socket(unassignable_host, 0).close()

    assert raised.value.errno not in _ADDRESS_IN_USE_ERRNOS
