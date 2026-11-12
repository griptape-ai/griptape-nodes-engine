"""Tests for the socket helper the static and MCP servers bind their ports with."""

from __future__ import annotations

import socket

from griptape_nodes.servers import bind_free_socket

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
