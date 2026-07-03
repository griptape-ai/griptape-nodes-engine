"""Tests for the per-library worker mode override stored on LibraryRegistration."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from griptape_nodes.node_library.library_declarations import (
    LibraryDeclaration,
    SuggestedWorkerMode,
    WorkerCompatibility,
    WorkerMode,
    WorkerModeCompatibility,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.settings import LibraryRegistration


def _make_library_manager() -> LibraryManager:
    return LibraryManager(event_manager=MagicMock(), worker_manager=MagicMock())


def _patch_libraries_to_register(raw_entries: list[Any]) -> Any:
    """Build a GriptapeNodes mock whose ConfigManager returns the given entries."""

    def get_config_value(key: str, *_args: Any, **_kwargs: Any) -> Any:
        if key.endswith("libraries_to_register"):
            return raw_entries
        return None

    mock_gtn = MagicMock()
    mock_gtn.ConfigManager.return_value.get_config_value.side_effect = get_config_value
    return mock_gtn


def _decls(
    *,
    compatibility: WorkerCompatibility | None = None,
    suggested: WorkerMode | None = None,
) -> list[LibraryDeclaration]:
    out: list[LibraryDeclaration] = []
    if compatibility is not None:
        out.append(WorkerModeCompatibility(compatibility=compatibility))
    if suggested is not None:
        out.append(SuggestedWorkerMode(mode=suggested))
    return out


class TestResolveRequiresWorker:
    def test_no_declarations_returns_false(self) -> None:
        mgr = _make_library_manager()

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes",
            _patch_libraries_to_register([]),
        ):
            assert mgr._resolve_requires_worker("/p.json", []) is False

    def test_incompatible_ignores_override(self) -> None:
        mgr = _make_library_manager()
        decls = _decls(compatibility=WorkerCompatibility.INCOMPATIBLE)
        entry = LibraryRegistration(path="/p.json", worker_mode_override=WorkerMode.WORKER)

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes",
            _patch_libraries_to_register([entry]),
        ):
            assert mgr._resolve_requires_worker("/p.json", decls) is False

    def test_compatible_with_suggested_orchestrator_no_override(self) -> None:
        mgr = _make_library_manager()
        decls = _decls(compatibility=WorkerCompatibility.COMPATIBLE, suggested=WorkerMode.ORCHESTRATOR)

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes",
            _patch_libraries_to_register([LibraryRegistration(path="/p.json")]),
        ):
            assert mgr._resolve_requires_worker("/p.json", decls) is False

    def test_compatible_with_suggested_worker_no_override(self) -> None:
        mgr = _make_library_manager()
        decls = _decls(compatibility=WorkerCompatibility.COMPATIBLE, suggested=WorkerMode.WORKER)

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes",
            _patch_libraries_to_register([LibraryRegistration(path="/p.json")]),
        ):
            assert mgr._resolve_requires_worker("/p.json", decls) is True

    def test_compatible_with_override_to_worker(self) -> None:
        mgr = _make_library_manager()
        decls = _decls(compatibility=WorkerCompatibility.COMPATIBLE, suggested=WorkerMode.ORCHESTRATOR)
        entry = LibraryRegistration(path="/p.json", worker_mode_override=WorkerMode.WORKER)

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes",
            _patch_libraries_to_register([entry]),
        ):
            assert mgr._resolve_requires_worker("/p.json", decls) is True

    def test_compatible_with_override_to_orchestrator(self) -> None:
        mgr = _make_library_manager()
        decls = _decls(compatibility=WorkerCompatibility.COMPATIBLE, suggested=WorkerMode.WORKER)
        entry = LibraryRegistration(path="/p.json", worker_mode_override=WorkerMode.ORCHESTRATOR)

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes",
            _patch_libraries_to_register([entry]),
        ):
            assert mgr._resolve_requires_worker("/p.json", decls) is False

    def test_dict_entry_with_override(self) -> None:
        # libraries_to_register may surface as raw dicts before validation.
        mgr = _make_library_manager()
        decls = _decls(compatibility=WorkerCompatibility.COMPATIBLE, suggested=WorkerMode.ORCHESTRATOR)
        entry = {"path": "/p.json", "worker_mode_override": "WORKER"}

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes",
            _patch_libraries_to_register([entry]),
        ):
            assert mgr._resolve_requires_worker("/p.json", decls) is True

    def test_bare_string_entry_no_override(self) -> None:
        mgr = _make_library_manager()
        decls = _decls(compatibility=WorkerCompatibility.COMPATIBLE, suggested=WorkerMode.WORKER)

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes",
            _patch_libraries_to_register(["/p.json"]),
        ):
            # Bare-string entry has no override; falls back to the manifest's suggested mode.
            assert mgr._resolve_requires_worker("/p.json", decls) is True

    def test_invalid_override_falls_back_to_manifest(self) -> None:
        mgr = _make_library_manager()
        decls = _decls(compatibility=WorkerCompatibility.COMPATIBLE, suggested=WorkerMode.WORKER)
        # Hand-edited config with garbage value.
        entry = {"path": "/p.json", "worker_mode_override": "BOGUS"}

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes",
            _patch_libraries_to_register([entry]),
        ):
            assert mgr._resolve_requires_worker("/p.json", decls) is True

    def test_path_match_is_case_insensitive(self) -> None:
        # Existing libraries_to_register comparison helpers use .lower(); mirror that.
        mgr = _make_library_manager()
        decls = _decls(compatibility=WorkerCompatibility.COMPATIBLE, suggested=WorkerMode.ORCHESTRATOR)
        entry = LibraryRegistration(path="/Some/Path.json", worker_mode_override=WorkerMode.WORKER)

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes",
            _patch_libraries_to_register([entry]),
        ):
            assert mgr._resolve_requires_worker("/some/path.json", decls) is True

    def test_no_matching_entry_falls_back_to_manifest(self) -> None:
        mgr = _make_library_manager()
        decls = _decls(compatibility=WorkerCompatibility.COMPATIBLE, suggested=WorkerMode.WORKER)

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes",
            _patch_libraries_to_register([LibraryRegistration(path="/other.json")]),
        ):
            assert mgr._resolve_requires_worker("/p.json", decls) is True

    def test_resolver_matches_by_registered_path_not_resolved_path(self) -> None:
        # Regression: when the user types a workspace-relative or `~`-prefixed path in
        # libraries_to_register, the engine resolves it to an absolute path for filesystem
        # operations but keeps the verbatim string as `registered_path`. The resolver must
        # match against the registered path so the override the user wrote against
        # `~/dev/lib.json` still applies to the resolved `/Users/me/dev/lib.json` library.
        mgr = _make_library_manager()
        decls = _decls(compatibility=WorkerCompatibility.COMPATIBLE, suggested=WorkerMode.ORCHESTRATOR)
        entry = LibraryRegistration(path="~/dev/lib.json", worker_mode_override=WorkerMode.WORKER)

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes",
            _patch_libraries_to_register([entry]),
        ):
            # Caller passes the user's `registered_path` (matches the entry verbatim), not
            # the resolved absolute path the engine would otherwise produce.
            assert mgr._resolve_requires_worker("~/dev/lib.json", decls) is True
            # Sanity: the resolved absolute path no longer matches anything in the config.
            assert mgr._resolve_requires_worker("/Users/me/dev/lib.json", decls) is False
