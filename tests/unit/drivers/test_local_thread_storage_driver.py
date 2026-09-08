"""Unit tests for the file-system thread storage driver.

The driver round-trips Pydantic AI ``ModelMessage`` history and a small JSON
metadata blob per thread. These tests exercise both, plus the metadata-bound
operations (rename, archive, delete, list).
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from pydantic_ai.messages import ImageUrl, ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

from griptape_nodes.drivers.thread_storage.local_thread_storage_driver import LocalThreadStorageDriver
from griptape_nodes.retained_mode.events.agent_events import RunRecord

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def storage(tmp_path: Path) -> LocalThreadStorageDriver:
    """Build a driver against a fresh tmp directory and stubbed managers."""
    return LocalThreadStorageDriver(tmp_path, config_manager=None, secrets_manager=None)  # type: ignore[arg-type]


def test_create_thread_returns_id_and_metadata(storage: LocalThreadStorageDriver) -> None:
    """create_thread returns a fresh id plus metadata, and the thread becomes visible."""
    thread_id, meta = storage.create_thread(title="planning", local_id="local-1")
    assert thread_id
    assert meta["title"] == "planning"
    assert meta["local_id"] == "local-1"
    assert "created_at" in meta
    assert "updated_at" in meta
    assert storage.thread_exists(thread_id)


def test_save_and_load_history_round_trips(storage: LocalThreadStorageDriver) -> None:
    """Saved Pydantic AI history reloads as the same typed messages."""
    thread_id, _ = storage.create_thread()
    history = [
        ModelRequest(parts=[UserPromptPart(content="Hi")]),
        ModelResponse(parts=[TextPart(content="Hello!")]),
    ]
    storage.save_history(thread_id, history)
    reloaded = storage.load_history(thread_id)
    assert len(reloaded) == 2  # noqa: PLR2004
    assert isinstance(reloaded[0], ModelRequest)
    assert isinstance(reloaded[1], ModelResponse)
    assert any(isinstance(p, TextPart) and p.content == "Hello!" for p in reloaded[1].parts)


def test_image_url_in_user_prompt_round_trips(storage: LocalThreadStorageDriver) -> None:
    """A user prompt carrying an ImageUrl reference survives save/load intact."""
    thread_id, _ = storage.create_thread()
    url = "http://localhost:8124/workspace/cat.png"
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content=["look", ImageUrl(url=url)])])]
    storage.save_history(thread_id, history)
    reloaded = storage.load_history(thread_id)

    assert isinstance(reloaded[0], ModelRequest)
    part = reloaded[0].parts[0]
    assert isinstance(part, UserPromptPart)
    assert part.content[0] == "look"
    assert isinstance(part.content[1], ImageUrl)
    assert part.content[1].url == url


def test_list_threads_returns_message_counts_and_sorts(storage: LocalThreadStorageDriver) -> None:
    """list_threads reports per-thread message counts and orders by recency.

    The clock is stubbed so successive writes get strictly increasing timestamps.
    This keeps the recency assertion deterministic on platforms with coarse
    wall-clock resolution (e.g. Windows), where two real back-to-back writes can
    otherwise land on the same tick and sort arbitrarily.
    """
    base = datetime(2024, 1, 1, tzinfo=UTC)
    tick = itertools.count()

    def increasing_now(_tz: object = None) -> datetime:
        return base + timedelta(seconds=next(tick))

    with patch("griptape_nodes.drivers.thread_storage.local_thread_storage_driver.datetime") as mock_datetime:
        mock_datetime.now.side_effect = increasing_now

        older_id, _ = storage.create_thread(title="older")
        newer_id, _ = storage.create_thread(title="newer")

        storage.save_history(older_id, [ModelRequest(parts=[UserPromptPart(content="Hi")])])
        storage.save_history(
            newer_id,
            [
                ModelRequest(parts=[UserPromptPart(content="Q1")]),
                ModelResponse(parts=[TextPart(content="A1")]),
            ],
        )

        threads = storage.list_threads()

    by_id = {t.thread_id: t for t in threads}
    assert by_id[older_id].message_count == 1
    assert by_id[newer_id].message_count == 2  # noqa: PLR2004
    # Most-recently-updated first.
    assert threads[0].thread_id == newer_id


def test_archive_then_delete(storage: LocalThreadStorageDriver) -> None:
    """Threads must be archived before they can be deleted."""
    thread_id, _ = storage.create_thread(title="ephemeral")

    with pytest.raises(ValueError, match="Archive it first"):
        storage.delete_thread(thread_id)

    storage.update_thread_metadata(thread_id, archived=True)
    storage.delete_thread(thread_id)
    assert not storage.thread_exists(thread_id)


def test_load_history_for_unknown_thread_returns_empty(storage: LocalThreadStorageDriver) -> None:
    """load_history of an unknown thread returns an empty list, not an exception."""
    assert storage.load_history("does-not-exist") == []


def test_load_history_preserves_corrupt_file(storage: LocalThreadStorageDriver) -> None:
    """A corrupt history file is moved aside, not silently destroyed, on load failure."""
    thread_id, _ = storage.create_thread()
    history_path = storage.threads_directory / f"thread_{thread_id}.json"
    history_path.write_bytes(b"not valid json")

    assert storage.load_history(thread_id) == []

    # The unreadable bytes survive in a sibling backup so they are recoverable.
    backups = list(storage.threads_directory.glob(f"thread_{thread_id}.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"not valid json"
    # A subsequent save does not clobber the preserved data.
    storage.save_history(thread_id, [ModelRequest(parts=[UserPromptPart(content="Hi")])])
    assert backups[0].read_bytes() == b"not valid json"


def test_update_thread_metadata_rename(storage: LocalThreadStorageDriver) -> None:
    """Renaming a thread overwrites the title and persists."""
    thread_id, _ = storage.create_thread(title="old name")
    updated = storage.update_thread_metadata(thread_id, title="new name")
    assert updated["title"] == "new name"
    assert storage.get_thread_metadata(thread_id)["title"] == "new name"


def test_append_run_record_appears_in_get_thread_metadata_full(storage: LocalThreadStorageDriver) -> None:
    """append_run_record persists a RunRecord that get_thread_metadata_full returns."""
    thread_id, _ = storage.create_thread(title="conversation")
    record = RunRecord(message_index=1, provider_name="griptape_cloud", model="claude-sonnet-5", mcp_servers=["brave"])
    storage.append_run_record(thread_id, record)

    thread = storage.get_thread_metadata_full(thread_id)
    assert len(thread.runs) == 1
    assert thread.runs[0] == record


def test_append_run_record_accumulates_across_turns(storage: LocalThreadStorageDriver) -> None:
    """Multiple append_run_record calls accumulate in order, not overwrite."""
    thread_id, _ = storage.create_thread()
    storage.append_run_record(thread_id, RunRecord(message_index=1, provider_name="ollama", model="llama3"))
    storage.append_run_record(thread_id, RunRecord(message_index=3, provider_name="griptape_cloud", model="gpt-4o"))

    thread = storage.get_thread_metadata_full(thread_id)
    assert len(thread.runs) == 2  # noqa: PLR2004
    assert thread.runs[0].message_index == 1
    assert thread.runs[1].message_index == 3  # noqa: PLR2004


def test_list_threads_omits_runs(storage: LocalThreadStorageDriver) -> None:
    """list_threads does not load runs — use get_thread_metadata_full for that."""
    thread_id, _ = storage.create_thread(title="fast-list")
    storage.append_run_record(thread_id, RunRecord(message_index=1, provider_name="ollama", model="llama3"))

    thread = next(t for t in storage.list_threads() if t.thread_id == thread_id)
    assert thread.runs == [], "list_threads must not deserialize runs"


def test_get_thread_metadata_full_skips_malformed_run_records(storage: LocalThreadStorageDriver) -> None:
    """A malformed run record in meta.json is skipped without crashing get_thread_metadata_full."""
    thread_id, _ = storage.create_thread(title="resilient")
    meta_path = storage.threads_directory / f"thread_{thread_id}.meta.json"
    meta = json.loads(meta_path.read_text())
    meta["runs"] = [
        {"bad_field": "value"},  # missing required fields — must be skipped
        {"message_index": 1, "provider_name": "ollama", "model": "llama3", "mcp_servers": []},
    ]
    meta_path.write_text(json.dumps(meta))

    thread = storage.get_thread_metadata_full(thread_id)
    assert len(thread.runs) == 1
    assert thread.runs[0].message_index == 1
