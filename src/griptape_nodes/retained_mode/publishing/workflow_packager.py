"""Shared packaging utilities for workflow publishers.

Extracted from the duplicated code across LocalPublisher, GriptapeCloudPublisher,
and NukeGizmoPublisher. Any library-specific publisher can compose with this class
to get standard bundling behavior (copy libraries, write .env, write config, etc.)
without reimplementing these utilities.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, NoReturn
from urllib.parse import urlparse
from urllib.request import url2pathname

from dotenv.main import DotEnv

from griptape_nodes.common.macro_parser import MacroSyntaxError, ParsedMacro
from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.param_components.huggingface.huggingface_model_parameter import HuggingFaceModelParameter
from griptape_nodes.files.path_utils import (
    canonicalize_for_identity,
    resolve_path_safely,
    strip_windows_long_path_prefix,
)
from griptape_nodes.node_library.library_registry import LibraryNameAndVersion, LibraryRegistry
from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
from griptape_nodes.retained_mode.events.app_events import (
    GetEngineVersionRequest,
    GetEngineVersionResultSuccess,
)
from griptape_nodes.retained_mode.events.base_events import (
    ExecutionEvent,
    ExecutionGriptapeNodeEvent,
)
from griptape_nodes.retained_mode.events.flow_events import GetTopLevelFlowRequest, GetTopLevelFlowResultSuccess
from griptape_nodes.retained_mode.events.os_events import (
    CopyFileRequest,
    CopyFileResultSuccess,
    CopyTreeRequest,
    CopyTreeResultSuccess,
    DeleteFileRequest,
    DeleteFileResultSuccess,
    DeletionBehavior,
    MakeDirectoryRequest,
    MakeDirectoryResultSuccess,
    ReadFileRequest,
    ReadFileResultSuccess,
    RenameFileRequest,
    RenameFileResultSuccess,
    WriteFileRequest,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    GetCurrentProjectRequest,
    GetCurrentProjectResultSuccess,
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
)
from griptape_nodes.retained_mode.events.secrets_events import (
    GetAllSecretValuesRequest,
    GetAllSecretValuesResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import PublishWorkflowProgressEvent
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.node_library.workflow_registry import Workflow

logger = logging.getLogger("workflow_packager")

# SelectFromProject node detection constants
SELECT_FROM_PROJECT_LIBRARY_NAME = "Griptape Nodes Library"
SELECT_FROM_PROJECT_NODE_TYPE = "SelectFromProject"
SELECT_FROM_PROJECT_PARAM_NAME = "selected_path"

# Model download script written into the bundle, and run by consumers when present
DOWNLOAD_MODELS_SCRIPT_NAME = "download_models.py"
DOWNLOAD_MODELS_TEMPLATE_NAME = "download_models_script.py"

# Resolved to locate the workflow's own directory, one of the anchors a static file reference
# can be rooted at.
WORKFLOW_DIR_MACRO = "{workflow_dir}"

# TODO: Read and write operations should all be using ReadtoFile and WriteToFile.  https://github.com/griptape-ai/griptape-nodes/issues/4397


class ResolvedFileReference(NamedTuple):
    """A static file reference resolved for bundling.

    Attributes:
        absolute_path: Where the file lives now, on the publishing machine.
        bundle_relative_path: Where it belongs inside the bundle, relative to the bundle root.
    """

    absolute_path: Path
    bundle_relative_path: Path


class FileReferenceOutcome(NamedTuple):
    """The result of resolving a static file reference for bundling.

    Attributes:
        reference: The resolved reference, or None when the file cannot be bundled.
        failure: Why it cannot be bundled, phrased to complete "will not be bundled because
            ...". None when ``reference`` is set. Carried out to the caller rather than logged
            here so every "not bundled" line comes from one place, and so the distinct
            causes -- an unresolvable macro, a path outside the project -- stay distinct
            instead of collapsing into one message that guesses.
    """

    reference: ResolvedFileReference | None
    failure: str | None


class WorkflowPackager:
    """Shared packaging utilities for workflow publishers.

    Provides reusable methods for the common parts of workflow publishing:
    copying files, bundling libraries, writing config/env, collecting
    dependencies, and gathering static assets.

    Usage:
        packager = WorkflowPackager("my_workflow")
        packager.package_to_folder(destination, workflow)
    """

    def __init__(self, workflow_name: str) -> None:
        self._workflow_name = workflow_name
        self._progress: float = 0.0

    # -- Progress events --

    def emit_progress(self, additional: float, message: str) -> None:
        """Emit a publish progress event."""
        self._progress = min(self._progress + additional, 100.0)
        event = ExecutionGriptapeNodeEvent(
            wrapped_event=ExecutionEvent(payload=PublishWorkflowProgressEvent(progress=self._progress, message=message))
        )
        GriptapeNodes.EventManager().put_event(event)

    # -- File copy utilities --

    @staticmethod
    def copy_file(source_path: str | Path, destination_path: str | Path) -> None:
        """Copy a single file using the engine's OS event system."""
        result = GriptapeNodes.handle_request(
            CopyFileRequest(source_path=str(source_path), destination_path=str(destination_path), overwrite=True)
        )
        if not isinstance(result, CopyFileResultSuccess):
            msg = f"Failed to copy file from '{source_path}' to '{destination_path}'."
            logger.error(msg)
            raise TypeError(msg)

    @staticmethod
    def copy_tree(
        source_path: str | Path,
        destination_path: str | Path,
        ignore_patterns: list[str] | None = None,
    ) -> None:
        """Copy a directory tree using the engine's OS event system.

        Pass an empty ``ignore_patterns`` list to copy everything; omitting it applies the
        default exclusions.
        """
        if ignore_patterns is None:
            ignore_patterns = [".venv", ".venv-exec", "__pycache__", ".git"]
        result = GriptapeNodes.handle_request(
            CopyTreeRequest(
                source_path=str(source_path),
                destination_path=str(destination_path),
                ignore_patterns=ignore_patterns,
                dirs_exist_ok=True,
            )
        )
        if not isinstance(result, CopyTreeResultSuccess):
            msg = f"Failed to copy tree from '{source_path}' to '{destination_path}'."
            logger.error(msg)
            raise TypeError(msg)

    # -- Library bundling --

    def _resolve_all_library_deps(
        self,
        initial: list[LibraryNameAndVersion],
    ) -> list[LibraryNameAndVersion]:
        """Expand the initial library set to include all transitive library_dependencies."""
        return GriptapeNodes.LibraryManager().resolve_transitive_library_deps(initial)

    def copy_libraries(
        self,
        node_libraries: list[LibraryNameAndVersion],
        destination_path: Path,
        workflow: Workflow,
    ) -> list[str]:
        """Copy library source trees to destination, returning relative library paths.

        For each referenced library with a .json definition, finds the common root
        directory of all node files and copies the entire tree.
        """
        library_paths: list[str] = []

        for library_ref in node_libraries:
            library = GriptapeNodes.LibraryManager().get_library_info_by_library_name(library_ref.library_name)
            if library is None:
                msg = (
                    f"Attempted to package workflow '{workflow.metadata.name}'. "
                    f"Failed gathering library info for library '{library_ref.library_name}'."
                )
                logger.error(msg)
                raise ValueError(msg)

            library_data = LibraryRegistry.get_library(library_ref.library_name).get_library_data()

            if library.library_path.endswith(".json"):
                library_path = Path(library.library_path)
                absolute_library_path = library_path.resolve()
                abs_paths = [absolute_library_path]
                for node in library_data.nodes:
                    p = (library_path.parent / Path(node.file_path)).resolve()
                    abs_paths.append(p)
                common_root = Path(os.path.commonpath([str(p) for p in abs_paths]))
                dest = destination_path / common_root.name
                self.copy_tree(common_root, dest)

                library_path_relative_to_common_root = absolute_library_path.relative_to(common_root)
                relative_path = (Path("libraries") / common_root.name / library_path_relative_to_common_root).as_posix()
                library_paths.append(relative_path)
            else:
                msg = f"Cannot find griptape-nodes-library.json for {library.library_name}. Appending path {library.library_path}."
                logger.warning(msg)
                library_paths.append(library.library_path)

        return library_paths

    # -- Config writing --

    @staticmethod
    def write_config(destination: Path, library_paths: list[str]) -> None:
        """Write griptape_nodes_config.json to the destination."""
        config: dict[str, Any] = {
            "workspace_directory": ".",
            "enable_workspace_file_watching": False,
            "app_events": {
                "on_app_initialization_complete": {
                    "workflows_to_register": [],
                    "libraries_to_register": library_paths,
                }
            },
        }
        config_path = destination / "griptape_nodes_config.json"
        result = GriptapeNodes.handle_request(
            WriteFileRequest(file_path=str(config_path), content=json.dumps(config, indent=4), encoding="utf-8")
        )
        if not isinstance(result, WriteFileResultSuccess):
            msg = f"Failed to write config to '{config_path}'."
            logger.error(msg)
            raise TypeError(msg)

    # -- Environment file --

    @staticmethod
    def get_merged_env_mapping(workspace_env_path: Path) -> dict[str, Any]:
        """Merge workspace .env file with SecretsManager secrets.

        Blank-valued entries are dropped: consumers test for key *presence* rather than a
        meaningful value, so a bundled blank shadows a real value instead of falling
        through to it.
        """
        env_file_dict: dict[str, Any] = {}
        if workspace_env_path.exists():
            env_file = DotEnv(workspace_env_path)
            env_file_dict = {key: value for key, value in env_file.dict().items() if str(value or "").strip()}

        result = GriptapeNodes.handle_request(GetAllSecretValuesRequest())
        if not isinstance(result, GetAllSecretValuesResultSuccess):
            msg = "Failed to get all secret values."
            logger.error(msg)
            raise TypeError(msg)

        for secret_name, secret_value in result.values.items():
            if secret_name not in env_file_dict and str(secret_value or "").strip():
                env_file_dict[secret_name] = secret_value

        return env_file_dict

    @staticmethod
    def get_process_env_secrets() -> dict[str, str]:
        """Return registered secrets exported into the process environment.

        ``SecretsManager.get_secret`` resolves OS environment variables ahead of both .env
        files, so an exported credential is both a working setup with nothing on disk to
        bundle and the value the live session uses when the two disagree. Only *registered*
        secret names are consulted, never the whole environment.
        """
        secrets_manager = GriptapeNodes.SecretsManager()
        env_secrets: dict[str, str] = {}
        for secret_name in secrets_manager.secrets_to_register:
            value = os.environ.get(secret_name)
            if value is not None and value.strip():
                env_secrets[secret_name] = value
        return env_secrets

    @staticmethod
    def write_env_file(env_file_path: Path, env_file_dict: dict[str, Any]) -> None:
        """Write a .env file from a key-value dict, replacing any existing file.

        Built from ``env_file_dict`` alone rather than updated key-by-key, so a key absent
        from the mapping leaves the file instead of persisting from an earlier write.
        """
        lines = [f"{key}={WorkflowPackager._quote_env_value(str(value))}" for key, value in env_file_dict.items()]
        content = "\n".join(lines)
        if content:
            content += "\n"
        result = GriptapeNodes.handle_request(
            WriteFileRequest(file_path=str(env_file_path), content=content, encoding="utf-8")
        )
        if not isinstance(result, WriteFileResultSuccess):
            msg = f"Failed to write environment file to '{env_file_path}'."
            logger.error(msg)
            raise TypeError(msg)

    @staticmethod
    def _quote_env_value(value: str) -> str:
        """Single-quote a .env value, matching what `dotenv.set_key` wrote before."""
        escaped = value.replace("'", "\\'")
        return f"'{escaped}'"

    def write_env(self, destination: Path) -> None:
        """Write a .env file with merged secrets to the destination."""
        secrets_manager = GriptapeNodes.SecretsManager()
        env_mapping = self.get_merged_env_mapping(secrets_manager.workspace_env_path)
        # Applied over the on-disk mapping, matching the precedence get_secret resolves
        # with: an exported credential is what the live session uses, so it is what the
        # bundle must ship.
        env_mapping.update(self.get_process_env_secrets())
        env_mapping["GTN_CONFIG_WORKSPACE_DIRECTORY"] = "."
        env_mapping["GTN_ENABLE_WORKSPACE_FILE_WATCHING"] = "false"
        self.write_env_file(destination / ".env", env_mapping)

    # -- Project template --

    @staticmethod
    def write_project_template(destination: Path) -> None:
        """Write the current project template (project.yml) to the destination.

        Failing to *retrieve* a template is legitimate (no current project) and leaves the
        bundle without one. Failing to *write* one that was retrieved raises: project.yml
        governs where published outputs land, so dropping it silently ships a wrong bundle.
        """
        result = GriptapeNodes.handle_request(GetCurrentProjectRequest())
        if not isinstance(result, GetCurrentProjectResultSuccess):
            logger.warning("Could not retrieve current project template. No project.yml will be written.")
            return
        project_yaml = result.project_info.template.to_yaml()
        write_result = GriptapeNodes.handle_request(
            WriteFileRequest(file_path=str(destination / "project.yml"), content=project_yaml, encoding="utf-8")
        )
        if not isinstance(write_result, WriteFileResultSuccess):
            msg = f"Failed to write the project template (project.yml) to '{destination}'."
            logger.error(msg)
            raise TypeError(msg)

    # -- Dependencies --

    def get_engine_version(self) -> str:
        """Get the current engine version string (e.g. 'v0.78.2')."""
        result = GriptapeNodes.handle_request(GetEngineVersionRequest())
        if not isinstance(result, GetEngineVersionResultSuccess):
            msg = f"Failed to get engine version for workflow '{self._workflow_name}'."
            logger.error(msg)
            raise TypeError(msg)
        return f"v{result.major}.{result.minor}.{result.patch}"

    @staticmethod
    def find_griptape_nodes_distribution() -> importlib.metadata.Distribution | None:
        """Find the griptape-nodes-engine distribution from the current executable's venv."""
        import sys

        exe_path = Path(sys.executable)
        venv_root = exe_path.parent.parent
        site_packages = venv_root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"

        if not site_packages.exists():
            logger.info("Venv site-packages not found at %s, falling back to default lookup", site_packages)
            try:
                return importlib.metadata.distribution("griptape-nodes-engine")
            except importlib.metadata.PackageNotFoundError:
                return None

        for dist in importlib.metadata.distributions(path=[str(site_packages)]):
            if dist.metadata["Name"] == "griptape-nodes-engine":
                return dist

        try:
            return importlib.metadata.distribution("griptape-nodes-engine")
        except importlib.metadata.PackageNotFoundError:
            return None

    def get_install_source(self) -> tuple[Literal["git", "file", "pypi"], str | None]:  # noqa: PLR0911
        """Detect whether griptape-nodes-engine was installed from git, file, or pypi."""
        dist = self.find_griptape_nodes_distribution()
        if dist is None:
            return "pypi", None
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text is None:
            return "pypi", None
        direct_url_info = json.loads(direct_url_text)
        url = direct_url_info.get("url", "")
        if url.startswith("file://"):
            git_exe = shutil.which("git")
            if git_exe is None:
                return "file", None
            # For editable installs, dist.locate_file("") points at the ephemeral venv's
            # site-packages, not the source checkout. The checkout (which holds the .git
            # metadata) is recorded in direct_url.json's url, so resolve the commit from
            # there. `git -C ... rev-parse` also handles worktrees, whose .git is a file
            # rather than a directory.
            source_dir = Path(url2pathname(urlparse(url).path))
            try:
                commit = (
                    subprocess.check_output(  # noqa: S603
                        [git_exe, "-C", str(source_dir), "rev-parse", "HEAD"],
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .strip()
                )
            except (subprocess.CalledProcessError, OSError):
                return "file", None
            else:
                return "git", commit

        if "vcs_info" in direct_url_info:
            commit_id = direct_url_info["vcs_info"].get("commit_id", "")
            if not commit_id:
                return "pypi", None
            return "git", commit_id

        return "pypi", None

    def collect_dependencies(self, workflow: Workflow) -> list[str]:
        """Collect all pip dependencies for the workflow."""
        engine_version = self.get_engine_version()
        source, commit_id = self.get_install_source()
        if source == "git" and commit_id is not None:
            engine_version = commit_id

        dependencies: list[str] = [
            f"griptape-nodes-engine @ git+https://github.com/griptape-ai/griptape-nodes.git@{engine_version}",
        ]

        for library_ref in self._resolve_all_library_deps(workflow.metadata.node_libraries_referenced):
            library_data = LibraryRegistry.get_library(library_ref.library_name).get_library_data()
            if library_data.metadata and library_data.metadata.dependencies:
                pip_deps = library_data.metadata.dependencies.pip_dependencies
                if pip_deps:
                    for dep in pip_deps:
                        if dep not in dependencies:
                            dependencies.append(dep)

        return dependencies

    def collect_pip_install_flags(self, workflow: Workflow) -> list[str]:
        """Collect all unique pip install flags from the workflow's referenced libraries."""
        flags: list[str] = []
        for library_ref in self._resolve_all_library_deps(workflow.metadata.node_libraries_referenced):
            library_data = LibraryRegistry.get_library(library_ref.library_name).get_library_data()
            if library_data.metadata and library_data.metadata.dependencies:
                install_flags = library_data.metadata.dependencies.pip_install_flags
                if install_flags:
                    for flag in install_flags:
                        if flag not in flags:
                            flags.append(flag)
        return flags

    @staticmethod
    def _uv_flags_to_toml_settings(flags: list[str]) -> dict[str, str | bool]:
        """Convert uv CLI flags to [tool.uv] pyproject.toml key/value pairs.

        Handles:
          --preview             -> preview = true
          --torch-backend=auto  -> torch-backend = "auto"
        """
        settings: dict[str, str | bool] = {}
        for flag in flags:
            if not flag.startswith("--"):
                continue
            flag_body = flag[2:]
            if "=" in flag_body:
                key, value = flag_body.split("=", 1)
                settings[key] = value
            else:
                settings[flag_body] = True
        return settings

    @staticmethod
    def _slugify(name: str) -> str:
        slug = name.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        return slug.strip("-")

    def write_pyproject_toml(self, destination: Path, workflow: Workflow) -> None:
        """Generate a pyproject.toml with pinned dependencies and uv settings."""
        project_name = self._slugify(self._workflow_name)
        dependencies = self.collect_dependencies(workflow)
        deps_toml = ",\n".join(f'    "{dep}"' for dep in dependencies)

        content = f"""\
[project]
name = "{project_name}"
description = "A published Griptape Nodes workflow packaged for headless execution."
readme = "README.md"
version = "0.1.0"
requires-python = ">=3.12.0, <3.13"
dependencies = [
{deps_toml},
]
"""

        uv_flags = self.collect_pip_install_flags(workflow)
        uv_settings = self._uv_flags_to_toml_settings(uv_flags)
        if uv_settings:
            content += "\n[tool.uv]\n"
            for key, value in uv_settings.items():
                if isinstance(value, bool):
                    content += f"{key} = {'true' if value else 'false'}\n"
                else:
                    content += f'{key} = "{value}"\n'

        result = GriptapeNodes.handle_request(
            WriteFileRequest(file_path=str(destination / "pyproject.toml"), content=content, encoding="utf-8")
        )
        if not isinstance(result, WriteFileResultSuccess):
            msg = f"Failed to write pyproject.toml to '{destination}'."
            logger.error(msg)
            raise TypeError(msg)

    # -- Static file / asset gathering --

    @staticmethod
    def collect_all_nodes() -> list[BaseNode]:
        """Collect all nodes from the workflow, recursing into node groups."""
        result = GriptapeNodes.handle_request(GetTopLevelFlowRequest())
        if not isinstance(result, GetTopLevelFlowResultSuccess) or result.flow_name is None:
            return []
        control_flow = GriptapeNodes.FlowManager().get_flow_by_name(result.flow_name)

        nodes: list[BaseNode] = []
        stack = list(control_flow.nodes.values())
        while stack:
            node = stack.pop()
            nodes.append(node)
            if isinstance(node, BaseNodeGroup):
                stack.extend(node.nodes.values())
        return nodes

    @staticmethod
    def gather_static_file_references(nodes: list[BaseNode]) -> list[tuple[str, str]]:
        """Scan nodes for static file references.

        Collects file references from:
        1. SelectFromProject nodes (legacy metadata-based detection)
        2. Any node's NodeDependencies.static_files (extensible mechanism)

        Returns:
            List of (node_name, value_string) tuples for parameters with non-empty values.
        """
        results: list[tuple[str, str]] = []
        seen_values: set[str] = set()

        # Legacy: SelectFromProject metadata scan
        for node in nodes:
            if (
                node.metadata.get("library") == SELECT_FROM_PROJECT_LIBRARY_NAME
                and node.metadata.get("node_type") == SELECT_FROM_PROJECT_NODE_TYPE
            ):
                value = node.get_parameter_value(SELECT_FROM_PROJECT_PARAM_NAME)
                if value and isinstance(value, str) and value not in seen_values:
                    seen_values.add(value)
                    results.append((node.name, value))

        # Also collect from any node that declares static files via get_node_dependencies()
        for node in nodes:
            deps = node.get_node_dependencies()
            if deps is None:
                continue
            # Sorted because static_files is a set: when two references land on the same place in
            # the bundle only the first is copied, and set iteration order varies between
            # processes, so without this two publishes of one workflow can ship different bytes
            # at the same bundle path.
            for file_ref in sorted(deps.static_files):
                if file_ref and file_ref not in seen_values:
                    seen_values.add(file_ref)
                    results.append((node.name, file_ref))

        return results

    @staticmethod
    def _resolve_file_reference(value_str: str, anchors: list[Path]) -> FileReferenceOutcome:
        """Resolve a file reference to its source path and its place in the bundle."""
        absolute_path: Path | None = None
        bundle_relative_path: Path | None = None
        macro_failure: str | None = None

        parsed: ParsedMacro | None = None
        try:
            parsed = ParsedMacro(value_str)
        except MacroSyntaxError:
            logger.debug("Could not parse %r as a macro; treating it as a plain path.", value_str, exc_info=True)
            macro_failure = "it is not valid macro syntax"

        if parsed is not None:
            resolve_result = GriptapeNodes.handle_request(GetPathForMacroRequest(parsed_macro=parsed, variables={}))
            if isinstance(resolve_result, GetPathForMacroResultSuccess):
                absolute_path = resolve_result.absolute_path
                # A RELATIVE resolved_path that the resolver did nothing but join onto the
                # workspace is already the destination the bundle will look in, and is
                # authoritative: the bundle's workspace IS the bundle root. Anchor-stripping must
                # not second-guess it -- for a workflow saved inside a directory that also
                # contains the file, the deepest anchor is the workflow's folder, which would
                # drop a leading path segment the run time still expects.
                if not resolve_result.resolved_path.is_absolute() and WorkflowPackager._is_joined_suffix(
                    resolve_result.absolute_path, resolve_result.resolved_path
                ):
                    bundle_relative_path = resolve_result.resolved_path
            else:
                # result_details carries the artist-readable reason ProjectManager built --
                # which directory or builtin failed, and why. failure_reason alone would say
                # only MACRO_RESOLUTION_ERROR.
                macro_failure = f"its macro could not be resolved ({resolve_result.result_details})"

        # A value the macro layer rejected can still be a usable path -- an absolute Windows
        # path is not valid macro syntax, and resolution can fail for reasons unrelated to the
        # value itself, such as there being no current project. But a macro that NAMES variables
        # and failed to resolve still carries its braces, so it can never be a usable path:
        # taking it as one reports a location instead of the variable that had no value.
        if absolute_path is None:
            candidate = Path(value_str)
            names_variables = parsed is not None and bool(parsed.get_variables())
            if names_variables or not candidate.is_absolute():
                return FileReferenceOutcome(
                    reference=None,
                    failure=macro_failure or "it is neither a resolvable macro nor an absolute path",
                )
            absolute_path = candidate

        # Otherwise derived from absolute_path, and never from an ABSOLUTE resolved_path: that
        # field holds the macro string after substitution, which is absolute whenever a
        # directory macro is absolute-rooted -- the v1 defaults anchor `inputs` and `outputs`
        # on `{workflow_dir}`. Joining an absolute path onto the bundle destination discards
        # the destination, so every macro-referenced dependency silently missed the bundle.
        # Checked before either way of deriving the destination, so it holds for a reference that
        # resolved relatively as much as one that was anchor-stripped: naming an anchor names the
        # folder the bundle replaces, not something inside it.
        if WorkflowPackager._matches_an_anchor(absolute_path, anchors):
            return FileReferenceOutcome(
                reference=None,
                failure=(
                    f"it resolves to '{absolute_path}', which is the folder the bundle itself "
                    f"replaces rather than something inside it"
                ),
            )

        if bundle_relative_path is None:
            bundle_relative_path = WorkflowPackager._bundle_relative_path(absolute_path, anchors)
        if bundle_relative_path is None:
            return FileReferenceOutcome(
                reference=None,
                failure=(f"it resolves to '{absolute_path}', which is outside the folders that travel with the bundle"),
            )

        return FileReferenceOutcome(
            reference=ResolvedFileReference(absolute_path=absolute_path, bundle_relative_path=bundle_relative_path),
            failure=None,
        )

    @staticmethod
    def _is_joined_suffix(absolute_path: Path, relative_path: Path) -> bool:
        """Whether ``absolute_path`` is ``relative_path`` joined onto a root and nothing more.

        The resolver expands ``~`` and environment variables and collapses ``..`` while building
        the absolute path, but leaves the substituted string it returns alone. Both ``../out/x.png``
        and ``~/media/x.png`` are therefore "relative" while naming somewhere the bundle has no
        say over: joining either onto the bundle destination writes outside the bundle, or creates
        a literal ``~`` directory inside it. Comparing the tail proves the join was all that
        happened, so a reference that needs real anchor-stripping falls through to it.
        """
        relative_parts = relative_path.parts
        if not relative_parts:
            return False
        return absolute_path.parts[-len(relative_parts) :] == relative_parts

    @staticmethod
    def _bundle_relative_path(absolute_path: Path, anchors: list[Path]) -> Path | None:
        """Locate a file under the anchor that becomes the bundle root, or None if under none.

        Publishing collapses every anchor a reference can be rooted at onto one directory: the
        workflow file is copied to the bundle root, the project template is written beside it,
        and the bundle's config points ``workspace_directory`` at the same place. Stripping the
        anchor a file sits under therefore reproduces the path the bundle's own macro
        resolution looks for when the published workflow runs.

        The longest matching anchor wins, so a workflow saved in a subdirectory of the
        workspace resolves against its own directory rather than the workspace root.

        Known limitation: the substituted path alone cannot say which anchor a directory macro
        was rooted on, and the deepest containing anchor is only a very good guess. It is right
        for every directory in the v0 and v1 defaults, but a hand-written macro naming a path
        inside the workflow's own folder from the workspace -- ``plates:
        "{workspace_dir}/shots/plates"`` with the workflow saved in ``shots`` -- is stripped
        against the workflow folder and bundled a level too shallow. Distinguishing them needs
        the directory's own ``path_macro``, which lives in the project layer.

        A reference that IS an anchor rather than something inside one gets None, even when a
        shallower anchor could place it. Every anchor collapses onto the bundle root, so
        `{workflow_dir}` resolves there when the published workflow runs and a copy made under
        the shallower anchor's name is somewhere nothing looks. Worse, when the bundle is written
        inside the referenced folder -- which the Nuke publisher does deliberately -- the
        destination sits inside the source, and ``copy_tree`` walks the source lazily while
        creating directories in it, so the copy never terminates. Refusing is the safer failure.
        ``_resolve_file_reference`` tests for that case ahead of this call to say so specifically;
        the refusal is repeated here so it holds for any caller that does not.

        A file under no anchor at all has nowhere to live in the bundle -- an external volume
        or network mount resolves to the same absolute path wherever the bundle runs, so there
        is no bundle-relative location to copy it to.

        Tried without following symlinks first, then with. A project that symlinks a media
        directory onto shared storage is an ordinary setup, and following the link on the first
        pass would take `{inputs}/image.jpg` to the storage mount, match no anchor, and report a
        file plainly inside the project as being outside it -- so the logical spelling wins when
        it matches. The symlink-resolved pass then catches the opposite case, where an anchor
        and the path reach the same directory by different spellings (``workspace_path``
        resolves symlinks, a ``{workflow_dir}`` path does not), which would otherwise drop every
        dependency in a workspace reached through a symlinked parent.
        """
        logical_match = WorkflowPackager._strip_longest_anchor(absolute_path, anchors, follow_symlinks=False)
        if logical_match is not None:
            return logical_match
        return WorkflowPackager._strip_longest_anchor(absolute_path, anchors, follow_symlinks=True)

    @staticmethod
    def _strip_longest_anchor(absolute_path: Path, anchors: list[Path], *, follow_symlinks: bool) -> Path | None:
        """Strip the deepest anchor containing ``absolute_path``, under one normalization.

        None when the deepest match leaves nothing to strip, i.e. the path IS that anchor. A
        shallower anchor is deliberately NOT tried in that case, so no caller can be handed
        ``Path(".")`` and turn it into a copy of a whole anchor into the bundle root.
        """
        normalize = canonicalize_for_identity if follow_symlinks else resolve_path_safely
        normalized_path = WorkflowPackager._comparable(normalize(absolute_path))

        best_relative_path: Path | None = None
        best_anchor_depth = -1
        for anchor in anchors:
            normalized_anchor = WorkflowPackager._comparable(normalize(anchor))
            if not normalized_path.is_relative_to(normalized_anchor):
                continue
            relative_path = normalized_path.relative_to(normalized_anchor)
            anchor_depth = len(normalized_anchor.parts)
            if anchor_depth > best_anchor_depth:
                best_anchor_depth = anchor_depth
                best_relative_path = relative_path

        if best_relative_path == Path():
            return None

        return best_relative_path

    @staticmethod
    def _matches_an_anchor(absolute_path: Path, anchors: list[Path]) -> bool:
        """Whether the reference names an anchor itself rather than something inside one.

        Separates "the bundle replaces this folder" from "this is somewhere the bundle does not
        reach": both leave nothing to copy, but only the second is the user's to act on.
        """
        for follow_symlinks in (False, True):
            normalize = canonicalize_for_identity if follow_symlinks else resolve_path_safely
            normalized_path = WorkflowPackager._comparable(normalize(absolute_path))
            if any(normalized_path == WorkflowPackager._comparable(normalize(anchor)) for anchor in anchors):
                return True
        return False

    @staticmethod
    def _comparable(path: Path) -> Path:
        r"""Strip the Windows long-path prefix so containment checks are meaningful.

        ``\\?\`` changes a path's anchor rather than just its spelling, so
        ``relative_to`` raises for a file that is plainly inside a directory when only one
        side carries it. Both sides go through here before being compared.
        """
        return Path(strip_windows_long_path_prefix(path))

    @staticmethod
    def _publish_anchors() -> list[Path]:
        """Collect the directories a static file reference can be rooted at.

        The workflow directory, the workspace, and the project base directory all become the
        bundle root once published, so a file under any of them has a place in the bundle. An
        anchor that cannot be determined is simply absent.

        ``{workflow_dir}`` is resolved through the same request the references themselves go
        through, rather than passed in, so the anchor is spelled exactly like the paths being
        matched against it and always belongs to the workflow whose references are being
        resolved.
        """
        anchors = [GriptapeNodes.ConfigManager().workspace_path]

        project_result = GriptapeNodes.handle_request(GetCurrentProjectRequest())
        if isinstance(project_result, GetCurrentProjectResultSuccess):
            anchors.append(project_result.project_info.project_base_dir)

        workflow_dir_result = GriptapeNodes.handle_request(
            GetPathForMacroRequest(parsed_macro=ParsedMacro(WORKFLOW_DIR_MACRO), variables={})
        )
        if isinstance(workflow_dir_result, GetPathForMacroResultSuccess):
            anchors.append(workflow_dir_result.absolute_path)
        else:
            # Expected whenever the workflow has not been saved; the remaining anchors still
            # place anything under the workspace or the project.
            logger.debug("Could not resolve %s as a publish anchor: %s", WORKFLOW_DIR_MACRO, workflow_dir_result)

        return anchors

    def copy_static_files(self, file_param_values: list[tuple[str, str]], destination: Path) -> None:
        """Resolve file references and copy them to the destination."""
        # Keyed on where a file lands in the bundle rather than where it came from: one source
        # can legitimately need two destinations (one reference resolving relatively, another
        # anchor-stripped), and keying on the source would bundle only the first of them.
        claimed_destinations: dict[Path, Path] = {}
        anchors = self._publish_anchors()

        for node_name, value_str in file_param_values:
            outcome = self._resolve_file_reference(value_str, anchors)
            if outcome.reference is None:
                logger.warning(
                    "File '%s' for node '%s' will not be bundled because %s.",
                    value_str,
                    node_name,
                    outcome.failure,
                )
                continue

            absolute_path = outcome.reference.absolute_path
            identity = canonicalize_for_identity(absolute_path)

            if not absolute_path.exists():
                logger.warning(
                    "File '%s' for node '%s' will not be bundled because there is no file at '%s'.",
                    value_str,
                    node_name,
                    absolute_path,
                )
                continue

            bundle_relative_path = outcome.reference.bundle_relative_path
            previous_source = claimed_destinations.get(bundle_relative_path)
            if previous_source == identity:
                logger.debug("Static file for node '%s' was already bundled: %s", node_name, absolute_path)
                continue
            # Every anchor collapses onto the bundle root, so two DIFFERENT files can want the
            # same place in the bundle. They would also resolve to that one place when the
            # published workflow runs, so the collision cannot be fixed by moving either copy --
            # but it must not pass silently, since one node then reads the other node's file.
            # The first claim is kept so the outcome does not depend on node iteration order.
            if previous_source is not None:
                logger.warning(
                    "Files '%s' and '%s' both belong at '%s' in the bundle. Only the first is "
                    "bundled, so a node may read the wrong file in the published workflow.",
                    previous_source,
                    absolute_path,
                    bundle_relative_path,
                )
                continue
            claimed_destinations[bundle_relative_path] = identity

            dest = destination / bundle_relative_path

            # The destination can resolve to the same file as the source when the package
            # destination lives inside the project root (e.g. the Nuke publisher writes the
            # bundle next to files the workflow already references). Copying a file onto
            # itself raises SameFileError, so treat it as already-in-place and skip.
            if identity == canonicalize_for_identity(dest):
                logger.info(
                    "Static file for node '%s' is already in place; skipping copy: %s", node_name, absolute_path
                )
                continue

            if absolute_path.is_dir():
                # A destination inside the source does not terminate: OSManager walks the source
                # with a lazy os.walk while creating directories in it, so it keeps descending
                # into what it just wrote. Reachable whenever a publisher writes the bundle
                # inside a folder a node references, which the Nuke publisher does deliberately.
                # Checked on the destination rather than on anchor identity, so it also covers a
                # referenced folder that is no anchor but still contains the bundle.
                if canonicalize_for_identity(dest).is_relative_to(identity):
                    logger.warning(
                        "Directory '%s' for node '%s' will not be bundled because the bundle is "
                        "being written inside it, at '%s'.",
                        value_str,
                        node_name,
                        dest,
                    )
                    continue
                self.copy_tree(absolute_path, dest)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                self.copy_file(absolute_path, dest)

            logger.info("Copied static file for node '%s': %s -> %s", node_name, absolute_path, dest)

    # -- HuggingFace model download --

    @staticmethod
    def collect_huggingface_download_commands(nodes: list[BaseNode]) -> list[str]:
        """Collect huggingface-cli download commands for all HuggingFace model parameters in the workflow."""
        workflow_manager = GriptapeNodes.WorkflowManager()
        commands: list[str] = []
        seen: set[str] = set()
        for node in nodes:
            hf_params: list[HuggingFaceModelParameter] = []

            def collect_hf_param(_cls: type, obj: Any, _hf_params: list = hf_params) -> None:
                if isinstance(obj, HuggingFaceModelParameter):
                    _hf_params.append(obj)

            workflow_manager._walk_object_tree(node, collect_hf_param)
            for hf_param in hf_params:
                for cmd in hf_param.get_download_commands():
                    if cmd not in seen:
                        seen.add(cmd)
                        commands.append(cmd)
        return commands

    def write_download_models_script(self, nodes: list[BaseNode], destination: Path) -> bool:
        """Write a model download script to destination if HuggingFace models are required.

        When no models are needed an existing script is removed, since consumers run it
        whenever the file is present and one from an earlier publish would download models
        this workflow no longer references.

        Returns True if a script was written, False if no models are needed.
        """
        commands = self.collect_huggingface_download_commands(nodes)
        if not commands:
            self._remove_stale_download_models_script(destination)
            return False

        template_path = Path(__file__).parent / DOWNLOAD_MODELS_TEMPLATE_NAME
        read_result = GriptapeNodes.handle_request(
            ReadFileRequest(file_path=str(template_path), workspace_only=False, encoding="utf-8")
        )
        if not isinstance(read_result, ReadFileResultSuccess):
            msg = f"Failed to read download models script template from '{template_path}'."
            logger.error(msg)
            raise TypeError(msg)
        template = read_result.content
        if not isinstance(template, str):
            msg = f"Expected text content for download models script template at '{template_path}'."
            logger.error(msg)
            raise TypeError(msg)
        commands_repr = ", ".join(repr(cmd) for cmd in commands)
        script_content = template.replace(
            '["REPLACE_DOWNLOAD_COMMANDS"]',
            f"[{commands_repr}]",
        )
        write_result = GriptapeNodes.handle_request(
            WriteFileRequest(
                file_path=str(destination / DOWNLOAD_MODELS_SCRIPT_NAME), content=script_content, encoding="utf-8"
            )
        )
        if not isinstance(write_result, WriteFileResultSuccess):
            msg = f"Failed to write download models script to '{destination}'."
            logger.error(msg)
            raise TypeError(msg)
        return True

    @staticmethod
    def _remove_stale_download_models_script(destination: Path) -> None:
        """Delete a model download script left by an earlier publish of the same bundle."""
        script_path = destination / DOWNLOAD_MODELS_SCRIPT_NAME
        if not script_path.exists():
            return
        result = GriptapeNodes.handle_request(
            DeleteFileRequest(
                path=str(script_path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.PERMANENTLY_DELETE,
            )
        )
        if not isinstance(result, DeleteFileResultSuccess):
            msg = (
                f"Failed to remove the model download script left by a previous publish at '{script_path}'. "
                f"Leaving it in place would download models this workflow no longer uses."
            )
            logger.error(msg)
            raise TypeError(msg)

    # -- Staging --

    @contextmanager
    def staged_publish(self, destination: Path, *, preserve: list[str] | None = None) -> Iterator[Path]:
        """Yield a staging directory that replaces ``destination`` only if the publish succeeds.

        Each writer in a publish decides for itself whether it overwrites what is already
        there, so writing straight into a persistent destination accumulates: a re-publish
        can add and update artifacts but never remove one. Staging makes each publish a
        clean build whose contents reflect only the current state of the world, and leaves
        the previous bundle untouched when a publish raises partway through.

        Wrap the *whole* publish, not just the packager. Publishers write their own
        artifacts after ``package_to_folder`` returns, so swapping when the packager
        finishes would delete the previous bundle's publisher-specific files and only then
        write the new ones -- leaving a bundle with no entrypoint if that tail failed.

        Args:
            destination: The final bundle directory, replaced wholesale on success.
            preserve: Top-level entry names under ``destination`` to carry into the new
                bundle, for content the publisher deliberately accumulates across publishes.
                Each is a literal name or an ``fnmatch`` pattern, so a publisher holding an
                open-ended set of entries can pass ``"v*"`` rather than enumerating them.
                Entries the publish itself wrote into staging win; patterns matching nothing
                are skipped.

        Yields:
            The staging directory to write the bundle into.
        """
        # Staged as a sibling of the destination so the swap is a same-filesystem rename.
        # A system temp dir is routinely on another filesystem, where a rename fails and
        # the swap would degrade to a non-atomic copy.
        staging_dir = destination.with_name(f"{destination.name}.publish-{uuid.uuid4().hex[:8]}")
        previous_dir = destination.with_name(f"{staging_dir.name}.previous")
        self._make_directory(staging_dir)
        try:
            yield staging_dir
            self._carry_preserved_entries(destination, staging_dir, preserve or [])
            self._swap_into_place(destination, staging_dir, previous_dir)
        finally:
            self._discard_directory(staging_dir)
            # Only once the destination is populated again. If a rollback could not put it
            # back, previous_dir holds the sole copy of the bundle and must be kept.
            if destination.exists():
                self._discard_directory(previous_dir)

    @classmethod
    def _carry_preserved_entries(cls, destination: Path, staging_dir: Path, preserve: list[str]) -> None:
        """Copy opted-in entries from the previous bundle into staging before the swap."""
        for name in cls._match_preserved_names(destination, preserve):
            previous = destination / name
            staged = staging_dir / name
            if staged.exists():
                continue
            if previous.is_dir():
                cls.copy_tree(previous, staged, ignore_patterns=[])
            else:
                cls.copy_file(previous, staged)

    @staticmethod
    def _match_preserved_names(destination: Path, preserve: list[str]) -> list[str]:
        """Resolve ``preserve`` names and patterns against the previous bundle's entries.

        Matching is done against the directory listing rather than by globbing, so a
        pattern cannot reach outside the bundle it is preserving from.
        """
        if not preserve or not destination.is_dir():
            return []
        return sorted(
            entry.name for entry in destination.iterdir() if any(fnmatch(entry.name, pattern) for pattern in preserve)
        )

    @classmethod
    def _swap_into_place(cls, destination: Path, staging_dir: Path, previous_dir: Path) -> None:
        """Move the staged bundle onto the destination, restoring the previous one on failure.

        The previous bundle is moved aside rather than deleted, so a failure between the
        two moves leaves the destination populated instead of missing.
        """
        had_previous = destination.exists()
        if had_previous and not cls._rename(destination, previous_dir):
            cls._raise_publish_failure(destination, "move the previous bundle aside")

        if not cls._rename(staging_dir, destination):
            # Report the failure that actually stopped the publish, not one from the
            # rollback -- so log a failed restore rather than raising over the cause.
            if had_previous and not cls._rename(previous_dir, destination):
                logger.error(
                    "Could not restore the previous bundle to '%s'. It remains at '%s'.",
                    destination,
                    previous_dir,
                )
            cls._raise_publish_failure(destination, "move the new bundle into place")

    @staticmethod
    def _make_directory(path: Path) -> None:
        """Create a directory using the engine's OS event system."""
        result = GriptapeNodes.handle_request(MakeDirectoryRequest(path=str(path), create_parents=True, exist_ok=True))
        if not isinstance(result, MakeDirectoryResultSuccess):
            msg = f"Failed to create directory '{path}'."
            logger.error(msg)
            raise TypeError(msg)

    @staticmethod
    def _rename(source: Path, destination: Path) -> bool:
        """Rename a file or directory using the engine's OS event system, reporting success.

        Reports failure by returning rather than raising: the caller drives a rollback off
        the result, and keying that off a caught exception would make a programming error
        here indistinguishable from a rename the OS refused.
        """
        result = GriptapeNodes.handle_request(
            RenameFileRequest(old_path=str(source), new_path=str(destination), workspace_only=False)
        )
        if not isinstance(result, RenameFileResultSuccess):
            logger.error("Could not rename '%s' to '%s'.", source, destination)
            return False
        return True

    @staticmethod
    def _raise_publish_failure(destination: Path, failure_context: str) -> NoReturn:
        """Raise a publish failure naming the bundle directory the publish was aimed at.

        The paths the swap renames through are internal working directories the user never
        configured, so the message stays on the destination regardless of which move failed.
        """
        msg = f"Failed to publish the workflow bundle to '{destination}'. Could not {failure_context}."
        logger.error(msg)
        raise TypeError(msg)

    @staticmethod
    def _discard_directory(path: Path) -> None:
        """Delete a staging/backup directory, logging rather than raising on failure.

        Called from cleanup, where the publish outcome is already decided -- a leftover
        directory is worth a log line but must not mask the result it is cleaning up after.
        """
        if not path.exists():
            return
        result = GriptapeNodes.handle_request(
            DeleteFileRequest(
                path=str(path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.PERMANENTLY_DELETE,
            )
        )
        if not isinstance(result, DeleteFileResultSuccess):
            logger.warning("Could not remove the publish working directory '%s'.", path)

    # -- Convenience: full standard bundle --

    def package_to_folder(self, destination: Path, workflow: Workflow) -> list[str]:
        """Bundle a workflow into a self-contained folder.

        Copies the workflow file, referenced libraries, config, .env, static
        assets, project template, and pyproject.toml into the destination.

        Writes into ``destination`` as given. Callers wanting a re-publish to be a clean
        rewrite should wrap their whole publish in ``staged_publish`` and pass the staging
        directory here.

        Returns:
            List of relative library paths (for config or further use).
        """
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except (FileNotFoundError, OSError) as err:
            msg = f"Failed to package to folder. Failed to create destination directory: {err}"
            logger.error(msg)
            raise TypeError(msg) from err

        # Copy workflow file
        self.emit_progress(10.0, "Copying workflow file...")
        workflow_file_path = workflow.file_path
        if workflow_file_path is None:
            msg = f"Cannot package unsaved workflow '{workflow.metadata.name}'. Save the workflow before packaging."
            logger.error(msg)
            raise TypeError(msg)
        full_path = WorkflowRegistry.get_complete_file_path(workflow_file_path)
        self.copy_file(full_path, destination / Path(full_path).name)

        # Copy libraries (including transitive library dependencies)
        self.emit_progress(15.0, "Copying libraries...")
        all_libraries = self._resolve_all_library_deps(workflow.metadata.node_libraries_referenced)
        library_paths = self.copy_libraries(
            node_libraries=all_libraries,
            destination_path=destination / "libraries",
            workflow=workflow,
        )

        # Write config
        self.emit_progress(5.0, "Writing configuration...")
        self.write_config(destination, library_paths)

        # Write project template
        self.emit_progress(3.0, "Writing project template...")
        self.write_project_template(destination)

        # Copy static files and check HuggingFace model dependencies
        self.emit_progress(5.0, "Copying static files...")
        all_nodes = self.collect_all_nodes()
        file_refs = self.gather_static_file_references(all_nodes)
        if file_refs:
            self.copy_static_files(file_refs, destination)

        # Write HuggingFace model download script if needed
        self.emit_progress(3.0, "Checking for HuggingFace model dependencies...")
        self.write_download_models_script(all_nodes, destination)

        # Write .env
        self.emit_progress(5.0, "Writing environment file...")
        self.write_env(destination)

        # Write pyproject.toml
        self.emit_progress(5.0, "Writing pyproject.toml...")
        self.write_pyproject_toml(destination, workflow)

        return library_paths
