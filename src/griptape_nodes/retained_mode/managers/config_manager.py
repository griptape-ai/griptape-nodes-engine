import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError
from xdg_base_dirs import xdg_config_home

from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.app_events import ConfigChanged
from griptape_nodes.retained_mode.events.artifact_events import (
    GetArtifactSchemasRequest,
    GetArtifactSchemasResultSuccess,
)
from griptape_nodes.retained_mode.events.base_events import ResultPayload
from griptape_nodes.retained_mode.events.config_events import (
    GetConfigCategoryRequest,
    GetConfigCategoryResultFailure,
    GetConfigCategoryResultSuccess,
    GetConfigPathRequest,
    GetConfigPathResultSuccess,
    GetConfigSchemaRequest,
    GetConfigSchemaResultFailure,
    GetConfigSchemaResultSuccess,
    GetConfigValueRequest,
    GetConfigValueResultFailure,
    GetConfigValueResultSuccess,
    GetWorkspaceRequest,
    GetWorkspaceResultSuccess,
    ResetConfigRequest,
    ResetConfigResultFailure,
    ResetConfigResultSuccess,
    SetConfigCategoryRequest,
    SetConfigCategoryResultFailure,
    SetConfigCategoryResultSuccess,
    SetConfigValueRequest,
    SetConfigValueResultFailure,
    SetConfigValueResultSuccess,
)
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    FileIOFailureReason,
    GetFileInfoRequest,
    GetFileInfoResultFailure,
    RenameFileRequest,
    RenameFileResultFailure,
    WriteFileRequest,
    WriteFileResultFailure,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.settings import WORKFLOWS_TO_REGISTER_KEY, Settings
from griptape_nodes.utils.dict_utils import get_dot_value, merge_dicts, set_dot_value

logger = logging.getLogger("griptape_nodes")

USER_CONFIG_PATH = xdg_config_home() / "griptape_nodes" / "griptape_nodes_config.json"


class ConfigManager:
    """A class to manage application configuration and file pathing.

    This class handles loading and saving configuration from multiple sources with the following precedence:
    1. Default configuration from Settings model (lowest priority)
    2. User global configuration from ~/.config/griptape_nodes/griptape_nodes_config.json
    3. Project-adjacent configuration from <project_dir>/griptape_nodes_config.json
    4. Workspace configuration from <workspace_dir>/griptape_nodes_config.json
    5. Environment variables with GTN_CONFIG_ prefix (highest priority)

    Environment variables starting with GTN_CONFIG_ are converted to config keys by removing the prefix
    and converting to lowercase (e.g., GTN_CONFIG_FOO=bar becomes {"foo": "bar"}).

    Supports categorized configuration using dot notation (e.g., 'category.subcategory.key')
    to organize related configuration items.

    Attributes:
        default_config (dict): The default configuration loaded from the Settings model.
        user_config (dict): The user configuration loaded from the config file.
        project_config (dict): The project-adjacent configuration loaded when a project is set.
        workspace_config (dict): The workspace configuration loaded when a workspace is resolved.
        env_config (dict): The configuration loaded from GTN_CONFIG_ environment variables.
        merged_config (dict): The merged configuration, combining all sources in precedence order.
    """

    def __init__(self, event_manager: EventManager | None = None) -> None:
        """Initialize the ConfigManager.

        Args:
            event_manager: The EventManager instance to use for event handling.
        """
        self._project_config_path: Path | None = None
        self._workspace_config_path: Path | None = None
        self._workspace_dir_override: str | None = None
        self.load_configs()

        self._set_log_level(self.merged_config.get("log_level", logging.INFO))

        # Store event manager reference for broadcasting config change events
        self._event_manager = event_manager

        if event_manager is not None:
            # Register all our listeners.
            event_manager.assign_manager_to_request_type(
                GetConfigCategoryRequest, self.on_handle_get_config_category_request
            )
            event_manager.assign_manager_to_request_type(
                SetConfigCategoryRequest, self.on_handle_set_config_category_request
            )
            event_manager.assign_manager_to_request_type(GetConfigValueRequest, self.on_handle_get_config_value_request)
            event_manager.assign_manager_to_request_type(SetConfigValueRequest, self.on_handle_set_config_value_request)
            event_manager.assign_manager_to_request_type(GetConfigPathRequest, self.on_handle_get_config_path_request)
            event_manager.assign_manager_to_request_type(GetWorkspaceRequest, self.on_handle_get_workspace_request)
            event_manager.assign_manager_to_request_type(
                GetConfigSchemaRequest, self.on_handle_get_config_schema_request
            )
            event_manager.assign_manager_to_request_type(ResetConfigRequest, self.on_handle_reset_config_request)

    @property
    def workspace_path(self) -> Path:
        """Get the base file path from the configuration.

        Returns:
            Path object representing the base file path.
        """
        return Path(self._workspace_path).resolve()

    @workspace_path.setter
    def workspace_path(self, path: str | Path) -> None:
        """Set the base file path in the configuration.

        Args:
            path: The path to set as the base file path.
        """
        self._workspace_path = str(Path(path).expanduser().resolve())

    def set_workspace_override(self, path: Path | None) -> None:
        """Set a runtime workspace directory override.

        This override takes precedence over config-file-based workspace_directory
        values (default, user, project, workspace configs) but is still overridden
        by the GTN_CONFIG_WORKSPACE_DIRECTORY environment variable.

        Used by ProjectManager to apply project_workspaces mappings and
        auto-default-to-project-dir behavior. Also updates workspace_path immediately
        so callers see the correct value before the next load_configs() call.

        Args:
            path: The workspace directory override, or None to clear it.
        """
        if path is None:
            self._workspace_dir_override = None
        else:
            resolved = str(Path(path).expanduser().resolve())
            self._workspace_dir_override = resolved
            self._workspace_path = resolved

    def clear_project_layers(self) -> None:
        """Drop all per-activation config state so the next activation starts clean.

        Resets the workspace override and the project-adjacent / workspace config-file
        paths. Without this, switching projects (or rolling back to one) inherits the
        prior project's config-file layer and workspace override. Callers remerge via
        load_configs()/load_project_config()/load_workspace_config() right after.
        """
        self._workspace_dir_override = None
        self._project_config_path = None
        self._workspace_config_path = None

    @property
    def config_files(self) -> list[Path]:
        """Get a list of config files in ascending order of priority.

        The last file shown has the highest priority and overrides
        any settings found in earlier files.

        Returns:
            List of Path objects representing the config files.
        """
        possible_config_files: list[Path] = [USER_CONFIG_PATH]

        if self._project_config_path is not None:
            possible_config_files.append(self._project_config_path)

        if self._workspace_config_path is not None:
            possible_config_files.append(self._workspace_config_path)

        return [config_file for config_file in possible_config_files if config_file.exists()]

    def _load_config_from_env_vars(self) -> dict[str, Any]:
        """Load configuration values from GTN_CONFIG_ environment variables.

        Environment variables starting with GTN_CONFIG_ are converted to config keys.
        GTN_CONFIG_FOO=bar becomes {"foo": "bar"}
        GTN_CONFIG_STORAGE_BACKEND=gtc becomes {"storage_backend": "gtc"}

        Returns:
            Dictionary containing config values from environment variables
        """
        env_config = {}
        for key, value in os.environ.items():
            if key.startswith("GTN_CONFIG_"):
                # Remove GTN_CONFIG_ prefix and convert to lowercase
                config_key = key[11:].lower()  # len("GTN_CONFIG_") = 11
                env_config[config_key] = value
                logger.debug("Loaded config from env var: %s -> %s", key, config_key)

        return env_config

    def _load_config_from_file(self, path: Path, label: str) -> dict:
        """Read and parse a JSON config file. Returns empty dict if missing or unparsable."""
        if not path.exists():
            logger.debug("No %s config file loaded", label)
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error("Error parsing %s config file: %s", label, e)
            return {}

    def load_configs(self) -> None:
        """Load and merge configs from all sources in priority order.

        Sets default_config, user_config, project_config, workspace_config, env_config,
        and merged_config attributes. Priority order (later entries win):
        defaults → user → project-adjacent → workspace → env vars.
        """
        self.default_config = Settings().model_dump()
        merged_config = self.default_config

        if USER_CONFIG_PATH.exists():
            self.user_config = self._load_config_from_file(USER_CONFIG_PATH, "user")
            merged_config = merge_dicts(merged_config, self.user_config)
        else:
            self.user_config = {}
            logger.debug("User config file not found")

        if self._project_config_path is not None:
            self.project_config = self._load_config_from_file(self._project_config_path, "project-adjacent")
            merged_config = merge_dicts(merged_config, self.project_config)
        else:
            self.project_config = {}

        # Skip workspace config when it points to the same file as project config
        # (this happens when workspace dir == project dir for self-contained projects).
        if self._workspace_config_path is not None and self._workspace_config_path != self._project_config_path:
            self.workspace_config = self._load_config_from_file(self._workspace_config_path, "workspace")
            merged_config = merge_dicts(merged_config, self.workspace_config)
        else:
            self.workspace_config = {}

        # Apply runtime workspace override (from ProjectManager's project_workspaces lookup
        # or auto-default-to-project-dir). Sits above config files but below env vars.
        if self._workspace_dir_override is not None:
            merged_config["workspace_directory"] = self._workspace_dir_override

        self.env_config = self._load_config_from_env_vars()
        if self.env_config:
            merged_config = merge_dicts(merged_config, self.env_config)
            logger.debug("Merged config from environment variables: %s", list(self.env_config.keys()))

        # Re-assign workspace path in case env var or project config overrides it
        self.workspace_path = merged_config["workspace_directory"]

        # Validate the full config against the Settings model.
        try:
            Settings.model_validate(merged_config)
            self.merged_config = merged_config
        except ValidationError as e:
            logger.error("Error validating config file: %s", e)
            self.merged_config = self.default_config

    def load_project_config(self, project_dir: Path) -> None:
        """Load the project-adjacent config from the given project directory and remerge all configs.

        Reads griptape_nodes_config.json from project_dir (if it exists) and stores it as
        the project_config layer. Then rebuilds the merged config with the updated layer order:
        default → user → project_config → workspace_config → env vars.

        Args:
            project_dir: Directory containing the project YAML file. Looks for
                griptape_nodes_config.json in this directory.
        """
        self._project_config_path = project_dir / "griptape_nodes_config.json"
        self.load_configs()

    def load_workspace_config(self, workspace_dir: Path) -> None:
        """Load the workspace config from the given workspace directory and remerge all configs.

        Reads griptape_nodes_config.json from workspace_dir (if it exists) and stores it as
        the workspace_config layer. When workspace_dir matches the project directory, the file
        is the same as the project-adjacent config and the workspace layer is skipped to avoid
        loading it twice. Rebuilds the merged config with the updated layer order:
        default → user → project_config → workspace_config → env vars.

        Args:
            workspace_dir: The resolved workspace directory. Looks for
                griptape_nodes_config.json in this directory.
        """
        self._workspace_config_path = workspace_dir / "griptape_nodes_config.json"
        self.load_configs()

    def compute_project_provisioning_config(
        self, project_dir: Path, workspace_dir: Path, *, apply_override: bool
    ) -> dict:
        """Return the merged config a project WOULD activate with, mutating nothing.

        Mirrors load_configs()'s layer order (defaults -> user -> project-adjacent ->
        workspace -> workspace override -> env vars) for the given project and
        workspace directories, reading files fresh into a local dict. The
        provisioning preview uses this so its plan reflects the same effective
        `libraries_to_register` / `requires_engine` that _reconcile_libraries_from_config
        reads from the live merged config after activation - instead of the
        project-adjacent file alone, which diverges when a higher-priority layer
        (a separate-dir workspace config, env vars, or the user config) sets those keys.

        `workspace_dir` and `apply_override` come from ProjectManager.decide_workspace,
        the same decision the live activation applies. The override is applied here only
        when `apply_override` is True (the project_workspaces mapping, configured-root
        inheritance, and auto-default branches), exactly as _activate_project calls
        set_workspace_override; for an env/project-adjacent workspace_directory it is False
        so the workspace config layer can re-point workspace_directory, matching the live
        path. When applied, the value is resolved the same way set_workspace_override
        resolves it (expanduser + resolve), so the merged workspace_directory matches the
        live merged config byte-for-byte.

        Args:
            project_dir: Directory holding the project YAML and its adjacent config.
            workspace_dir: The resolved workspace directory for this project.
            apply_override: Whether activation would pin workspace_directory to
                workspace_dir via set_workspace_override.
        """
        merged = Settings().model_dump()

        if USER_CONFIG_PATH.exists():
            merged = merge_dicts(merged, self._load_config_from_file(USER_CONFIG_PATH, "user"))

        project_config_path = project_dir / "griptape_nodes_config.json"
        merged = merge_dicts(merged, self._load_config_from_file(project_config_path, "project-adjacent"))

        # Skip the workspace layer when it resolves to the project-adjacent file
        # (workspace dir == project dir for self-contained projects), matching load_configs.
        workspace_config_path = workspace_dir / "griptape_nodes_config.json"
        if workspace_config_path != project_config_path:
            merged = merge_dicts(merged, self._load_config_from_file(workspace_config_path, "workspace"))

        # Apply the runtime workspace override conditionally, mirroring _activate_project:
        # only the project_workspaces and auto-default branches pin it (apply_override),
        # and the value is resolved exactly as set_workspace_override would so preview and
        # live agree. It sits above config files but below env vars.
        if apply_override:
            merged["workspace_directory"] = str(Path(workspace_dir).expanduser().resolve())

        env_config = self._load_config_from_env_vars()
        if env_config:
            merged = merge_dicts(merged, env_config)

        return merged

    def compute_system_defaults_provisioning_config(self) -> dict:
        """Return the merged config system defaults WOULD activate with, mutating nothing.

        Mirrors what _activate_project does for SYSTEM_DEFAULTS_KEY: clear_project_layers()
        drops the project-adjacent and workspace config-file layers and the workspace
        override, then load_configs() merges defaults -> user -> env vars only. The
        provisioning preview uses this so a switch to "Default Project" shows the same
        `libraries_to_register` / `requires_engine` that _reconcile_libraries_from_config
        reads from the live merged config after activation. Unlike
        compute_project_provisioning_config, it reads no project-adjacent or workspace
        griptape_nodes_config.json, because the system-defaults activation path reads
        neither.
        """
        merged = Settings().model_dump()

        if USER_CONFIG_PATH.exists():
            merged = merge_dicts(merged, self._load_config_from_file(USER_CONFIG_PATH, "user"))

        env_config = self._load_config_from_env_vars()
        if env_config:
            merged = merge_dicts(merged, env_config)

        return merged

    def reset_user_config(self) -> None:
        """Reset the user configuration to the default values.

        An exception is made for `workflows_to_register` since resetting it gives the appearance of the user losing their workflows.
        """
        # TODO: https://github.com/griptape-ai/griptape-nodes/issues/1241 need a better way to annotate fields to ignore.
        workflows_to_register = self.get_config_value(WORKFLOWS_TO_REGISTER_KEY)
        USER_CONFIG_PATH.write_text(
            json.dumps(
                {
                    "app_events": {
                        "on_app_initialization_complete": {
                            "workflows_to_register": workflows_to_register,
                        }
                    }
                },
                indent=2,
            )
        )
        self._workspace_dir_override = None
        self.load_configs()

    def delete_user_workflow(self, workflow_file_name: str) -> None:
        default_workflows = self.get_config_value(WORKFLOWS_TO_REGISTER_KEY)
        if default_workflows:
            default_workflows = [
                saved_workflow
                for saved_workflow in default_workflows
                if (saved_workflow.lower() != workflow_file_name.lower())
            ]
            self.set_config_value(WORKFLOWS_TO_REGISTER_KEY, default_workflows)

    def get_full_path(self, relative_path: str) -> Path:
        """Get a full path by combining the base path with a relative path.

        Args:
            relative_path: A path relative to the base path.

        Returns:
            Path object representing the full path.
        """
        workspace_path = self.workspace_path
        return workspace_path / relative_path

    def _coerce_to_type(self, value: Any, cast_type: type) -> Any:
        """Coerce a value to the specified type.

        This is particularly useful for environment variables which are always strings.

        Args:
            value: The value to coerce.
            cast_type: The type to coerce to (bool, int, float, or str).

        Returns:
            The coerced value.
        """
        if cast_type is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() not in ("false", "0", "no", "")
            return bool(value)
        if cast_type is int:
            return int(value)
        if cast_type is float:
            return float(value)
        # str is a no-op
        return value

    def get_config_value(
        self,
        key: str,
        *,
        should_load_env_var_if_detected: bool = True,
        config_source: Literal[
            "user_config", "project_config", "workspace_config", "default_config", "merged_config"
        ] = "merged_config",
        default: Any | None = None,
        cast_type: type[bool] | type[int] | type[float] | type[str] | None = None,
    ) -> Any:
        """Get a value from the configuration.

        If `should_load_env_var_if_detected` is True (default), and the value starts with a $, it will be pulled from the environment variables.

        Args:
            key: The configuration key to get. Can use dot notation for nested keys (e.g., 'category.subcategory.key').
                 If the key refers to a category (dictionary), returns the entire category.
            should_load_env_var_if_detected: If True, and the value starts with a $, it will be pulled from the environment variables.
            config_source: The source of the configuration to use. Can be 'user_config', 'project_config', 'default_config', or 'merged_config'.
            default: The default value to return if the key is not found in the configuration.
            cast_type: Optional type to coerce the value to (bool, int, float, or str). Useful for environment
                       variables which are always strings (e.g., "false" -> False when cast_type=bool).

        Returns:
            The value associated with the key, or the entire category if key points to a dict.
        """
        config_source_map = {
            "user_config": self.user_config,
            "project_config": self.project_config,
            "workspace_config": self.workspace_config,
            "merged_config": self.merged_config,
            "default_config": self.default_config,
        }
        config = config_source_map.get(config_source, self.merged_config)
        value = get_dot_value(config, key, default)

        if value is None:
            msg = f"Config key '{key}' not found in config file."
            logger.debug(msg)
            return None

        if should_load_env_var_if_detected and isinstance(value, str) and value.startswith("$"):
            from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

            value = GriptapeNodes.SecretsManager().get_secret(value[1:])

        if cast_type is not None:
            value = self._coerce_to_type(value, cast_type)

        return value

    def read_config_file(self, path: Path) -> dict:
        """Read and parse a single JSON config file in isolation, mutating nothing.

        Returns the raw parsed dict (empty when the file is missing or unparsable),
        without merging it into the live config layers. Used to inspect a
        project-adjacent config (e.g. for a provisioning preview or a read-only
        workspace-dir decision) for a project other than the active one.

        Args:
            path: The config file to read.
        """
        return self._load_config_from_file(path, label=str(path))

    def read_env_config(self) -> dict[str, Any]:
        """Return the config layer derived from GTN_CONFIG_ environment variables, mutating nothing.

        Public read-only view of the env-var layer for callers (e.g. a provisioning
        preview's read-only workspace-dir decision) that need to inspect it without
        triggering a full load_configs().
        """
        return self._load_config_from_env_vars()

    def read_config_file_value(self, path: Path, key: str, *, default: Any | None = None) -> Any:
        """Read a single dot-notation key from a config file without merging it into the live config.

        Reads and parses the JSON at `path` in isolation, then pulls `key` from it.
        Used to inspect a project-adjacent config (e.g. for a provisioning preview)
        without disturbing the active config layers. Returns `default` when the
        file is missing/unparsable or the key is absent.

        Args:
            path: The config file to read.
            key: Dot-notation key (e.g. 'category.subcategory.key').
            default: Value to return when the key is not present.
        """
        config = self.read_config_file(path)
        return get_dot_value(config, key, default)

    def set_config_value(self, key: str, value: Any, *, should_set_env_var_if_detected: bool = True) -> bool:
        """Set a value in the configuration.

        Args:
            key: The configuration key to set. Can use dot notation for nested keys (e.g., 'category.subcategory.key').
            value: The value to associate with the key.
            should_set_env_var_if_detected: If True, and the value starts with a $, it will be set in the environment variables.

        Returns:
            True if the change was persisted to disk; False if the underlying
            ``_write_user_config_delta`` call failed. Callers that surface a
            result payload to a request handler should propagate the failure
            instead of reporting success on a stale write.
        """
        # Capture old value before making changes (for event emission)
        old_value = self.get_config_value(key, should_load_env_var_if_detected=False)

        delta = set_dot_value({}, key, value)
        if key == "log_level":
            self._set_log_level(value)
        elif key == "workspace_directory":
            self.workspace_path = value
        self.user_config = merge_dicts(self.merged_config, delta)
        write_succeeded = self._write_user_config_delta(delta)

        if should_set_env_var_if_detected and isinstance(value, str) and value.startswith("$"):
            from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

            value = GriptapeNodes.SecretsManager().set_secret(value[1:], "")

        # We need to fully reload the user config because we need to regenerate the merged config.
        # Also eventually need to reload registered workflows.
        # TODO: https://github.com/griptape-ai/griptape-nodes/issues/437
        self.load_configs()
        logger.debug("Config value '%s' set to '%s'", key, value)

        # Broadcast a domain event on success only. Listeners (in production:
        # WorkerManager) take it from here -- this manager has no knowledge of
        # who consumes the event. Failed writes are logged inside
        # ``_write_user_config_delta``; no event fires so listeners cannot act
        # on a state that does not exist on disk.
        if write_succeeded and self._event_manager is not None:
            event = ConfigChanged(key=key, old_value=old_value, new_value=value)
            self._event_manager.broadcast_app_event(event)

        return write_succeeded

    def on_handle_get_config_category_request(self, request: GetConfigCategoryRequest) -> ResultPayload:
        if request.category is None or request.category == "":
            # Return the whole shebang. Start with the defaults and then layer on the user config.
            contents = self.merged_config
            result_details = "Successfully returned the entire config dictionary."
            return GetConfigCategoryResultSuccess(contents=contents, result_details=result_details)

        # See if we got something valid.
        find_results = self.get_config_value(request.category)
        if find_results is None:
            result_details = f"Attempted to get config details for category '{request.category}'. Failed because no such category could be found."
            return GetConfigCategoryResultFailure(result_details=result_details)

        if not isinstance(find_results, dict):
            result_details = f"Attempted to get config details for category '{request.category}'. Failed because this was was not a dictionary."
            return GetConfigCategoryResultFailure(result_details=result_details)

        result_details = f"Successfully returned the config dictionary for section '{request.category}'."
        return GetConfigCategoryResultSuccess(contents=find_results, result_details=result_details)

    def on_handle_set_config_category_request(self, request: SetConfigCategoryRequest) -> ResultPayload:
        # Validate the value is a dict
        if not isinstance(request.contents, dict):
            result_details = f"Attempted to set config details for category '{request.category}'. Failed because the contents provided were not a dictionary."
            return SetConfigCategoryResultFailure(result_details=result_details)

        # Get old value before changing for event emission
        old_value = None
        if request.category and request.category != "":
            old_value = self.get_config_value(request.category)

        if request.category is None or request.category == "":
            # Assign the whole shebang.
            write_succeeded = self._write_user_config_delta(request.contents)
            if not write_succeeded:
                result_details = (
                    "Attempted to assign the entire config dictionary. Failed because the user config "
                    "file could not be written; see prior logs for the underlying I/O error."
                )
                return SetConfigCategoryResultFailure(result_details=result_details)

            result_details = "Successfully assigned the entire config dictionary."

            # Domain event on success only -- listeners (e.g. WorkerManager)
            # decide what to do with it.
            if self._event_manager is not None:
                event = ConfigChanged(
                    key="",
                    old_value=old_value,
                    new_value=request.contents,
                )
                self._event_manager.broadcast_app_event(event)

            return SetConfigCategoryResultSuccess(result_details=result_details)

        write_succeeded = self.set_config_value(key=request.category, value=request.contents)
        if not write_succeeded:
            result_details = (
                f"Attempted to set config category '{request.category}'. Failed because the user config "
                "file could not be written; see prior logs for the underlying I/O error."
            )
            return SetConfigCategoryResultFailure(result_details=result_details)

        result_details = f"Successfully assigned the config dictionary for section '{request.category}'."
        return SetConfigCategoryResultSuccess(result_details=result_details)

    def on_handle_get_config_value_request(self, request: GetConfigValueRequest) -> ResultPayload:
        if request.category_and_key == "":
            result_details = "Attempted to get config value but no category or key was specified."
            return GetConfigValueResultFailure(result_details=result_details)

        # See if we got something valid.
        find_results = self.get_config_value(request.category_and_key)
        if find_results is None:
            result_details = f"Attempted to get config value for category.key '{request.category_and_key}'. Failed because no such category.key could be found."
            return GetConfigValueResultFailure(result_details=result_details)

        result_details = f"Successfully returned the config value for section '{request.category_and_key}'."
        return GetConfigValueResultSuccess(value=find_results, result_details=result_details)

    def on_handle_get_config_path_request(self, request: GetConfigPathRequest) -> ResultPayload:  # noqa: ARG002
        result_details = "Successfully returned the config path."
        return GetConfigPathResultSuccess(config_path=str(USER_CONFIG_PATH), result_details=result_details)

    def on_handle_get_workspace_request(self, request: GetWorkspaceRequest) -> ResultPayload:  # noqa: ARG002
        result_details = "Successfully returned the absolute workspace path."
        return GetWorkspaceResultSuccess(workspace_path=str(self.workspace_path), result_details=result_details)

    def on_handle_get_config_schema_request(self, request: GetConfigSchemaRequest) -> ResultPayload:  # noqa: ARG002
        """Handle request to get the configuration schema with current values and library settings.

        This method returns a clean structure with four main components:
        1. base_schema: Core settings schema from Pydantic Settings model with categories
        2. library_schemas: Library-specific schemas from definition files (preserves enums)
        3. artifact_schemas: Dynamically generated artifact provider schemas (enums, types, defaults)
        4. current_values: All current configuration values from merged config

        The approach separates concerns for frontend flexibility and simplicity.
        Library settings with explicit schemas (including enums) are preserved, while
        libraries without schemas get simple object types.
        """
        try:
            from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

            # Get base settings schema and current values
            base_schema = Settings.model_json_schema()
            current_values = self.merged_config.copy()

            # Get library schemas
            library_schemas = LibraryRegistry.get_all_library_schemas()

            # Get artifact schemas (dynamically generated from registered providers/generators)
            schemas_request = GetArtifactSchemasRequest()
            schemas_result = GriptapeNodes.handle_request(schemas_request)

            if not isinstance(schemas_result, GetArtifactSchemasResultSuccess):
                result_details = f"Failed to retrieve artifact schemas: {schemas_result.result_details}"
                return GetConfigSchemaResultFailure(result_details=result_details)

            artifact_schemas = schemas_result.schemas

            # Return clean structure
            schema_with_defaults = {
                "base_schema": base_schema,
                "library_schemas": library_schemas,
                "artifact_schemas": artifact_schemas,
                "current_values": current_values,
            }

            result_details = "Successfully returned the configuration schema with default values, library settings, and artifact schemas."
            return GetConfigSchemaResultSuccess(schema=schema_with_defaults, result_details=result_details)
        except Exception as e:
            result_details = f"Failed to generate configuration schema: {e}"
            return GetConfigSchemaResultFailure(result_details=result_details)

    def on_handle_reset_config_request(self, request: ResetConfigRequest) -> ResultPayload:  # noqa: ARG002
        try:
            self.reset_user_config()
            self._set_log_level(str(self.merged_config["log_level"]))

            result_details = "Successfully reset user configuration."
            # Reset is a full replacement; emit the same shape of ConfigChanged
            # that ``on_handle_set_config_category_request`` does for category=None,
            # so listeners cannot tell the two paths apart.
            if self._event_manager is not None:
                event = ConfigChanged(key="", old_value=None, new_value=self.merged_config)
                self._event_manager.broadcast_app_event(event)
            return ResetConfigResultSuccess(result_details=result_details)
        except Exception as e:
            result_details = f"Attempted to reset user configuration but failed: {e}."
            return ResetConfigResultFailure(result_details=result_details)

    def _get_diff(self, old_value: Any, new_value: Any) -> dict[Any, Any]:
        """Generate a diff between the old and new values."""
        if isinstance(old_value, dict) and isinstance(new_value, dict):
            diff = {
                key: (old_value.get(key), new_value.get(key))
                for key in new_value
                if old_value.get(key) != new_value.get(key)
            }
        elif isinstance(old_value, list) and isinstance(new_value, list):
            diff = {
                str(i): (old, new) for i, (old, new) in enumerate(zip(old_value, new_value, strict=False)) if old != new
            }

            # Handle added or removed elements
            if len(old_value) > len(new_value):
                for i in range(len(new_value), len(old_value)):
                    diff[str(i)] = (old_value[i], None)
            elif len(new_value) > len(old_value):
                for i in range(len(old_value), len(new_value)):
                    diff[str(i)] = (None, new_value[i])
        else:
            diff = {"old": old_value, "new": new_value}
        return diff

    def _format_diff(self, diff: dict[Any, Any]) -> str:
        """Format the diff dictionary into a readable string."""
        formatted_lines = []
        for key, (old, new) in diff.items():
            if old is None:
                formatted_lines.append(f"[{key}]: ADDED: '{new}'")
            elif new is None:
                formatted_lines.append(f"[{key}]: REMOVED: '{old}'")
            else:
                formatted_lines.append(f"[{key}]:\n\tFROM: '{old}'\n\t  TO: '{new}'")
        return "\n".join(formatted_lines)

    def on_handle_set_config_value_request(self, request: SetConfigValueRequest) -> ResultPayload:
        if request.category_and_key == "":
            result_details = "Attempted to set config value but no category or key was specified."
            return SetConfigValueResultFailure(result_details=result_details)

        # Fetch the existing value (don't go to the env vars directly; we want the key)
        old_value = self.get_config_value(request.category_and_key, should_load_env_var_if_detected=False)

        # Make a copy of the existing value if it is a dict or list
        if isinstance(old_value, (dict, list)):
            old_value_copy = copy.deepcopy(old_value)
        else:
            old_value_copy = old_value

        # Set the new value
        write_succeeded = self.set_config_value(key=request.category_and_key, value=request.value)
        if not write_succeeded:
            result_details = (
                f"Attempted to set config value '{request.category_and_key}'. Failed because the user "
                "config file could not be written; see prior logs for the underlying I/O error."
            )
            return SetConfigValueResultFailure(result_details=result_details)

        # For container types, indicate the change with a diff
        if isinstance(request.value, (dict, list)):
            if old_value_copy is not None:
                diff = self._get_diff(old_value_copy, request.value)
                formatted_diff = self._format_diff(diff)
                if formatted_diff:
                    result_details = f"Successfully updated {type(request.value).__name__} at '{request.category_and_key}'. Changes:\n{formatted_diff}"
                else:
                    result_details = f"Successfully updated {type(request.value).__name__} at '{request.category_and_key}'. No changes detected."
            else:
                result_details = f"Successfully updated {type(request.value).__name__} at '{request.category_and_key}'"
        else:
            result_details = f"Successfully assigned the config value for '{request.category_and_key}':\n\tFROM '{old_value_copy}'\n\tTO: '{request.value}'"

        return SetConfigValueResultSuccess(result_details=result_details)

    def _write_user_config_delta(self, user_config_delta: dict) -> bool:  # noqa: C901, PLR0911, PLR0912, PLR0915
        """Write user configuration delta to config file with atomic read-modify-write.

        This method performs an atomic read-modify-write operation on the user config file:
        1. Checks if config file exists, creates if missing
        2. Reads current config with file locking (prevents concurrent write corruption)
        3. Merges the delta with current config
        4. Writes merged config back with file locking
        5. Reloads all configs to reflect changes

        Uses OSManager request types (GetFileInfoRequest, ReadFileRequest, WriteFileRequest)
        for centralized file I/O with automatic file locking, structured error handling,
        and audit trail capabilities.

        Args:
            user_config_delta: Configuration changes to merge with existing config.
                              Uses dot notation keys (e.g., {"nodes.max_depth": 10})

        Returns:
            True if the merged config was written to disk; False if any step
            (file info, create, read, write) failed. Callers must gate
            worker fan-out on this so workers don't reload from a file that
            wasn't actually updated.
        """
        # Lazy import to avoid circular dependency during initialization
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        os_manager = GriptapeNodes.OSManager()
        config_path_str = str(USER_CONFIG_PATH)

        # Step 1: Check if config file exists
        info_request = GetFileInfoRequest(path=config_path_str, workspace_only=False)
        info_result = os_manager.on_get_file_info_request(info_request)

        # Handle failures getting file info
        if isinstance(info_result, GetFileInfoResultFailure):
            logger.error(
                "Attempted to check if user config exists at '%s'. Failed due to: %s",
                config_path_str,
                info_result.result_details,
            )
            return False

        # Step 2: Create config file if it doesn't exist
        if info_result.file_entry is None:
            logger.info("User config file does not exist at '%s', creating with empty config", config_path_str)

            # Create empty config with proper JSON formatting
            empty_config = json.dumps({}, indent=2)

            create_request = WriteFileRequest(
                file_path=config_path_str,
                content=empty_config,
                encoding="utf-8",
                existing_file_policy=ExistingFilePolicy.FAIL,  # Should not exist, fail if it does
                create_parents=True,  # Create parent directories if missing
                skip_metadata_injection=True,
            )
            create_result = os_manager.on_write_file_request(create_request)

            if isinstance(create_result, WriteFileResultFailure):
                logger.error(
                    "Attempted to create user config file at '%s'. Failed due to: %s",
                    config_path_str,
                    create_result.result_details,
                )
                return False

        # Step 3: Read current config directly from disk.
        #
        # We intentionally bypass the ReadFileRequest handler here. The enclosing writes
        # already use os_manager.on_write_file_request directly (sync), so the read matches
        # that bootstrap-path style and avoids coupling config load to event-loop state.
        try:
            file_content = Path(config_path_str).read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.error(
                "Attempted to read user config at '%s'. File not found despite creation attempt.",
                config_path_str,
            )
            return False
        except PermissionError as e:
            logger.error(
                "Attempted to read user config at '%s'. Permission denied: %s",
                config_path_str,
                e,
            )
            return False
        except UnicodeDecodeError as e:
            logger.error(
                "Attempted to read user config at '%s'. Encoding error: %s",
                config_path_str,
                e,
            )
            return False
        except OSError as e:
            logger.error(
                "Attempted to read user config at '%s'. Failed with: %s",
                config_path_str,
                e,
            )
            return False

        # Step 4: Parse JSON from file content
        try:
            current_config = json.loads(file_content)
        except json.JSONDecodeError as e:
            # Config file is corrupted - back it up and start fresh
            backup_path_str = str(USER_CONFIG_PATH.with_suffix(".bak"))

            logger.warning(
                "User config file at '%s' contained invalid JSON. Attempting to back up to '%s'. Parse error: %s",
                config_path_str,
                backup_path_str,
                str(e),
            )

            # Use RenameFileRequest to back up corrupted file
            rename_request = RenameFileRequest(
                old_path=config_path_str,
                new_path=backup_path_str,
                workspace_only=False,
            )
            rename_result = os_manager.on_rename_file_request(rename_request)

            if isinstance(rename_result, RenameFileResultFailure):
                logger.error(
                    "Failed to back up corrupted config from '%s' to '%s': %s. Using empty config.",
                    config_path_str,
                    backup_path_str,
                    rename_result.result_details,
                )
            else:
                logger.info("Successfully backed up corrupted config to '%s'", backup_path_str)

            # Use empty config regardless of backup success
            current_config = {}

        # Step 5: Merge delta with current config
        merged_config = merge_dicts(current_config, user_config_delta)

        # Step 6: Write merged config back with file locking (atomic write)
        write_request = WriteFileRequest(
            file_path=config_path_str,
            content=json.dumps(merged_config, indent=2),
            encoding="utf-8",
            existing_file_policy=ExistingFilePolicy.OVERWRITE,
            create_parents=True,
        )
        write_result = os_manager.on_write_file_request(write_request)

        # Handle write failures
        if isinstance(write_result, WriteFileResultFailure):
            match write_result.failure_reason:
                case FileIOFailureReason.PERMISSION_DENIED:
                    logger.error(
                        "Attempted to write merged config to '%s'. Permission denied: %s",
                        config_path_str,
                        write_result.result_details,
                    )
                case FileIOFailureReason.DISK_FULL:
                    logger.error(
                        "Attempted to write merged config to '%s'. Disk full: %s",
                        config_path_str,
                        write_result.result_details,
                    )
                case FileIOFailureReason.FILE_LOCKED:
                    logger.error(
                        "Attempted to write merged config to '%s'. File is locked by another process: %s",
                        config_path_str,
                        write_result.result_details,
                    )
                case FileIOFailureReason.IS_DIRECTORY:
                    logger.error(
                        "Attempted to write merged config to '%s'. Path is a directory, not a file: %s",
                        config_path_str,
                        write_result.result_details,
                    )
                case FileIOFailureReason.ENCODING_ERROR:
                    logger.error(
                        "Attempted to write merged config to '%s'. Encoding error: %s",
                        config_path_str,
                        write_result.result_details,
                    )
                case _:
                    logger.error(
                        "Attempted to write merged config to '%s'. Failed with: %s",
                        config_path_str,
                        write_result.result_details,
                    )
            return False

        # Success path: Reload configs to reflect the changes
        logger.debug("Successfully wrote user config delta to '%s', reloading configs", config_path_str)
        return True

    def _set_log_level(self, level: str) -> None:
        """Set the log level for the logger.

        Args:
            level: The log level to set (e.g., 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL').
        """
        try:
            level_upper = level.upper()
            log_level = getattr(logging, level_upper)
            logger.setLevel(log_level)
        except (ValueError, AttributeError):
            logger.error("Invalid log level %s. Defaulting to INFO.", level)
            logger.setLevel(logging.INFO)
