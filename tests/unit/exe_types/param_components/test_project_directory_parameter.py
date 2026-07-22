"""Unit tests for _build_directory_destination_from_situation."""

from unittest import mock

from griptape_nodes.common.project_templates import situation
from griptape_nodes.exe_types.param_components import project_directory_parameter
from griptape_nodes.files import directory as directory_mod
from griptape_nodes.retained_mode.events import os_events, project_events

HANDLE_REQUEST_PATH = "griptape_nodes.files.project_file.GriptapeNodes.handle_request"

_POLICY_MAP = {
    "CREATE_NEW": situation.SituationFilePolicy.CREATE_NEW,
    "OVERWRITE": situation.SituationFilePolicy.OVERWRITE,
    "FAIL": situation.SituationFilePolicy.FAIL,
}


def _make_situation(
    macro: str,
    on_collision: str = "CREATE_NEW",
    *,
    create_dirs: bool = True,
) -> situation.SituationTemplate:
    return situation.SituationTemplate(
        name="test_situation",
        macro=macro,
        policy=situation.SituationPolicy(on_collision=_POLICY_MAP[on_collision], create_dirs=create_dirs),
    )


class TestBuildDirectoryDestinationFromSituation:
    """Tests for _build_directory_destination_from_situation helper."""

    def test_uses_situation_macro(self) -> None:
        sit = _make_situation("{outputs}/{node_name}/{dir_name}_v{_index:03}")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")

        with mock.patch(HANDLE_REQUEST_PATH, return_value=success):
            dest = project_directory_parameter._build_directory_destination_from_situation(
                "renders", "save_output_directory"
            )

        assert isinstance(dest, directory_mod.DirectoryDestination)
        assert isinstance(dest._dir_path, project_events.MacroPath)
        assert dest._dir_path.parsed_macro.template == "{outputs}/{node_name}/{dir_name}_v{_index:03}"

    def test_falls_back_to_default_macro_when_situation_not_found(self) -> None:
        failure = project_events.GetSituationResultFailure(result_details="not found")

        with mock.patch(HANDLE_REQUEST_PATH, return_value=failure):
            dest = project_directory_parameter._build_directory_destination_from_situation(
                "renders", "missing_situation"
            )

        assert isinstance(dest._dir_path, project_events.MacroPath)
        assert dest._dir_path.parsed_macro.template == project_directory_parameter._FALLBACK_DIRECTORY_MACRO

    def test_wires_dirname_as_macro_variable(self) -> None:
        sit = _make_situation("{outputs}/{dir_name}_v{_index:03}")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")

        with mock.patch(HANDLE_REQUEST_PATH, return_value=success):
            dest = project_directory_parameter._build_directory_destination_from_situation(
                "frames", "save_output_directory"
            )

        assert isinstance(dest._dir_path, project_events.MacroPath)
        assert dest._dir_path.variables["dir_name"] == "frames"

    def test_extra_vars_forwarded_to_macro(self) -> None:
        sit = _make_situation("{outputs}/{node_name}/{dir_name}_v{_index:03}")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")

        with mock.patch(HANDLE_REQUEST_PATH, return_value=success):
            dest = project_directory_parameter._build_directory_destination_from_situation(
                "renders", "save_output_directory", node_name="MyNode"
            )

        assert isinstance(dest._dir_path, project_events.MacroPath)
        assert dest._dir_path.variables["node_name"] == "MyNode"

    def test_situation_overwrite_policy_maps_to_overwrite(self) -> None:
        sit = _make_situation("{outputs}/{dir_name}", on_collision="OVERWRITE")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")

        with mock.patch(HANDLE_REQUEST_PATH, return_value=success):
            dest = project_directory_parameter._build_directory_destination_from_situation(
                "renders", "save_output_directory"
            )

        assert dest._existing_dir_policy == os_events.ExistingFilePolicy.OVERWRITE

    def test_situation_create_new_policy_maps_to_create_new(self) -> None:
        sit = _make_situation("{outputs}/{dir_name}", on_collision="CREATE_NEW")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")

        with mock.patch(HANDLE_REQUEST_PATH, return_value=success):
            dest = project_directory_parameter._build_directory_destination_from_situation(
                "renders", "save_output_directory"
            )

        assert dest._existing_dir_policy == os_events.ExistingFilePolicy.CREATE_NEW

    def test_situation_fail_policy_maps_to_fail(self) -> None:
        sit = _make_situation("{outputs}/{dir_name}", on_collision="FAIL")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")

        with mock.patch(HANDLE_REQUEST_PATH, return_value=success):
            dest = project_directory_parameter._build_directory_destination_from_situation(
                "renders", "save_output_directory"
            )

        assert dest._existing_dir_policy == os_events.ExistingFilePolicy.FAIL

    def test_situation_create_dirs_false_propagated(self) -> None:
        sit = _make_situation("{outputs}/{dir_name}", create_dirs=False)
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")

        with mock.patch(HANDLE_REQUEST_PATH, return_value=success):
            dest = project_directory_parameter._build_directory_destination_from_situation(
                "renders", "save_output_directory"
            )

        assert dest._create_parents is False

    def test_fallback_uses_create_new_policy(self) -> None:
        failure = project_events.GetSituationResultFailure(result_details="not found")

        with mock.patch(HANDLE_REQUEST_PATH, return_value=failure):
            dest = project_directory_parameter._build_directory_destination_from_situation(
                "renders", "missing_situation"
            )

        assert dest._existing_dir_policy == os_events.ExistingFilePolicy.CREATE_NEW

    def test_multiple_extra_vars_all_forwarded(self) -> None:
        sit = _make_situation("{outputs}/{dir_name}")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")

        with mock.patch(HANDLE_REQUEST_PATH, return_value=success):
            dest = project_directory_parameter._build_directory_destination_from_situation(
                "renders",
                "save_output_directory",
                node_name="MyNode",
                sub_dirs="pass_1",
            )

        assert isinstance(dest._dir_path, project_events.MacroPath)
        assert dest._dir_path.variables["node_name"] == "MyNode"
        assert dest._dir_path.variables["sub_dirs"] == "pass_1"
        assert dest._dir_path.variables["dir_name"] == "renders"

    def test_returns_directory_destination(self) -> None:
        sit = _make_situation("{outputs}/{dir_name}")
        success = project_events.GetSituationResultSuccess(situation=sit, result_details="ok")

        with mock.patch(HANDLE_REQUEST_PATH, return_value=success):
            dest = project_directory_parameter._build_directory_destination_from_situation(
                "renders", "save_output_directory"
            )

        assert isinstance(dest, directory_mod.DirectoryDestination)
