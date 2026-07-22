"""Unit tests for _build_sequence_destination_from_situation."""

from unittest import mock

from griptape_nodes.common.project_templates import situation
from griptape_nodes.exe_types.param_components import project_file_sequence_parameter
from griptape_nodes.files import file_sequence
from griptape_nodes.retained_mode.events import os_events, project_events

HANDLE_REQUEST_PATH = "griptape_nodes.files.project_file.GriptapeNodes.handle_request"
BUILD_VERSIONED_PATH = "griptape_nodes.files.file_sequence.build_versioned_sequence_destination"

_POLICY_MAP = {
    "CREATE_NEW": situation.SituationFilePolicy.CREATE_NEW,
    "OVERWRITE": situation.SituationFilePolicy.OVERWRITE,
    "FAIL": situation.SituationFilePolicy.FAIL,
}


def _make_situation(
    macro: str,
    on_collision: str = "OVERWRITE",
    *,
    create_dirs: bool = True,
) -> situation.SituationTemplate:
    return situation.SituationTemplate(
        name="test_situation",
        macro=macro,
        policy=situation.SituationPolicy(on_collision=_POLICY_MAP[on_collision], create_dirs=create_dirs),
    )


class TestBuildSequenceDestinationFromSituation:
    """Tests for _build_sequence_destination_from_situation helper."""

    def test_uses_situation_macro(self) -> None:
        situation_macro = (
            "{outputs}/{node_name?:_}{file_name_base}_v{_index:03}/{file_name_base}_{entry:04}.{file_extension}"
        )
        sit = _make_situation(situation_macro)
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")
        mock_dest = mock.MagicMock(spec=file_sequence.FileSequenceDestination)

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(BUILD_VERSIONED_PATH, return_value=mock_dest) as mock_build,
        ):
            project_file_sequence_parameter._build_sequence_destination_from_situation(
                "frame.exr", "save_file_sequence"
            )

        call_args = mock_build.call_args
        macro_path = call_args.args[0]
        assert macro_path.parsed_macro.template == situation_macro

    def test_falls_back_to_default_macro_when_situation_not_found(self) -> None:
        failure = project_events.GetSituationResultFailure(result_details="not found")
        mock_dest = mock.MagicMock(spec=file_sequence.FileSequenceDestination)

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=failure),
            mock.patch(BUILD_VERSIONED_PATH, return_value=mock_dest) as mock_build,
        ):
            project_file_sequence_parameter._build_sequence_destination_from_situation("frame.exr", "missing_situation")

        call_args = mock_build.call_args
        macro_path = call_args.args[0]
        assert macro_path.parsed_macro.template == project_file_sequence_parameter._FALLBACK_SEQUENCE_MACRO

    def test_plain_filename_parsed_into_stem_and_extension(self) -> None:
        sit = _make_situation("{outputs}/{file_name_base}_{entry:04}.{file_extension}")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")
        mock_dest = mock.MagicMock(spec=file_sequence.FileSequenceDestination)

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(BUILD_VERSIONED_PATH, return_value=mock_dest) as mock_build,
        ):
            project_file_sequence_parameter._build_sequence_destination_from_situation(
                "frame.exr", "save_file_sequence"
            )

        macro_path = mock_build.call_args.args[0]
        assert macro_path.variables["file_name_base"] == "frame"
        assert macro_path.variables["file_extension"] == "exr"

    def test_hash_pattern_filename_converted_before_parsing(self) -> None:
        sit = _make_situation("{outputs}/{file_name_base}_{entry:04}.{file_extension}")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")
        mock_dest = mock.MagicMock(spec=file_sequence.FileSequenceDestination)

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(BUILD_VERSIONED_PATH, return_value=mock_dest) as mock_build,
        ):
            project_file_sequence_parameter._build_sequence_destination_from_situation(
                "frame_####.exr", "save_file_sequence"
            )

        macro_path = mock_build.call_args.args[0]
        assert macro_path.variables["file_extension"] == "exr"

    def test_extra_vars_forwarded_to_macro(self) -> None:
        sit = _make_situation("{outputs}/{node_name}/{file_name_base}_{entry:04}.{file_extension}")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")
        mock_dest = mock.MagicMock(spec=file_sequence.FileSequenceDestination)

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(BUILD_VERSIONED_PATH, return_value=mock_dest) as mock_build,
        ):
            project_file_sequence_parameter._build_sequence_destination_from_situation(
                "frame.exr", "save_file_sequence", node_name="MyNode"
            )

        macro_path = mock_build.call_args.args[0]
        assert macro_path.variables["node_name"] == "MyNode"

    def test_situation_overwrite_policy_forwarded(self) -> None:
        sit = _make_situation("{outputs}/{entry:04}.exr", on_collision="OVERWRITE")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")
        mock_dest = mock.MagicMock(spec=file_sequence.FileSequenceDestination)

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(BUILD_VERSIONED_PATH, return_value=mock_dest) as mock_build,
        ):
            project_file_sequence_parameter._build_sequence_destination_from_situation(
                "frame.exr", "save_file_sequence"
            )

        call_kwargs = mock_build.call_args.kwargs
        assert call_kwargs["existing_file_policy"] == os_events.ExistingFilePolicy.OVERWRITE

    def test_situation_create_dirs_forwarded(self) -> None:
        sit = _make_situation("{outputs}/{entry:04}.exr", create_dirs=False)
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")
        mock_dest = mock.MagicMock(spec=file_sequence.FileSequenceDestination)

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(BUILD_VERSIONED_PATH, return_value=mock_dest) as mock_build,
        ):
            project_file_sequence_parameter._build_sequence_destination_from_situation(
                "frame.exr", "save_file_sequence"
            )

        call_kwargs = mock_build.call_args.kwargs
        assert call_kwargs["create_parents"] is False

    def test_fallback_uses_overwrite_policy(self) -> None:
        failure = project_events.GetSituationResultFailure(result_details="not found")
        mock_dest = mock.MagicMock(spec=file_sequence.FileSequenceDestination)

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=failure),
            mock.patch(BUILD_VERSIONED_PATH, return_value=mock_dest) as mock_build,
        ):
            project_file_sequence_parameter._build_sequence_destination_from_situation("frame.exr", "missing_situation")

        call_kwargs = mock_build.call_args.kwargs
        assert call_kwargs["existing_file_policy"] == os_events.ExistingFilePolicy.OVERWRITE

    def test_returns_file_sequence_destination(self) -> None:
        sit = _make_situation("{outputs}/{entry:04}.exr")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")
        mock_dest = mock.MagicMock(spec=file_sequence.FileSequenceDestination)

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(BUILD_VERSIONED_PATH, return_value=mock_dest),
        ):
            result = project_file_sequence_parameter._build_sequence_destination_from_situation(
                "frame.exr", "save_file_sequence"
            )

        assert result is mock_dest

    def test_multiple_extra_vars_all_forwarded(self) -> None:
        sit = _make_situation("{outputs}/{file_name_base}_{entry:04}.{file_extension}")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")
        mock_dest = mock.MagicMock(spec=file_sequence.FileSequenceDestination)

        with (
            mock.patch(HANDLE_REQUEST_PATH, return_value=success),
            mock.patch(BUILD_VERSIONED_PATH, return_value=mock_dest) as mock_build,
        ):
            project_file_sequence_parameter._build_sequence_destination_from_situation(
                "render.exr",
                "save_file_sequence",
                node_name="Renderer",
                sub_dirs="pass_1",
            )

        macro_path = mock_build.call_args.args[0]
        assert macro_path.variables["node_name"] == "Renderer"
        assert macro_path.variables["sub_dirs"] == "pass_1"
        assert macro_path.variables["file_name_base"] == "render"
        assert macro_path.variables["file_extension"] == "exr"
