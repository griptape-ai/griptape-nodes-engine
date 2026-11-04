"""Tests for absolute and relative directory path resolution in LibraryManager."""

import asyncio
import logging
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from griptape_nodes.files.path_utils import canonicalize_for_identity_preserving_symlinks, canonicalize_for_io
from griptape_nodes.node_library.library_registry import Library, LibraryMetadata, LibraryRegistry, LibrarySchema
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.library_events import (
    DownloadLibraryResultFailure,
    LoadLibraryMetadataFromFileResultSuccess,
    RegisterSandboxNodeFromSourceRequest,
    RegisterSandboxNodeFromSourceResultFailure,
    RegisterSandboxNodeFromSourceResultSuccess,
    UpdateLibraryRequest,
    UpdateLibraryResultFailure,
)
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.library.git_operations import LibraryGitOperationContext
from griptape_nodes.retained_mode.managers.library.sandbox import LibrarySandbox
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.utils.git_utils import GitCloneError, GitPullError


class TestGetSandboxDirectory:
    """Test get_sandbox_directory resolves absolute and relative paths."""

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        return workspace

    @pytest.fixture
    def config_manager(self, engine: Engine, monkeypatch: pytest.MonkeyPatch, workspace: Path) -> Mock:
        config_manager = Mock(spec=ConfigManager)
        config_manager.workspace_path = workspace
        config_manager.get_config_value.return_value = "sandbox_library"
        monkeypatch.setattr(engine, "_config_manager", config_manager)
        return config_manager

    @pytest.fixture
    def path_identity(self, monkeypatch: pytest.MonkeyPatch) -> Mock:
        """Spy on the link-preserving path identity the sandbox directory is built with."""
        path_identity = Mock(
            spec=canonicalize_for_identity_preserving_symlinks,
            side_effect=canonicalize_for_identity_preserving_symlinks,
        )
        monkeypatch.setattr(
            "griptape_nodes.retained_mode.managers.library.sandbox.canonicalize_for_identity_preserving_symlinks",
            path_identity,
        )
        return path_identity

    def test_relative_path(self, engine: Engine, config_manager: Mock, workspace: Path, path_identity: Mock) -> None:
        """A relative sandbox_library_directory is resolved against the workspace."""
        (workspace / "sandbox_library").mkdir()

        result = engine.library_manager.sandbox.get_sandbox_directory()

        config_manager.get_config_value.assert_called_once_with("sandbox_library_directory")
        path_identity.assert_called_once_with("sandbox_library", base=workspace)
        assert result == workspace / "sandbox_library"

    def test_home_directory_is_expanded(
        self, engine: Engine, config_manager: Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sandbox_library_directory starting with `~` names a folder in the home directory."""
        home = tmp_path / "home"
        (home / "my_sandbox").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        config_manager.get_config_value.return_value = str(Path("~") / "my_sandbox")

        result = engine.library_manager.sandbox.get_sandbox_directory()

        assert result == home / "my_sandbox"

    def test_environment_variable_is_expanded(
        self, engine: Engine, config_manager: Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An environment variable later in a sandbox_library_directory is replaced by its value."""
        (tmp_path / "sandboxes" / "mine").mkdir(parents=True)
        monkeypatch.setenv("GTN_TEST_SANDBOX_NAME", "mine")
        config_manager.get_config_value.return_value = str(tmp_path / "sandboxes" / "$GTN_TEST_SANDBOX_NAME")

        result = engine.library_manager.sandbox.get_sandbox_directory()

        assert result == tmp_path / "sandboxes" / "mine"

    def test_absolute_path(self, engine: Engine, config_manager: Mock, tmp_path: Path) -> None:
        """An absolute sandbox_library_directory is used as-is."""
        sandbox = tmp_path / "elsewhere" / "sandbox"
        sandbox.mkdir(parents=True)
        config_manager.get_config_value.return_value = str(sandbox)

        result = engine.library_manager.sandbox.get_sandbox_directory()

        assert result == sandbox

    def test_parent_segments_are_normalized(self, engine: Engine, config_manager: Mock, workspace: Path) -> None:
        """A path with `..` in it names the folder it lands on, not the detour."""
        (workspace / "sandbox_library").mkdir()
        config_manager.get_config_value.return_value = str(Path("detour") / ".." / "sandbox_library")

        result = engine.library_manager.sandbox.get_sandbox_directory()

        assert result == workspace / "sandbox_library"

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
    def test_linked_sandbox_keeps_the_link(self, engine: Engine, config_manager: Mock, workspace: Path) -> None:
        """A sandbox folder that is a link is named by the link, not by its target."""
        outside = workspace.parent / "outside"
        outside.mkdir()
        (workspace / "sandbox_library").symlink_to(outside, target_is_directory=True)
        config_manager.get_config_value.return_value = "sandbox_library"

        result = engine.library_manager.sandbox.get_sandbox_directory()

        assert result == workspace / "sandbox_library"

    def test_not_configured_returns_none(self, engine: Engine, config_manager: Mock, path_identity: Mock) -> None:
        """When sandbox_library_directory is empty, returns None without resolving."""
        config_manager.get_config_value.return_value = ""

        result = engine.library_manager.sandbox.get_sandbox_directory()

        assert result is None
        path_identity.assert_not_called()

    def test_nonexistent_directory_returns_none(self, engine: Engine, config_manager: Mock) -> None:
        """When the resolved directory does not exist, returns None."""
        config_manager.get_config_value.return_value = "sandbox_library"

        result = engine.library_manager.sandbox.get_sandbox_directory()

        assert result is None

    def test_missing_directory_is_logged_at_debug(
        self, engine: Engine, config_manager: Mock, workspace: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing sandbox folder is logged with the setting and the path it became."""
        config_manager.get_config_value.return_value = "sandbox_library"

        with caplog.at_level(logging.DEBUG, logger="griptape_nodes"):
            result = engine.library_manager.sandbox.get_sandbox_directory()

        assert result is None
        message = (
            f"The sandbox library directory 'sandbox_library' does not exist "
            f"at '{workspace / 'sandbox_library'}', so no sandbox is loaded."
        )
        assert [(record.levelno, record.getMessage()) for record in caplog.records] == [(logging.DEBUG, message)]

    def test_unset_environment_variable_later_in_the_path_logs_the_path_it_resolved_to(
        self,
        engine: Engine,
        config_manager: Mock,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An unset variable after the first segment is left as written, and the debug log says where it looked."""
        monkeypatch.delenv("GTN_UNSET_TEST_VAR", raising=False)
        # Forward slash: sanitize_path_string drops a backslash before "$" in a relative Windows path.
        configured = "sandbox/$GTN_UNSET_TEST_VAR"
        config_manager.get_config_value.return_value = configured

        with caplog.at_level(logging.DEBUG, logger="griptape_nodes"):
            result = engine.library_manager.sandbox.get_sandbox_directory()

        assert result is None
        resolved = workspace / "sandbox" / "$GTN_UNSET_TEST_VAR"
        message = (
            f"The sandbox library directory '{configured}' does not exist at '{resolved}', so no sandbox is loaded."
        )
        assert [(record.levelno, record.getMessage()) for record in caplog.records] == [(logging.DEBUG, message)]


_SANDBOX_NODE_SOURCE = (
    "from griptape_nodes.exe_types.node_types import DataNode\n"
    "\n"
    "class {class_name}(DataNode):\n"
    "    def process(self) -> None:\n"
    "        return None\n"
)


class SandboxLinkedFoldersBase:
    """Shared setup for sandbox scans that reach folders through links."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        """LibraryRegistry holds class-level state that survives the engine reset fixture."""
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    @pytest.fixture
    def sandbox_directory(self, tmp_path: Path) -> Path:
        sandbox_directory = tmp_path / "sandbox"
        sandbox_directory.mkdir()
        return sandbox_directory

    @pytest.fixture
    def linked_folder(self, tmp_path: Path, sandbox_directory: Path) -> Path:
        """A folder outside the sandbox holding a node file, linked into the sandbox as `link_name`."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "node.py").write_text(_SANDBOX_NODE_SOURCE.format(class_name="LinkedNode"))
        link = sandbox_directory / "link_name"
        link.symlink_to(outside, target_is_directory=True)
        return link

    @pytest.fixture
    def library_info(self, sandbox_directory: Path) -> LibraryManager.LibraryInfo:
        return LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.DEPENDENCIES_INSTALLED,
            fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
            library_path=str(sandbox_directory / LibraryManager.LIBRARY_CONFIG_FILENAME),
            is_sandbox=True,
            library_name=LibraryManager.SANDBOX_LIBRARY_NAME,
        )

    async def _load(self, engine: Engine, sandbox_directory: Path, library_info: LibraryManager.LibraryInfo) -> Library:
        """Scan and load the sandbox the way engine startup does, and return the Sandbox Library."""
        library_manager = engine.library_manager
        scan_result = library_manager.sandbox._generate_sandbox_library_metadata(sandbox_directory=sandbox_directory)
        assert isinstance(scan_result, LoadLibraryMetadataFromFileResultSuccess), scan_result

        await library_manager.sandbox.attempt_generate_sandbox_library_from_schema(
            library_schema=scan_result.library_schema,
            sandbox_directory=str(sandbox_directory),
            library_info=library_info,
        )

        assert library_info.problems == []
        return LibraryRegistry.get_library(LibraryManager.SANDBOX_LIBRARY_NAME)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
class TestGenerateSandboxLibraryMetadata(SandboxLinkedFoldersBase):
    """Tests for how LibrarySandbox._generate_sandbox_library_metadata finds files through links."""

    @pytest.mark.usefixtures("linked_folder")
    def test_file_in_a_linked_folder_keeps_the_link_in_its_path(self, engine: Engine, sandbox_directory: Path) -> None:
        assert self._scanned_file_paths(engine, sandbox_directory) == [str(Path("link_name") / "node.py")]

    def test_link_back_to_an_enclosing_folder_is_not_followed_forever(
        self, engine: Engine, sandbox_directory: Path
    ) -> None:
        nested = sandbox_directory / "nested"
        nested.mkdir()
        (nested / "node.py").write_text(_SANDBOX_NODE_SOURCE.format(class_name="NestedNode"))
        (nested / "loop").symlink_to(sandbox_directory, target_is_directory=True)

        assert self._scanned_file_paths(engine, sandbox_directory) == [str(Path("nested") / "node.py")]

    def test_folder_reached_through_two_links_is_scanned_once(
        self, engine: Engine, sandbox_directory: Path, linked_folder: Path
    ) -> None:
        (sandbox_directory / "second_link").symlink_to(linked_folder.resolve(), target_is_directory=True)

        assert len(self._scanned_file_paths(engine, sandbox_directory)) == 1

    def test_hidden_and_excluded_folders_are_still_skipped(self, engine: Engine, sandbox_directory: Path) -> None:
        for skipped in (".hidden", "venv", "__pycache__"):
            (sandbox_directory / skipped).mkdir()
            (sandbox_directory / skipped / "node.py").write_text(_SANDBOX_NODE_SOURCE.format(class_name="Skipped"))

        assert self._scanned_file_paths(engine, sandbox_directory) == []

    def _scanned_file_paths(self, engine: Engine, sandbox_directory: Path) -> list[str]:
        scan_result = engine.library_manager.sandbox._generate_sandbox_library_metadata(
            sandbox_directory=sandbox_directory
        )
        assert isinstance(scan_result, LoadLibraryMetadataFromFileResultSuccess), scan_result
        return [node.file_path for node in scan_result.library_schema.nodes]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
class TestAttemptGenerateSandboxLibraryFromSchemaThroughLinks(SandboxLinkedFoldersBase):
    """Tests for LibrarySandbox.attempt_generate_sandbox_library_from_schema loading files reached through links."""

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("linked_folder")
    async def test_node_in_a_linked_folder_is_loaded(
        self, engine: Engine, sandbox_directory: Path, library_info: LibraryManager.LibraryInfo
    ) -> None:
        library_manager = engine.library_manager
        scan_result = library_manager.sandbox._generate_sandbox_library_metadata(sandbox_directory=sandbox_directory)
        assert isinstance(scan_result, LoadLibraryMetadataFromFileResultSuccess), scan_result

        await library_manager.sandbox.attempt_generate_sandbox_library_from_schema(
            library_schema=scan_result.library_schema,
            sandbox_directory=str(sandbox_directory),
            library_info=library_info,
        )

        library = LibraryRegistry.get_library(LibraryManager.SANDBOX_LIBRARY_NAME)
        assert library.get_registered_nodes() == ["LinkedNode"]
        assert library_info.problems == []

    @pytest.mark.asyncio
    async def test_node_in_a_linked_folder_is_imported_as_one_module(
        self,
        engine: Engine,
        sandbox_directory: Path,
        linked_folder: Path,
        library_info: LibraryManager.LibraryInfo,
    ) -> None:
        node_file = linked_folder / "node.py"

        library = await self._load(engine, sandbox_directory, library_info)

        live_modules = _live_dynamic_modules_for(node_file)
        assert len(live_modules) == 1, live_modules
        assert library.get_node_class("LinkedNode") is live_modules[0].LinkedNode

    @pytest.mark.asyncio
    async def test_node_in_a_linked_sandbox_is_imported_as_one_module(
        self, engine: Engine, sandbox_directory: Path, tmp_path: Path, library_info: LibraryManager.LibraryInfo
    ) -> None:
        sandbox_link = tmp_path / "sandbox_link"
        sandbox_link.symlink_to(sandbox_directory, target_is_directory=True)
        node_file = sandbox_link / "node.py"
        node_file.write_text(_SANDBOX_NODE_SOURCE.format(class_name="LinkedSandboxNode"))

        library = await self._load(engine, sandbox_link, library_info)

        live_modules = _live_dynamic_modules_for(node_file)
        assert len(live_modules) == 1, live_modules
        assert library.get_node_class("LinkedSandboxNode") is live_modules[0].LinkedSandboxNode


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
class TestRegisterSandboxNodeFromSourceRequestThroughLinks(SandboxLinkedFoldersBase):
    """Tests for LibrarySandbox.register_sandbox_node_from_source_request with files reached through links."""

    @pytest.fixture(autouse=True)
    def sandbox_directory_lookup(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, sandbox_directory: Path
    ) -> Mock:
        """Point the handler at the test sandbox rather than the configured one."""
        library_manager = engine.library_manager
        sandbox_directory_lookup = Mock(spec=LibrarySandbox.get_sandbox_directory, return_value=sandbox_directory)
        monkeypatch.setattr(library_manager.sandbox, "get_sandbox_directory", sandbox_directory_lookup)
        return sandbox_directory_lookup

    @pytest.fixture
    def sandbox_library(self) -> Library:
        """An empty Sandbox Library for the handler to register into, as engine startup would leave it."""
        return LibraryRegistry.generate_new_library(
            library_data=LibrarySchema(
                name=LibraryManager.SANDBOX_LIBRARY_NAME,
                library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
                metadata=LibraryMetadata(
                    author="test",
                    description="test sandbox",
                    library_version="1.0.0",
                    engine_version="1.0.0",
                    tags=[],
                ),
                categories=[],
                nodes=[],
            )
        )

    @pytest.fixture
    def io_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sandbox_directory: Path) -> Mock:
        """Hand the handler a second-link spelling of each file for I/O, as the Windows long-path prefix does."""
        io_alias = tmp_path / "io_alias"
        io_alias.symlink_to(sandbox_directory, target_is_directory=True)

        def respell_for_io(path: str | Path, *, base: Path | None = None) -> Path:
            return io_alias / canonicalize_for_io(path, base=base).relative_to(sandbox_directory)

        io_path = Mock(spec=canonicalize_for_io, side_effect=respell_for_io)
        monkeypatch.setattr("griptape_nodes.retained_mode.managers.library.sandbox.canonicalize_for_io", io_path)
        return io_path

    @pytest.fixture
    def linked_sandbox(self, tmp_path: Path, sandbox_directory: Path, sandbox_directory_lookup: Mock) -> Path:
        """Configure the sandbox as a link to the real sandbox folder, and return the link."""
        sandbox_link = tmp_path / "sandbox_link"
        sandbox_link.symlink_to(sandbox_directory, target_is_directory=True)
        sandbox_directory_lookup.return_value = sandbox_link
        return sandbox_link

    @pytest.mark.usefixtures("linked_folder")
    def test_file_in_a_linked_folder_is_accepted(self, engine: Engine, sandbox_library: Library) -> None:
        result = engine.library_manager.sandbox.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=str(Path("link_name") / "node.py"))
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultSuccess), result.result_details
        assert result.registered_class_names == ["LinkedNode"]
        assert sandbox_library.has_node_type("LinkedNode")

    @pytest.mark.usefixtures("linked_folder", "sandbox_library")
    @pytest.mark.parametrize(
        "requested_path",
        [
            pytest.param(str(Path("..") / "escape.py"), id="parent_folder"),
            pytest.param(str(Path("link_name") / ".." / ".." / "escape.py"), id="through_a_link_then_back_out"),
            pytest.param("{tmp_path}/escape.py", id="absolute_path_outside"),
            pytest.param("{tmp_path}/outside/node.py", id="absolute_path_to_a_linked_folder_target"),
        ],
    )
    def test_path_outside_the_sandbox_is_rejected(self, engine: Engine, tmp_path: Path, requested_path: str) -> None:
        (tmp_path / "escape.py").write_text(_SANDBOX_NODE_SOURCE.format(class_name="EscapedNode"))

        result = engine.library_manager.sandbox.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=requested_path.format(tmp_path=tmp_path))
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultFailure)
        assert "is not inside the sandbox directory" in str(result.result_details)

    @pytest.mark.usefixtures("linked_sandbox")
    def test_file_named_by_the_real_location_of_a_linked_sandbox_is_accepted(
        self, engine: Engine, sandbox_directory: Path, sandbox_library: Library
    ) -> None:
        """A linked sandbox's real folder is the same folder, so a path through it is inside."""
        real_file = sandbox_directory / "node.py"
        real_file.write_text(_SANDBOX_NODE_SOURCE.format(class_name="RealLocationNode"))

        result = engine.library_manager.sandbox.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=str(real_file))
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultSuccess), result.result_details
        assert result.registered_class_names == ["RealLocationNode"]
        assert sandbox_library.has_node_type("RealLocationNode")

    @pytest.mark.asyncio
    async def test_file_in_a_linked_folder_stays_one_module_when_registered_live(
        self,
        engine: Engine,
        sandbox_directory: Path,
        linked_folder: Path,
        library_info: LibraryManager.LibraryInfo,
    ) -> None:
        node_file = linked_folder / "node.py"
        library = await self._load(engine, sandbox_directory, library_info)

        result = engine.library_manager.sandbox.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=str(Path("link_name") / "node.py"))
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultSuccess), result.result_details
        live_modules = _live_dynamic_modules_for(node_file)
        assert len(live_modules) == 1, live_modules
        assert library.get_node_class("LinkedNode") is live_modules[0].LinkedNode

    @pytest.mark.asyncio
    async def test_file_in_a_linked_folder_spelled_three_ways_is_one_module(
        self,
        engine: Engine,
        sandbox_directory: Path,
        linked_folder: Path,
        sandbox_library: Library,
        library_info: LibraryManager.LibraryInfo,
    ) -> None:
        node_file = linked_folder / "node.py"
        spellings = [
            str(Path("link_name") / "node.py"),
            str(Path("link_name") / ".." / "link_name" / "node.py"),
            str(sandbox_directory / "link_name" / "node.py"),
        ]
        for spelling in spellings:
            result = engine.library_manager.sandbox.register_sandbox_node_from_source_request(
                RegisterSandboxNodeFromSourceRequest(file_path=spelling)
            )
            assert isinstance(result, RegisterSandboxNodeFromSourceResultSuccess), result.result_details
        assert sandbox_library.has_node_type("LinkedNode")
        # Engine startup builds the Sandbox Library afresh, so the live one makes way for the load.
        LibraryRegistry.unregister_library(LibraryManager.SANDBOX_LIBRARY_NAME, event_manager=engine.event_manager)

        library = await self._load(engine, sandbox_directory, library_info)

        live_modules = _live_dynamic_modules_for(node_file)
        assert len(live_modules) == 1, live_modules
        assert library.get_node_class("LinkedNode") is live_modules[0].LinkedNode

    @pytest.mark.usefixtures("sandbox_library", "linked_folder")
    def test_file_in_a_linked_folder_is_reported_by_its_link(
        self,
        engine: Engine,
        sandbox_directory: Path,
    ) -> None:
        link_path = sandbox_directory / "link_name" / "node.py"

        result = engine.library_manager.sandbox.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=str(Path("link_name") / "node.py"))
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultSuccess), result.result_details
        assert result.file_path == str(link_path)

    @pytest.mark.usefixtures("linked_folder")
    def test_parent_segment_after_a_link_is_removed_before_the_link_is_followed(
        self, engine: Engine, sandbox_directory: Path, tmp_path: Path, sandbox_library: Library
    ) -> None:
        """`link_name/../x.py` names the sandbox's own `x.py`, never the one beside the link's target."""
        sandbox_file = sandbox_directory / "x.py"
        sandbox_file.write_text(_SANDBOX_NODE_SOURCE.format(class_name="SandboxSideNode"))
        # `link_name` points at `tmp_path / "outside"`, so this is `<target>/../x.py`.
        (tmp_path / "x.py").write_text(_SANDBOX_NODE_SOURCE.format(class_name="TargetSideNode"))

        result = engine.library_manager.sandbox.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=str(Path("link_name") / ".." / "x.py"))
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultSuccess), result.result_details
        assert result.registered_class_names == ["SandboxSideNode"]
        assert not sandbox_library.has_node_type("TargetSideNode")
        live_modules = _live_dynamic_modules_for(sandbox_file)
        assert len(live_modules) == 1, live_modules
        assert sandbox_library.get_node_class("SandboxSideNode") is live_modules[0].SandboxSideNode

    @pytest.mark.usefixtures("linked_folder", "sandbox_library")
    def test_missing_file_in_a_linked_folder_is_named_by_its_link(
        self, engine: Engine, sandbox_directory: Path
    ) -> None:
        requested_path = str(Path("link_name") / "missing.py")

        result = engine.library_manager.sandbox.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=requested_path)
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultFailure)
        assert str(result.result_details) == (
            f"Attempted to register a sandbox node with file_path={requested_path!r}. "
            f"Failed because no file exists at the path '{sandbox_directory / 'link_name' / 'missing.py'}'. "
            "Write the source file into the sandbox directory before calling this request."
        )

    @pytest.mark.usefixtures("sandbox_library")
    def test_registered_file_is_named_by_its_link_kept_path_in_the_summary(
        self, engine: Engine, sandbox_directory: Path, io_path: Mock
    ) -> None:
        (sandbox_directory / "node.py").write_text(_SANDBOX_NODE_SOURCE.format(class_name="PlainNode"))

        result = engine.library_manager.sandbox.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path="node.py")
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultSuccess), result.result_details
        assert str(result.result_details) == (
            f"Registered 1 node type(s) from '{sandbox_directory / 'node.py'}' into the Sandbox Library (replaced: 0)."
        )
        io_path.assert_called_once_with("node.py", base=sandbox_directory)

    @pytest.mark.usefixtures("io_path", "sandbox_library")
    def test_duplicate_node_type_is_named_by_its_link_kept_path(self, engine: Engine, sandbox_directory: Path) -> None:
        (sandbox_directory / "node.py").write_text(_SANDBOX_NODE_SOURCE.format(class_name="PlainNode"))
        first = engine.library_manager.sandbox.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path="node.py")
        )
        assert isinstance(first, RegisterSandboxNodeFromSourceResultSuccess), first.result_details

        result = engine.library_manager.sandbox.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path="node.py", replace_if_exists=False)
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultFailure)
        assert str(result.result_details) == (
            f"Attempted to register node type 'PlainNode' from '{sandbox_directory / 'node.py'}'. "
            "Failed because a node type with that name is already registered in the Sandbox Library "
            "and replace_if_exists=False."
        )

    @pytest.mark.usefixtures("io_path", "sandbox_library")
    def test_file_without_node_types_is_named_by_its_link_kept_path(
        self, engine: Engine, sandbox_directory: Path
    ) -> None:
        (sandbox_directory / "no_nodes.py").write_text("VALUE = 1\n")

        result = engine.library_manager.sandbox.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path="no_nodes.py")
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultFailure)
        assert str(result.result_details) == (
            f"Imported '{sandbox_directory / 'no_nodes.py'}' successfully, but it does not declare any BaseNode "
            "subclasses (must be `class X(BaseNode):` defined in this file, not re-exported from another module). "
            "Nothing was registered."
        )


def _live_dynamic_modules_for(file_path: Path) -> list[ModuleType]:
    """The dynamically imported node modules in `sys.modules` whose source is `file_path`, wherever it was reached from."""
    real_file_path = file_path.resolve()
    live_modules = []
    for module_name, module in list(sys.modules.items()):
        if not module_name.startswith("gtn_dynamic_module_"):
            continue
        module_file = getattr(module, "__file__", None)
        if module_file is not None and Path(module_file).resolve() == real_file_path:
            live_modules.append(module)
    return live_modules


class TestDownloadLibrariesFromGitUrlsPath:
    """Test download_libraries_from_git_urls resolves absolute and relative paths."""

    @pytest.mark.asyncio
    async def test_uses_resolved_libraries_root(self, engine: Engine) -> None:
        """The download root comes from ConfigManager.resolved_libraries_root (own/inherited or default)."""
        library_manager = engine.library_manager
        config_mgr = MagicMock()
        config_mgr.resolved_libraries_root.return_value = Path("/workspace/libraries")

        with patch.object(engine, "_config_manager", config_mgr):
            result = await library_manager.provisioning.download_libraries_from_git_urls([])

        config_mgr.resolved_libraries_root.assert_called_once_with()
        assert result == {}


class TestDownloadLibraryRequestPath:
    """Test download_library_request resolves absolute and relative paths."""

    @pytest.mark.asyncio
    async def test_uses_resolved_libraries_root(self, engine: Engine) -> None:
        """The download root comes from ConfigManager.resolved_libraries_root."""
        library_manager = engine.library_manager
        config_mgr = MagicMock()
        config_mgr.resolved_libraries_root.return_value = Path("/workspace/libraries")

        request = MagicMock()
        request.git_url = "https://github.com/user/repo.git"
        request.branch_tag_commit = None
        request.target_directory_name = None
        request.download_directory = None

        with (
            patch.object(engine, "_config_manager", config_mgr),
            patch(
                "griptape_nodes.retained_mode.managers.library.git_operations.normalize_github_url",
                return_value="https://github.com/user/repo.git",
            ),
            patch("anyio.Path.mkdir"),
            patch("anyio.Path.exists", return_value=False),
            patch.object(asyncio, "to_thread", side_effect=GitCloneError("stop test here")),
        ):
            result = await library_manager.git_operations.download_library_request(request)

        config_mgr.resolved_libraries_root.assert_called_once_with()
        assert isinstance(result, DownloadLibraryResultFailure)

    @pytest.mark.asyncio
    async def test_custom_download_directory_skips_config(self, engine: Engine) -> None:
        """When download_directory is provided, it is used directly without resolving config."""
        library_manager = engine.library_manager
        config_mgr = MagicMock()
        config_mgr.workspace_path = Path("/workspace")

        request = MagicMock()
        request.git_url = "https://github.com/user/repo.git"
        request.branch_tag_commit = None
        request.target_directory_name = None
        request.download_directory = "/custom/dir"

        with (
            patch.object(engine, "_config_manager", config_mgr),
            patch(
                "griptape_nodes.retained_mode.managers.library.git_operations.normalize_github_url",
                return_value="https://github.com/user/repo.git",
            ),
            patch("anyio.Path.mkdir"),
            patch("anyio.Path.exists", return_value=False),
            patch.object(asyncio, "to_thread", side_effect=GitCloneError("stop test here")),
        ):
            await library_manager.git_operations.download_library_request(request)

        config_mgr.resolved_libraries_root.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_target_dir_sets_existing_path(self, engine: Engine) -> None:
        """Existing target directory failure carries the path in ``existing_path``.

        When the target directory already exists and fail_on_exists is True, the failure
        carries the absolute target path in the structured ``existing_path`` field so clients
        can render it without having to parse the human-readable error message (which is
        unreliable for paths containing ``:``, e.g. Windows drive letters).
        """
        library_manager = engine.library_manager
        config_mgr = MagicMock()
        config_mgr.resolved_libraries_root.return_value = Path("/opt/libraries")

        request = MagicMock()
        request.git_url = "https://github.com/user/repo.git"
        request.branch_tag_commit = None
        request.target_directory_name = "repo"
        request.download_directory = None
        request.overwrite_existing = False
        request.fail_on_exists = True

        with (
            patch.object(engine, "_config_manager", config_mgr),
            patch(
                "griptape_nodes.retained_mode.managers.library.git_operations.normalize_github_url",
                return_value="https://github.com/user/repo.git",
            ),
            patch("anyio.Path.mkdir"),
            patch("anyio.Path.exists", return_value=True),
        ):
            result = await library_manager.git_operations.download_library_request(request)

        assert isinstance(result, DownloadLibraryResultFailure)
        assert result.retryable is True
        assert result.existing_path == str(Path("/opt/libraries/repo"))


class TestDownloadLibraryRequestUrlRef:
    """Test download_library_request honors a url@ref suffix."""

    async def _clone_args(self, engine: Engine, git_url: str, branch_tag_commit: str | None) -> tuple:
        config_mgr = MagicMock()
        config_mgr.resolved_libraries_root.return_value = Path("/workspace/libraries")

        request = MagicMock()
        request.git_url = git_url
        request.branch_tag_commit = branch_tag_commit
        request.target_directory_name = None
        request.download_directory = None

        with (
            patch.object(engine, "_config_manager", config_mgr),
            patch("anyio.Path.mkdir"),
            patch("anyio.Path.exists", return_value=False),
            patch.object(asyncio, "to_thread", side_effect=GitCloneError("stop test here")) as mock_to_thread,
        ):
            await engine.library_manager.git_operations.download_library_request(request)

        _, clone_url, target_path, ref = mock_to_thread.call_args.args
        return clone_url, target_path, ref

    @pytest.mark.asyncio
    async def test_url_ref_suffix_becomes_clone_ref(self, engine: Engine) -> None:
        """The @ref suffix is stripped from the clone URL and target dir and used as the ref."""
        clone_url, target_path, ref = await self._clone_args(engine, "https://github.com/user/repo@stable", None)

        assert clone_url == "https://github.com/user/repo.git"
        assert target_path == Path("/workspace/libraries/repo")
        assert ref == "stable"

    @pytest.mark.asyncio
    async def test_explicit_branch_tag_commit_overrides_url_ref(self, engine: Engine) -> None:
        """An explicit branch_tag_commit wins over the URL's @ref suffix."""
        clone_url, _, ref = await self._clone_args(engine, "https://github.com/user/repo@stable", "main")

        assert clone_url == "https://github.com/user/repo.git"
        assert ref == "main"


class TestUpdateLibraryRequestExistingPath:
    """Test update_library_request reports the dirty library directory in ``existing_path``."""

    @pytest.mark.asyncio
    async def test_uncommitted_changes_sets_existing_path(self, engine: Engine) -> None:
        """Uncommitted-changes update failure carries the library directory in ``existing_path``.

        Without this, clients on Windows cannot recover the path from the error message because
        the drive-letter colon collides with the ``<path>: <reason>`` separator in the
        human-readable message.
        """
        library_manager = engine.library_manager
        library_dir = Path("/var/lib/test_lib")

        validation_context = LibraryGitOperationContext(
            old_version="1.0.0",
            library_file_path=str(library_dir / "griptape_nodes_library.json"),
            library_dir=library_dir,
        )

        with (
            patch.object(
                library_manager.git_operations,
                "_validate_and_prepare_library_for_git_operation",
                new=AsyncMock(return_value=validation_context),
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library.git_operations.is_monorepo",
                return_value=False,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library.git_operations.update_library_git",
                side_effect=GitPullError(
                    f"Cannot update library at {library_dir}: You have uncommitted changes. "
                    "Use overwrite_existing=True to discard them."
                ),
            ),
        ):
            result = await library_manager.git_operations.update_library_request(
                UpdateLibraryRequest(library_name="test_lib", overwrite_existing=False)
            )

        assert isinstance(result, UpdateLibraryResultFailure)
        assert result.retryable is True
        assert result.existing_path == str(library_dir)
