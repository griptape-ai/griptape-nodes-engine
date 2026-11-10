"""Preparing a library's widgets: building the TypeScript source a library ships into bundles.

A library can ship its custom widgets as source instead of as built bundles, by setting
`metadata.widget_build.mode` to "source" and pointing `metadata.widget_build.root` at an npm
package inside the library. Preparing builds that package with `npm ci` and the package's own
`build` script. Until a preparation succeeds for the library's current revision, the catalog
publishes none of the library's widgets and the editor shows the normal parameter controls.
Libraries that ship built bundles, and every library's Python nodes, are unaffected.

A build runs code from the library and from every package it installs, on this computer, with the
user's permissions. So it runs only when all of these hold, checked in this order:

1. The engine provisions its own libraries. An environment that provides them builds nothing.
2. The "Library widget builds" beta feature is on.
3. The user set both `widget_tooling.node_executable` and `widget_tooling.npm_executable`. The
   engine never looks Node.js up on PATH and never downloads it.
4. Both are absolute paths to existing files, and they report Node.js 20 or later and npm 10 or
   later.
5. The request says the user agreed to run the build (`allow_build_execution`). Neither the beta
   feature nor the configured tools imply it.
6. The widget package is complete and stays inside the library, symlinks included.

Every refusal happens before anything runs. The build itself installs with `--ignore-scripts`, so
no dependency's install script runs, and runs `npm run build --ignore-scripts`, so the package's
`prebuild` and `postbuild` do not run either. Both commands run as the configured node executing
npm's own JavaScript entry point, without a shell, in a new process group, with an environment
built from an allowlist: no credentials or engine settings reach them. A stage that runs past its
timeout, or a cancelled preparation, stops the whole process group.

The outcome is recorded per library under the engine's state directory, together with the
revision it was built from: the git HEAD commit, or "local" for a library that is not a git
checkout. `is_prepared` trusts only a successful record for the revision on disk now. A git change
marks the record stale before anything else, so a later refusal or failure leaves the widgets
withheld rather than publishing a build of other source. Edits to a local checkout without a new
commit are not detected.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from griptape_nodes.files.path_utils import canonicalize_for_identity
from griptape_nodes.retained_mode.beta_features import WIDGET_SOURCE_BUILDS, is_beta_enabled, library_config_slug
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    LoadLibraryMetadataFromFileRequest,
    LoadLibraryMetadataFromFileResultSuccess,
    PrepareLibraryWidgetsRequest,
    PrepareLibraryWidgetsResultFailure,
    PrepareLibraryWidgetsResultSuccess,
    WidgetPreparationFailureReason,
)
from griptape_nodes.retained_mode.managers.library.managed_environment import LibrariesProvidedByEnvironmentError
from griptape_nodes.retained_mode.managers.settings import (
    WIDGET_TOOLING_NODE_EXECUTABLE_KEY,
    WIDGET_TOOLING_NPM_EXECUTABLE_KEY,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.async_utils import run_process_tree
from griptape_nodes.utils.engine_dirs import engine_state_dir
from griptape_nodes.utils.git_utils import GitError, get_local_commit_sha

if TYPE_CHECKING:
    from griptape_nodes.node_library.library_registry import LibrarySchema
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")

# Seconds each of `npm ci` and the build may run before the preparation stops it. Provisional: a
# first guess, to revisit once real widget packages have been timed.
STAGE_TIMEOUT_SECONDS = 600
# Seconds `node --version` and `npm --version` may take.
VERSION_CHECK_TIMEOUT_SECONDS = 30
# How many trailing characters of npm's output a result carries.
OUTPUT_LIMIT_CHARS = 20_000
MINIMUM_NODE_MAJOR = 20
MINIMUM_NPM_MAJOR = 10
# The revision recorded for a library that is not a git checkout.
LOCAL_REVISION = "local"
STATE_DIR_NAME = "widget_preparation"

INSTALL_STAGE = "install"
BUILD_STAGE = "build"
INSTALL_ARGS = ("ci", "--include=dev", "--include=optional", "--ignore-scripts")
BUILD_ARGS = ("run", "build", "--ignore-scripts")

PACKAGE_JSON = "package.json"
PACKAGE_LOCK_JSON = "package-lock.json"
NPM_CLI_SUFFIXES = frozenset({".js", ".cjs"})
# Where npm's entry point sits relative to the npm the user configured: next to it in a Windows
# install, and under ../lib in a POSIX one.
NPM_CLI_CANDIDATES = (
    Path("node_modules") / "npm" / "bin" / "npm-cli.js",
    Path("..") / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
)

# The only variables a build inherits from the engine. Names are matched exactly; on Windows
# `os.environ` reports them in upper case.
CHILD_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "SYSTEMROOT",
        "SystemRoot",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    }
)
# Set for every build, so npm neither phones home nor prints prompts into the captured output.
CHILD_ENV_OVERRIDES = {
    "npm_config_update_notifier": "false",
    "npm_config_fund": "false",
    "npm_config_audit": "false",
}

_VERSION_PATTERN = re.compile(r"^v?(\d+)\.\d+")


class WidgetPreparationStatus(StrEnum):
    """What the last preparation of a library's widgets left behind."""

    # A preparation is running.
    PREPARING = "preparing"
    # The widgets were built from the recorded revision.
    PREPARED = "prepared"
    # The last preparation failed, timed out, or was cancelled.
    FAILED = "failed"
    # The library's files changed since the last preparation.
    STALE = "stale"


@dataclass(frozen=True)
class ConfiguredTooling:
    """The Node.js and npm paths from settings, before they are checked."""

    node: Path
    npm: Path


@dataclass(frozen=True)
class WidgetTooling:
    """A Node.js and npm that passed every check, with the versions they reported."""

    node: Path
    npm_cli: Path
    node_version: str
    npm_version: str


def build_child_environment(node_executable: Path) -> dict[str, str]:
    """The environment a widget build runs with.

    Only the variables in CHILD_ENV_ALLOWLIST pass through, so no credential, engine setting
    (`GT_*`, `GTN_*`), or npm setting (`NPM_CONFIG_*`) reaches the build. PATH is the configured
    node's directory followed by the engine's PATH, so the build script's own `node` is the
    configured one.
    """
    env = {name: value for name, value in os.environ.items() if name in CHILD_ENV_ALLOWLIST}
    original_path = os.environ.get("PATH", "")
    if original_path:
        env["PATH"] = f"{node_executable.parent}{os.pathsep}{original_path}"
    else:
        env["PATH"] = str(node_executable.parent)
    env.update(CHILD_ENV_OVERRIDES)
    return env


def resolve_npm_cli(npm_executable: Path) -> Path | None:
    """Find the JavaScript entry point of the npm the user configured, or None.

    The configured path may be npm-cli.js itself, a link to it, or the `npm` launcher next to an
    npm install. Running the entry point under the configured node avoids the launcher scripts,
    which on Windows need a shell.
    """
    if npm_executable.suffix in NPM_CLI_SUFFIXES:
        return npm_executable

    resolved = npm_executable.resolve()
    if resolved.suffix in NPM_CLI_SUFFIXES and resolved.is_file():
        return resolved

    for candidate in NPM_CLI_CANDIDATES:
        candidate_path = npm_executable.parent / candidate
        if candidate_path.is_file():
            return candidate_path.resolve()
    return None


class LibraryWidgetPreparation(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        # One lock per library name, so two preparations of one library never share its files.
        self._locks: dict[str, asyncio.Lock] = {}
        event_manager.register_request_handlers(self)

    @handles(PrepareLibraryWidgetsRequest)
    async def prepare_library_widgets_request(self, request: PrepareLibraryWidgetsRequest) -> ResultPayload:
        library_name = request.library_name
        library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        if library_info is None:
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.LIBRARY_NOT_FOUND,
                "no Library with that name was found",
            )
        if library_info.is_sandbox:
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.NOT_SOURCE_MODE,
                "the sandbox library has no widget package to build",
            )

        async with self._lock_for(library_name):
            return await self._prepare(
                library_name,
                Path(library_info.library_path),
                allow_build_execution=request.allow_build_execution,
            )

    async def prepare_after_checkout(
        self,
        library_name: str | None,
        library_json_path: Path,
        *,
        allow_build_execution: bool,
    ) -> PrepareLibraryWidgetsResultSuccess | PrepareLibraryWidgetsResultFailure | None:
        """Prepare a library's widgets after git changed its files, before the library loads again.

        Marks the earlier preparation stale first, so whatever happens next, widgets built from
        other source are no longer published. Then runs the same gates and build as a
        PrepareLibraryWidgetsRequest.

        Args:
            library_name: The library's name before the change, or None for a fresh download
            library_json_path: The library JSON now on disk
            allow_build_execution: Whether the user agreed to run the library's build tools

        Returns:
            None when the library does not build its widgets from source; otherwise the result of
            preparing them. Never raises for a refusal or a failed build.
        """
        schema = self._read_schema(library_json_path)
        names = {name for name in (library_name, schema.name if schema is not None else None) if name}
        if not names:
            return None
        # The library loads again under the name in its manifest, which is what the catalog asks
        # about, so the state is kept under that name.
        state_name = schema.name if schema is not None else next(iter(names))

        async with self._lock_for(state_name):
            for name in names:
                self._mark_stale(name)
            if schema is None or not self._builds_from_source(schema):
                return None
            return await self._prepare(state_name, library_json_path, allow_build_execution=allow_build_execution)

    def is_prepared(self, library_name: str, library_dir: Path) -> bool:
        """Whether the library's widgets were built from the revision now on disk.

        Blocking: may run git. Callers on the event loop run this in a thread.
        """
        state = self._read_state(library_name)
        if state is None:
            return False
        if state.get("status") != WidgetPreparationStatus.PREPARED:
            return False
        if state.get("library_name") != library_name:
            return False
        if state.get("library_dir") != str(canonicalize_for_identity(library_dir)):
            return False
        return state.get("revision") == self._revision(library_dir)

    def state_path(self, library_name: str) -> Path:
        """Where the outcome of preparing this library's widgets is recorded."""
        name_hash = hashlib.sha256(library_name.encode("utf-8")).hexdigest()[:8]
        slug = library_config_slug(library_name)
        file_stem = f"{slug}-{name_hash}" if slug else name_hash
        return engine_state_dir() / STATE_DIR_NAME / f"{file_stem}.json"

    async def _prepare(  # noqa: PLR0911 (one early return per gate, in order)
        self, library_name: str, library_json_path: Path, *, allow_build_execution: bool
    ) -> PrepareLibraryWidgetsResultSuccess | PrepareLibraryWidgetsResultFailure:
        """Run every gate in order, then the build. The caller holds the library's lock."""
        managed = self.engine.library_manager.managed_environment
        if managed.provisioned_by_environment():
            return PrepareLibraryWidgetsResultFailure(
                library_name=library_name,
                reason=WidgetPreparationFailureReason.MANAGED_ENVIRONMENT,
                result_details=managed.environment_provides_libraries_message(
                    f"build the widgets of Library '{library_name}'"
                ),
            )

        if not is_beta_enabled(WIDGET_SOURCE_BUILDS, self.engine.config_manager):
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.BETA_DISABLED,
                f"the '{WIDGET_SOURCE_BUILDS.name}' beta feature is off. Turn it on under Beta Features in Settings",
            )

        configured = self._configured_tooling(library_name)
        if isinstance(configured, PrepareLibraryWidgetsResultFailure):
            return configured

        tooling = await self._validate_tooling(library_name, configured)
        if isinstance(tooling, PrepareLibraryWidgetsResultFailure):
            return tooling

        if not allow_build_execution:
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.NOT_AUTHORIZED,
                "building runs the library's own build tools on this computer, and that was not allowed",
            )

        package_dir = self._widget_package_dir(library_name, library_json_path)
        if isinstance(package_dir, PrepareLibraryWidgetsResultFailure):
            return package_dir

        # The check above is the one users see; this one holds even if a caller skipped it.
        try:
            managed.ensure_engine_provisions(f"build the widgets of Library '{library_name}'")
        except LibrariesProvidedByEnvironmentError as e:
            return PrepareLibraryWidgetsResultFailure(
                library_name=library_name,
                reason=WidgetPreparationFailureReason.MANAGED_ENVIRONMENT,
                result_details=str(e),
            )

        library_dir = library_json_path.parent
        revision = await asyncio.to_thread(self._revision, library_dir)
        return await self._build(library_name, library_dir, package_dir, tooling, revision)

    async def _build(
        self,
        library_name: str,
        library_dir: Path,
        package_dir: Path,
        tooling: WidgetTooling,
        revision: str,
    ) -> PrepareLibraryWidgetsResultSuccess | PrepareLibraryWidgetsResultFailure:
        """Run `npm ci` and then the build script, recording the outcome."""
        self._write_state(
            library_name=library_name,
            library_dir=library_dir,
            status=WidgetPreparationStatus.PREPARING,
            revision=revision,
            stage=None,
            tooling=tooling,
        )
        env = build_child_environment(tooling.node)
        outputs: list[str] = []
        for stage, npm_args in ((INSTALL_STAGE, INSTALL_ARGS), (BUILD_STAGE, BUILD_ARGS)):
            command = [str(tooling.node), str(tooling.npm_cli), *npm_args]
            logger.info("Building the widgets of Library '%s': running %s in %s", library_name, command, package_dir)
            try:
                result = await run_process_tree(
                    command,
                    cwd=package_dir,
                    env=env,
                    timeout=STAGE_TIMEOUT_SECONDS,
                    output_limit=OUTPUT_LIMIT_CHARS,
                )
            except asyncio.CancelledError:
                self._write_state(
                    library_name=library_name,
                    library_dir=library_dir,
                    status=WidgetPreparationStatus.FAILED,
                    revision=revision,
                    stage=stage,
                    tooling=tooling,
                )
                raise
            except OSError as e:
                self._write_state(
                    library_name=library_name,
                    library_dir=library_dir,
                    status=WidgetPreparationStatus.FAILED,
                    revision=revision,
                    stage=stage,
                    tooling=tooling,
                )
                return self._failure(
                    library_name,
                    WidgetPreparationFailureReason.BUILD_FAILED,
                    f"'{' '.join(npm_args)}' could not be started: {e}",
                    stage=stage,
                )

            if result.timed_out:
                self._write_state(
                    library_name=library_name,
                    library_dir=library_dir,
                    status=WidgetPreparationStatus.FAILED,
                    revision=revision,
                    stage=stage,
                    tooling=tooling,
                )
                return self._failure(
                    library_name,
                    WidgetPreparationFailureReason.TIMED_OUT,
                    f"'npm {' '.join(npm_args)}' ran longer than {STAGE_TIMEOUT_SECONDS} seconds and was stopped",
                    stage=stage,
                    output=result.output,
                )
            if result.returncode != 0:
                self._write_state(
                    library_name=library_name,
                    library_dir=library_dir,
                    status=WidgetPreparationStatus.FAILED,
                    revision=revision,
                    stage=stage,
                    tooling=tooling,
                )
                return self._failure(
                    library_name,
                    WidgetPreparationFailureReason.BUILD_FAILED,
                    f"'npm {' '.join(npm_args)}' exited with code {result.returncode}. Its output is in the result",
                    stage=stage,
                    output=result.output,
                )
            outputs.append(result.output)

        self._write_state(
            library_name=library_name,
            library_dir=library_dir,
            status=WidgetPreparationStatus.PREPARED,
            revision=revision,
            stage=None,
            tooling=tooling,
        )
        details = f"Built the widgets of Library '{library_name}' (revision {revision})."
        return PrepareLibraryWidgetsResultSuccess(
            library_name=library_name,
            revision=revision,
            node_version=tooling.node_version,
            npm_version=tooling.npm_version,
            output="\n".join(outputs)[-OUTPUT_LIMIT_CHARS:],
            result_details=ResultDetails(message=details, level=logging.INFO),
        )

    def _configured_tooling(self, library_name: str) -> ConfiguredTooling | PrepareLibraryWidgetsResultFailure:
        """Read both tool paths from settings. A missing or blank value counts as unset."""
        config = self.engine.config_manager
        values = {
            key: config.get_config_value(key, should_load_env_var_if_detected=False)
            for key in (WIDGET_TOOLING_NODE_EXECUTABLE_KEY, WIDGET_TOOLING_NPM_EXECUTABLE_KEY)
        }
        unset = [
            key for key, value in values.items() if value is None or (isinstance(value, str) and not value.strip())
        ]
        if unset:
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.TOOLING_NOT_CONFIGURED,
                f"{' and '.join(unset)} {'is' if len(unset) == 1 else 'are'} not set. Set both to the absolute "
                f"paths of Node.js {MINIMUM_NODE_MAJOR}+ and npm {MINIMUM_NPM_MAJOR}+ in Settings",
            )

        not_text = [key for key, value in values.items() if not isinstance(value, str)]
        if not_text:
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.TOOLING_INVALID,
                f"{' and '.join(not_text)} must be a path, not {', '.join(repr(values[key]) for key in not_text)}",
            )

        return ConfiguredTooling(
            node=Path(values[WIDGET_TOOLING_NODE_EXECUTABLE_KEY]),
            npm=Path(values[WIDGET_TOOLING_NPM_EXECUTABLE_KEY]),
        )

    async def _validate_tooling(
        self, library_name: str, configured: ConfiguredTooling
    ) -> WidgetTooling | PrepareLibraryWidgetsResultFailure:
        """Check that both paths are usable files and report new enough versions."""
        for key, path in (
            (WIDGET_TOOLING_NODE_EXECUTABLE_KEY, configured.node),
            (WIDGET_TOOLING_NPM_EXECUTABLE_KEY, configured.npm),
        ):
            problem = self._path_problem(path)
            if problem is not None:
                return self._failure(
                    library_name, WidgetPreparationFailureReason.TOOLING_INVALID, f"{key} is '{path}', which {problem}"
                )

        npm_cli = await asyncio.to_thread(resolve_npm_cli, configured.npm)
        if npm_cli is None:
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.TOOLING_INVALID,
                f"npm's entry point (npm-cli.js) could not be found from {WIDGET_TOOLING_NPM_EXECUTABLE_KEY} "
                f"'{configured.npm}'. Set it to the npm inside a Node.js install, or to its npm-cli.js",
            )

        node_version = await self._tool_version(configured.node, [str(configured.node), "--version"])
        node_problem = self._version_problem("Node.js", node_version, MINIMUM_NODE_MAJOR)
        if node_problem is not None:
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.TOOLING_INVALID,
                f"{WIDGET_TOOLING_NODE_EXECUTABLE_KEY} '{configured.node}' {node_problem}",
            )

        npm_version = await self._tool_version(configured.node, [str(configured.node), str(npm_cli), "--version"])
        npm_problem = self._version_problem("npm", npm_version, MINIMUM_NPM_MAJOR)
        if npm_problem is not None:
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.TOOLING_INVALID,
                f"{WIDGET_TOOLING_NPM_EXECUTABLE_KEY} '{configured.npm}' {npm_problem}",
            )

        return WidgetTooling(
            node=configured.node,
            npm_cli=npm_cli,
            node_version=node_version or "",
            npm_version=npm_version or "",
        )

    def _widget_package_dir(  # noqa: PLR0911 (one early return per manifest problem)
        self, library_name: str, library_json_path: Path
    ) -> Path | PrepareLibraryWidgetsResultFailure:
        """Check the manifest's widget package and return its directory."""
        schema = self._read_schema(library_json_path)
        if schema is None:
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.INVALID_MANIFEST,
                f"its library JSON at '{library_json_path}' could not be read",
            )
        widget_build = schema.metadata.widget_build
        if widget_build is None or widget_build.mode != "source":
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.NOT_SOURCE_MODE,
                'the Library does not build its widgets from source (metadata.widget_build.mode is not "source")',
            )

        library_dir = canonicalize_for_identity(library_json_path.parent)
        if Path(widget_build.root).is_absolute():
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.INVALID_MANIFEST,
                f"metadata.widget_build.root '{widget_build.root}' must be relative to the library JSON",
            )
        package_dir = canonicalize_for_identity(library_json_path.parent / widget_build.root)
        if not package_dir.is_relative_to(library_dir):
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.INVALID_MANIFEST,
                f"metadata.widget_build.root '{widget_build.root}' points outside the Library's directory",
            )
        if not package_dir.is_dir():
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.INVALID_MANIFEST,
                f"metadata.widget_build.root '{widget_build.root}' is not a directory",
            )

        for file_name in (PACKAGE_JSON, PACKAGE_LOCK_JSON):
            file_path = package_dir / file_name
            if not file_path.is_file() or not canonicalize_for_identity(file_path).is_relative_to(library_dir):
                return self._failure(
                    library_name,
                    WidgetPreparationFailureReason.INVALID_MANIFEST,
                    f"the widget package at '{widget_build.root}' has no {file_name} inside the Library",
                )

        build_script = self._build_script(package_dir / PACKAGE_JSON)
        if build_script is None:
            return self._failure(
                library_name,
                WidgetPreparationFailureReason.INVALID_MANIFEST,
                f"the {PACKAGE_JSON} at '{widget_build.root}' has no \"build\" script",
            )
        return package_dir

    def _read_schema(self, library_json_path: Path) -> LibrarySchema | None:
        metadata_result = self.engine.library_manager.metadata_loading.read_library_metadata(
            LoadLibraryMetadataFromFileRequest(file_path=str(library_json_path))
        )
        if not isinstance(metadata_result, LoadLibraryMetadataFromFileResultSuccess):
            logger.debug(
                "Could not read %s for widget preparation: %s", library_json_path, metadata_result.result_details
            )
            return None
        return metadata_result.library_schema

    def _lock_for(self, library_name: str) -> asyncio.Lock:
        return self._locks.setdefault(library_name, asyncio.Lock())

    def _mark_stale(self, library_name: str) -> None:
        state = self._read_state(library_name)
        if state is None:
            return
        state["status"] = WidgetPreparationStatus.STALE.value
        state["updated_at"] = datetime.now(tz=UTC).isoformat()
        self._write_state_file(library_name, state)

    def _write_state(  # noqa: PLR0913 (one argument per recorded field)
        self,
        *,
        library_name: str,
        library_dir: Path,
        status: WidgetPreparationStatus,
        revision: str,
        stage: str | None,
        tooling: WidgetTooling,
    ) -> None:
        state = {
            "library_name": library_name,
            "library_dir": str(canonicalize_for_identity(library_dir)),
            "status": status.value,
            "revision": revision,
            "stage": stage,
            "node_version": tooling.node_version,
            "npm_version": tooling.npm_version,
            "updated_at": datetime.now(tz=UTC).isoformat(),
        }
        self._write_state_file(library_name, state)

    def _write_state_file(self, library_name: str, state: dict[str, Any]) -> None:
        """Replace the state file in one step, so a reader never sees half of it."""
        state_path = self.state_path(library_name)
        temp_path = state_path.with_suffix(".json.tmp")
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            temp_path.replace(state_path)
        except OSError as e:
            logger.error(
                "Attempted to record the widget preparation state of Library '%s' in %s. Failed due to: %s",
                library_name,
                state_path,
                e,
            )

    def _read_state(self, library_name: str) -> dict[str, Any] | None:
        state_path = self.state_path(library_name)
        if not state_path.exists():
            return None
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning(
                "Attempted to read the widget preparation state of Library '%s' from %s. Failed due to: %s",
                library_name,
                state_path,
                e,
            )
            return None
        if not isinstance(state, dict):
            return None
        return state

    async def _tool_version(self, node: Path, command: list[str]) -> str | None:
        """What a version command printed, or None when it could not run or failed."""
        try:
            result = await run_process_tree(
                command,
                cwd=node.parent,
                env=build_child_environment(node),
                timeout=VERSION_CHECK_TIMEOUT_SECONDS,
            )
        except OSError as e:
            logger.debug("Attempted to run %s. Failed due to: %s", command, e)
            return None
        if result.timed_out or result.returncode != 0:
            logger.debug("Version check %s failed: %s", command, result.output)
            return None
        lines = result.output.strip().splitlines()
        if not lines:
            return None
        return lines[-1].strip()

    def _failure(
        self,
        library_name: str,
        reason: WidgetPreparationFailureReason,
        because: str,
        *,
        stage: str | None = None,
        output: str = "",
    ) -> PrepareLibraryWidgetsResultFailure:
        details = f"Attempted to build the widgets of Library '{library_name}'. Failed due to: {because}."
        return PrepareLibraryWidgetsResultFailure(
            library_name=library_name,
            reason=reason,
            stage=stage,
            output=output,
            result_details=details,
        )

    @staticmethod
    def _builds_from_source(schema: LibrarySchema) -> bool:
        widget_build = schema.metadata.widget_build
        return widget_build is not None and widget_build.mode == "source"

    @staticmethod
    def _revision(library_dir: Path) -> str:
        """The git HEAD commit of the library, or LOCAL_REVISION when it is not a git checkout."""
        try:
            commit_sha = get_local_commit_sha(library_dir)
        except GitError as e:
            logger.debug("Could not read the git revision of %s: %s", library_dir, e)
            return LOCAL_REVISION
        if commit_sha is None:
            return LOCAL_REVISION
        return commit_sha

    @staticmethod
    def _path_problem(path: Path) -> str | None:
        if not path.is_absolute():
            return "is not an absolute path"
        if not path.exists():
            return "does not exist"
        if not path.is_file():
            return "is not a file"
        return None

    @staticmethod
    def _version_problem(tool: str, version: str | None, minimum_major: int) -> str | None:
        if version is None:
            return f"did not report a {tool} version"
        match = _VERSION_PATTERN.match(version)
        if match is None:
            return f"reported '{version}', which is not a {tool} version"
        if int(match.group(1)) < minimum_major:
            return f"is {tool} {version}; {tool} {minimum_major} or later is required"
        return None

    @staticmethod
    def _build_script(package_json_path: Path) -> str | None:
        """The package's non-empty `build` script, or None."""
        try:
            package = json.loads(package_json_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.debug("Could not read %s: %s", package_json_path, e)
            return None
        scripts = package.get("scripts") if isinstance(package, dict) else None
        build_script = scripts.get("build") if isinstance(scripts, dict) else None
        if not isinstance(build_script, str) or not build_script.strip():
            return None
        return build_script
