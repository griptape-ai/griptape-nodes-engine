"""Package for web servers the engine may need to start."""

import socket
import sys


def bind_free_socket(host: str, port: int) -> socket.socket:
    """Bind a TCP socket to the given host and port and return it.

    When port is 0 the OS assigns a free port automatically. When a
    non-zero port is requested but already in use, the function falls
    back to port 0 so the OS assigns a free port. The caller can read
    the actual port via ``sock.getsockname()[1]`` and must eventually
    close the socket.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # So a restart is not refused by a socket the previous run left in a wait state. On Windows
    # SO_REUSEADDR instead grants this bind the port even while another process is listening on
    # it, so the bind below succeeds, the fallback never runs, and two servers advertise one port
    # while only one of them answers. SO_EXCLUSIVEADDRUSE is the option that skips the wait state
    # without granting that. CPython's asyncio draws the same line, defaulting reuse_address to
    # POSIX only, and the editor server in griptape-nodes-app sets these the same way.
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.bind((host, 0))
    return sock
