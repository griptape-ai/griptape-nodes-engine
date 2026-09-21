"""Which engine code may read another node's parameter through the translating accessor.

`get_parameter_value` substitutes a held object for the key standing in for it. That is right for a node
reading its own input and wrong for engine code that copies a value from one node to another, saves it, or
sends it somewhere: those want `get_raw_parameter_value`, because the key is what travels.

The distinction is invisible in a diff. A site that should have been converted and was not looks untouched,
because it is untouched -- which is how two of them survived several rounds of review. So the set is pinned
here instead: adding a cross-node translating read fails this test, and the fix is either to use the raw
accessor or to add an entry below saying why the value can never be a key.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "griptape_nodes"

# A call on `self` is a node reading its own parameter, which is the accessor's purpose. A call on
# `self._node` is a parameter component doing the same on behalf of its node. Anything else is reaching
# into another node.
_CROSS_NODE_READ = re.compile(r"(?<![\w.])(?!self\.)([\w.]+)\.get_parameter_value\(")
_EXEMPT_RECEIVERS = {"self", "self._node"}

# Each entry is a site that reads another node's parameter and is safe because the parameter named can
# never hold a key: it is an engine-owned scalar, not something a library assigns an object to.
ALLOWED_CROSS_NODE_READS = {
    # Which venue a node executes in: a string chosen from a dropdown.
    ("common/node_executor.py", "execution_environment"),
    ("retained_mode/managers/node_manager.py", "execution_environment"),
    # Whether a loop runs sequentially: a bool.
    ("common/node_executor.py", "run_in_order"),
    # The project-relative file a publish step selects: a string path.
    ("retained_mode/publishing/workflow_packager.py", "SELECT_FROM_PROJECT_PARAM_NAME"),
}


def _cross_node_reads() -> set[tuple[str, str]]:
    """Every `<other>.get_parameter_value(<arg>)` in the engine, as (relative path, argument text)."""
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        for line in path.read_text().splitlines():
            for match in _CROSS_NODE_READ.finditer(line):
                if match.group(1) in _EXEMPT_RECEIVERS:
                    continue
                argument = line[match.end() :].split(")")[0].strip().strip("\"'")
                # Normalise to the parameter being named, so a site keeps its entry when the receiver
                # variable is renamed. A parameter reached as an attribute is named by its declaring
                # attribute rather than by the trailing `.name`.
                argument = argument.removesuffix(".name")
                found.add((relative, argument.split(".")[-1]))
    return found


class TestCrossNodeParameterReads:
    def test_no_unreviewed_cross_node_translating_reads(self) -> None:
        unexpected = _cross_node_reads() - ALLOWED_CROSS_NODE_READS

        assert unexpected == set(), (
            "New cross-node read(s) through get_parameter_value. If the value could ever be a held "
            "object's key, use get_raw_parameter_value -- copying a live object into another node's "
            "parameter_values puts it on the wire. If it genuinely cannot, add it to "
            f"ALLOWED_CROSS_NODE_READS with the reason: {sorted(unexpected)}"
        )

    def test_every_allowance_still_describes_a_real_site(self) -> None:
        """A stale entry would silently re-permit the shape it was written for."""
        stale = ALLOWED_CROSS_NODE_READS - _cross_node_reads()

        assert stale == set(), f"Allowances no longer matching any site, remove them: {sorted(stale)}"
