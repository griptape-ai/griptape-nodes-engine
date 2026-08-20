"""Standalone platform-detection predicates.

Single source of truth for "which OS are we on?" checks. Lives in ``files/``
(not ``retained_mode``) so low-level utilities like ``path_utils`` can reach it
without importing ``OSManager`` — a manager-layer import from ``files/`` would
add circular/heavy-import risk.

``OSManager.is_windows`` / ``is_mac`` / ``is_linux`` delegate here so callers
that rely on the existing static methods keep working against a single
implementation.
"""

import sys


def is_windows() -> bool:
    """Return True when running on Windows."""
    return sys.platform.startswith("win")


def is_mac() -> bool:
    """Return True when running on macOS."""
    return sys.platform.startswith("darwin")


def is_linux() -> bool:
    """Return True when running on Linux."""
    return sys.platform.startswith("linux")


def os_display_name() -> str:
    """Return the name of this OS as a user would recognize it.

    For showing the user which machine something is running on -- an engine in a client's
    engine list, for instance. ``sys.platform`` spells macOS "darwin" and Windows "win32",
    neither of which means anything to an artist, so each supported platform gets a
    familiar spelling. Anything else falls back to ``sys.platform``, which is always set,
    so the result is never empty.
    """
    if is_windows():
        return "Windows"
    if is_mac():
        return "macOS"
    if is_linux():
        return "Linux"
    return sys.platform
