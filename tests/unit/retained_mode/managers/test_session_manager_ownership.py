"""Tests for session resume ownership.

Engine identity is re-adopted from disk (`engines.json` default_engine_id), so two engines
started on one machine resolve to the same `engines/<engine_id>/sessions.json`. Resuming the
saved session unconditionally meant both processes subscribed to `sessions/<id>/request` and
BOTH serviced every client request. Reads were merely duplicated; requests with side effects
ran twice, which is how a single click on a library's "Open" button opened two file browsers,
both resolving the older engine's library path.

A session now records the PID that claimed it, and boot skips any session another live
process is still serving.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.retained_mode.managers import session_manager as session_manager_module
from griptape_nodes.retained_mode.managers.session_manager import SessionManager

if TYPE_CHECKING:
    from pathlib import Path

SESSION_ID = "b06cea3a68bd4a0ca4fa6e9d205fed46"
ENGINE_ID = "engine-under-test"
OTHER_PID = 999_001


def _write_sessions(state_home: Path, *, owner_pid: int | None) -> None:
    """Persist one saved session, optionally stamped with an owning PID."""
    session: dict[str, object] = {
        "session_id": SESSION_ID,
        "engine_id": ENGINE_ID,
        "started_at": "2026-08-28T14:19:33+00:00",
        "last_updated": "2026-08-28T14:19:33+00:00",
    }
    if owner_pid is not None:
        session["owner_pid"] = owner_pid
    sessions_dir = state_home / "griptape_nodes" / "engines" / ENGINE_ID
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "sessions.json").write_text(json.dumps({"sessions": [session]}), encoding="utf-8")


@pytest.fixture
def state_home(tmp_path: Path) -> Path:
    """Point the manager's XDG state lookup at a temp dir."""
    return tmp_path / "state"


def _build_manager(state_home: Path) -> SessionManager:
    identity = MagicMock()
    identity.active_engine_id = ENGINE_ID
    with patch.object(session_manager_module, "xdg_state_home", return_value=state_home):
        return SessionManager(identity)


class TestSessionResumeOwnership:
    def test_resumes_a_session_with_no_recorded_owner(self, state_home: Path) -> None:
        """Records written before owner_pid existed carry no ownership info, so they resume."""
        _write_sessions(state_home, owner_pid=None)

        manager = _build_manager(state_home)

        assert manager.active_session_id == SESSION_ID

    def test_resumes_a_session_this_process_owns(self, state_home: Path) -> None:
        """An engine resuming its own session across a reconnect is the normal case."""
        _write_sessions(state_home, owner_pid=session_manager_module.os.getpid())

        manager = _build_manager(state_home)

        assert manager.active_session_id == SESSION_ID

    def test_resumes_a_session_whose_owner_has_exited(self, state_home: Path) -> None:
        """The reattach-after-restart case: the previous engine is gone, so resume is safe."""
        _write_sessions(state_home, owner_pid=OTHER_PID)

        with patch.object(session_manager_module.os, "kill", side_effect=ProcessLookupError):
            manager = _build_manager(state_home)

        assert manager.active_session_id == SESSION_ID

    def test_does_not_resume_a_session_a_live_process_still_owns(self, state_home: Path) -> None:
        """The reported failure: a second engine must not join a session already being served.

        Returning None here makes the next AppStartSessionRequest mint a fresh id, so the two
        engines never share a request topic and side-effecting requests run once.
        """
        _write_sessions(state_home, owner_pid=OTHER_PID)

        with patch.object(session_manager_module.os, "kill", return_value=None):
            manager = _build_manager(state_home)

        assert manager.active_session_id is None

    def test_a_pid_owned_by_another_user_is_not_one_of_our_engines(self, state_home: Path) -> None:
        """PID reuse by an unrelated process must not strand the session forever."""
        _write_sessions(state_home, owner_pid=OTHER_PID)

        with patch.object(session_manager_module.os, "kill", side_effect=PermissionError):
            manager = _build_manager(state_home)

        assert manager.active_session_id == SESSION_ID

    def test_saving_a_session_stamps_the_owning_pid(self, state_home: Path) -> None:
        """Without the stamp the next engine cannot tell an orphan from a live session."""
        manager = _build_manager(state_home)

        with patch.object(session_manager_module, "xdg_state_home", return_value=state_home):
            manager.save_session(SESSION_ID)

        saved = json.loads(
            (state_home / "griptape_nodes" / "engines" / ENGINE_ID / "sessions.json").read_text(encoding="utf-8")
        )
        assert saved["sessions"][0]["owner_pid"] == session_manager_module.os.getpid()
