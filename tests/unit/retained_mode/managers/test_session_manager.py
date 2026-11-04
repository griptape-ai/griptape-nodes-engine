"""Tests for the clients a session reports to discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from griptape_nodes.retained_mode.events.app_events import (
    AppEndSessionRequest,
    AppEndSessionResultSuccess,
    AppJoinSessionRequest,
    AppJoinSessionResultFailure,
    AppJoinSessionResultSuccess,
    AppLeaveSessionRequest,
    AppLeaveSessionResultFailure,
    AppLeaveSessionResultSuccess,
    AppStartSessionRequest,
    AppStartSessionResultSuccess,
)
from griptape_nodes.retained_mode.managers.session_manager import SessionManager

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionManager:
    """A session manager whose state file lives under tmp_path."""
    monkeypatch.setattr(
        "griptape_nodes.retained_mode.managers.session_manager.engine_state_dir",
        lambda: tmp_path,
    )
    identity = Mock()
    identity.active_engine_id = "engine-1"
    return SessionManager(identity)


class TestSessionClients:
    def test_attach_is_refused_when_no_session_is_active(self, manager: SessionManager) -> None:
        assert manager.active_session_id is None

        assert manager.attach_client("nuke:shot010", "nuke") is False
        assert manager.clients == []

    def test_attach_joins_the_active_session(self, manager: SessionManager) -> None:
        manager.save_session("session-a")

        assert manager.attach_client("nuke:shot010", "nuke") is True

        (client,) = manager.clients
        assert client.client_id == "nuke:shot010"
        assert client.client_type == "nuke"
        assert client.connected_at

    def test_reattaching_the_same_client_refreshes_rather_than_duplicates(self, manager: SessionManager) -> None:
        manager.save_session("session-a")
        manager.attach_client("nuke:shot010", "nuke")
        first = manager.clients[0].connected_at

        manager.attach_client("nuke:shot010", "nuke")

        assert len(manager.clients) == 1
        assert manager.clients[0].connected_at >= first

    def test_detach_removes_only_that_client(self, manager: SessionManager) -> None:
        manager.save_session("session-a")
        manager.attach_client("nuke:shot010", "nuke")
        manager.attach_client("nuke:shot020", "nuke")

        manager.detach_client("nuke:shot010")
        manager.detach_client("never-attached")

        assert [c.client_id for c in manager.clients] == ["nuke:shot020"]

    @pytest.mark.asyncio
    async def test_starting_a_session_with_a_client_id_lists_that_client(self, manager: SessionManager) -> None:
        result = await manager.handle_session_start_request(
            AppStartSessionRequest(client_id="nuke:shot010", client_type="nuke")
        )

        assert isinstance(result, AppStartSessionResultSuccess)
        assert manager.active_session_id == result.session_id
        assert [(c.client_id, c.client_type) for c in manager.clients] == [("nuke:shot010", "nuke")]

    @pytest.mark.asyncio
    async def test_a_client_label_is_kept_for_display(self, manager: SessionManager) -> None:
        await manager.handle_session_start_request(
            AppStartSessionRequest(client_id="tab-1", client_type="editor", client_label="Kate (Chrome)")
        )

        (client,) = manager.clients
        assert (client.client_type, client.client_label) == ("editor", "Kate (Chrome)")

    @pytest.mark.asyncio
    async def test_joining_with_a_client_id_adds_to_the_existing_session(self, manager: SessionManager) -> None:
        manager.save_session("session-a")

        result = await manager.handle_session_start_request(
            AppStartSessionRequest(client_id="nuke:shot010", client_type="nuke")
        )

        assert isinstance(result, AppStartSessionResultSuccess)
        assert result.session_id == "session-a"
        assert [c.client_id for c in manager.clients] == ["nuke:shot010"]

    @pytest.mark.asyncio
    async def test_an_anonymous_start_lists_nobody(self, manager: SessionManager) -> None:
        await manager.handle_session_start_request(AppStartSessionRequest())

        assert manager.active_session_id is not None
        assert manager.clients == []

    @pytest.mark.asyncio
    async def test_join_lists_the_client_on_the_named_active_session(self, manager: SessionManager) -> None:
        manager.save_session("session-a")

        result = await manager.handle_join_session_request(
            AppJoinSessionRequest(session_id="session-a", client_id="tab-1", client_type="editor", client_label="Kate")
        )

        assert isinstance(result, AppJoinSessionResultSuccess)
        assert result.session_id == "session-a"
        assert [(c.client_id, c.client_label) for c in manager.clients] == [("tab-1", "Kate")]

    @pytest.mark.asyncio
    async def test_join_refuses_a_session_that_is_not_the_active_one(self, manager: SessionManager) -> None:
        manager.save_session("session-a")

        result = await manager.handle_join_session_request(
            AppJoinSessionRequest(session_id="session-stale", client_id="tab-1")
        )

        assert isinstance(result, AppJoinSessionResultFailure)
        assert "session-a" in str(result.result_details)
        assert manager.clients == []

    @pytest.mark.asyncio
    async def test_join_fails_without_a_session(self, manager: SessionManager) -> None:
        result = await manager.handle_join_session_request(AppJoinSessionRequest(session_id="session-a"))

        assert isinstance(result, AppJoinSessionResultFailure)

    @pytest.mark.asyncio
    async def test_join_without_a_client_id_lists_nobody(self, manager: SessionManager) -> None:
        manager.save_session("session-a")

        result = await manager.handle_join_session_request(AppJoinSessionRequest(session_id="session-a"))

        assert isinstance(result, AppJoinSessionResultSuccess)
        assert manager.clients == []

    @pytest.mark.asyncio
    async def test_leave_request_delists_the_client(self, manager: SessionManager) -> None:
        await manager.handle_session_start_request(AppStartSessionRequest(client_id="nuke:shot010", client_type="nuke"))

        result = await manager.handle_leave_session_request(AppLeaveSessionRequest(client_id="nuke:shot010"))

        assert isinstance(result, AppLeaveSessionResultSuccess)
        assert manager.clients == []

    @pytest.mark.asyncio
    async def test_leave_request_fails_without_a_session(self, manager: SessionManager) -> None:
        result = await manager.handle_leave_session_request(AppLeaveSessionRequest(client_id="nuke:shot010"))

        assert isinstance(result, AppLeaveSessionResultFailure)

    @pytest.mark.asyncio
    async def test_ending_the_session_clears_its_clients(self, manager: SessionManager) -> None:
        manager.save_session("session-a")
        manager.attach_client("nuke:shot010", "nuke")

        result = await manager.handle_session_end_request(AppEndSessionRequest())

        assert isinstance(result, AppEndSessionResultSuccess)
        assert manager.clients == []

    def test_clearing_saved_sessions_clears_clients(self, manager: SessionManager) -> None:
        manager.save_session("session-a")
        manager.attach_client("nuke:shot010", "nuke")

        manager.clear_saved_session()

        assert manager.active_session_id is None
        assert manager.clients == []

    def test_switching_to_another_session_drops_the_old_sessions_clients(self, manager: SessionManager) -> None:
        manager.save_session("session-a")
        manager.attach_client("nuke:shot010", "nuke")

        manager.save_session("session-b")

        assert manager.clients == []

    def test_resaving_the_active_session_keeps_its_clients(self, manager: SessionManager) -> None:
        manager.save_session("session-a")
        manager.attach_client("nuke:shot010", "nuke")

        manager.save_session("session-a")

        assert [c.client_id for c in manager.clients] == ["nuke:shot010"]

    def test_removing_the_active_session_drops_its_clients(self, manager: SessionManager) -> None:
        manager.save_session("session-a")
        manager.save_session("session-b")
        manager.attach_client("nuke:shot010", "nuke")

        manager.remove_session("session-b")

        assert manager.active_session_id == "session-a"
        assert manager.clients == []

    def test_removing_another_session_keeps_the_active_sessions_clients(self, manager: SessionManager) -> None:
        manager.save_session("session-a")
        manager.save_session("session-b")
        manager.attach_client("nuke:shot010", "nuke")

        manager.remove_session("session-a")

        assert manager.active_session_id == "session-b"
        assert [c.client_id for c in manager.clients] == ["nuke:shot010"]

    def test_clients_are_not_persisted_with_the_session(self, manager: SessionManager, tmp_path: Path) -> None:
        manager.save_session("session-a")
        manager.attach_client("nuke:shot010", "nuke")

        state = (tmp_path / "engines" / "engine-1" / "sessions.json").read_text(encoding="utf-8")

        assert "nuke:shot010" not in state
