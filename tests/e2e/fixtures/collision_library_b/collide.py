"""Node fixture B used by tests/e2e/test_library_module_lifecycle.py's namespace-collision test.

Counterpart to fixtures/collision_library_a/collide.py; see that file's docstring. This one
lives in a library named "Collision-Library" (hyphenated), which sanitizes to the same safe
module segment as "Collision Library" (spaced).
"""

from __future__ import annotations

from griptape_nodes.exe_types.node_types import DataNode


class CollisionNodeB(DataNode):
    def process(self) -> None:
        pass
