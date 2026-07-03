import logging
import os
import re
from pathlib import Path
from typing import Literal, overload

from dotenv import dotenv_values, get_key, set_key, unset_key
from dotenv.main import DotEnv
from xdg_base_dirs import xdg_config_home

from griptape_nodes.retained_mode.events.app_events import SecretChanged
from griptape_nodes.retained_mode.events.base_events import ResultPayload
from griptape_nodes.retained_mode.events.secrets_events import (
    DeleteSecretValueRequest,
    DeleteSecretValueResultFailure,
    DeleteSecretValueResultSuccess,
    GetAllSecretValuesRequest,
    GetAllSecretValuesResultSuccess,
    GetSecretValueRequest,
    GetSecretValueResultFailure,
    GetSecretValueResultSuccess,
    SetSecretValueRequest,
    SetSecretValueResultSuccess,
)
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.settings import SECRETS_TO_REGISTER_KEY
from griptape_nodes.utils.dict_utils import normalize_secrets_to_register

logger = logging.getLogger("griptape_nodes")

ENV_VAR_PATH = xdg_config_home() / "griptape_nodes" / ".env"


class SecretsManager:
    def __init__(self, config_manager: ConfigManager, event_manager: EventManager | None = None) -> None:
        self.config_manager = config_manager
        self._event_manager = event_manager

        # Track keys this manager itself has installed into os.environ from a
        # .env file -- distinct from "keys seen in a file." The single
        # invariant: ``self._managed_env_keys`` exactly equals the set of
        # keys this manager has written to ``os.environ`` from a file. Only
        # managed keys are safe to overwrite or pop in
        # ``refresh_from_env_file`` and the delete-secret handler; OS-set
        # keys (e.g. container-injected env vars) must be left alone even
        # if they collide with a .env entry. The invariant is held by
        # routing every ``os.environ`` mutation through
        # ``_install_managed`` / ``_uninstall_managed``.
        self._managed_env_keys: set[str] = set()

        self._load_env_files_into_environ()

        if event_manager is not None:
            self._register_handlers(event_manager)

    def refresh_from_env_file(self) -> None:
        """Re-read the .env files into os.environ for keys this manager owns.

        Same-machine workers share ~/.config/griptape_nodes/.env with the
        orchestrator, but each process captures the file contents into os.environ
        at boot. When the orchestrator updates the file, a worker's os.environ
        keeps the old value -- and get_secret() sees environment variables first,
        so it returns the stale value.

        Override only keys this manager already installed from a file
        (tracked in ``_managed_env_keys``). A key that is also a real OS
        env var -- e.g. a container-injected ``OPENAI_API_KEY`` that
        collides with a .env entry -- was preserved over the file at boot
        and must keep being preserved here. Otherwise an unrelated secret
        change could silently swap an operator-set value for the file's.

        Drops managed keys that disappeared from both files. Only managed
        keys are eligible to pop -- removing a key from the file does not
        give the manager license to delete an unrelated OS env var that
        happens to share the name. If neither file exists at refresh time,
        no pop happens at all: a transient missing file must not wipe
        state.
        """
        if not ENV_VAR_PATH.exists() and not self.workspace_env_path.exists():
            logger.debug("No .env files to refresh from; leaving os.environ untouched.")
            return

        merged = self._read_merged_env_files()
        previously_managed = set(self._managed_env_keys)
        installed = 0
        overridden = 0
        survived: set[str] = set()
        for key, value in merged.items():
            if key in previously_managed:
                # Manager-owned: overwrite with the file's value.
                self._install_managed(key, value)
                survived.add(key)
                overridden += 1
            elif key not in os.environ:
                # New file entry that does not collide with an OS-set var.
                self._install_managed(key, value)
                survived.add(key)
                installed += 1
            # else: OS-set value the manager does not own. Leave it alone.

        # Pop managed keys that vanished from both files. Skip non-managed
        # keys: removing a key from a file does not authorize wiping an OS
        # env var that happens to share the name.
        popped = 0
        for key in previously_managed - survived:
            self._uninstall_managed(key)
            popped += 1

        logger.debug(
            "Refreshed secrets: installed=%d overridden=%d popped=%d managed_total=%d",
            installed,
            overridden,
            popped,
            len(self._managed_env_keys),
        )

    @property
    def workspace_env_path(self) -> Path:
        return self.config_manager.workspace_path / ".env"

    @property
    def secrets_to_register(self) -> dict[str, str]:
        """Get secrets_to_register as a dict, normalizing list format to dict."""
        value = self.config_manager.get_config_value(SECRETS_TO_REGISTER_KEY, default={})
        return normalize_secrets_to_register(value)

    def register_all_secrets(self) -> None:
        """Register all secrets from config and library settings.

        This should be called after libraries are loaded and their settings
        are merged into the config.
        """
        for secret_name, default_value in self.secrets_to_register.items():
            if self.get_secret(secret_name, should_error_on_not_found=False) is None:
                self.set_secret(secret_name, default_value)

    def on_handle_get_secret_request(self, request: GetSecretValueRequest) -> ResultPayload:
        secret_key = SecretsManager._apply_secret_name_compliance(request.key)
        secret_value = self.get_secret(secret_key, should_error_on_not_found=request.should_error_on_not_found)

        if secret_value is None and request.should_error_on_not_found:
            details = f"Secret '{secret_key}' not found."
            logger.error(details)
            return GetSecretValueResultFailure(result_details=details)

        return GetSecretValueResultSuccess(
            value=secret_value, result_details=f"Successfully retrieved secret value for key: {secret_key}"
        )

    def on_handle_set_secret_request(self, request: SetSecretValueRequest) -> ResultPayload:
        secret_name = SecretsManager._apply_secret_name_compliance(request.key)
        secret_value = request.value

        # We don't want to echo the secret value back to the user, but we can at least tell them it changed.
        old_value = self.get_secret(secret_name, should_error_on_not_found=False)
        if old_value == secret_value:
            logger.info("Attempted to update secret '%s' but no change detected.", secret_name)
        elif old_value:
            logger.info("Secret '%s' changed.", secret_name)
        else:
            logger.info("Created secret '%s'", secret_name)

        self.set_secret(secret_name, secret_value)

        # Domain event on success only -- listeners (e.g. WorkerManager) decide
        # what to do with it. set_secret raises on a write failure, so reaching
        # this line means the .env file was updated.
        if self._event_manager is not None:
            self._event_manager.broadcast_app_event(SecretChanged(key=secret_name))

        return SetSecretValueResultSuccess(result_details=f"Successfully set secret value for key: {secret_name}")

    def on_handle_get_all_secret_values_request(self, request: GetAllSecretValuesRequest) -> ResultPayload:  # noqa: ARG002
        secret_values = dotenv_values(ENV_VAR_PATH)

        return GetAllSecretValuesResultSuccess(
            values=secret_values, result_details=f"Successfully retrieved {len(secret_values)} secret values"
        )

    def on_handle_delete_secret_value_request(self, request: DeleteSecretValueRequest) -> ResultPayload:
        secret_name = SecretsManager._apply_secret_name_compliance(request.key)

        if not ENV_VAR_PATH.exists():
            details = f"Secret file does not exist: '{ENV_VAR_PATH}'"
            logger.error(details)
            return DeleteSecretValueResultFailure(result_details=details)

        if get_key(ENV_VAR_PATH, secret_name) is None:
            details = f"Secret {secret_name} not found in {ENV_VAR_PATH}"
            logger.error(details)
            return DeleteSecretValueResultFailure(result_details=details)

        unset_key(ENV_VAR_PATH, secret_name)
        # ``_uninstall_managed`` is a no-op when the manager does not own
        # the key, which preserves a colliding OS-set env var
        # (operator-injected) that survived boot via override=False.
        self._uninstall_managed(secret_name)

        logger.info("Secret '%s' deleted.", secret_name)

        if self._event_manager is not None:
            self._event_manager.broadcast_app_event(SecretChanged(key=secret_name))

        return DeleteSecretValueResultSuccess(result_details=f"Successfully deleted secret: {secret_name}")

    @overload
    def get_secret(self, secret_name: str, *, should_error_on_not_found: Literal[True] = True) -> str: ...

    @overload
    def get_secret(self, secret_name: str, *, should_error_on_not_found: Literal[False]) -> str | None: ...

    def get_secret(self, secret_name: str, *, should_error_on_not_found: bool = True) -> str | None:
        """Return the secret value with the following search precedence (highest to lowest priority).

        1. OS environment variables (highest priority)
        2. Workspace .env file (<workspace>/.env)
        3. Global .env file (~/.config/griptape_nodes/.env) (lowest priority)
        """
        secret_name = SecretsManager._apply_secret_name_compliance(secret_name)

        search_order = [
            ("environment variables", lambda: os.getenv(secret_name)),
            (str(self.workspace_env_path), lambda: DotEnv(self.workspace_env_path).get(secret_name)),
            (str(ENV_VAR_PATH), lambda: DotEnv(ENV_VAR_PATH).get(secret_name)),
        ]

        value = None
        for source, fetch in search_order:
            value = fetch()
            if value is not None:
                logger.debug("Secret '%s' found in '%s'", secret_name, source)
                return value
            logger.debug("Secret '%s' not found in '%s'", secret_name, source)

        if should_error_on_not_found:
            logger.error("Secret '%s' not found", secret_name)
        return value

    def set_secret(self, secret_name: str, secret_value: str) -> None:
        if not ENV_VAR_PATH.exists():
            ENV_VAR_PATH.touch()
        set_key(ENV_VAR_PATH, secret_name, secret_value)
        # An explicit set_secret call is the user's stated intent: the new
        # value wins from now on, and the manager claims ownership of the
        # key. If the key was previously OS-set (operator-injected) the
        # manager is overwriting it -- warn so the asymmetry with the
        # refresh / delete paths (which preserve OS-set values) is not
        # silent.
        if secret_name not in self._managed_env_keys and secret_name in os.environ:
            logger.warning(
                "set_secret('%s') is overriding an OS-set environment variable. "
                "The OS-set value is replaced for the lifetime of this process; "
                "restart with the operator-set value cleared to revert.",
                secret_name,
            )
        self._install_managed(secret_name, secret_value)

    def _load_env_files_into_environ(self) -> None:
        """Read both .env files into ``os.environ`` and seed ``_managed_env_keys``.

        Mirrors the precedence ``get_secret`` documents: OS env beats .env
        files; among files, workspace beats global. Reads each file once
        via ``dotenv_values`` and applies values manually so the precedence
        rule is expressed in exactly one place (this method) and the rest
        of the manager keeps its bookkeeping invariant via
        ``_install_managed`` / ``_uninstall_managed``.

        A key already present in ``os.environ`` before this runs is
        treated as OS-set (operator-injected, container-injected, etc.)
        and is left untouched, which is what ``override=False`` semantics
        delivered before.
        """
        pre_load_environ = set(os.environ.keys())
        merged = self._read_merged_env_files()
        for key, value in merged.items():
            if key in pre_load_environ:
                # OS-owned. Leave alone.
                continue
            self._install_managed(key, value)

    def _read_merged_env_files(self) -> dict[str, str]:
        """Return the merged contents of both .env files with workspace winning.

        Workspace overrides global because that is the precedence order
        ``get_secret`` documents. Empty values (``FOO=`` in the file) are
        kept as empty strings; missing files are skipped. ``None`` values
        from ``dotenv_values`` are filtered out so callers can rely on
        ``dict[str, str]`` shape.
        """
        merged: dict[str, str] = {}
        if ENV_VAR_PATH.exists():
            merged.update({k: v for k, v in dotenv_values(ENV_VAR_PATH).items() if v is not None})
        if self.workspace_env_path.exists():
            merged.update({k: v for k, v in dotenv_values(self.workspace_env_path).items() if v is not None})
        return merged

    def _register_handlers(self, event_manager: EventManager) -> None:
        """Wire request types to their handlers."""
        event_manager.assign_manager_to_request_type(GetSecretValueRequest, self.on_handle_get_secret_request)
        event_manager.assign_manager_to_request_type(SetSecretValueRequest, self.on_handle_set_secret_request)
        event_manager.assign_manager_to_request_type(
            GetAllSecretValuesRequest, self.on_handle_get_all_secret_values_request
        )
        event_manager.assign_manager_to_request_type(
            DeleteSecretValueRequest, self.on_handle_delete_secret_value_request
        )

    def _install_managed(self, key: str, value: str) -> None:
        """Write ``key`` to ``os.environ`` and claim it as manager-owned.

        Single source of truth for installs into ``os.environ`` from a
        .env file. Idempotent: re-installing an already-managed key is a
        plain overwrite. The bookkeeping invariant is held here -- every
        caller that touches ``os.environ`` for a file-sourced value goes
        through this method (or its inverse, ``_uninstall_managed``).
        """
        os.environ[key] = value
        self._managed_env_keys.add(key)

    def _uninstall_managed(self, key: str) -> None:
        """Pop ``key`` from ``os.environ`` and release manager ownership.

        Counterpart to ``_install_managed``. No-op when ``key`` is not in
        ``_managed_env_keys`` -- popping a key the manager does not own
        would delete an OS-set env var (operator-injected, container-
        injected) the manager has no business touching. The ownership
        check lives here so callers cannot violate the invariant by
        forgetting to guard.
        """
        if key not in self._managed_env_keys:
            return
        os.environ.pop(key, None)
        self._managed_env_keys.discard(key)

    @staticmethod
    def _apply_secret_name_compliance(secret_name: str) -> str:
        # Ensure the string is in uppercase
        string = secret_name.upper()

        # Replace any spaces or invalid characters with underscores
        string = re.sub(r"\W+", "_", string)

        # Ensure it doesn't start with a number by prefixing an underscore if necessary
        if string and string[0].isdigit():
            string = "_" + string

        return string
