"""Base thread storage driver abstract class.

Threads carry a Pydantic AI message history (``list[ModelMessage]``) plus a
metadata dictionary that the chat sidebar uses for thread listing, archiving,
and titles. Backends differ in where they put that data (local JSON files,
Griptape Cloud, etc.) but they all expose the same surface here.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.events.agent_events import ThreadMetadata
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage


class BaseThreadStorageDriver(ABC):
    """Abstract base class for thread storage backends."""

    def __init__(self, config_manager: ConfigManager, secrets_manager: SecretsManager) -> None:
        """Initialize the thread storage driver.

        Args:
            config_manager: Configuration manager instance.
            secrets_manager: Secrets manager instance.
        """
        self.config_manager = config_manager
        self.secrets_manager = secrets_manager

    @abstractmethod
    def create_thread(self, title: str | None = None, local_id: str | None = None) -> tuple[str, dict]:
        """Create a new thread with metadata.

        Args:
            title: Optional thread title.
            local_id: Optional client-side identifier.

        Returns:
            Tuple of (thread_id, metadata_dict).
        """
        raise NotImplementedError

    @abstractmethod
    def get_thread_metadata(self, thread_id: str) -> dict:
        """Get metadata for a thread.

        Args:
            thread_id: The thread identifier.

        Returns:
            Metadata dictionary.
        """
        raise NotImplementedError

    @abstractmethod
    def update_thread_metadata(self, thread_id: str, **updates: object) -> dict:
        """Update thread metadata.

        Args:
            thread_id: The thread identifier.
            **updates: Key-value pairs to merge into existing metadata.

        Returns:
            Updated metadata dictionary.
        """
        raise NotImplementedError

    @abstractmethod
    def list_threads(self) -> list[ThreadMetadata]:
        """List every thread, sorted most-recently-updated first."""
        raise NotImplementedError

    @abstractmethod
    def delete_thread(self, thread_id: str) -> None:
        """Delete a thread.

        Args:
            thread_id: The thread identifier.

        Raises:
            ValueError: If the thread is not archived or does not exist.
        """
        raise NotImplementedError

    @abstractmethod
    def thread_exists(self, thread_id: str) -> bool:
        """Return True iff this backend currently holds a thread with this id."""
        raise NotImplementedError

    @abstractmethod
    def load_history(self, thread_id: str) -> list["ModelMessage"]:
        """Load the persisted Pydantic AI message history for a thread.

        Returns an empty list when the thread has no history yet (e.g. a brand
        new thread). The caller is responsible for handling missing threads
        via :meth:`thread_exists`.
        """
        raise NotImplementedError

    @abstractmethod
    def save_history(self, thread_id: str, messages: list["ModelMessage"]) -> None:
        """Persist a Pydantic AI message history for a thread.

        Implementations must overwrite, not append: the caller passes the full
        history every time. Implementations are also responsible for bumping
        ``updated_at`` in metadata so the thread floats to the top of listings.
        """
        raise NotImplementedError
