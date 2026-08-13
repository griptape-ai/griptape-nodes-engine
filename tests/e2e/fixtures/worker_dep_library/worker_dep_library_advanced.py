"""Advanced module that imports a third-party manifest dependency at module scope.

Mirrors the shape of real model libraries (Depth Anything 3, diffusers, ...), which
imported pygit2 here. The import only resolves when the engine has created the library
venv, installed the manifest's pip_dependencies into it, and put its site-packages on
sys.path BEFORE importing this module -- the contract pinned by
tests/e2e/test_library_dependency_environment_init.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import fakegit  # type: ignore[reportMissingImports]  # The pygit2 stand-in: only importable from the library venv.

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary

if TYPE_CHECKING:
    from griptape_nodes.node_library.library_registry import Library, LibrarySchema

# Appended to by the hooks so tests can assert they ran (and against which dep version).
HOOKS_SEEN: list[str] = []


class WorkerDepLibraryAdvanced(AdvancedNodeLibrary):
    def before_library_nodes_loaded(self, library_data: LibrarySchema, library: Library) -> None:  # noqa: ARG002 (hook signature)
        HOOKS_SEEN.append(f"before:{fakegit.__version__}")

    def after_library_nodes_loaded(self, library_data: LibrarySchema, library: Library) -> None:  # noqa: ARG002 (hook signature)
        HOOKS_SEEN.append("after")
