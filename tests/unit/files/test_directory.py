"""Unit tests for Directory and DirectoryDestination."""

import pathlib
from unittest import mock

import pytest

from griptape_nodes.common import macro_parser
from griptape_nodes.files import directory as directory_mod
from griptape_nodes.retained_mode.events import os_events, project_events

HANDLE_REQUEST_PATH = "griptape_nodes.files.directory.griptape_nodes_mod.GriptapeNodes.handle_request"


class TestDirectoryConstructor:
    """Tests that Directory constructor stores references without I/O."""

    def test_stores_plain_string(self) -> None:
        d = directory_mod.Directory("workspace/renders")
        assert d._dir_path == "workspace/renders"

    def test_does_no_io(self) -> None:
        with mock.patch(HANDLE_REQUEST_PATH) as mock_handle:
            directory_mod.Directory("workspace/renders")
        mock_handle.assert_not_called()

    def test_auto_wraps_macro_string_in_macro_path(self) -> None:
        d = directory_mod.Directory("{outputs}/frames")
        assert isinstance(d._dir_path, project_events.MacroPath)
        assert d._dir_path.variables == {}

    def test_keeps_plain_string_without_vars_unchanged(self) -> None:
        d = directory_mod.Directory("workspace/frames")
        assert d._dir_path == "workspace/frames"

    def test_stores_macro_path_unchanged(self) -> None:
        macro_path = project_events.MacroPath(macro_parser.ParsedMacro("{outputs}/renders"), {"outputs": "/resolved"})
        d = directory_mod.Directory(macro_path)
        assert d._dir_path is macro_path

    def test_invalid_macro_syntax_stored_as_plain_string(self) -> None:
        with mock.patch(
            "griptape_nodes.files.directory.macro_parser.ParsedMacro", side_effect=macro_parser.MacroSyntaxError("bad")
        ):
            d = directory_mod.Directory("{unclosed")
        assert d._dir_path == "{unclosed"

    def test_macro_string_preserves_template(self) -> None:
        d = directory_mod.Directory("{outputs}/renders_v001")
        assert isinstance(d._dir_path, project_events.MacroPath)
        assert d._dir_path.parsed_macro.template == "{outputs}/renders_v001"


class TestDirectoryResolve:
    """Tests for Directory.resolve()."""

    def test_resolve_plain_string_returns_path(self, tmp_path: pathlib.Path) -> None:
        dir_path = str(tmp_path / "renders")
        d = directory_mod.Directory(dir_path)
        with mock.patch(HANDLE_REQUEST_PATH) as mock_handle:
            result = d.resolve()
        mock_handle.assert_not_called()
        assert result == pathlib.Path(dir_path)

    def test_resolve_macro_path_calls_handle_request(self) -> None:
        macro_path = project_events.MacroPath(
            macro_parser.ParsedMacro("{outputs}/renders"), {"outputs": "/workspace/outputs"}
        )
        resolve_result = project_events.GetPathForMacroResultSuccess(
            result_details="OK",
            resolved_path=pathlib.Path("outputs/renders"),
            absolute_path=pathlib.Path("/workspace/outputs/renders"),
        )
        with mock.patch(HANDLE_REQUEST_PATH, return_value=resolve_result):
            result = directory_mod.Directory(macro_path).resolve()
        assert result == pathlib.Path("/workspace/outputs/renders")

    def test_resolve_macro_path_failure_raises_directory_error(self) -> None:
        macro_path = project_events.MacroPath(macro_parser.ParsedMacro("{outputs}/renders"), {})
        failure = project_events.GetPathForMacroResultFailure(
            result_details="Missing variables: outputs",
            failure_reason=project_events.PathResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
            missing_variables={"outputs"},
        )
        with mock.patch(HANDLE_REQUEST_PATH, return_value=failure), pytest.raises(directory_mod.DirectoryError):
            directory_mod.Directory(macro_path).resolve()


class TestDirectoryLocation:
    """Tests for Directory.location property."""

    def test_location_plain_string(self) -> None:
        d = directory_mod.Directory("workspace/renders")
        assert d.location == "workspace/renders"

    def test_location_macro_path_returns_template(self) -> None:
        d = directory_mod.Directory("{outputs}/renders")
        assert d.location == "{outputs}/renders"

    def test_location_macro_path_object_returns_template(self) -> None:
        macro_path = project_events.MacroPath(macro_parser.ParsedMacro("{outputs}/renders"), {"outputs": "/resolved"})
        d = directory_mod.Directory(macro_path)
        assert d.location == "{outputs}/renders"

    def test_location_no_io_performed(self) -> None:
        with mock.patch(HANDLE_REQUEST_PATH) as mock_handle:
            d = directory_mod.Directory("{outputs}/renders")
            _ = d.location
        mock_handle.assert_not_called()


class TestDirectoryName:
    """Tests for Directory.name property."""

    def test_name_plain_string(self) -> None:
        d = directory_mod.Directory("workspace/renders")
        assert d.name == "renders"

    def test_name_macro_template(self) -> None:
        d = directory_mod.Directory("{outputs}/renders_v001")
        assert d.name == "renders_v001"

    def test_name_nested_path(self) -> None:
        d = directory_mod.Directory("workspace/project/outputs/frames")
        assert d.name == "frames"


class TestDirectoryDestinationConstructor:
    """Tests for DirectoryDestination constructor."""

    def test_does_no_io(self) -> None:
        with mock.patch(HANDLE_REQUEST_PATH) as mock_handle:
            directory_mod.DirectoryDestination("workspace/renders")
        mock_handle.assert_not_called()

    def test_defaults_create_new_and_create_parents(self) -> None:
        dest = directory_mod.DirectoryDestination("workspace/renders")
        assert dest._existing_dir_policy == os_events.ExistingFilePolicy.CREATE_NEW
        assert dest._create_parents is True

    def test_stores_overwrite_policy(self) -> None:
        dest = directory_mod.DirectoryDestination(
            "workspace/renders", existing_dir_policy=os_events.ExistingFilePolicy.OVERWRITE
        )
        assert dest._existing_dir_policy == os_events.ExistingFilePolicy.OVERWRITE

    def test_stores_create_parents_false(self) -> None:
        dest = directory_mod.DirectoryDestination("workspace/renders", create_parents=False)
        assert dest._create_parents is False


class TestDirectoryDestinationCreateDirect:
    """Tests for DirectoryDestination.create() in non-versioning (direct/overwrite) mode."""

    def test_create_plain_string_creates_directory(self, tmp_path: pathlib.Path) -> None:
        dir_path = str(tmp_path / "renders")
        mkdir_result = os_events.MakeDirectoryResultSuccess(result_details="OK", created_path=dir_path)
        map_result = project_events.AttemptMapAbsolutePathToProjectResultSuccess(result_details="OK", mapped_path=None)
        dest = directory_mod.DirectoryDestination(dir_path, existing_dir_policy=os_events.ExistingFilePolicy.OVERWRITE)
        with mock.patch(HANDLE_REQUEST_PATH, side_effect=[mkdir_result, map_result]):
            directory = dest.create()
        assert directory.resolve() == tmp_path / "renders"

    def test_create_overwrite_existing_dir_succeeds(self, tmp_path: pathlib.Path) -> None:
        existing = tmp_path / "renders"
        existing.mkdir()
        mkdir_result = os_events.MakeDirectoryResultSuccess(
            result_details="OK", created_path=str(existing), already_existed=True
        )
        map_result = project_events.AttemptMapAbsolutePathToProjectResultSuccess(result_details="OK", mapped_path=None)
        dest = directory_mod.DirectoryDestination(
            str(existing), existing_dir_policy=os_events.ExistingFilePolicy.OVERWRITE
        )
        with mock.patch(HANDLE_REQUEST_PATH, side_effect=[mkdir_result, map_result]):
            directory = dest.create()
        assert existing.is_dir()
        assert directory.resolve() == existing

    def test_create_fail_policy_on_existing_raises(self, tmp_path: pathlib.Path) -> None:
        existing = tmp_path / "renders"
        existing.mkdir()
        dest = directory_mod.DirectoryDestination(str(existing), existing_dir_policy=os_events.ExistingFilePolicy.FAIL)
        with mock.patch(HANDLE_REQUEST_PATH) as mock_handle, pytest.raises(directory_mod.DirectoryError):
            dest.create()
        mock_handle.assert_not_called()

    def test_create_returns_directory_with_absolute_location(self, tmp_path: pathlib.Path) -> None:
        dir_path = str(tmp_path / "output")
        mkdir_result = os_events.MakeDirectoryResultSuccess(result_details="OK", created_path=dir_path)
        map_result = project_events.AttemptMapAbsolutePathToProjectResultSuccess(result_details="OK", mapped_path=None)
        dest = directory_mod.DirectoryDestination(dir_path, existing_dir_policy=os_events.ExistingFilePolicy.OVERWRITE)
        with mock.patch(HANDLE_REQUEST_PATH, side_effect=[mkdir_result, map_result]):
            directory = dest.create()
        assert pathlib.Path(directory.location).is_absolute()

    def test_create_returns_directory_with_mapped_macro_when_inside_project(self, tmp_path: pathlib.Path) -> None:
        dir_path = str(tmp_path / "renders")
        mkdir_result = os_events.MakeDirectoryResultSuccess(result_details="OK", created_path=dir_path)
        map_result = project_events.AttemptMapAbsolutePathToProjectResultSuccess(
            result_details="OK",
            mapped_path="{outputs}/renders",
        )
        dest = directory_mod.DirectoryDestination(dir_path, existing_dir_policy=os_events.ExistingFilePolicy.OVERWRITE)
        with mock.patch(HANDLE_REQUEST_PATH, side_effect=[mkdir_result, map_result]):
            directory = dest.create()
        assert directory.location == "{outputs}/renders"

    def test_create_mkdir_failure_raises_directory_error(self, tmp_path: pathlib.Path) -> None:
        dir_path = str(tmp_path / "renders")
        mkdir_failure = os_events.MakeDirectoryResultFailure(
            result_details="Permission denied",
            failure_reason=os_events.FileIOFailureReason.PERMISSION_DENIED,
        )
        dest = directory_mod.DirectoryDestination(dir_path, existing_dir_policy=os_events.ExistingFilePolicy.OVERWRITE)
        with mock.patch(HANDLE_REQUEST_PATH, return_value=mkdir_failure), pytest.raises(directory_mod.DirectoryError):
            dest.create()


class TestDirectoryDestinationCreateVersioning:
    """Tests for DirectoryDestination.create() in versioning (CREATE_NEW) mode."""

    def test_versioning_macro_path_first_available_used(self, tmp_path: pathlib.Path) -> None:
        missing_dir = tmp_path / "renders_v001"

        index_result = os_events.GetNextVersionIndexResultSuccess(result_details="OK", index=1)
        resolve_result = project_events.GetPathForMacroResultSuccess(
            result_details="OK",
            resolved_path=pathlib.Path("renders_v001"),
            absolute_path=missing_dir,
        )
        mkdir_result = os_events.MakeDirectoryResultSuccess(result_details="OK", created_path=str(missing_dir))
        map_result = project_events.AttemptMapAbsolutePathToProjectResultSuccess(result_details="OK", mapped_path=None)

        macro_path = project_events.MacroPath(macro_parser.ParsedMacro("{outputs}/renders_v{_index:03}"), {})
        dest = directory_mod.DirectoryDestination(
            macro_path, existing_dir_policy=os_events.ExistingFilePolicy.CREATE_NEW
        )

        with mock.patch(HANDLE_REQUEST_PATH, side_effect=[index_result, resolve_result, mkdir_result, map_result]):
            directory = dest.create()

        assert directory.location == "{outputs}/renders_v{_index:03}"

    def test_versioning_macro_path_uses_index_from_engine(self, tmp_path: pathlib.Path) -> None:
        missing_dir = tmp_path / "renders_v003"

        index_result = os_events.GetNextVersionIndexResultSuccess(result_details="OK", index=3)
        resolve_result = project_events.GetPathForMacroResultSuccess(
            result_details="OK",
            resolved_path=pathlib.Path("renders_v003"),
            absolute_path=missing_dir,
        )
        mkdir_result = os_events.MakeDirectoryResultSuccess(result_details="OK", created_path=str(missing_dir))
        map_result = project_events.AttemptMapAbsolutePathToProjectResultSuccess(result_details="OK", mapped_path=None)

        macro_path = project_events.MacroPath(macro_parser.ParsedMacro("{outputs}/renders_v{_index:03}"), {})
        dest = directory_mod.DirectoryDestination(
            macro_path, existing_dir_policy=os_events.ExistingFilePolicy.CREATE_NEW
        )

        with mock.patch(HANDLE_REQUEST_PATH, side_effect=[index_result, resolve_result, mkdir_result, map_result]):
            directory = dest.create()

        assert directory.location == "{outputs}/renders_v{_index:03}"

    def test_versioning_none_index_treated_as_one(self, tmp_path: pathlib.Path) -> None:
        missing_dir = tmp_path / "renders_v001"

        index_result = os_events.GetNextVersionIndexResultSuccess(result_details="OK", index=None)
        resolve_result = project_events.GetPathForMacroResultSuccess(
            result_details="OK",
            resolved_path=pathlib.Path("renders_v001"),
            absolute_path=missing_dir,
        )
        mkdir_result = os_events.MakeDirectoryResultSuccess(result_details="OK", created_path=str(missing_dir))
        map_result = project_events.AttemptMapAbsolutePathToProjectResultSuccess(result_details="OK", mapped_path=None)

        macro_path = project_events.MacroPath(macro_parser.ParsedMacro("{outputs}/renders_v{_index:03}"), {})
        dest = directory_mod.DirectoryDestination(
            macro_path, existing_dir_policy=os_events.ExistingFilePolicy.CREATE_NEW
        )

        with mock.patch(HANDLE_REQUEST_PATH, side_effect=[index_result, resolve_result, mkdir_result, map_result]):
            directory = dest.create()

        assert directory.location == "{outputs}/renders_v{_index:03}"

    def test_versioning_index_request_failure_raises_directory_error(self) -> None:
        index_failure = os_events.GetNextVersionIndexResultFailure(
            result_details="Failed to determine next index",
            failure_reason=os_events.FileIOFailureReason.MISSING_MACRO_VARIABLES,
        )
        macro_path = project_events.MacroPath(macro_parser.ParsedMacro("{outputs}/renders_v{_index:03}"), {})
        dest = directory_mod.DirectoryDestination(
            macro_path, existing_dir_policy=os_events.ExistingFilePolicy.CREATE_NEW
        )

        with mock.patch(HANDLE_REQUEST_PATH, return_value=index_failure), pytest.raises(directory_mod.DirectoryError):
            dest.create()

    def test_versioning_macro_resolve_failure_raises_directory_error(self) -> None:
        index_result = os_events.GetNextVersionIndexResultSuccess(result_details="OK", index=1)
        resolve_failure = project_events.GetPathForMacroResultFailure(
            result_details="Macro resolution failed",
            failure_reason=project_events.PathResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
            missing_variables={"outputs"},
        )
        macro_path = project_events.MacroPath(macro_parser.ParsedMacro("{outputs}/renders_v{_index:03}"), {})
        dest = directory_mod.DirectoryDestination(
            macro_path, existing_dir_policy=os_events.ExistingFilePolicy.CREATE_NEW
        )

        with (
            mock.patch(HANDLE_REQUEST_PATH, side_effect=[index_result, resolve_failure]),
            pytest.raises(directory_mod.DirectoryError),
        ):
            dest.create()

    def test_versioning_mkdir_failure_raises_directory_error(self, tmp_path: pathlib.Path) -> None:
        missing_dir = tmp_path / "renders_v001"

        index_result = os_events.GetNextVersionIndexResultSuccess(result_details="OK", index=1)
        resolve_result = project_events.GetPathForMacroResultSuccess(
            result_details="OK",
            resolved_path=pathlib.Path("renders_v001"),
            absolute_path=missing_dir,
        )
        mkdir_failure = os_events.MakeDirectoryResultFailure(
            result_details="Directory already exists",
            failure_reason=os_events.FileIOFailureReason.POLICY_NO_OVERWRITE,
        )

        macro_path = project_events.MacroPath(macro_parser.ParsedMacro("{outputs}/renders_v{_index:03}"), {})
        dest = directory_mod.DirectoryDestination(
            macro_path, existing_dir_policy=os_events.ExistingFilePolicy.CREATE_NEW
        )

        with (
            mock.patch(HANDLE_REQUEST_PATH, side_effect=[index_result, resolve_result, mkdir_failure]),
            pytest.raises(directory_mod.DirectoryError),
        ):
            dest.create()

    def test_create_new_plain_string_appends_index(self, tmp_path: pathlib.Path) -> None:
        """Regression: CREATE_NEW with a plain string must use versioning, not silently reuse the directory."""
        dir_path = str(tmp_path / "renders")
        versioned_dir = tmp_path / "renders_1"

        index_result = os_events.GetNextVersionIndexResultSuccess(result_details="OK", index=1)
        resolve_result = project_events.GetPathForMacroResultSuccess(
            result_details="OK",
            resolved_path=pathlib.Path("renders_1"),
            absolute_path=versioned_dir,
        )
        mkdir_result = os_events.MakeDirectoryResultSuccess(result_details="OK", created_path=str(versioned_dir))
        map_result = project_events.AttemptMapAbsolutePathToProjectResultSuccess(result_details="OK", mapped_path=None)

        dest = directory_mod.DirectoryDestination(dir_path, existing_dir_policy=os_events.ExistingFilePolicy.CREATE_NEW)

        with mock.patch(HANDLE_REQUEST_PATH, side_effect=[index_result, resolve_result, mkdir_result, map_result]):
            directory = dest.create()

        assert directory.resolve() == versioned_dir

    def test_create_new_plain_string_increments_for_next_run(self, tmp_path: pathlib.Path) -> None:
        """Regression: second CREATE_NEW call on the same plain-string base uses index=2, not index=1."""
        dir_path = str(tmp_path / "renders")
        versioned_dir = tmp_path / "renders_2"

        index_result = os_events.GetNextVersionIndexResultSuccess(result_details="OK", index=2)
        resolve_result = project_events.GetPathForMacroResultSuccess(
            result_details="OK",
            resolved_path=pathlib.Path("renders_2"),
            absolute_path=versioned_dir,
        )
        mkdir_result = os_events.MakeDirectoryResultSuccess(result_details="OK", created_path=str(versioned_dir))
        map_result = project_events.AttemptMapAbsolutePathToProjectResultSuccess(result_details="OK", mapped_path=None)

        dest = directory_mod.DirectoryDestination(dir_path, existing_dir_policy=os_events.ExistingFilePolicy.CREATE_NEW)

        with mock.patch(HANDLE_REQUEST_PATH, side_effect=[index_result, resolve_result, mkdir_result, map_result]):
            directory = dest.create()

        assert directory.resolve() == versioned_dir

    def test_create_new_plain_string_index_request_failure_raises(self) -> None:
        index_failure = os_events.GetNextVersionIndexResultFailure(
            result_details="Failed to determine next index",
            failure_reason=os_events.FileIOFailureReason.MISSING_MACRO_VARIABLES,
        )
        dest = directory_mod.DirectoryDestination(
            "/some/path/renders", existing_dir_policy=os_events.ExistingFilePolicy.CREATE_NEW
        )

        with mock.patch(HANDLE_REQUEST_PATH, return_value=index_failure), pytest.raises(directory_mod.DirectoryError):
            dest.create()

    def test_create_new_macro_template_string_uses_versioning(self, tmp_path: pathlib.Path) -> None:
        """A macro template string passed directly to DirectoryDestination uses versioning.

        A string with variables but no {_index} is treated as a MacroPath for versioning.
        """
        versioned_dir = tmp_path / "renders_v001"

        index_result = os_events.GetNextVersionIndexResultSuccess(result_details="OK", index=1)
        resolve_result = project_events.GetPathForMacroResultSuccess(
            result_details="OK",
            resolved_path=pathlib.Path("renders_v001"),
            absolute_path=versioned_dir,
        )
        mkdir_result = os_events.MakeDirectoryResultSuccess(result_details="OK", created_path=str(versioned_dir))
        map_result = project_events.AttemptMapAbsolutePathToProjectResultSuccess(result_details="OK", mapped_path=None)

        dest = directory_mod.DirectoryDestination(
            "{outputs}/renders_v{_index:03}", existing_dir_policy=os_events.ExistingFilePolicy.CREATE_NEW
        )

        with mock.patch(HANDLE_REQUEST_PATH, side_effect=[index_result, resolve_result, mkdir_result, map_result]):
            directory = dest.create()

        assert directory.location == "{outputs}/renders_v{_index:03}"
