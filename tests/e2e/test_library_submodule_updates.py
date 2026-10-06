"""Download and update, through engine requests, a library that vendors code as a git submodule.

The fixture's load hook does what SAM3's does: when the submodule folder is empty it initializes
the submodule itself and rewrites its tracked library JSON. Either leaves the checkout dirty, and
the next update then stops with an "uncommitted changes" failure.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import LibrarySchema
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.library_events import (
    DownloadLibraryRequest,
    DownloadLibraryResultSuccess,
    UpdateLibraryRequest,
    UpdateLibraryResultSuccess,
)
from griptape_nodes.utils import git_utils
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from pathlib import Path

LIBRARY_NAME = "Submodule Library"

LIBRARY_JSON = {
    "name": LIBRARY_NAME,
    "library_schema_version": LibrarySchema.LATEST_SCHEMA_VERSION,
    "advanced_library_path": "submodule_library_advanced.py",
    "metadata": {
        "author": "Test Fixture",
        "description": "Library that vendors code as a git submodule",
        "library_version": "0.1.0",
        "engine_version": engine_version,
        "tags": ["test"],
        "dependencies": {"pip_dependencies": []},
    },
    "categories": [
        {"test": {"title": "test", "description": "Test nodes", "color": "border-gray-500", "icon": "Folder"}}
    ],
    "nodes": [
        {
            "class_name": "VendoredNode",
            "file_path": "vendored_node.py",
            "metadata": {"category": "test", "description": "Minimal fixture node", "display_name": "Vendored Node"},
        }
    ],
}

NODE = """
from griptape_nodes.exe_types.node_types import DataNode


class VendoredNode(DataNode):
    def process(self) -> None:
        pass
"""

ADVANCED_LIBRARY = """
import json
import subprocess
from pathlib import Path

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary


class SubmoduleLibraryAdvanced(AdvancedNodeLibrary):
    def before_library_nodes_loaded(self, library_data, library):
        root = Path(__file__).parent
        vendor = root / "vendor" / "upstream"
        if vendor.exists() and any(vendor.iterdir()):
            return
        subprocess.run(
            ["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        json_path = root / "griptape_nodes_library.json"
        data = json.loads(json_path.read_text())
        with json_path.open("w") as f:
            json.dump(data, f, indent=2)
"""


def git(cwd: Path, *args: str) -> str:
    """Run git with a throwaway identity, allowing local repositories as submodules."""
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "-c",
            "protocol.file.allow=always",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_upstream(upstream: Path, content: str) -> str:
    """Commit content to upstream/code.py and return the new SHA."""
    (upstream / "code.py").write_text(content, encoding="utf-8")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", content)
    return git(upstream, "rev-parse", "HEAD")


def make_origin(tmp_path: Path) -> Path:
    """Create the origin library, vendoring tmp_path/upstream at vendor/upstream and tagged `latest`."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    commit_upstream(upstream, "v1")

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    (origin / "griptape_nodes_library.json").write_text(json.dumps(LIBRARY_JSON, indent=2) + "\n", encoding="utf-8")
    (origin / "submodule_library_advanced.py").write_text(ADVANCED_LIBRARY, encoding="utf-8")
    (origin / "vendored_node.py").write_text(NODE, encoding="utf-8")
    (origin / ".gitignore").write_text(".venv\n__pycache__/\n", encoding="utf-8")
    git(origin, "submodule", "add", str(upstream), "vendor/upstream")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "initial")
    git(origin, "tag", "latest")
    return origin


def bump_submodule(tmp_path: Path, origin: Path) -> str:
    """Advance upstream and commit the new pointer in origin, moving `latest`. Returns the new SHA."""
    sha = commit_upstream(tmp_path / "upstream", "v2")
    vendored = origin / "vendor" / "upstream"
    git(vendored, "fetch", "origin")
    git(vendored, "checkout", sha)
    git(origin, "add", "vendor/upstream")
    git(origin, "commit", "-m", "bump submodule")
    git(origin, "tag", "-f", "latest")
    return sha


@pytest.fixture(autouse=True)
def allow_file_submodules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture vendors a local repository, which production refuses for submodules."""
    monkeypatch.setattr(git_utils, "_GIT_SUBMODULE_ALLOWED_PROTOCOLS", git_utils._GIT_ALLOWED_PROTOCOLS)


@pytest.mark.parametrize("ref", [None, "latest"])
def test_library_with_submodule_stays_clean_across_download_and_updates(tmp_path: Path, ref: str | None) -> None:
    """Download, then update twice across a submodule bump. The checkout must stay clean throughout."""
    engine = current_engine()
    origin = make_origin(tmp_path)

    download = asyncio.run(
        engine.ahandle_request(
            DownloadLibraryRequest(
                git_url=origin.as_posix(), branch_tag_commit=ref, download_directory=str(tmp_path / "libraries")
            )
        )
    )
    assert isinstance(download, DownloadLibraryResultSuccess), download.result_details
    checkout = tmp_path / "libraries" / "origin"
    assert git(checkout, "status", "--porcelain") == ""
    assert (checkout / "vendor" / "upstream" / "code.py").read_text(encoding="utf-8") == "v1"

    new_sha = bump_submodule(tmp_path, origin)

    for _ in range(2):
        update = asyncio.run(engine.ahandle_request(UpdateLibraryRequest(library_name=LIBRARY_NAME)))
        assert isinstance(update, UpdateLibraryResultSuccess), update.result_details
        assert git(checkout, "status", "--porcelain") == ""
        assert git(checkout / "vendor" / "upstream", "rev-parse", "HEAD") == new_sha
