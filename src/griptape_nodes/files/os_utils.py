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
