"""Manages session state and saving using XDG state directory.

Handles storing and retrieving multiple session information across engine restarts.
Sessions are tied to specific engines, with each engine maintaining its own session store.
Supports multiple concurrent sessions per engine with one active session.
Storage structure: ~/.local/state/griptape_nodes/engines/{engine_id}/sessions.json
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel
from xdg_base_dirs import xdg_state_home

from griptape_nodes.retained_mode.events.app_events import (
    AppEndSessionRequest,
    AppEndSessionResultFailure,
    AppEndSessionResultSuccess,
    AppGetSessionRequest,
    AppGetSessionResultSuccess,
    AppStartSessionRequest,
    AppStartSessionResultSuccess,
    SessionHeartbeatRequest,
    SessionHeartbeatResultFailure,
    SessionHeartbeatResultSuccess,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.engine_identity_manager import EngineIdentityManager
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


class SessionData(BaseModel):
    """Represents a single session's data."""

    session_id: str
    engine_id: str | None = None
    started_at: str
    last_updated: str
    # PID of the engine process that claimed this session. Used at boot to tell a session
    # left behind by a process that has since exited (safe to resume) from one another
    # running engine is still serving (must not be resumed -- see
    # `_get_or_initialize_active_session`). None on records written before this field
    # existed, which are treated as unowned.
    owner_pid: int | None = None


class SessionsStorage(BaseModel):
    """Represents the sessions storage structure."""

    sessions: list[SessionData]


class SessionManager:
    """Manages session saving and active session state."""

    _SESSION_STATE_FILE = "sessions.json"

    def __init__(
        self,
        engine_identity_manager: EngineIdentityManager,
        event_manager: EventManager | None = None,
    ) -> None:
        """Initialize the SessionManager.

        Args:
            engine_identity_manager: The EngineIdentityManager instance to use for engine ID operations.
            event_manager: The EventManager instance to use for event handling.
        """
        self._engine_identity_manager = engine_identity_manager
        self._sessions_data = self._load_sessions_data()
        self._active_session_id = self._get_or_initialize_active_session()
        if event_manager is not None:
            event_manager.assign_manager_to_request_type(AppStartSessionRequest, self.handle_session_start_request)
            event_manager.assign_manager_to_request_type(AppEndSessionRequest, self.handle_session_end_request)
            event_manager.assign_manager_to_request_type(AppGetSessionRequest, self.handle_get_session_request)
            event_manager.assign_manager_to_request_type(SessionHeartbeatRequest, self.handle_session_heartbeat_request)

    @property
    def active_session_id(self) -> str | None:
        """Get the active session ID.

        Returns:
            str | None: The active session ID or None if not set
        """
        return self._active_session_id

    @active_session_id.setter
    def active_session_id(self, session_id: str) -> None:
        """Set the active session ID.

        Args:
            session_id: The session ID to set as active
        """
        self._active_session_id = session_id
        logger.debug("Set active session ID to: %s", session_id)

    @property
    def all_sessions(self) -> list[SessionData]:
        """Get all registered sessions for the current engine.

        Returns:
            list[SessionData]: List of all session data for the current engine
        """
        return self._sessions_data.sessions

    def save_session(self, session_id: str) -> None:
        """Save a session and make it the active session.

        Args:
            session_id: The session ID to save
        """
        engine_id = self._get_current_engine_id()
        session_data = SessionData(
            session_id=session_id,
            engine_id=engine_id,
            started_at=datetime.now(tz=UTC).isoformat(),
            last_updated=datetime.now(tz=UTC).isoformat(),
            owner_pid=os.getpid(),
        )

        # Add or update the session
        self._add_or_update_session(session_data)

        # Set as active session
        self._active_session_id = session_id
        logger.info("Saved and activated session: %s for engine: %s", session_id, engine_id)

    def remove_session(self, session_id: str) -> None:
        """Remove a session from the sessions data for the current engine.

        Args:
            session_id: The session ID to remove
        """
        engine_id = self._get_current_engine_id()

        # Remove the session
        self._sessions_data.sessions = [
            session for session in self._sessions_data.sessions if session.session_id != session_id
        ]

        # Clear active session if it was the removed session
        if self._active_session_id == session_id:
            # Set to first remaining session or None
            self._active_session_id = (
                self._sessions_data.sessions[0].session_id if self._sessions_data.sessions else None
            )
            logger.info(
                "Removed active session %s for engine %s, set new active session to: %s",
                session_id,
                engine_id,
                self._active_session_id,
            )

        self._save_sessions_data(self._sessions_data, engine_id)
        logger.info("Removed session: %s from engine: %s", session_id, engine_id)

    def clear_saved_session(self) -> None:
        """Clear all saved session data for the current engine."""
        # Clear active session
        self._active_session_id = None

        # Clear in-memory session data
        self._sessions_data = SessionsStorage(sessions=[])

        engine_id = self._get_current_engine_id()
        session_state_file = self._get_session_state_file(engine_id)
        if session_state_file.exists():
            try:
                # TODO: Replace with DeleteFileRequest https://github.com/griptape-ai/griptape-nodes/issues/3765
                session_state_file.unlink()
                logger.info("Cleared all saved session data for engine: %s", engine_id)
            except OSError:
                # If we can't delete the file, just clear its contents
                self._save_sessions_data(self._sessions_data, engine_id)
                logger.warning("Could not delete session file for engine %s, cleared contents instead", engine_id)

    def _get_or_initialize_active_session(self) -> str | None:
        """Get or initialize the active session ID.

        Resumes the first saved session that no live engine is already serving. Returns
        None when there is nothing safe to resume, which makes the next
        AppStartSessionRequest mint a fresh session id.

        Engine identity is itself re-adopted from disk (`engines.json` default_engine_id),
        so two engines started on one machine land on the same `sessions.json`. Resuming
        blindly meant both subscribed to `sessions/<id>/request` and BOTH serviced every
        client request -- harmless for a read, but requests with side effects ran twice,
        which is how one click on a library's "Open" button produced two file browsers.

        Returns:
            str | None: The session ID to resume, or None if none can be resumed
        """
        for session in self._sessions_data.sessions:
            if self._is_session_owned_by_live_process(session):
                logger.info(
                    "Not resuming session %s: it is still owned by running engine process %s. "
                    "A new session will be created instead so both engines do not serve the same session.",
                    session.session_id,
                    session.owner_pid,
                )
                continue
            logger.debug(
                "Initialized active session to saved session: %s for engine: %s",
                session.session_id,
                session.engine_id,
            )
            return session.session_id

        return None

    def _is_session_owned_by_live_process(self, session: SessionData) -> bool:
        """Whether another running process is still serving this session.

        Records written before `owner_pid` existed, and records this very process wrote,
        both count as unowned: the former carry no ownership information, and the latter
        are this engine resuming its own session.
        """
        owner_pid = session.owner_pid
        if owner_pid is None or owner_pid == os.getpid():
            return False
        try:
            # Signal 0 performs the permission/existence check without delivering a signal.
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # The PID is live but owned by another user, so it is not one of our engines.
            return False
        except OSError:
            # Cannot determine liveness; assume free rather than stranding the session.
            return False
        return True

    def _add_or_update_session(self, session_data: SessionData) -> None:
        """Add or update a session in the sessions data structure.

        Args:
            session_data: The session data to add or update
        """
        engine_id = self._get_current_engine_id()

        # Find existing session
        existing_session = self._find_session_by_id(self._sessions_data, session_data.session_id)

        if existing_session:
            # Update existing session
            existing_session.session_id = session_data.session_id
            existing_session.engine_id = session_data.engine_id
            existing_session.started_at = session_data.started_at
            existing_session.last_updated = datetime.now(tz=UTC).isoformat()
        else:
            # Add new session
            self._sessions_data.sessions.append(session_data)

        self._save_sessions_data(self._sessions_data, engine_id)

    def _get_current_engine_id(self) -> str | None:
        """Get the current engine ID from EngineIdentityManager.

        Returns:
            str | None: The current engine ID or None if not set
        """
        return self._engine_identity_manager.active_engine_id

    def _load_sessions_data(self) -> SessionsStorage:
        """Load sessions data from storage.

        Returns:
            SessionsStorage: Sessions data structure with sessions array
        """
        engine_id = self._get_current_engine_id()
        session_state_file = self._get_session_state_file(engine_id)

        if session_state_file.exists():
            try:
                with session_state_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "sessions" in data:
                        return SessionsStorage.model_validate(data)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass

        return SessionsStorage(sessions=[])

    def _save_sessions_data(self, sessions_data: SessionsStorage, engine_id: str | None = None) -> None:
        """Save sessions data to storage.

        Args:
            sessions_data: Sessions data structure to save
            engine_id: Optional engine ID to save engine-specific sessions
        """
        session_state_dir = self._get_session_state_dir(engine_id)
        session_state_dir.mkdir(parents=True, exist_ok=True)

        session_state_file = self._get_session_state_file(engine_id)
        with session_state_file.open("w", encoding="utf-8") as f:
            json.dump(sessions_data.model_dump(exclude_none=True), f, indent=2)

        # Update in-memory copy
        self._sessions_data = sessions_data

    async def handle_session_start_request(self, request: AppStartSessionRequest) -> ResultPayload:  # noqa: ARG002
        current_session_id = self.active_session_id
        if current_session_id is None:
            # Client wants a new session
            current_session_id = uuid.uuid4().hex
            self.save_session(current_session_id)
            details = f"New session '{current_session_id}' started at {datetime.now(tz=UTC)}."
            logger.info(details)
        else:
            details = f"Session '{current_session_id}' already active. Joining..."

        return AppStartSessionResultSuccess(current_session_id, result_details="Session started successfully.")

    async def handle_session_end_request(self, _: AppEndSessionRequest) -> ResultPayload:
        try:
            previous_session_id = self.active_session_id
            if previous_session_id is None:
                details = "No active session to end."
                logger.info(details)
            else:
                details = f"Session '{previous_session_id}' ended at {datetime.now(tz=UTC)}."
                logger.info(details)
                self.clear_saved_session()

            return AppEndSessionResultSuccess(
                session_id=previous_session_id, result_details="Session ended successfully."
            )
        except Exception as err:
            details = f"Failed to end session due to '{err}'."
            logger.error(details)
            return AppEndSessionResultFailure(result_details=details)

    def handle_get_session_request(self, _: AppGetSessionRequest) -> ResultPayload:
        return AppGetSessionResultSuccess(
            session_id=self.active_session_id,
            result_details="Session ID retrieved successfully.",
        )

    def handle_session_heartbeat_request(self, request: SessionHeartbeatRequest) -> ResultPayload:  # noqa: ARG002
        """Handle session heartbeat requests.

        Simply verifies that the session is active and responds with success.
        """
        try:
            active_session_id = self.active_session_id
            if active_session_id is None:
                details = "Session heartbeat received but no active session found"
                logger.warning(details)
                return SessionHeartbeatResultFailure(result_details=details)

            details = f"Session heartbeat successful for session: {active_session_id}"
            return SessionHeartbeatResultSuccess(result_details=details)
        except Exception as err:
            details = f"Failed to handle session heartbeat: {err}"
            logger.error(details)
            return SessionHeartbeatResultFailure(result_details=details)

    @staticmethod
    def _find_session_by_id(sessions_data: SessionsStorage, session_id: str) -> SessionData | None:
        """Find a session by ID in the sessions data.

        Args:
            sessions_data: The sessions data structure
            session_id: The session ID to find

        Returns:
            SessionData | None: The session data if found, None otherwise
        """
        for session in sessions_data.sessions:
            if session.session_id == session_id:
                return session
        return None

    @staticmethod
    def _get_session_state_file(engine_id: str | None = None) -> Path:
        """Get the path to the session state storage file.

        Args:
            engine_id: Optional engine ID to get engine-specific session file
        """
        return SessionManager._get_session_state_dir(engine_id) / SessionManager._SESSION_STATE_FILE

    @staticmethod
    def _get_session_state_dir(engine_id: str | None = None) -> Path:
        """Get the XDG state directory for session storage.

        Args:
            engine_id: Optional engine ID to create engine-specific directory
        """
        base_dir = xdg_state_home() / "griptape_nodes"
        if engine_id:
            return base_dir / "engines" / engine_id
        return base_dir
