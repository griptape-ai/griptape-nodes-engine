"""Node fixture A used by tests/e2e/test_library_module_lifecycle.py's namespace-collision test.

Shares its file stem ("collide") with fixtures/collision_library_b/collide.py while living in
a library named "Collision Library". "Collision Library" and "Collision-Library" both sanitize
to the same safe module segment ("collision_library"), so the two libraries' node files map to
the same base stable namespace and exercise LibraryManager._resolve_stable_namespace's
same-namespace-different-file disambiguation across two real libraries, not just two files
inside one library.
"""

from __future__ import annotations

from griptape_nodes.exe_types.node_types import DataNode


class CollisionNodeA(DataNode):
    def process(self) -> None:
        pass
