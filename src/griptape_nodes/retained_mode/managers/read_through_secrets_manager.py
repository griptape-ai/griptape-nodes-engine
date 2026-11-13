"""A secrets manager that reads every secret through a reader it is given."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, overload

from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
    from griptape_nodes.retained_mode.managers.event_manager import EventManager


class ReadThroughSecretsManager(SecretsManager):
    """Reads every secret through `read_secret`, and writes none.

    This process's environment and `.env` files are never read, so a secret the reader does not
    answer cannot be found here.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        event_manager: EventManager | None = None,
        *,
        read_secret: Callable[[str], str | None],
    ) -> None:
        """Build the manager.

        Args:
            config_manager: Locates the workspace `.env` file.
            event_manager: Receives the secret request handlers and change notifications.
            read_secret: Answers each secret by name, or None when it has none by that name.
        """
        self._read_secret = read_secret
        super().__init__(config_manager, event_manager)

    @overload
    def get_secret(self, secret_name: str, *, should_error_on_not_found: Literal[True] = True) -> str: ...

    @overload
    def get_secret(self, secret_name: str, *, should_error_on_not_found: Literal[False]) -> str | None: ...

    def get_secret(self, secret_name: str, *, should_error_on_not_found: bool = True) -> str | None:
        """Return what `read_secret` answers for `secret_name`."""
        secret_name = SecretsManager._apply_secret_name_compliance(secret_name)
        secret_value = self._read_secret(secret_name)
        if secret_value is None and should_error_on_not_found:
            SecretsManager._log_missing_secret(secret_name)
        return secret_value

    def set_secret(self, secret_name: str, secret_value: str) -> None:  # noqa: ARG002
        """Refuse: the secrets belong to whatever `read_secret` reads from.

        Raises:
            RuntimeError: Always.
        """
        msg = (
            f"Attempted to set the secret '{secret_name}'. Failed because this process reads its secrets "
            "from the engine that started it; set it there instead."
        )
        raise RuntimeError(msg)

    def register_all_secrets(self) -> None:
        """Do nothing: whatever `read_secret` reads from registers them."""

    def refresh_from_env_file(self) -> None:
        """Do nothing: no secret is read from this machine's `.env` files."""

    def _load_env_files_into_environ(self) -> None:
        """Do nothing: no secret is read from this machine's `.env` files."""
