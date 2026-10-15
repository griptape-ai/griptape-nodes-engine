"""Run npm's Windows ``.cmd`` shims through node directly."""

from __future__ import annotations

import logging
import re
import shutil
import sys
from pathlib import Path, PureWindowsPath

logger = logging.getLogger("griptape_nodes")

# npm, and pnpm's copy of its format, name the script relative to the shim's folder.
_SHIM_SCRIPT = re.compile(r'"%~?dp0%?\\([^"]+\.[cm]?js)"', re.IGNORECASE)
_SHIM_SUFFIXES = frozenset({".cmd", ".bat"})


def launch_command(executable: str) -> list[str]:
    """Return the argv prefix that runs ``executable``, bypassing cmd.exe for npm shims."""
    path = Path(executable)
    if sys.platform != "win32" or path.suffix.lower() not in _SHIM_SUFFIXES:
        return [executable]
    script = shim_script(path)
    if script is None:
        logger.debug("Running %s through cmd.exe; it is not an npm shim.", executable)
        return [executable]
    # cmd.exe reparses arguments, so an & in free text can run a command.
    bundled_node = path.parent / "node.exe"
    if bundled_node.is_file():
        return [str(bundled_node), str(script)]
    node = shutil.which("node")
    if node is None:
        logger.debug("Running %s through cmd.exe; node was not found.", executable)
        return [executable]
    return [node, str(script)]


def shim_script(shim: Path) -> Path | None:
    """Return the JavaScript file an npm ``.cmd`` shim runs, if it names one that exists."""
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = _SHIM_SCRIPT.findall(text)
    if not matches:
        return None
    script = shim.parent.joinpath(*PureWindowsPath(matches[-1]).parts)
    if not script.is_file():
        return None
    return script
