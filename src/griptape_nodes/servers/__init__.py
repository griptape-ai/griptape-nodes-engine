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
    # POSIX only, so a restart is not refused by a socket the previous run left in TIME_WAIT.
    # Windows reads the same option as permission to take a port from a process still listening
    # on it, which is the opposite of what a server wants: the bind succeeds, the fallback below
    # never runs, and two servers advertise one port while only one answers. Windows refuses that
    # bind with no option set, so the default is what this helper wants there. CPython's asyncio
    # draws the line in the same place, defaulting reuse_address to POSIX.
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.bind((host, 0))
    return sock
