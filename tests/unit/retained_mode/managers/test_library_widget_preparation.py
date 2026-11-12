"""Tests for preparing a library's widgets from the TypeScript source it ships.

Preparation runs `npm ci` and the package's `build` script with the Node.js and npm the user
configured, and only when every gate allows it. The tests that run npm use the real tools on the
test machine and are skipped where they are not installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest

from griptape_nodes.node_library.library_registry import (
    CategoryDefinition,
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
    WidgetBuildMetadata,
    WidgetDefinition,
)
from griptape_nodes.retained_mode.beta_features import BETA_FEATURES_ENABLED_KEY, WIDGET_SOURCE_BUILDS
from griptape_nodes.retained_mode.events.library_events import (
    PrepareLibraryWidgetsRequest,
    PrepareLibraryWidgetsResultFailure,
    PrepareLibraryWidgetsResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
    UpdateLibraryResultFailure,
    WidgetPreparationFailureReason,
)
from griptape_nodes.retained_mode.managers.library import widget_preparation as widget_preparation_module
from griptape_nodes.retained_mode.managers.library.widget_preparation import (
    INSTALL_ARGS,
    WidgetPreparationStatus,
    build_child_environment,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.settings import (
    WIDGET_TOOLING_NODE_EXECUTABLE_KEY,
    WIDGET_TOOLING_NPM_EXECUTABLE_KEY,
)
from griptape_nodes.utils.async_utils import run_process_tree

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload

LIBRARY_NAME = "Widget Library"
WIDGET_ROOT = "widget-src"
WIDGET_PATH = f"{WIDGET_ROOT}/dist/Example.js"

NODE_PATH = shutil.which("node")
NPM_PATH = shutil.which("npm")
GIT_PATH = shutil.which("git")
PREPARATION_MODULE = "griptape_nodes.retained_mode.managers.library.widget_preparation"
needs_node = pytest.mark.skipif(NODE_PATH is None or NPM_PATH is None, reason="needs node and npm on PATH")
needs_git = pytest.mark.skipif(GIT_PATH is None, reason="needs git on PATH")

BUILD_JS = """
const fs = require("fs");
const path = require("path");
fs.mkdirSync("dist", { recursive: true });
fs.writeFileSync(path.join("dist", "Example.js"), "export default function Example() {}\\n");
fs.writeFileSync("env.json", JSON.stringify(process.env));
console.log("built Example");
"""

# Writes its pid, then never finishes, so a timeout or cancellation has to stop it.
HANGING_BUILD_JS = """
const fs = require("fs");
fs.writeFileSync("build.pid", String(process.pid));
console.log("hanging");
setInterval(() => {}, 1000);
"""

# Records when each build starts and ends, so overlapping builds show up in the log.
SLOW_BUILD_JS = """
const fs = require("fs");
fs.appendFileSync("../builds.log", "start\\n");
setTimeout(() => {
  fs.mkdirSync("dist", { recursive: true });
  fs.writeFileSync("dist/Example.js", "export default 1;\\n");
  fs.appendFileSync("../builds.log", "end\\n");
}, 500);
"""

LIFECYCLE_MARKER_SCRIPT = "node -e \"require('fs').writeFileSync('{name}-ran', '')\""


def _write_manifest(library_dir: Path, *, widget_build: WidgetBuildMetadata | None) -> Path:
    schema = LibrarySchema(
        name=LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description="library with a widget",
            library_version="1.0.0",
            engine_version="0.98.0",
            tags=[],
            widget_build=widget_build,
        ),
        categories=[{"Test": CategoryDefinition(title="Test", description="test", color="#000", icon="Folder")}],
        nodes=[
            NodeDefinition(
                class_name="WidgetLibraryNode",
                file_path="node.py",
                metadata=NodeMetadata(category="Test", description="test node", display_name="Widget Node"),
            )
        ],
        widgets=[WidgetDefinition(name="Example", path=WIDGET_PATH, description="example widget")],
    )
    library_dir.mkdir(parents=True, exist_ok=True)
    (library_dir / "node.py").write_text(
        "from griptape_nodes.exe_types.node_types import BaseNode\n\n\n"
        "class WidgetLibraryNode(BaseNode):\n"
        "    def process(self):\n"
        "        return None\n",
        encoding="utf-8",
    )
    manifest = library_dir / "griptape_nodes_library.json"
    manifest.write_text(json.dumps(schema.model_dump(mode="json")), encoding="utf-8")
    return manifest


def _write_package(
    package_dir: Path,
    *,
    build_js: str = BUILD_JS,
    scripts: dict[str, str] | None = None,
    dependencies: dict[str, str] | None = None,
    write_lock: bool = True,
) -> None:
    """A widget package with no real dependencies, so `npm ci` needs no network."""
    package_dir.mkdir(parents=True, exist_ok=True)
    if scripts is None:
        scripts = {
            "build": "node build.js",
            "prepare": LIFECYCLE_MARKER_SCRIPT.format(name="prepare"),
            "prebuild": LIFECYCLE_MARKER_SCRIPT.format(name="prebuild"),
        }
    package: dict[str, object] = {"name": "example-widget", "version": "1.0.0", "private": True, "scripts": scripts}
    if dependencies:
        package["dependencies"] = dependencies
    (package_dir / "package.json").write_text(json.dumps(package), encoding="utf-8")
    if write_lock:
        lock = {
            "name": "example-widget",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {"": {"name": "example-widget", "version": "1.0.0"}},
        }
        (package_dir / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (package_dir / "build.js").write_text(build_js, encoding="utf-8")


def _track(library_manager: LibraryManager, manifest: Path) -> None:
    library_manager._library_file_path_to_info[str(manifest)] = LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
        fitness=LibraryManager.LibraryFitness.GOOD,
        library_path=str(manifest),
        is_sandbox=False,
        library_name=LIBRARY_NAME,
        library_version="1.0.0",
    )


def _git(library_dir: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 (fixed git arguments in a test)
        [GIT_PATH or "git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=library_dir,
        check=True,
        capture_output=True,
    )


def _enable_beta(engine: Engine) -> None:
    engine.config_manager.set_config_value(WIDGET_SOURCE_BUILDS.config_key, value=True)


def _configure_tooling(engine: Engine, node: str | None = NODE_PATH, npm: str | None = NPM_PATH) -> None:
    engine.config_manager.set_config_value(WIDGET_TOOLING_NODE_EXECUTABLE_KEY, node)
    engine.config_manager.set_config_value(WIDGET_TOOLING_NPM_EXECUTABLE_KEY, npm)


def _fake_tool(path: Path, version: str) -> str:
    """An executable standing in for node that reports `version`."""
    path.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


@pytest.fixture
def library_dir(tmp_path: Path) -> Path:
    """Where the test library lives; created by the `manifest` fixture."""
    return tmp_path / "widget_library"


@pytest.fixture
def manifest(engine: Engine, library_dir: Path) -> Path:
    """A tracked library whose widget is built from source in `widget-src`."""
    manifest = _write_manifest(library_dir, widget_build=WidgetBuildMetadata(mode="source", root=WIDGET_ROOT))
    _write_package(library_dir / WIDGET_ROOT)
    _track(engine.library_manager, manifest)
    return manifest


@pytest.fixture
def ready(engine: Engine, manifest: Path) -> Path:
    """Beta on and the real tools configured: every gate before authorisation passes."""
    _enable_beta(engine)
    _configure_tooling(engine)
    return manifest


async def _prepare(engine: Engine, *, allow: bool = True) -> ResultPayload:
    return await engine.ahandle_request(
        PrepareLibraryWidgetsRequest(library_name=LIBRARY_NAME, allow_build_execution=allow)
    )


def _assert_refused(result: object, reason: WidgetPreparationFailureReason) -> PrepareLibraryWidgetsResultFailure:
    assert isinstance(result, PrepareLibraryWidgetsResultFailure), result
    assert result.reason == reason, result.result_details
    assert result.library_name == LIBRARY_NAME
    return result


class TestGatesBeforeTooling:
    @pytest.mark.asyncio
    async def test_unknown_library_is_refused(self, engine: Engine) -> None:
        result = await engine.ahandle_request(
            PrepareLibraryWidgetsRequest(library_name="Nope", allow_build_execution=True)
        )

        assert isinstance(result, PrepareLibraryWidgetsResultFailure)
        assert result.reason == WidgetPreparationFailureReason.LIBRARY_NOT_FOUND

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_environment_mode_refuses_before_anything_else(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__PROVISIONED_BY", "environment")
        engine.config_manager.load_configs()

        result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.MANAGED_ENVIRONMENT)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("manifest")
    async def test_beta_feature_off_is_refused(self, engine: Engine) -> None:
        _configure_tooling(engine)

        result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.BETA_DISABLED)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_beta_features_turned_off_globally_is_refused(self, engine: Engine) -> None:
        engine.config_manager.set_config_value(BETA_FEATURES_ENABLED_KEY, value=False)

        result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.BETA_DISABLED)


class TestToolingGates:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("manifest")
    async def test_unset_tooling_is_refused(self, engine: Engine) -> None:
        _enable_beta(engine)

        result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.TOOLING_NOT_CONFIGURED)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("manifest")
    async def test_only_node_configured_is_refused(self, engine: Engine) -> None:
        _enable_beta(engine)
        _configure_tooling(engine, node="/usr/bin/node", npm=None)

        result = await _prepare(engine)

        refused = _assert_refused(result, WidgetPreparationFailureReason.TOOLING_NOT_CONFIGURED)
        assert WIDGET_TOOLING_NPM_EXECUTABLE_KEY in str(refused.result_details)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("manifest")
    async def test_empty_string_counts_as_unset(self, engine: Engine) -> None:
        _enable_beta(engine)
        _configure_tooling(engine, node="  ", npm="")

        result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.TOOLING_NOT_CONFIGURED)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("manifest")
    async def test_a_relative_path_is_refused(self, engine: Engine) -> None:
        _enable_beta(engine)
        _configure_tooling(engine, node="node", npm="npm")

        result = await _prepare(engine)

        refused = _assert_refused(result, WidgetPreparationFailureReason.TOOLING_INVALID)
        assert "absolute" in str(refused.result_details)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("manifest")
    async def test_a_nonexistent_path_is_refused(self, engine: Engine, tmp_path: Path) -> None:
        _enable_beta(engine)
        missing = tmp_path / "no" / "node"
        _configure_tooling(engine, node=str(missing), npm=str(tmp_path / "no" / "npm"))

        result = await _prepare(engine)

        refused = _assert_refused(result, WidgetPreparationFailureReason.TOOLING_INVALID)
        assert str(missing) in str(refused.result_details)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("manifest")
    async def test_a_directory_is_refused(self, engine: Engine, tmp_path: Path) -> None:
        _enable_beta(engine)
        _configure_tooling(engine, node=str(tmp_path), npm=str(tmp_path))

        result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.TOOLING_INVALID)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("manifest")
    async def test_a_value_that_is_not_a_string_is_refused(self, engine: Engine) -> None:
        """Settings rejects such a value, but the merged config the engine reads keeps it as written."""
        _enable_beta(engine)
        config_manager = engine.config_manager
        raw_values = {WIDGET_TOOLING_NODE_EXECUTABLE_KEY: 5, WIDGET_TOOLING_NPM_EXECUTABLE_KEY: ["npm"]}
        original_get = config_manager.get_config_value

        def get_config_value(key: str, **kwargs: object) -> object:
            if key in raw_values:
                return raw_values[key]
            return original_get(key, **kwargs)  # type: ignore[arg-type]

        with patch.object(config_manager, "get_config_value", side_effect=get_config_value):
            result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.TOOLING_INVALID)

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="the stand-in node is a shell script")
    @pytest.mark.usefixtures("manifest")
    async def test_node_older_than_20_is_refused(self, engine: Engine, tmp_path: Path) -> None:
        _enable_beta(engine)
        old_node = _fake_tool(tmp_path / "node", "v18.19.0")
        npm_cli = tmp_path / "npm-cli.js"
        npm_cli.write_text("", encoding="utf-8")
        _configure_tooling(engine, node=old_node, npm=str(npm_cli))

        result = await _prepare(engine)

        refused = _assert_refused(result, WidgetPreparationFailureReason.TOOLING_INVALID)
        assert "v18.19.0" in str(refused.result_details)

    @pytest.mark.asyncio
    @needs_node
    @pytest.mark.usefixtures("manifest")
    async def test_an_npm_whose_cli_cannot_be_found_is_refused(self, engine: Engine, tmp_path: Path) -> None:
        _enable_beta(engine)
        lone_npm = tmp_path / "bin" / "npm"
        lone_npm.parent.mkdir()
        lone_npm.write_text("#!/bin/sh\n", encoding="utf-8")
        _configure_tooling(engine, node=NODE_PATH, npm=str(lone_npm))

        result = await _prepare(engine)

        refused = _assert_refused(result, WidgetPreparationFailureReason.TOOLING_INVALID)
        assert str(lone_npm) in str(refused.result_details)


@needs_node
class TestAuthorisationAndManifestGates:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_not_allowing_build_execution_is_refused(self, engine: Engine, library_dir: Path) -> None:
        result = await _prepare(engine, allow=False)

        _assert_refused(result, WidgetPreparationFailureReason.NOT_AUTHORIZED)
        assert not (library_dir / WIDGET_ROOT / "node_modules").exists()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_a_library_without_widget_build_is_refused(self, engine: Engine, library_dir: Path) -> None:
        _write_manifest(library_dir, widget_build=None)

        result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.NOT_SOURCE_MODE)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_a_prebuilt_library_is_refused(self, engine: Engine, library_dir: Path) -> None:
        _write_manifest(library_dir, widget_build=WidgetBuildMetadata(mode="prebuilt", root=WIDGET_ROOT))

        result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.NOT_SOURCE_MODE)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_a_root_outside_the_library_is_refused(
        self, engine: Engine, library_dir: Path, tmp_path: Path
    ) -> None:
        _write_package(tmp_path / "outside")
        _write_manifest(library_dir, widget_build=WidgetBuildMetadata(mode="source", root="../outside"))

        result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.INVALID_MANIFEST)
        assert not (tmp_path / "outside" / "dist").exists()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_a_root_that_links_outside_the_library_is_refused(
        self, engine: Engine, library_dir: Path, tmp_path: Path
    ) -> None:
        _write_package(tmp_path / "outside")
        (library_dir / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
        _write_manifest(library_dir, widget_build=WidgetBuildMetadata(mode="source", root="linked"))

        result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.INVALID_MANIFEST)
        assert not (tmp_path / "outside" / "dist").exists()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_a_missing_lockfile_is_refused(self, engine: Engine, library_dir: Path) -> None:
        (library_dir / WIDGET_ROOT / "package-lock.json").unlink()

        result = await _prepare(engine)

        refused = _assert_refused(result, WidgetPreparationFailureReason.INVALID_MANIFEST)
        assert "package-lock.json" in str(refused.result_details)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_a_package_without_a_build_script_is_refused(self, engine: Engine, library_dir: Path) -> None:
        _write_package(library_dir / WIDGET_ROOT, scripts={"test": "echo hi"})

        result = await _prepare(engine)

        refused = _assert_refused(result, WidgetPreparationFailureReason.INVALID_MANIFEST)
        assert "build" in str(refused.result_details)


@needs_node
class TestBuilding:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_builds_the_widget_without_running_lifecycle_scripts(self, engine: Engine, library_dir: Path) -> None:
        package_dir = library_dir / WIDGET_ROOT

        result = await _prepare(engine)

        assert isinstance(result, PrepareLibraryWidgetsResultSuccess), result.result_details
        assert result.library_name == LIBRARY_NAME
        assert result.revision == "local"
        assert result.node_version.startswith("v")
        assert result.npm_version[0].isdigit()
        assert "built Example" in result.output
        assert (package_dir / "dist" / "Example.js").is_file()
        assert not (package_dir / "prepare-ran").exists()
        assert not (package_dir / "prebuild-ran").exists()
        assert engine.library_manager.widget_preparation.is_prepared(LIBRARY_NAME, library_dir)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_records_the_preparation_state(self, engine: Engine, library_dir: Path) -> None:
        preparation = engine.library_manager.widget_preparation

        result = await _prepare(engine)

        assert isinstance(result, PrepareLibraryWidgetsResultSuccess)
        state = json.loads(preparation.state_path(LIBRARY_NAME, library_dir).read_text(encoding="utf-8"))
        assert state["status"] == WidgetPreparationStatus.PREPARED
        assert state["revision"] == "local"
        assert state["node_version"] == result.node_version
        assert state["npm_version"] == result.npm_version
        assert "updated_at" in state

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_the_build_gets_only_the_allowed_environment(
        self, engine: Engine, library_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GT_CLOUD_API_KEY", "secret-cloud-key")
        monkeypatch.setenv("GTN_SOMETHING", "secret-gtn-value")
        monkeypatch.setenv("OPENAI_API_KEY", "secret-openai-key")
        monkeypatch.setenv("NPM_CONFIG_USERCONFIG", "/home/someone-else/not-this-npmrc")

        result = await _prepare(engine)

        assert isinstance(result, PrepareLibraryWidgetsResultSuccess), result.result_details
        child_env = json.loads((library_dir / WIDGET_ROOT / "env.json").read_text(encoding="utf-8"))
        values = set(child_env.values())
        assert "secret-cloud-key" not in values
        assert "secret-gtn-value" not in values
        assert "secret-openai-key" not in values
        assert "/home/someone-else/not-this-npmrc" not in values
        assert "GT_CLOUD_API_KEY" not in child_env
        # npm puts the package's own bin directories in front; the configured node comes next.
        assert str(Path(NODE_PATH or "").parent) in child_env["PATH"].split(os.pathsep)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_a_lockfile_out_of_sync_fails_at_install(self, engine: Engine, library_dir: Path) -> None:
        package_dir = library_dir / WIDGET_ROOT
        _write_package(package_dir, dependencies={"left-pad": "1.3.0"})

        result = await _prepare(engine)

        refused = _assert_refused(result, WidgetPreparationFailureReason.BUILD_FAILED)
        assert refused.stage == "install"
        assert "left-pad" in refused.output
        assert not (package_dir / "dist").exists()
        assert not engine.library_manager.widget_preparation.is_prepared(LIBRARY_NAME, library_dir)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_a_failing_build_script_fails_at_build(self, engine: Engine, library_dir: Path) -> None:
        _write_package(library_dir / WIDGET_ROOT, build_js="console.error('nope'); process.exit(2);")

        result = await _prepare(engine)

        refused = _assert_refused(result, WidgetPreparationFailureReason.BUILD_FAILED)
        assert refused.stage == "build"
        assert "nope" in refused.output

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="checks the build process with POSIX signals")
    @pytest.mark.usefixtures("ready")
    async def test_a_build_that_runs_too_long_is_stopped(
        self, engine: Engine, library_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        package_dir = library_dir / WIDGET_ROOT
        _write_package(package_dir, build_js=HANGING_BUILD_JS)
        monkeypatch.setattr(widget_preparation_module, "STAGE_TIMEOUT_SECONDS", 3)

        result = await _prepare(engine)

        refused = _assert_refused(result, WidgetPreparationFailureReason.TIMED_OUT)
        assert refused.stage == "build"
        build_pid = int((package_dir / "build.pid").read_text(encoding="utf-8"))
        assert await _process_exits(build_pid)
        assert not engine.library_manager.widget_preparation.is_prepared(LIBRARY_NAME, library_dir)

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="checks the build process with POSIX signals")
    @pytest.mark.usefixtures("ready")
    async def test_cancelling_stops_the_build_and_reraises(self, engine: Engine, library_dir: Path) -> None:
        package_dir = library_dir / WIDGET_ROOT
        _write_package(package_dir, build_js=HANGING_BUILD_JS)
        preparation = engine.library_manager.widget_preparation
        task = asyncio.create_task(_prepare(engine))
        pid_file = package_dir / "build.pid"
        for _ in range(400):
            if pid_file.exists() and pid_file.read_text(encoding="utf-8"):
                break
            await asyncio.sleep(0.05)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _process_exits(int(pid_file.read_text(encoding="utf-8")))
        state = json.loads(preparation.state_path(LIBRARY_NAME, library_dir).read_text(encoding="utf-8"))
        assert state["status"] == WidgetPreparationStatus.FAILED
        assert state["stage"] == "build"
        assert not preparation.is_prepared(LIBRARY_NAME, library_dir)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_preparations_of_one_library_run_one_at_a_time(self, engine: Engine, library_dir: Path) -> None:
        _write_package(library_dir / WIDGET_ROOT, build_js=SLOW_BUILD_JS)

        results = await asyncio.gather(_prepare(engine), _prepare(engine))

        assert all(isinstance(result, PrepareLibraryWidgetsResultSuccess) for result in results)
        log = (library_dir / "builds.log").read_text(encoding="utf-8").split()
        assert log == ["start", "end", "start", "end"]


async def _process_exits(pid: int) -> bool:
    for _ in range(100):
        if not _process_is_running(pid):
            return True
        await asyncio.sleep(0.05)
    return False


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status_path = Path(f"/proc/{pid}/status")
    if status_path.exists():
        return "State:\tZ" not in status_path.read_text(encoding="utf-8")
    return True


@needs_node
class TestPreparedState:
    @pytest.mark.asyncio
    async def test_a_failed_update_after_a_success_is_not_prepared(
        self, engine: Engine, ready: Path, library_dir: Path
    ) -> None:
        preparation = engine.library_manager.widget_preparation
        assert isinstance(await _prepare(engine), PrepareLibraryWidgetsResultSuccess)
        _write_package(library_dir / WIDGET_ROOT, dependencies={"left-pad": "1.3.0"})

        result = await preparation.prepare_after_checkout(LIBRARY_NAME, ready, allow_build_execution=True)

        assert isinstance(result, PrepareLibraryWidgetsResultFailure)
        assert result.reason == WidgetPreparationFailureReason.BUILD_FAILED
        assert not preparation.is_prepared(LIBRARY_NAME, library_dir)

    @pytest.mark.asyncio
    async def test_a_checkout_without_permission_to_build_is_not_prepared(
        self, engine: Engine, ready: Path, library_dir: Path
    ) -> None:
        preparation = engine.library_manager.widget_preparation
        assert isinstance(await _prepare(engine), PrepareLibraryWidgetsResultSuccess)

        result = await preparation.prepare_after_checkout(LIBRARY_NAME, ready, allow_build_execution=False)

        assert isinstance(result, PrepareLibraryWidgetsResultFailure)
        assert result.reason == WidgetPreparationFailureReason.NOT_AUTHORIZED
        assert not preparation.is_prepared(LIBRARY_NAME, library_dir)
        state = json.loads(preparation.state_path(LIBRARY_NAME, library_dir).read_text(encoding="utf-8"))
        assert state["status"] == WidgetPreparationStatus.STALE

    @pytest.mark.asyncio
    async def test_a_checkout_that_drops_source_builds_returns_none_and_marks_stale(
        self, engine: Engine, ready: Path, library_dir: Path
    ) -> None:
        preparation = engine.library_manager.widget_preparation
        assert isinstance(await _prepare(engine), PrepareLibraryWidgetsResultSuccess)
        _write_manifest(library_dir, widget_build=None)

        result = await preparation.prepare_after_checkout(LIBRARY_NAME, ready, allow_build_execution=True)

        assert result is None
        assert not preparation.is_prepared(LIBRARY_NAME, library_dir)

    @pytest.mark.asyncio
    async def test_a_checkout_builds_when_allowed(self, engine: Engine, ready: Path, library_dir: Path) -> None:
        preparation = engine.library_manager.widget_preparation

        result = await preparation.prepare_after_checkout(None, ready, allow_build_execution=True)

        assert isinstance(result, PrepareLibraryWidgetsResultSuccess), result
        assert preparation.is_prepared(LIBRARY_NAME, library_dir)

    @pytest.mark.asyncio
    @needs_git
    @pytest.mark.usefixtures("ready")
    async def test_a_new_commit_is_not_prepared(self, engine: Engine, library_dir: Path) -> None:
        preparation = engine.library_manager.widget_preparation
        (library_dir / ".gitignore").write_text("node_modules/\ndist/\nenv.json\n", encoding="utf-8")
        _git(library_dir, "init", "-q")
        _git(library_dir, "add", ".")
        _git(library_dir, "commit", "-q", "-m", "first")

        result = await _prepare(engine)

        assert isinstance(result, PrepareLibraryWidgetsResultSuccess), result.result_details
        assert len(result.revision) == 40  # noqa: PLR2004
        assert preparation.is_prepared(LIBRARY_NAME, library_dir)

        _git(library_dir, "commit", "-q", "--allow-empty", "-m", "second")

        assert not preparation.is_prepared(LIBRARY_NAME, library_dir)

    def test_a_library_never_prepared_is_not_prepared(self, engine: Engine, library_dir: Path) -> None:
        assert not engine.library_manager.widget_preparation.is_prepared(LIBRARY_NAME, library_dir)


@needs_node
class TestNothingRunsWithoutPermission:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_an_unauthorised_request_starts_no_process(self, engine: Engine) -> None:
        """Valid tooling and the beta on still run nothing, not even `node --version`, until allowed."""
        with patch(f"{PREPARATION_MODULE}.run_process_tree", new=AsyncMock()) as run:
            result = await _prepare(engine, allow=False)

        _assert_refused(result, WidgetPreparationFailureReason.NOT_AUTHORIZED)
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unauthorised_checkout_starts_no_process(self, engine: Engine, ready: Path) -> None:
        preparation = engine.library_manager.widget_preparation
        with patch(f"{PREPARATION_MODULE}.run_process_tree", new=AsyncMock()) as run:
            result = await preparation.prepare_after_checkout(LIBRARY_NAME, ready, allow_build_execution=False)

        _assert_refused(result, WidgetPreparationFailureReason.NOT_AUTHORIZED)
        run.assert_not_called()


def _failing_state_replace(fail_on_call: int) -> object:
    """A Path.replace that fails on the `fail_on_call`-th state file write and passes the rest through."""
    original_replace = Path.replace
    calls = {"state": 0}

    def replace(self: Path, target: Path) -> Path:
        if self.name.endswith(".json.tmp"):
            calls["state"] += 1
            if calls["state"] == fail_on_call:
                msg = "No space left on device"
                raise OSError(msg)
        return original_replace(self, target)

    return replace


@needs_node
class TestStateThatCannotBeRecorded:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_failing_to_record_preparing_fails_before_npm_runs(self, engine: Engine) -> None:
        with (
            patch.object(Path, "replace", new=_failing_state_replace(fail_on_call=1)),
            patch(f"{PREPARATION_MODULE}.run_process_tree", wraps=run_process_tree) as run,
        ):
            result = await _prepare(engine)

        failure = _assert_refused(result, WidgetPreparationFailureReason.BUILD_FAILED)
        assert failure.stage == "state"
        npm_runs = [call for call in run.call_args_list if INSTALL_ARGS[0] in call.args[0]]
        assert npm_runs == []

    @pytest.mark.asyncio
    async def test_failing_to_mark_a_checkout_stale_fails_before_npm_runs(
        self, engine: Engine, ready: Path, library_dir: Path
    ) -> None:
        preparation = engine.library_manager.widget_preparation
        assert isinstance(await _prepare(engine), PrepareLibraryWidgetsResultSuccess)

        with (
            patch.object(Path, "replace", new=_failing_state_replace(fail_on_call=1)),
            patch(f"{PREPARATION_MODULE}.run_process_tree", new=AsyncMock()) as run,
        ):
            result = await preparation.prepare_after_checkout(LIBRARY_NAME, ready, allow_build_execution=True)

        failure = _assert_refused(result, WidgetPreparationFailureReason.BUILD_FAILED)
        assert failure.stage == "state"
        run.assert_not_called()
        assert preparation.state_path(LIBRARY_NAME, library_dir).exists()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_failing_to_record_prepared_is_a_failure(self, engine: Engine, library_dir: Path) -> None:
        preparation = engine.library_manager.widget_preparation
        with patch.object(Path, "replace", new=_failing_state_replace(fail_on_call=2)):
            result = await _prepare(engine)

        failure = _assert_refused(result, WidgetPreparationFailureReason.BUILD_FAILED)
        assert failure.stage == "state"
        assert not preparation.is_prepared(LIBRARY_NAME, library_dir)


@needs_node
class TestCopiesOfOneLibrary:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_a_second_copy_leaves_the_state_of_the_first_alone(
        self, engine: Engine, library_dir: Path, tmp_path: Path
    ) -> None:
        preparation = engine.library_manager.widget_preparation
        assert isinstance(await _prepare(engine), PrepareLibraryWidgetsResultSuccess)
        copy_dir = tmp_path / "copy_of_widget_library"
        copy_manifest = _write_manifest(copy_dir, widget_build=WidgetBuildMetadata(mode="source", root=WIDGET_ROOT))
        _write_package(copy_dir / WIDGET_ROOT)

        result = await preparation.prepare_after_checkout(LIBRARY_NAME, copy_manifest, allow_build_execution=False)

        _assert_refused(result, WidgetPreparationFailureReason.NOT_AUTHORIZED)
        assert preparation.state_path(LIBRARY_NAME, copy_dir) != preparation.state_path(LIBRARY_NAME, library_dir)
        assert preparation.is_prepared(LIBRARY_NAME, library_dir)
        assert not preparation.is_prepared(LIBRARY_NAME, copy_dir)


@needs_node
class TestNodeModulesContainment:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_node_modules_linked_outside_the_package_is_refused(
        self, engine: Engine, library_dir: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        (library_dir / WIDGET_ROOT / "node_modules").symlink_to(outside, target_is_directory=True)

        with patch(f"{PREPARATION_MODULE}.run_process_tree", new=AsyncMock()) as run:
            result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.INVALID_MANIFEST)
        run.assert_not_called()
        assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_node_modules_linked_inside_the_package_is_refused(self, engine: Engine, library_dir: Path) -> None:
        package_dir = library_dir / WIDGET_ROOT
        (package_dir / "elsewhere").mkdir()
        (package_dir / "node_modules").symlink_to(package_dir / "elsewhere", target_is_directory=True)

        result = await _prepare(engine)

        _assert_refused(result, WidgetPreparationFailureReason.INVALID_MANIFEST)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("ready")
    async def test_an_existing_node_modules_directory_is_fine(self, engine: Engine, library_dir: Path) -> None:
        (library_dir / WIDGET_ROOT / "node_modules").mkdir()

        result = await _prepare(engine)

        assert isinstance(result, PrepareLibraryWidgetsResultSuccess), result.result_details


class TestChildEnvironment:
    def test_keeps_only_allowed_variables_and_puts_node_first_on_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", "/home/someone")
        monkeypatch.setenv("https_proxy", "http://proxy:3128")
        monkeypatch.setenv("GT_CLOUD_API_KEY", "secret")
        monkeypatch.setenv("GTN_CONFIG_LIBRARY__PROVISIONED_BY", "engine")
        monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://registry.example.com")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setenv("PATH", "/original/bin")
        node = tmp_path / "bin" / "node"

        env = build_child_environment(node)

        assert env["HOME"] == "/home/someone"
        assert env["https_proxy"] == "http://proxy:3128"
        assert env["PATH"] == f"{node.parent}{os.pathsep}/original/bin"
        assert env["npm_config_update_notifier"] == "false"
        assert env["npm_config_fund"] == "false"
        assert env["npm_config_audit"] == "false"
        assert "GT_CLOUD_API_KEY" not in env
        assert "GTN_CONFIG_LIBRARY__PROVISIONED_BY" not in env
        assert "NPM_CONFIG_REGISTRY" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env


class TestCatalogPublishesWidgets:
    @pytest.fixture(autouse=True)
    def _static_server(self, engine: Engine) -> Generator[None, None, None]:
        with patch.object(
            type(engine.static_files_manager),
            "static_server_base_url",
            new_callable=PropertyMock,
            return_value="http://localhost:8124",
        ):
            yield

    def _widgets(self, engine: Engine, widget_build: WidgetBuildMetadata | None, path: str = WIDGET_PATH) -> list:
        widget_defs = [WidgetDefinition(name="Example", path=path, description="example widget")]
        return engine.library_manager.catalog._build_widget_info(LIBRARY_NAME, widget_defs, widget_build)

    def _write_bundle(self, library_dir: Path) -> None:
        bundle = library_dir / WIDGET_PATH
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text("export default 1;\n", encoding="utf-8")

    @pytest.mark.usefixtures("manifest")
    def test_a_prebuilt_widget_is_published(self, engine: Engine, library_dir: Path) -> None:
        self._write_bundle(library_dir)

        widgets = self._widgets(engine, WidgetBuildMetadata(mode="prebuilt", root=WIDGET_ROOT))

        assert [widget.name for widget in widgets] == ["Example"]
        assert "?v=" in widgets[0].bundle_url

    @pytest.mark.usefixtures("manifest")
    def test_a_widget_of_a_library_without_widget_build_is_published(self, engine: Engine, library_dir: Path) -> None:
        self._write_bundle(library_dir)

        widgets = self._widgets(engine, None)

        assert [widget.name for widget in widgets] == ["Example"]

    @pytest.mark.usefixtures("manifest")
    def test_a_missing_bundle_is_withheld(self, engine: Engine) -> None:
        assert self._widgets(engine, None) == []

    @pytest.mark.usefixtures("manifest")
    def test_a_bundle_outside_the_library_is_withheld(self, engine: Engine, tmp_path: Path) -> None:
        (tmp_path / "outside.js").write_text("export default 1;\n", encoding="utf-8")

        assert self._widgets(engine, None, path="../outside.js") == []

    @pytest.mark.usefixtures("manifest")
    def test_a_bundle_that_links_outside_the_library_is_withheld(
        self, engine: Engine, library_dir: Path, tmp_path: Path
    ) -> None:
        (tmp_path / "outside.js").write_text("export default 1;\n", encoding="utf-8")
        (library_dir / "linked.js").symlink_to(tmp_path / "outside.js")

        assert self._widgets(engine, None, path="linked.js") == []

    @pytest.mark.usefixtures("manifest")
    def test_an_unprepared_source_widget_is_withheld(self, engine: Engine, library_dir: Path) -> None:
        self._write_bundle(library_dir)

        assert self._widgets(engine, WidgetBuildMetadata(mode="source", root=WIDGET_ROOT)) == []

    @pytest.mark.usefixtures("manifest")
    def test_a_prepared_source_widget_is_published(self, engine: Engine, library_dir: Path) -> None:
        self._write_bundle(library_dir)
        preparation = engine.library_manager.widget_preparation

        with patch.object(preparation, "is_prepared", return_value=True) as is_prepared:
            widgets = self._widgets(engine, WidgetBuildMetadata(mode="source", root=WIDGET_ROOT))

        assert [widget.name for widget in widgets] == ["Example"]
        is_prepared.assert_called_once_with(LIBRARY_NAME, library_dir)

    @pytest.mark.usefixtures("manifest")
    def test_publishing_does_not_consult_the_beta_or_the_tooling(self, engine: Engine, library_dir: Path) -> None:
        """A build that already ran stays published after the beta is turned off; only `is_prepared` decides."""
        self._write_bundle(library_dir)
        preparation = engine.library_manager.widget_preparation

        with patch.object(preparation, "is_prepared", return_value=True):
            widgets = self._widgets(engine, WidgetBuildMetadata(mode="source", root=WIDGET_ROOT))

        assert len(widgets) == 1


class TestNothingElseBuilds:
    @pytest.mark.asyncio
    async def test_registering_and_reloading_never_prepare_widgets(
        self, engine: Engine, ready: Path, library_dir: Path
    ) -> None:
        """Python nodes load as before, and nothing but an explicit preparation or checkout runs npm."""
        library_manager = engine.library_manager
        library_manager._library_file_path_to_info.clear()
        preparation = library_manager.widget_preparation
        with (
            patch.object(preparation, "prepare_after_checkout", new=AsyncMock()) as after_checkout,
            patch.object(widget_preparation_module, "run_process_tree", new=AsyncMock()) as run,
        ):
            registered = await library_manager.registration.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path=str(ready))
            )
            assert isinstance(registered, RegisterLibraryFromFileResultSuccess), registered.result_details
            reloaded = await library_manager.git_operations.reload_library(
                LIBRARY_NAME, str(ready), failure_result_class=UpdateLibraryResultFailure
            )

        assert reloaded == "1.0.0"
        after_checkout.assert_not_called()
        run.assert_not_called()
        assert "WidgetLibraryNode" in LibraryRegistry.get_library(LIBRARY_NAME).get_registered_nodes()
        assert not (library_dir / WIDGET_ROOT / "node_modules").exists()
