"""Node libraries for tests, each a directory holding a ``griptape_nodes_library.json``."""

import json
import shutil
from pathlib import Path

from griptape_nodes.node_library.library_registry import LibrarySchema
from griptape_nodes.testing.offline_wheels import build_wheel, offline_install_flags
from griptape_nodes.utils.version_utils import engine_version

WORKER_BEHAVIOR_LIBRARY_DIR = Path(__file__).parent / "worker_behavior_library"
WORKER_BEHAVIOR_LIBRARY_NAME = "Worker Behavior Library"


def materialize_worker_behavior_library(target_dir: Path, *, execution_dependency: str | None = None) -> Path:
    """Write a registrable copy of the worker behavior library.

    A library runs its nodes in a worker only when it declares an execution dependency. With
    ``execution_dependency``, the copy declares it, built here as an offline wheel, and its
    ``.venv-exec`` is built beside the copy, so every engine registering the returned path shares one
    environment. Without it, the copy runs its nodes on the orchestrator.

    Returns:
        The path of the copy's ``griptape_nodes_library.json``.
    """
    library_dir = target_dir / "worker_behavior_library"
    shutil.copytree(WORKER_BEHAVIOR_LIBRARY_DIR, library_dir, ignore=shutil.ignore_patterns("__pycache__"))

    library_json = library_dir / "griptape_nodes_library.json"
    schema = json.loads(library_json.read_text())
    schema["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
    schema["metadata"]["engine_version"] = engine_version
    if execution_dependency is not None:
        wheel_dir = target_dir / "wheels"
        build_wheel(wheel_dir, execution_dependency)
        schema["metadata"]["dependencies"] = {
            "pip_dependencies": [],
            "pip_dependencies_exec": [execution_dependency],
            "pip_install_flags": offline_install_flags(wheel_dir),
        }
    library_json.write_text(json.dumps(schema, indent=2))
    return library_json
