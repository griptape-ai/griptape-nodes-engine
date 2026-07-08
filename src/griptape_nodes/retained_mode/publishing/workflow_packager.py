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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from dotenv import set_key
from dotenv.main import DotEnv

from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.param_components.huggingface.huggingface_model_parameter import HuggingFaceModelParameter
from griptape_nodes.files.path_utils import canonicalize_for_identity
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
    ReadFileRequest,
    ReadFileResultSuccess,
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
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.node_library.workflow_registry import Workflow

logger = logging.getLogger("workflow_packager")

# SelectFromProject node detection constants
SELECT_FROM_PROJECT_LIBRARY_NAME = "Griptape Nodes Library"
SELECT_FROM_PROJECT_NODE_TYPE = "SelectFromProject"
SELECT_FROM_PROJECT_PARAM_NAME = "selected_path"

# TODO: Read and write operations should all be using ReadtoFile and WriteToFile.  https://github.com/griptape-ai/griptape-nodes/issues/4397


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
        """Copy a directory tree using the engine's OS event system."""
        result = GriptapeNodes.handle_request(
            CopyTreeRequest(
                source_path=str(source_path),
                destination_path=str(destination_path),
                ignore_patterns=ignore_patterns or [".venv", "__pycache__", ".git"],
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
        """Merge workspace .env file with SecretsManager secrets."""
        env_file_dict: dict[str, Any] = {}
        if workspace_env_path.exists():
            env_file = DotEnv(workspace_env_path)
            env_file_dict = env_file.dict()

        result = GriptapeNodes.handle_request(GetAllSecretValuesRequest())
        if not isinstance(result, GetAllSecretValuesResultSuccess):
            msg = "Failed to get all secret values."
            logger.error(msg)
            raise TypeError(msg)

        for secret_name, secret_value in result.values.items():
            if secret_name not in env_file_dict:
                env_file_dict[secret_name] = secret_value

        return env_file_dict

    @staticmethod
    def write_env_file(env_file_path: Path, env_file_dict: dict[str, Any]) -> None:
        """Write a .env file from a key-value dict."""
        env_file_path.touch(exist_ok=True)
        for key, val in env_file_dict.items():
            set_key(env_file_path, key, str(val))

    def write_env(self, destination: Path) -> None:
        """Write a .env file with merged secrets to the destination."""
        secrets_manager = GriptapeNodes.SecretsManager()
        env_mapping = self.get_merged_env_mapping(secrets_manager.workspace_env_path)
        env_mapping["GTN_CONFIG_WORKSPACE_DIRECTORY"] = "."
        env_mapping["GTN_ENABLE_WORKSPACE_FILE_WATCHING"] = "false"
        self.write_env_file(destination / ".env", env_mapping)

    # -- Project template --

    @staticmethod
    def write_project_template(destination: Path) -> None:
        """Write the current project template (project.yml) to the destination."""
        result = GriptapeNodes.handle_request(GetCurrentProjectRequest())
        if not isinstance(result, GetCurrentProjectResultSuccess):
            logger.warning("Could not retrieve current project template. No project.yml will be written.")
            return
        project_yaml = result.project_info.template.to_yaml()
        write_result = GriptapeNodes.handle_request(
            WriteFileRequest(file_path=str(destination / "project.yml"), content=project_yaml, encoding="utf-8")
        )
        if not isinstance(write_result, WriteFileResultSuccess):
            logger.warning("Could not write project.yml to '%s'.", destination)

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
        commit = None
        if url.startswith("file://"):
            git_exe = shutil.which("git")
            if git_exe is None:
                return "file", None
            try:
                pkg_dir = Path(str(dist.locate_file(""))).resolve()
                git_root = next(p for p in (pkg_dir, *pkg_dir.parents) if (p / ".git").is_dir())
                commit = (
                    subprocess.check_output(  # noqa: S603
                        [git_exe, "rev-parse", "--short", "HEAD"],
                        cwd=git_root,
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .strip()
                )
            except (StopIteration, subprocess.CalledProcessError):
                return "file", None
            else:
                return "git", commit

        if "vcs_info" in direct_url_info:
            commit_id = direct_url_info["vcs_info"].get("commit_id", "")[:7]
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
            for file_ref in deps.static_files:
                if file_ref and file_ref not in seen_values:
                    seen_values.add(file_ref)
                    results.append((node.name, file_ref))

        return results

    @staticmethod
    def _resolve_file_reference(value_str: str, project_root: Path | None) -> tuple[Path, Path] | None:
        """Resolve a file reference to (absolute_path, relative_path) or None."""
        from griptape_nodes.common.macro_parser import ParsedMacro

        try:
            parsed = ParsedMacro(value_str)
            resolve_result = GriptapeNodes.handle_request(GetPathForMacroRequest(parsed_macro=parsed, variables={}))
            if isinstance(resolve_result, GetPathForMacroResultSuccess):
                return resolve_result.absolute_path, resolve_result.resolved_path
        except Exception:
            logger.debug("Macro resolution failed for %r; falling back to path resolution.", value_str, exc_info=True)

        candidate = Path(value_str)
        if candidate.is_absolute() and project_root and candidate.is_relative_to(project_root):
            return candidate, candidate.relative_to(project_root)

        return None

    def copy_static_files(self, file_param_values: list[tuple[str, str]], destination: Path) -> None:
        """Resolve file references and copy them to the destination."""
        copied: set[Path] = set()

        project_result = GriptapeNodes.handle_request(GetCurrentProjectRequest())
        project_root: Path | None = None
        if isinstance(project_result, GetCurrentProjectResultSuccess):
            project_root = project_result.project_info.project_base_dir

        for node_name, value_str in file_param_values:
            resolved = self._resolve_file_reference(value_str, project_root)
            if resolved is None:
                msg = f"Couldn't resolve file reference for {node_name}. It will not be bundled."
                logger.warning(msg)
                continue

            absolute_path, resolved_relative = resolved

            if not absolute_path.exists() or absolute_path in copied:
                msg = f"Couldn't resolve file reference for {node_name}. The absolute path does not exist. It will not be bundled."
                logger.warning(msg)
                continue

            dest = destination / resolved_relative

            # The destination can resolve to the same file as the source when the package
            # destination lives inside the project root (e.g. the Nuke publisher writes the
            # bundle next to files the workflow already references). Copying a file onto
            # itself raises SameFileError, so treat it as already-in-place and skip.
            if canonicalize_for_identity(absolute_path) == canonicalize_for_identity(dest):
                copied.add(absolute_path)
                logger.info(
                    "Static file for node '%s' is already in place; skipping copy: %s", node_name, absolute_path
                )
                continue

            if absolute_path.is_dir():
                self.copy_tree(absolute_path, dest)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                self.copy_file(absolute_path, dest)

            copied.add(absolute_path)
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
        """Write a download_models.py script to destination if HuggingFace models are required.

        Returns True if a script was written, False if no models are needed.
        """
        commands = self.collect_huggingface_download_commands(nodes)
        if not commands:
            return False

        template_path = Path(__file__).parent / "download_models_script.py"
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
                file_path=str(destination / "download_models.py"), content=script_content, encoding="utf-8"
            )
        )
        if not isinstance(write_result, WriteFileResultSuccess):
            msg = f"Failed to write download models script to '{destination}'."
            logger.error(msg)
            raise TypeError(msg)
        return True

    # -- Convenience: full standard bundle --

    def package_to_folder(self, destination: Path, workflow: Workflow) -> list[str]:
        """Bundle a workflow into a self-contained folder.

        Copies the workflow file, referenced libraries, config, .env, static
        assets, project template, and pyproject.toml into the destination.

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
