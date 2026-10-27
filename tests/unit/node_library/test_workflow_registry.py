"""Tests for the workflow registry and its `WorkflowRegistry` shim."""

from __future__ import annotations

import logging
import os
import platform
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.files.path_utils import derive_registry_key
from griptape_nodes.node_library.workflow_registry import (
    DuplicateWorkflowIdError,
    Workflow,
    WorkflowMetadata,
    WorkflowPathTakenError,
    WorkflowRegistry,
    WorkflowRegistryError,
    _WorkflowRegistry,
)
from griptape_nodes.retained_mode.engine import (
    Engine,
    current_engine,
    engine_scope,
    has_current_engine,
    reset_root_engine,
)


class TestDeriveRegistryKey:
    def test_flat_workflow(self) -> None:
        assert derive_registry_key("my_workflow.py") == "my_workflow"

    def test_subdirectory(self) -> None:
        assert derive_registry_key("subdir/my_workflow.py") == "subdir/my_workflow"

    def test_nested_subdirectory(self) -> None:
        assert derive_registry_key("a/b/c/my_workflow.py") == "a/b/c/my_workflow"

    def test_backslash_normalization(self) -> None:
        assert derive_registry_key("subdir\\my_workflow.py") == "subdir/my_workflow"

    def test_no_extension(self) -> None:
        assert derive_registry_key("my_workflow") == "my_workflow"

    def test_dot_prefix_normalized(self) -> None:
        assert derive_registry_key("./my_workflow.py") == "my_workflow"

    @pytest.mark.parametrize(
        ("input_path", "expected"),
        [
            ("my_workflow.py", "my_workflow"),
            ("subdir/my_workflow.py", "subdir/my_workflow"),
            ("a/b/deep_workflow.py", "a/b/deep_workflow"),
            ("windows\\path\\workflow.py", "windows/path/workflow"),
            # Case is preserved on every platform.
            ("MyFlow.py", "MyFlow"),
            ("UPPER/MixedCase.py", "UPPER/MixedCase"),
            # Only the final suffix is stripped. `%d.%m_%H.%M` is the engine's own
            # default auto-generated workflow name, so internal dots are common.
            ("03.07_18.30.py", "03.07_18.30"),
            ("subdir/03.07_18.30.py", "subdir/03.07_18.30"),
        ],
    )
    def test_known_inputs(self, input_path: str, expected: str) -> None:
        assert derive_registry_key(input_path) == expected

    def test_not_idempotent_on_dotted_names(self) -> None:
        """Applying the helper to an already-derived dotted key truncates it.

        Callers holding a registry key (e.g. ``ContextManager.get_current_workflow_name()``)
        must compare it directly rather than re-deriving. Pinned so the trap stays visible.
        """
        key = derive_registry_key("03.07_18.30.py")
        assert key == "03.07_18.30"
        assert derive_registry_key(key) == "03.07_18"


class TestGetCompleteFilePath:
    """Tests for resolving registry file paths against the workspace."""

    def test_get_complete_file_path_with_absolute_path(self, engine: Engine) -> None:
        """Test that get_complete_file_path returns absolute paths as-is."""
        # Use a platform-appropriate absolute path
        if os.name == "nt":  # Windows
            absolute_path = "C:\\absolute\\path\\to\\workflow.py"
        else:  # Unix-like
            absolute_path = "/absolute/path/to/workflow.py"

        result = engine.workflow_registry.get_complete_file_path(absolute_path)

        # On Windows, paths starting with / are not considered absolute
        # so they get treated as relative paths
        if os.name == "nt" and absolute_path.startswith("/"):
            # On Windows, Unix-style paths are relative
            assert Path(result).is_absolute()
        else:
            assert result == absolute_path

    def test_get_complete_file_path_with_unix_style_on_windows(self, engine: Engine) -> None:
        """Test Unix-style paths on Windows (treated as relative)."""
        unix_style_path = "/absolute/path/to/workflow.py"
        result = engine.workflow_registry.get_complete_file_path(unix_style_path)

        if os.name == "nt":  # Windows
            # Unix-style paths are treated as relative on Windows
            # The result should be the workspace path + the Unix path
            assert result.endswith("\\absolute\\path\\to\\workflow.py")
            # Verify it's been made absolute
            assert Path(result).is_absolute()
        else:
            # On Unix, this is an absolute path
            assert result == unix_style_path

    def test_get_complete_file_path_with_absolute_windows_path(self, engine: Engine) -> None:
        """Test that get_complete_file_path handles Windows absolute paths."""
        windows_path = "C:\\Users\\test\\workflow.py"

        result = engine.workflow_registry.get_complete_file_path(windows_path)

        # On Windows, it should be returned as-is
        # On Unix systems, Path.is_absolute() returns False for Windows paths,
        # so it will be treated as relative
        if platform.system() == "Windows":
            assert result == windows_path
        else:
            # On Unix, Windows paths are treated as relative
            assert result.endswith("C:\\Users\\test\\workflow.py")

    def test_get_complete_file_path_with_relative_path(self, engine: Engine) -> None:
        """Test that get_complete_file_path resolves relative paths to workspace."""
        relative_path = "workflows/my_workflow.py"

        # Get the actual workspace path from the config manager
        workspace_path = engine.config_manager.workspace_path

        result = engine.workflow_registry.get_complete_file_path(relative_path)

        expected = str(workspace_path / relative_path)
        assert result == expected

    def test_get_complete_file_path_with_home_expansion(self, engine: Engine) -> None:
        """Test that get_complete_file_path handles paths with home directory expansion."""
        home_path = "~/workflows/my_workflow.py"

        # Home paths starting with ~ are NOT considered absolute by Path.is_absolute()
        # so they will be treated as relative paths
        result = engine.workflow_registry.get_complete_file_path(home_path)

        # Should be treated as relative and appended to workspace
        if os.name == "nt":  # Windows
            # On Windows, ~ is just a regular character in the path
            assert result.endswith("~\\workflows\\my_workflow.py")
        else:
            # On Unix, ~ is also treated as relative (not expanded)
            assert result.endswith("~/workflows/my_workflow.py")

    def test_get_complete_file_path_with_current_dir_relative(self, engine: Engine) -> None:
        """Test that get_complete_file_path handles current directory relative paths."""
        current_dir_path = "./my_workflow.py"

        # Get the actual workspace path from the config manager
        workspace_path = engine.config_manager.workspace_path

        result = engine.workflow_registry.get_complete_file_path(current_dir_path)

        expected = str(workspace_path / current_dir_path)
        assert result == expected

    def test_get_complete_file_path_with_parent_dir_relative(self, engine: Engine) -> None:
        """Test that get_complete_file_path handles parent directory relative paths."""
        parent_dir_path = "../external/my_workflow.py"

        # Get the actual workspace path from the config manager
        workspace_path = engine.config_manager.workspace_path

        result = engine.workflow_registry.get_complete_file_path(parent_dir_path)

        # resolve_workspace_path normalizes the path by resolving .. components
        expected = str((workspace_path / parent_dir_path).resolve())
        assert result == expected


def _metadata(name: str = "test") -> WorkflowMetadata:
    return WorkflowMetadata(
        name=name,
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="test",
        node_libraries_referenced=[],
    )


def _registry(workspace: Path) -> _WorkflowRegistry:
    config_manager = MagicMock()
    config_manager.workspace_path = workspace
    return _WorkflowRegistry(config_manager)


def _workflow_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# stub")
    return path


class TestWorkflowRegistryOperations:
    """Tests for the id-keyed registry and its path and name indexes."""

    def test_register_file_mints_an_id_and_indexes_the_file(self, tmp_path: Path) -> None:
        """Never derived from the path, so the same file registered by another registry gets another id."""
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "sub" / "flow.py")

        workflow = registry.register_file("sub/flow.py", _metadata())
        other = _registry(tmp_path).register_file("sub/flow.py", _metadata())

        assert uuid.UUID(hex=workflow.workflow_id).hex == workflow.workflow_id
        assert other.workflow_id != workflow.workflow_id
        assert registry.get(workflow.workflow_id) is workflow
        assert registry.find_by_name("sub/flow") is workflow
        assert registry.find_by_path(tmp_path / "sub" / "flow.py") is workflow
        assert workflow.file_path == str(Path("sub") / "flow.py")
        assert workflow.absolute_path == tmp_path / "sub" / "flow.py"

    def test_register_file_keeps_the_callers_metadata(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "flow.py")
        metadata = _metadata()

        workflow = registry.register_file("flow.py", metadata)

        assert workflow.metadata is metadata

    def test_register_file_requires_the_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            _registry(tmp_path).register_file("missing.py", _metadata())

    def test_refused_path_names_the_holder_and_changes_nothing(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "flow.py")
        holder = registry.register_file("flow.py", _metadata())

        with pytest.raises(WorkflowPathTakenError) as raised:
            registry.register_file(tmp_path / "flow.py", _metadata())

        assert raised.value.existing is holder
        assert registry.workflow_ids() == {holder.workflow_id}
        assert registry.find_by_path(tmp_path / "flow.py") is holder

    def test_refused_name_names_the_holder_and_changes_nothing(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "flow.py")
        _workflow_file(tmp_path / "flow.txt")
        holder = registry.register_file("flow.py", _metadata())

        with pytest.raises(WorkflowPathTakenError, match="already registered under that name") as raised:
            registry.register_file("flow.txt", _metadata())

        assert raised.value.existing is holder
        assert registry.workflow_ids() == {holder.workflow_id}
        assert registry.find_by_path(tmp_path / "flow.txt") is None
        assert registry.find_by_name("flow") is holder

    @pytest.mark.skipif(platform.system() == "Windows", reason="symlink creation needs privileges on Windows")
    def test_same_file_through_a_link_is_an_alias(self, tmp_path: Path) -> None:
        """A file reached through a link is the same workflow, never a duplicate."""
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "real" / "flow.py")
        (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
        workflow = registry.register_file("real/flow.py", _metadata())

        alias = registry.register_file("linked/flow.py", _metadata())

        assert alias is workflow
        assert registry.find_by_path(tmp_path / "linked" / "flow.py") is workflow
        assert registry.find_by_name("linked/flow") is workflow
        assert list(registry.list_workflows()) == ["real/flow"]

    def test_register_unsaved_names_it_by_its_id(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)

        workflow = registry.register_unsaved("Untitled")

        assert workflow.name == f"unsaved:{workflow.workflow_id}"
        assert workflow.file_path is None
        assert workflow.absolute_path is None
        assert workflow.metadata.name == "Untitled"
        assert registry.find_by_name(workflow.name) is workflow
        assert registry.unsaved_name_workflow_id(workflow.name) == workflow.workflow_id

    def test_register_unsaved_refuses_an_id_another_entry_has(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        existing = registry.register_unsaved("First")

        with pytest.raises(DuplicateWorkflowIdError) as raised:
            registry.register_unsaved("Second", workflow_id=existing.workflow_id)

        assert raised.value.existing is existing
        assert list(registry.list_workflows()) == [existing.name]

    @pytest.mark.parametrize(
        "name",
        [
            "unsaved:01234567-89ab-cdef-0123-456789abcdef",
            "unsaved:0123456789ABCDEF0123456789ABCDEF",
            "unsaved:not-an-id",
            "0123456789abcdef0123456789abcdef",
        ],
    )
    def test_unsaved_name_workflow_id_takes_only_the_hex_form(self, tmp_path: Path, name: str) -> None:
        assert _registry(tmp_path).unsaved_name_workflow_id(name) is None

    def test_set_file_path_reindexes_and_keeps_the_id(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "old.py")
        _workflow_file(tmp_path / "moved" / "new.py")
        workflow = registry.register_file("old.py", _metadata())
        workflow_id = workflow.workflow_id

        registry.set_file_path(workflow, "moved/new.py")

        assert workflow.workflow_id == workflow_id
        assert workflow.name == "moved/new"
        assert workflow.file_path == str(Path("moved") / "new.py")
        assert registry.find_by_name("moved/new") is workflow
        assert registry.find_by_name("old") is None
        assert registry.find_by_path(tmp_path / "old.py") is None
        assert registry.find_by_path(tmp_path / "moved" / "new.py") is workflow

    def test_set_file_path_saves_an_unsaved_workflow(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        workflow = registry.register_unsaved("Untitled")
        unsaved_name = workflow.name
        _workflow_file(tmp_path / "saved.py")

        registry.set_file_path(workflow, tmp_path / "saved.py")

        assert workflow.is_saved
        assert workflow.name == "saved"
        assert registry.find_by_name(unsaved_name) is None
        assert registry.get(workflow.workflow_id) is workflow

    def test_set_file_path_refuses_a_path_another_entry_holds(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "a.py")
        _workflow_file(tmp_path / "b.py")
        first = registry.register_file("a.py", _metadata())
        registry.register_file("b.py", _metadata())

        with pytest.raises(WorkflowPathTakenError):
            registry.set_file_path(first, "b.py")

        assert first.name == "a"
        assert registry.find_by_name("a") is first

    def test_set_file_path_refuses_a_name_another_entry_holds_and_changes_nothing(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "a.py")
        _workflow_file(tmp_path / "b.py")
        _workflow_file(tmp_path / "b.txt")
        first = registry.register_file("a.py", _metadata())
        holder = registry.register_file("b.py", _metadata())

        with pytest.raises(WorkflowPathTakenError) as raised:
            registry.set_file_path(first, "b.txt")

        assert raised.value.existing is holder
        assert first.absolute_path == tmp_path / "a.py"
        assert first.name == "a"
        assert registry.find_by_path(tmp_path / "a.py") is first
        assert registry.find_by_path(tmp_path / "b.txt") is None
        assert registry.find_by_name("a") is first
        assert registry.find_by_name("b") is holder

    def test_set_file_path_refuses_an_unregistered_workflow(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "flow.py")
        stranger = Workflow(registry=registry, workflow_id="stranger", metadata=_metadata(), absolute_path=None)

        with pytest.raises(KeyError, match="not registered"):
            registry.set_file_path(stranger, "flow.py")

        assert registry.find_by_path(tmp_path / "flow.py") is None

    def test_set_file_path_requires_the_file(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        workflow = registry.register_unsaved("Untitled")

        with pytest.raises(ValueError, match="does not exist"):
            registry.set_file_path(workflow, "missing.py")

        assert not workflow.is_saved
        assert registry.find_by_name(workflow.name) is workflow

    def test_remove_drops_every_index(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "flow.py")
        workflow = registry.register_file("flow.py", _metadata())

        removed = registry.remove(workflow.workflow_id)

        assert removed is workflow
        assert registry.find_by_name("flow") is None
        assert registry.find_by_path(tmp_path / "flow.py") is None
        with pytest.raises(KeyError, match="no workflow has that id"):
            registry.get(workflow.workflow_id)

    def test_remove_saved_workflows_except_keeps_kept_and_unsaved_entries(self, tmp_path: Path) -> None:
        """A refresh drops the saved entries its scan did not find, but keeps what a scan cannot find."""
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "found.py")
        _workflow_file(tmp_path / "missed.py")
        found = registry.register_file("found.py", _metadata())
        missed = registry.register_file("missed.py", _metadata())
        unsaved = registry.register_unsaved("Scratch")

        registry.remove_saved_workflows_except({found.workflow_id})

        assert registry.find_by_id(missed.workflow_id) is None
        assert registry.find_by_name("missed") is None
        assert registry.find_by_path(tmp_path / "missed.py") is None
        assert registry.find_by_name("found") is found
        assert registry.find_by_name(unsaved.name) is unsaved

    def test_reindex_workflows_renames_entries_for_a_new_workspace(self, tmp_path: Path) -> None:
        """Every entry's name is derived again against the workspace a project switch moved to."""
        registry = _registry(tmp_path / "ws")
        _workflow_file(tmp_path / "ws" / "shots" / "flow.py")
        workflow = registry.register_file("shots/flow.py", _metadata())

        registry._config_manager.workspace_path = tmp_path / "ws" / "shots"
        registry.reindex_workflows()

        assert workflow.name == "flow"
        assert registry.find_by_name("flow") is workflow
        assert registry.find_by_name("shots/flow") is None
        assert registry.find_by_path(tmp_path / "ws" / "shots" / "flow.py") is workflow

    @pytest.mark.skipif(platform.system() == "Windows", reason="symlink creation needs privileges on Windows")
    def test_reindex_workflows_drops_an_entry_whose_link_now_reaches_a_file_another_entry_holds(
        self, tmp_path: Path
    ) -> None:
        """A link retargeted outside the engine must not leave two entries claiming one file."""
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "a" / "flow.py")
        _workflow_file(tmp_path / "b" / "flow.py")
        (tmp_path / "linked").symlink_to(tmp_path / "a", target_is_directory=True)
        through_link = registry.register_file("linked/flow.py", _metadata())
        holder = registry.register_file("b/flow.py", _metadata())
        (tmp_path / "linked").unlink()
        (tmp_path / "linked").symlink_to(tmp_path / "b", target_is_directory=True)

        registry.reindex_workflows()

        assert registry.find_by_id(through_link.workflow_id) is None
        assert registry.find_by_path(tmp_path / "b" / "flow.py") is holder
        assert registry.find_by_name("b/flow") is holder

    def test_refresh_file_keeps_the_entry_and_takes_the_new_metadata(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "flow.py")
        first = registry.register_file("flow.py", _metadata(name="Before"))

        again = registry.refresh_file("flow.py", _metadata(name="After"))

        assert again is first
        assert again.metadata.name == "After"

    def test_refresh_file_registers_a_new_file(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "flow.py")

        workflow = registry.refresh_file("flow.py", _metadata())

        assert registry.find_by_path(tmp_path / "flow.py") is workflow

    def test_refresh_file_requires_the_file(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "flow.py")
        registry.register_file("flow.py", _metadata())
        (tmp_path / "flow.py").unlink()

        with pytest.raises(ValueError, match="does not exist"):
            registry.refresh_file("flow.py", _metadata())

    def test_a_file_registered_again_after_a_remove_gets_a_new_id(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "flow.py")
        first = registry.register_file("flow.py", _metadata())

        registry.remove(first.workflow_id)
        recreated = registry.refresh_file("flow.py", _metadata())

        assert recreated.workflow_id != first.workflow_id

    @pytest.mark.skipif(platform.system() == "Windows", reason="symlink creation needs privileges on Windows")
    def test_refresh_file_through_a_link_keeps_the_entry(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "real" / "flow.py")
        (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
        first = registry.register_file("real/flow.py", _metadata())

        assert registry.refresh_file("linked/flow.py", _metadata()) is first
        assert registry.refresh_file("real/flow.py", _metadata()) is first

    def test_listed_entries_carry_their_id(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        workflow = registry.register_unsaved("Untitled")

        with patch.object(registry, "get_synced_workflows_path", return_value=tmp_path / "synced"):
            listing = registry.list_workflows()

        assert listing[workflow.name]["workflow_id"] == workflow.workflow_id

    def test_engines_do_not_share_workflows(self, engine: Engine) -> None:
        other = Engine()

        workflow = engine.workflow_registry.register_unsaved("Mine")

        assert engine.workflow_registry.has_workflow_with_name(workflow.name)
        assert not other.workflow_registry.has_workflow_with_name(workflow.name)


class TestNameKeyedMethods:
    """The name-keyed methods node libraries reach through the deprecated `WorkflowRegistry` classmethods."""

    def test_generate_new_workflow_registers_the_file(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "my_workflow.py")

        workflow = registry.generate_new_workflow(
            registry_key="my_workflow", metadata=_metadata(), file_path="my_workflow.py"
        )

        assert registry.find_by_name("my_workflow") is workflow

    def test_generate_new_workflow_duplicate_key_raises(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "my_workflow.py")
        registry.generate_new_workflow(registry_key="my_workflow", metadata=_metadata(), file_path="my_workflow.py")

        with pytest.raises(KeyError, match="my_workflow"):
            registry.generate_new_workflow(registry_key="my_workflow", metadata=_metadata(), file_path="my_workflow.py")

    def test_generate_new_workflow_refuses_a_key_its_file_does_not_give(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "sub" / "x.py")

        with pytest.raises(WorkflowRegistryError, match="names it 'sub/x'"):
            registry.generate_new_workflow(registry_key="my_key", metadata=_metadata(), file_path="sub/x.py")

        assert registry.workflow_ids() == set()

    @pytest.mark.skipif(platform.system() == "Windows", reason="symlink creation needs privileges on Windows")
    def test_generate_new_workflow_through_a_link_gives_the_entry_its_metadata(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "real" / "flow.py")
        (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
        workflow = registry.register_file("real/flow.py", _metadata("Before"))

        alias = registry.generate_new_workflow(
            registry_key="linked/flow", metadata=_metadata("After"), file_path="linked/flow.py"
        )

        assert alias is workflow
        assert registry.get_workflow_by_name("linked/flow") is workflow
        assert workflow.metadata.name == "After"

    def test_generate_new_workflow_without_a_file_requires_an_unsaved_key(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must be 'unsaved:'"):
            _registry(tmp_path).generate_new_workflow(registry_key="my_workflow", metadata=_metadata())

    def test_ensure_unsaved_registers_once_under_the_id_in_the_key(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        key = f"unsaved:{uuid.uuid4().hex}"

        workflow = registry.ensure_unsaved(key=key, display_name="Untitled")

        assert workflow.name == key
        assert registry.ensure_unsaved(key=key, display_name="Other") is workflow

    def test_ensure_unsaved_refuses_a_key_without_a_hex_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="32 hex digits"):
            _registry(tmp_path).ensure_unsaved(key=f"unsaved:{uuid.uuid4()}", display_name="Untitled")

    def test_delete_workflow_by_name_removes_the_entry(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        workflow = registry.register_unsaved("Untitled")

        assert registry.delete_workflow_by_name(workflow.name) is workflow
        assert registry.find_by_id(workflow.workflow_id) is None

    def test_clear_user_workflows_keeps_library_workflows(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "library.py")
        library_metadata = _metadata()
        library_metadata.is_griptape_provided = True
        library_workflow = registry.register_file("library.py", library_metadata)
        registry.register_unsaved("Mine")

        registry.clear_user_workflows()

        assert registry.workflow_ids() == {library_workflow.workflow_id}

    def test_rekey_workflow_follows_the_file_to_its_new_name(self, tmp_path: Path) -> None:
        registry = _registry(tmp_path)
        _workflow_file(tmp_path / "old_key.py")
        workflow = registry.register_file("old_key.py", _metadata())
        (tmp_path / "old_key.py").rename(tmp_path / "new_key.py")

        registry.rekey_workflow("old_key", "new_key")

        assert registry.find_by_name("new_key") is workflow
        assert registry.find_by_name("old_key") is None

    def test_rekey_workflow_missing_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError, match="not_there"):
            _registry(tmp_path).rekey_workflow("not_there", "new_key")


class TestWorkflowRegistryShim:
    """`WorkflowRegistry` classmethods act on the current engine's registry."""

    def test_reads_what_the_engine_registered(self, engine: Engine) -> None:
        workflow = engine.workflow_registry.register_unsaved("Untitled")

        assert WorkflowRegistry.has_workflow_with_name(workflow.name)
        assert WorkflowRegistry.get_workflow_by_name(workflow.name) is workflow

    def test_writes_land_in_the_engine_registry(self, engine: Engine) -> None:
        key = f"unsaved:{uuid.uuid4().hex}"

        WorkflowRegistry.ensure_unsaved(key=key, display_name="Untitled")

        assert engine.workflow_registry.has_workflow_with_name(key)

    def test_follows_the_scoped_engine(self) -> None:
        root = current_engine()
        key = f"unsaved:{uuid.uuid4().hex}"

        with engine_scope() as scoped:
            WorkflowRegistry.ensure_unsaved(key=key, display_name="Scoped")

        assert scoped.workflow_registry.has_workflow_with_name(key)
        assert not root.workflow_registry.has_workflow_with_name(key)

    def test_unsaved_key_prefix_matches(self) -> None:
        assert WorkflowRegistry.UNSAVED_KEY_PREFIX == _WorkflowRegistry.UNSAVED_KEY_PREFIX

    @pytest.mark.parametrize(
        ("method_name", "replacement"),
        [
            ("ensure_unsaved", "SetWorkflowContextRequest"),
            ("delete_workflow_by_name", "DeleteWorkflowRequest"),
            ("clear_user_workflows", "RefreshWorkflowRegistryRequest"),
        ],
    )
    def test_deprecated_methods_log_their_replacement(
        self, engine: Engine, caplog: pytest.LogCaptureFixture, method_name: str, replacement: str
    ) -> None:
        workflow = engine.workflow_registry.register_unsaved("Untitled")
        arguments = {
            "ensure_unsaved": {"key": workflow.name, "display_name": "Untitled"},
            "delete_workflow_by_name": {"name": workflow.name},
            "clear_user_workflows": {},
        }[method_name]

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            getattr(WorkflowRegistry, method_name)(**arguments)

        assert f"WorkflowRegistry.{method_name}() is deprecated" in caplog.text
        assert replacement in caplog.text

    def test_clear_user_workflows_does_not_build_an_engine(self) -> None:
        reset_root_engine()

        WorkflowRegistry.clear_user_workflows()

        assert not has_current_engine()


class TestGetWorkflowMetadata:
    """Test Workflow.get_workflow_metadata path optimization.

    When synced_path and workspace_path are pre-computed by list_workflows(),
    get_workflow_metadata must use them directly instead of calling is_synced,
    which would resolve them per workflow.
    """

    def _make_workflow(self, engine: Engine, file_path: str | None = "workflows/test.json") -> Workflow:
        mock_metadata = MagicMock()
        mock_metadata.model_dump.return_value = {"name": "test"}
        absolute_path = None if file_path is None else Path("/workspace") / file_path
        workflow = Workflow(
            registry=engine.workflow_registry,
            workflow_id="test",
            metadata=mock_metadata,
            absolute_path=absolute_path,
        )
        # The registry form `file_path` reports, set as the registry would when it indexes the entry.
        workflow._file_path = file_path
        return workflow

    def test_uses_precomputed_paths_instead_of_is_synced_property(self, engine: Engine) -> None:
        workflow = self._make_workflow(engine)
        synced_path = Path("/synced")
        workspace_path = Path("/workspace")

        with (
            patch.object(
                type(workflow),
                "is_synced",
                new_callable=lambda: property(
                    lambda _: (_ for _ in ()).throw(
                        AssertionError("is_synced must not be called when paths are pre-computed")
                    )
                ),
            ),
            patch(
                "griptape_nodes.node_library.workflow_registry.resolve_workspace_path",
                return_value=Path("/workspace/workflows/test.json"),
            ),
        ):
            result = workflow.get_workflow_metadata(synced_path=synced_path, workspace_path=workspace_path)

        assert "is_synced" in result

    def test_falls_back_to_is_synced_when_paths_not_provided(self, engine: Engine) -> None:
        workflow = self._make_workflow(engine)

        with patch.object(type(workflow), "is_synced", new_callable=lambda: property(lambda _: True)):
            result = workflow.get_workflow_metadata()

        assert result["is_synced"] is True

    def test_falls_back_to_is_synced_when_file_path_is_none(self, engine: Engine) -> None:
        workflow = self._make_workflow(engine, file_path=None)
        synced_path = Path("/synced")
        workspace_path = Path("/workspace")

        with patch.object(type(workflow), "is_synced", new_callable=lambda: property(lambda _: False)):
            result = workflow.get_workflow_metadata(synced_path=synced_path, workspace_path=workspace_path)

        assert result["is_synced"] is False
