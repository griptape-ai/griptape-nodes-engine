"""Package for web servers the engine may need to start."""

import errno
import logging
import socket
import sys

logger = logging.getLogger("griptape_nodes")

# Windows reports an address already in use as WSAEADDRINUSE, not the C runtime's EADDRINUSE.
_ADDRESS_IN_USE_ERRNOS = frozenset({errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)})


def bind_free_socket(host: str, port: int) -> socket.socket:
    """Bind a TCP socket to the given host and port and return it.

    When port is 0 the OS assigns a free port automatically. When a
    non-zero port is requested but already in use, the function falls
    back to port 0 so the OS assigns a free port. The caller can read
    the actual port via ``sock.getsockname()[1]`` and must eventually
    close the socket.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR on POSIX only, so a restart is not refused by a port the previous run left in
    # TIME_WAIT. Windows reads the same option as permission to bind a port another process is
    # still listening on, so nothing is set there.
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind((host, port))
    except OSError as exc:
        if exc.errno not in _ADDRESS_IN_USE_ERRNOS:
            sock.close()
            raise
        try:
            sock.bind((host, 0))
        except OSError:
            sock.close()
            raise
        logger.warning(
            "Attempted to start a server on %s port %s. That port is already in use, so the "
            "server is running on port %s instead. Anything pointed at port %s reaches whichever "
            "program took it first.",
            host,
            port,
            sock.getsockname()[1],
            port,
        )

    return sock
