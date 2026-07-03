"""Manages engine identity state.

Centralizes engine identity management, providing a consistent interface for
engine ID and name operations.
Handles engine ID, name storage, and generation for unique engine identification.
Supports multiple engines with selection via GTN_ENGINE_ID environment variable.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel
from xdg_base_dirs import xdg_data_home

from griptape_nodes.retained_mode.events.app_events import (
    GetEngineNameRequest,
    GetEngineNameResultFailure,
    GetEngineNameResultSuccess,
    SetEngineNameRequest,
    SetEngineNameResultFailure,
    SetEngineNameResultSuccess,
)
from griptape_nodes.retained_mode.events.base_events import (
    BaseEvent,
    ResultDetails,
    ResultPayload,
)
from griptape_nodes.retained_mode.utils.name_generator import generate_engine_name

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


class EngineData(BaseModel):
    """Represents a single engine's data."""

    id: str
    name: str
    created_at: str
    updated_at: str | None = None


class EnginesStorage(BaseModel):
    """Represents the engines storage structure."""

    engines: list[EngineData]
    default_engine_id: str | None = None


class EngineIdentityManager:
    """Manages engine identity and active engine state."""

    _ENGINE_DATA_FILE = "engines.json"

    def __init__(self, event_manager: EventManager | None = None) -> None:
        """Initialize the EngineIdentityManager.

        Args:
            event_manager: The EventManager instance to use for event handling.
        """
        self._active_engine_id: str | None = None
        self._engines_data = self._load_engines_data()
        self._current_engine_data = self._get_or_initialize_engine_data()

        if event_manager is not None:
            event_manager.assign_manager_to_request_type(GetEngineNameRequest, self.handle_get_engine_name_request)
            event_manager.assign_manager_to_request_type(SetEngineNameRequest, self.handle_set_engine_name_request)

    @property
    def active_engine_id(self) -> str | None:
        """Get the active engine ID.

        Returns:
            str | None: The active engine ID or None if not set
        """
        return self._active_engine_id

    @active_engine_id.setter
    def active_engine_id(self, engine_id: str) -> None:
        """Set the active engine ID.

        Args:
            engine_id: The engine ID to set as active
        """
        self._active_engine_id = engine_id
        logger.debug("Set active engine ID to: %s", engine_id)

    @property
    def engine_id(self) -> str:
        """Get the engine ID.

        Returns:
            str: The engine ID (UUID)
        """
        return self._current_engine_data.id

    @property
    def engine_name(self) -> str:
        """Get the engine name.

        Returns:
            str: The engine name
        """
        return self._current_engine_data.name

    @engine_name.setter
    def engine_name(self, engine_name: str) -> None:
        """Set and persist the current engine name.

        Args:
            engine_name: The new engine name to set
        """
        # Update cached engine data
        self._current_engine_data.name = engine_name
        self._current_engine_data.updated_at = datetime.now(tz=UTC).isoformat()

        # Save updated engine data
        self._add_or_update_engine(self._current_engine_data)
        logger.info("Updated engine name to: %s", engine_name)

    @property
    def all_engines(self) -> list[EngineData]:
        """Get all registered engines.

        Returns:
            list[EngineData]: List of all engine data
        """
        return self._engines_data.engines

    def handle_get_engine_name_request(self, request: GetEngineNameRequest) -> ResultPayload:  # noqa: ARG002
        """Handle requests to get the current engine name."""
        try:
            engine_name = self.engine_name
            return GetEngineNameResultSuccess(
                engine_name=engine_name, result_details="Engine name retrieved successfully."
            )
        except Exception as err:
            error_message = f"Failed to get engine name: {err}"
            logger.error(error_message)
            return GetEngineNameResultFailure(error_message=error_message, result_details=error_message)

    def handle_set_engine_name_request(self, request: SetEngineNameRequest) -> ResultPayload:
        """Handle requests to set a new engine name."""
        try:
            if not request.engine_name or not request.engine_name.strip():
                error_message = "Engine name cannot be empty"
                logger.warning(error_message)
                return SetEngineNameResultFailure(error_message=error_message, result_details=error_message)

            self.engine_name = request.engine_name.strip()
            details = f"Engine name set to: {request.engine_name.strip()}"
            return SetEngineNameResultSuccess(
                engine_name=request.engine_name.strip(),
                result_details=ResultDetails(message=details, level=logging.INFO),
            )

        except Exception as err:
            error_message = f"Failed to set engine name: {err}"
            logger.error(error_message)
            return SetEngineNameResultFailure(error_message=error_message, result_details=error_message)

    def _get_or_initialize_engine_data(self) -> EngineData:
        """Get the current engine data, creating default if it doesn't exist.

        Returns:
            EngineData: The current engine data
        """
        engine_data = None

        # Step 1: Determine which engine ID to use
        target_engine_id = os.getenv("GTN_ENGINE_ID")
        if not target_engine_id:
            # Use default or first available
            if self._engines_data.default_engine_id:
                target_engine_id = self._engines_data.default_engine_id
            elif self._engines_data.engines:
                engine_data = self._engines_data.engines[0]
            else:
                # No engines exist, will create new one
                target_engine_id = str(uuid.uuid4())

        # Step 2: Try to find existing engine if we don't already have one
        if engine_data is None and target_engine_id is not None:
            engine_data = self._find_engine_by_id(self._engines_data, target_engine_id)

        # Step 3: Create new engine if not found
        if engine_data is None:
            # If target_engine_id is still None, generate a new UUID
            if target_engine_id is None:
                target_engine_id = str(uuid.uuid4())
            engine_data = EngineData(
                id=target_engine_id,
                name=generate_engine_name(),
                created_at=datetime.now(tz=UTC).isoformat(),
            )
            self._add_or_update_engine(engine_data)

        # Register engine with BaseEvent
        BaseEvent._engine_id = engine_data.id
        self._active_engine_id = engine_data.id
        logger.debug("Initialized engine ID: %s", engine_data.id)
        return engine_data

    def _add_or_update_engine(self, engine_data: EngineData) -> None:
        """Add or update an engine in the engines data structure.

        Args:
            engine_data: The engine data to add or update
        """
        # Find existing engine
        existing_engine = self._find_engine_by_id(self._engines_data, engine_data.id)

        if existing_engine:
            # Update existing engine
            existing_engine.name = engine_data.name
            existing_engine.created_at = engine_data.created_at
            existing_engine.updated_at = datetime.now(tz=UTC).isoformat()
        else:
            # Add new engine
            self._engines_data.engines.append(engine_data)

            # Set as default if it's the first engine
            if self._engines_data.default_engine_id is None and len(self._engines_data.engines) == 1:
                self._engines_data.default_engine_id = engine_data.id

        self._save_engines_data(self._engines_data)

    def _load_engines_data(self) -> EnginesStorage:
        """Load engines data from storage.

        Returns:
            EnginesStorage: Engines data structure with engines array and default_engine_id
        """
        engine_data_file = self._get_engine_data_file()

        if engine_data_file.exists():
            try:
                with engine_data_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "engines" in data:
                        return EnginesStorage.model_validate(data)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass

        return EnginesStorage(engines=[], default_engine_id=None)

    def _save_engines_data(self, engines_data: EnginesStorage) -> None:
        """Save engines data to storage.

        Args:
            engines_data: Engines data structure to save
        """
        engine_data_dir = self._get_engine_data_dir()
        engine_data_dir.mkdir(parents=True, exist_ok=True)

        engine_data_file = self._get_engine_data_file()
        with engine_data_file.open("w", encoding="utf-8") as f:
            json.dump(engines_data.model_dump(exclude_none=True), f, indent=2)

        # Update in-memory copy
        self._engines_data = engines_data

    @staticmethod
    def _get_engine_data_dir() -> Path:
        """Get the XDG data directory for engine identity storage."""
        return xdg_data_home() / "griptape_nodes"

    @staticmethod
    def _get_engine_data_file() -> Path:
        """Get the path to the engine data storage file."""
        return EngineIdentityManager._get_engine_data_dir() / EngineIdentityManager._ENGINE_DATA_FILE

    @staticmethod
    def _find_engine_by_id(engines_data: EnginesStorage, engine_id: str) -> EngineData | None:
        """Find an engine by ID in the engines data.

        Args:
            engines_data: The engines data structure
            engine_id: The engine ID to find

        Returns:
            EngineData | None: The engine data if found, None otherwise
        """
        for engine in engines_data.engines:
            if engine.id == engine_id:
                return engine
        return None
