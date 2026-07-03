"""Local filesystem thread storage driver, backed by Pydantic AI message history.

Each thread lives in two files inside ``threads_directory``:

  * ``thread_{id}.json``       - the message history, encoded by
    :class:`pydantic_ai.messages.ModelMessagesTypeAdapter`.
  * ``thread_{id}.meta.json``  - a small metadata dict (title, timestamps,
    archived flag, optional ``local_id``).

Splitting the two keeps history reads cheap when listing threads (we don't
deserialize messages we never show) and keeps metadata writes atomic when the
agent isn't actually saving any new messages.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic_ai.messages import ModelMessagesTypeAdapter

from griptape_nodes.drivers.thread_storage.base_thread_storage_driver import BaseThreadStorageDriver
from griptape_nodes.retained_mode.events.agent_events import ThreadMetadata
from griptape_nodes.utils.file_utils import atomic_write_bytes

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage

    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
    from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager


logger = logging.getLogger("griptape_nodes")


class LocalThreadStorageDriver(BaseThreadStorageDriver):
    """Filesystem-backed thread storage."""

    def __init__(
        self,
        threads_directory: Path,
        config_manager: ConfigManager,
        secrets_manager: SecretsManager,
    ) -> None:
        super().__init__(config_manager, secrets_manager)
        threads_directory.mkdir(parents=True, exist_ok=True)
        self.threads_directory = threads_directory

    def create_thread(self, title: str | None = None, local_id: str | None = None) -> tuple[str, dict]:
        thread_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        meta: dict[str, Any] = {"created_at": now, "updated_at": now, "message_count": 0}
        if title is not None:
            meta["title"] = title
        if local_id is not None:
            meta["local_id"] = local_id

        self._write_meta(thread_id, meta)
        atomic_write_bytes(self._history_path(thread_id), b"[]")
        return thread_id, meta

    def get_thread_metadata(self, thread_id: str) -> dict:
        return self._read_meta(thread_id)

    def update_thread_metadata(self, thread_id: str, **updates: object) -> dict:
        meta = self._read_meta(thread_id)
        for key, value in updates.items():
            if value is not None:
                meta[key] = value
        meta["updated_at"] = datetime.now(UTC).isoformat()
        meta.setdefault("created_at", meta["updated_at"])
        self._write_meta(thread_id, meta)
        return meta

    def list_threads(self) -> list[ThreadMetadata]:
        if not self.threads_directory.exists():
            return []

        threads: list[ThreadMetadata] = []
        for meta_file in self.threads_directory.glob("thread_*.meta.json"):
            thread_id = meta_file.stem.removeprefix("thread_").removesuffix(".meta")
            meta = self._read_meta(thread_id)
            threads.append(
                ThreadMetadata(
                    thread_id=thread_id,
                    title=meta.get("title"),
                    created_at=meta.get("created_at", ""),
                    updated_at=meta.get("updated_at", ""),
                    message_count=meta.get("message_count", 0),
                    archived=meta.get("archived", False),
                    local_id=meta.get("local_id"),
                ),
            )

        threads.sort(key=lambda t: t.updated_at, reverse=True)
        return threads

    def delete_thread(self, thread_id: str) -> None:
        if not self.thread_exists(thread_id):
            msg = f"Thread {thread_id} not found"
            raise ValueError(msg)

        meta = self._read_meta(thread_id)
        if not meta.get("archived", False):
            msg = f"Cannot delete thread {thread_id}. Archive it first."
            raise ValueError(msg)

        self._history_path(thread_id).unlink(missing_ok=True)
        self._meta_path(thread_id).unlink(missing_ok=True)

    def thread_exists(self, thread_id: str) -> bool:
        return self._meta_path(thread_id).exists()

    def load_history(self, thread_id: str) -> list[ModelMessage]:
        path = self._history_path(thread_id)
        if not path.exists():
            return []
        raw = path.read_bytes()
        if not raw:
            return []
        try:
            return list(ModelMessagesTypeAdapter.validate_json(raw))
        except Exception:
            backup_path = self._backup_corrupt_history(path)
            logger.exception(
                "Failed to load thread history at %s; preserved the file as %s and starting fresh.",
                path,
                backup_path,
            )
            return []

    def save_history(self, thread_id: str, messages: list[ModelMessage]) -> None:
        atomic_write_bytes(self._history_path(thread_id), ModelMessagesTypeAdapter.dump_json(list(messages)))
        meta = self._read_meta(thread_id)
        now = datetime.now(UTC).isoformat()
        meta.setdefault("created_at", now)
        meta["updated_at"] = now
        meta["message_count"] = len(messages)
        self._write_meta(thread_id, meta)

    def _history_path(self, thread_id: str) -> Path:
        return self.threads_directory / f"thread_{thread_id}.json"

    def _meta_path(self, thread_id: str) -> Path:
        return self.threads_directory / f"thread_{thread_id}.meta.json"

    def _read_meta(self, thread_id: str) -> dict[str, Any]:
        path = self._meta_path(thread_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.exception("Failed to read thread metadata at %s.", path)
            return {}

    def _write_meta(self, thread_id: str, meta: dict[str, Any]) -> None:
        atomic_write_bytes(self._meta_path(thread_id), json.dumps(meta, indent=2).encode("utf-8"))

    def _backup_corrupt_history(self, path: Path) -> Path:
        """Move an unreadable history file aside so the next save can't destroy it.

        Returns the backup path so the caller can log where the data went.
        """
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        backup_path = path.with_name(f"{path.name}.corrupt-{timestamp}")
        try:
            path.replace(backup_path)
        except OSError:
            logger.exception("Failed to preserve corrupt thread history at %s.", path)
            return path
        return backup_path
