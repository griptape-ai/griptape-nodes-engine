"""Unit tests for ProjectFileDestination."""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from griptape_nodes.common.project_templates.situation import SituationTemplate
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.retained_mode.file_metadata.provenance_record import ProvenanceContent

HANDLE_REQUEST_PATH = "griptape_nodes.files.project_file.GriptapeNodes.handle_request"


class TestProjectFileDestinationInit:
    """Tests for ProjectFileDestination.__init__() metadata construction."""

    def test_file_metadata_set_when_situation_found(self) -> None:
        """ProjectFileDestination builds SidecarContent when the situation is resolved."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import GetSituationResultSuccess

        situation = SituationTemplate(
            name="save_node_output",
            macro="{outputs}/{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        with patch(
            HANDLE_REQUEST_PATH, return_value=GetSituationResultSuccess(situation=situation, result_details="ok")
        ):
            dest = ProjectFileDestination.from_situation("image.png", "save_node_output")

        assert dest._file._provenance is not None
        assert isinstance(dest._file._provenance, ProvenanceContent)
        assert dest._file._provenance.situation is not None
        assert dest._file._provenance.situation.name == "save_node_output"
        assert dest._file._provenance.situation.macro == "{outputs}/{file_name_base}.{file_extension}"

    def test_file_metadata_contains_variables(self) -> None:
        """SidecarContent variables include filename parts and extra_vars."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import GetSituationResultSuccess

        situation = SituationTemplate(
            name="save_node_output",
            macro="{outputs}/{node_name}/{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        with patch(
            HANDLE_REQUEST_PATH, return_value=GetSituationResultSuccess(situation=situation, result_details="ok")
        ):
            dest = ProjectFileDestination.from_situation("render.png", "save_node_output", node_name="MyNode")

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is not None
        assert dest._file._provenance.situation.variables is not None
        variables = dest._file._provenance.situation.variables
        assert variables["file_name_base"] == "render"
        assert variables["file_extension"] == "png"
        assert variables["node_name"] == "MyNode"

    def test_provenance_has_no_situation_when_situation_not_found(self) -> None:
        """The election still exists on the fallback path, but with no situation block."""
        from griptape_nodes.retained_mode.events.project_events import GetSituationResultFailure

        with patch(HANDLE_REQUEST_PATH, return_value=GetSituationResultFailure(result_details="not found")):
            dest = ProjectFileDestination.from_situation("image.png", "missing_situation")

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is None

    def _make_extension_directory_handle_request(
        self,
        situation: SituationTemplate,
        file_extension_directories: dict[str, str],
        macro_resolver: Callable[[object], object] | None = None,
        call_log: list[object] | None = None,
    ) -> Callable[[object], object]:
        """Build a handle_request side_effect that answers situation + current-project lookups.

        The file_extension_directory lookup in from_situation calls
        GetCurrentProjectRequest to read the project's routing table, so the
        mock has to dispatch by request type instead of the blanket return_value
        the older tests use.

        When an extension macro value contains macro syntax, from_situation
        also issues a GetPathForMacroRequest. Pass ``macro_resolver`` to handle
        those; omitting it asserts no such request is expected (plain-name
        values, explicit overrides). ``call_log``, if provided, receives every
        dispatched request so tests can assert on request ordering / absence.
        """
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.common.project_templates.project import ProjectTemplate
        from griptape_nodes.common.project_templates.validation import (
            ProjectValidationInfo,
            ProjectValidationStatus,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            GetCurrentProjectRequest,
            GetCurrentProjectResultSuccess,
            GetPathForMacroRequest,
            GetSituationRequest,
            GetSituationResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

        template = ProjectTemplate(
            project_template_schema_version=DEFAULT_PROJECT_TEMPLATE.project_template_schema_version,
            name="Test",
            situations={situation.name: situation},
            directories={},
            environment={},
            file_extension_directories=file_extension_directories,
        )
        project_info = ProjectInfo(
            project_id="test",
            project_file_path=None,
            project_base_dir=Path("/tmp/test"),  # noqa: S108
            template=template,
            validation=ProjectValidationInfo(status=ProjectValidationStatus.GOOD),
            parsed_situation_schemas={},
            parsed_directory_schemas={},
        )

        def dispatch(request: object) -> object:
            if call_log is not None:
                call_log.append(request)
            if isinstance(request, GetSituationRequest):
                return GetSituationResultSuccess(situation=situation, result_details="ok")
            if isinstance(request, GetCurrentProjectRequest):
                return GetCurrentProjectResultSuccess(project_info=project_info, result_details="ok")
            if isinstance(request, GetPathForMacroRequest):
                if macro_resolver is None:
                    msg = "Unexpected GetPathForMacroRequest - test did not supply a macro_resolver"
                    raise AssertionError(msg)
                return macro_resolver(request)
            msg = f"Unexpected request type: {type(request).__name__}"
            raise AssertionError(msg)

        return dispatch

    def test_from_situation_sidecar_omits_derived_variables(self) -> None:
        """Sidecar stores only caller-supplied inputs; derived values like file_extension_directory are not persisted."""
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )

        situation = SituationTemplate(
            name="save_node_output",
            macro="{outputs}/{file_extension_directory?:/}{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        dispatch = self._make_extension_directory_handle_request(
            situation, DEFAULT_PROJECT_TEMPLATE.file_extension_directories
        )

        with patch(HANDLE_REQUEST_PATH, side_effect=dispatch):
            dest = ProjectFileDestination.from_situation("foo.png", "save_node_output")

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is not None
        variables = dest._file._provenance.situation.variables
        assert variables is not None
        assert variables["file_name_base"] == "foo"
        assert variables["file_extension"] == "png"
        assert "file_extension_directory" not in variables

    def test_from_situation_file_extension_directory_unmapped_extension(self) -> None:
        """An extension with no mapping leaves file_extension_directory unset so the optional slot degrades."""
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )

        situation = SituationTemplate(
            name="save_node_output",
            macro="{outputs}/{file_extension_directory?:/}{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        dispatch = self._make_extension_directory_handle_request(
            situation, DEFAULT_PROJECT_TEMPLATE.file_extension_directories
        )

        with patch(HANDLE_REQUEST_PATH, side_effect=dispatch):
            dest = ProjectFileDestination.from_situation("foo.xyz", "save_node_output")

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is not None
        variables = dest._file._provenance.situation.variables
        assert variables is not None
        assert "file_extension_directory" not in variables

    def test_from_situation_explicit_file_extension_directory_wins_over_extension_derived(self) -> None:
        """An explicit file_extension_directory kwarg is not clobbered by the mapping-derived default."""
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )

        situation = SituationTemplate(
            name="save_node_output",
            macro="{outputs}/{file_extension_directory?:/}{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        dispatch = self._make_extension_directory_handle_request(
            situation, DEFAULT_PROJECT_TEMPLATE.file_extension_directories
        )

        with patch(HANDLE_REQUEST_PATH, side_effect=dispatch):
            dest = ProjectFileDestination.from_situation(
                "foo.png", "save_node_output", file_extension_directory="custom"
            )

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is not None
        variables = dest._file._provenance.situation.variables
        assert variables is not None
        assert variables["file_extension_directory"] == "custom"

    def test_from_situation_file_extension_directory_case_insensitive(self) -> None:
        """Uppercase extensions still map to the same destination as their lowercase siblings."""
        from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )

        situation = SituationTemplate(
            name="save_node_output",
            macro="{outputs}/{file_extension_directory?:/}{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        dispatch = self._make_extension_directory_handle_request(
            situation, DEFAULT_PROJECT_TEMPLATE.file_extension_directories
        )

        with patch(HANDLE_REQUEST_PATH, side_effect=dispatch):
            dest = ProjectFileDestination.from_situation("FOO.PNG", "save_node_output")

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is not None
        variables = dest._file._provenance.situation.variables
        assert variables is not None
        # Derived values (file_extension_directory) no longer stored in sidecar;
        # the case-insensitive lookup is exercised at resolve time instead.
        assert "file_extension_directory" not in variables

    def test_from_situation_file_extension_directory_uses_project_taxonomy(self) -> None:
        """The mapping comes from the current project's template, not an engine constant."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )

        situation = SituationTemplate(
            name="save_node_output",
            macro="{outputs}/{file_extension_directory?:/}{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        # Project supplies its own taxonomy, including a bespoke extension.
        dispatch = self._make_extension_directory_handle_request(situation, {"png": "renders", "psd": "renders"})

        with patch(HANDLE_REQUEST_PATH, side_effect=dispatch):
            png_dest = ProjectFileDestination.from_situation("foo.png", "save_node_output")
            psd_dest = ProjectFileDestination.from_situation("bar.psd", "save_node_output")

        # Derived values no longer persisted in sidecar -- re-computed at resolve time.
        assert png_dest._file._provenance is not None
        assert png_dest._file._provenance.situation is not None
        assert png_dest._file._provenance.situation.variables is not None
        assert "file_extension_directory" not in png_dest._file._provenance.situation.variables
        assert psd_dest._file._provenance is not None
        assert psd_dest._file._provenance.situation is not None
        assert psd_dest._file._provenance.situation.variables is not None
        assert "file_extension_directory" not in psd_dest._file._provenance.situation.variables

    def test_from_situation_file_extension_directory_resolution_failure_falls_through(self) -> None:
        """Resolution failures leave file_extension_directory unset so the optional slot degrades."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            GetPathForMacroResultFailure,
            PathResolutionFailureReason,
        )

        situation = SituationTemplate(
            name="save_node_output",
            macro="{outputs}/{file_extension_directory?:/}{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        def macro_resolver(_req: object) -> object:
            return GetPathForMacroResultFailure(
                failure_reason=PathResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                missing_variables={"does_not_exist"},
                result_details="missing variable",
            )

        dispatch = self._make_extension_directory_handle_request(
            situation,
            {"mp4": "{does_not_exist}/videos"},
            macro_resolver=macro_resolver,
        )

        with patch(HANDLE_REQUEST_PATH, side_effect=dispatch):
            dest = ProjectFileDestination.from_situation("clip.mp4", "save_node_output")

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is not None
        assert dest._file._provenance.situation.variables is not None
        assert "file_extension_directory" not in dest._file._provenance.situation.variables

    def test_from_situation_explicit_file_extension_directory_skips_resolution(self) -> None:
        """Explicit caller override wins and never consults the project taxonomy or resolver."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            GetCurrentProjectRequest,
            GetPathForMacroRequest,
        )

        situation = SituationTemplate(
            name="save_node_output",
            macro="{outputs}/{file_extension_directory?:/}{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        call_log: list[object] = []
        # Even though the taxonomy would need a resolver, we don't supply one --
        # the explicit kwarg must short-circuit before any lookup.
        dispatch = self._make_extension_directory_handle_request(
            situation,
            {"mp4": "{outputs}/videos"},
            call_log=call_log,
        )

        with patch(HANDLE_REQUEST_PATH, side_effect=dispatch):
            dest = ProjectFileDestination.from_situation(
                "clip.mp4", "save_node_output", file_extension_directory="caller_wins"
            )

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is not None
        assert dest._file._provenance.situation.variables is not None
        assert dest._file._provenance.situation.variables["file_extension_directory"] == "caller_wins"
        # Neither the project lookup nor the macro resolver should fire.
        assert not any(isinstance(req, GetCurrentProjectRequest) for req in call_log)
        assert not any(isinstance(req, GetPathForMacroRequest) for req in call_log)

    def test_from_situation_derives_sub_dirs_from_filename_directory(self) -> None:
        """A path-prefixed filename populates sub_dirs in the resolution variables."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import GetSituationResultSuccess

        situation = SituationTemplate(
            name="save_workflow",
            macro="{workspace_dir}/{sub_dirs?:/}{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        with patch(
            HANDLE_REQUEST_PATH, return_value=GetSituationResultSuccess(situation=situation, result_details="ok")
        ):
            dest = ProjectFileDestination.from_situation("renders/foo.png", "save_workflow")

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is not None
        variables = dest._file._provenance.situation.variables
        assert variables is not None
        assert variables["sub_dirs"] == "renders"
        assert variables["file_name_base"] == "foo"
        assert variables["file_extension"] == "png"

    def test_from_situation_derives_nested_sub_dirs_from_filename(self) -> None:
        """A filename with nested directory components populates sub_dirs with the full relative path."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import GetSituationResultSuccess

        situation = SituationTemplate(
            name="save_workflow",
            macro="{workspace_dir}/{sub_dirs?:/}{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        with patch(
            HANDLE_REQUEST_PATH, return_value=GetSituationResultSuccess(situation=situation, result_details="ok")
        ):
            dest = ProjectFileDestination.from_situation("act_1/scene_3/intro.py", "save_workflow")

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is not None
        variables = dest._file._provenance.situation.variables
        assert variables is not None
        assert variables["sub_dirs"] == str(Path("act_1/scene_3"))
        assert variables["file_name_base"] == "intro"
        assert variables["file_extension"] == "py"

    def test_from_situation_no_sub_dirs_when_filename_has_no_directory(self) -> None:
        """A bare filename leaves sub_dirs unpopulated so the builtin can fill in."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import GetSituationResultSuccess

        situation = SituationTemplate(
            name="save_node_output",
            macro="{outputs}/{sub_dirs?:/}{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        with patch(
            HANDLE_REQUEST_PATH, return_value=GetSituationResultSuccess(situation=situation, result_details="ok")
        ):
            dest = ProjectFileDestination.from_situation("image.png", "save_node_output")

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is not None
        variables = dest._file._provenance.situation.variables
        assert variables is not None
        assert "sub_dirs" not in variables

    def test_from_situation_explicit_sub_dirs_wins_over_filename_directory(self) -> None:
        """An explicit sub_dirs kwarg is not clobbered by a filename-derived value."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import GetSituationResultSuccess

        situation = SituationTemplate(
            name="save_workflow",
            macro="{workspace_dir}/{sub_dirs?:/}{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        with patch(
            HANDLE_REQUEST_PATH, return_value=GetSituationResultSuccess(situation=situation, result_details="ok")
        ):
            dest = ProjectFileDestination.from_situation(
                "renders/foo.png", "save_workflow", sub_dirs="explicit_override"
            )

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is not None
        variables = dest._file._provenance.situation.variables
        assert variables is not None
        assert variables["sub_dirs"] == "explicit_override"

    def test_from_situation_absolute_filename_bypasses_macro(self, tmp_path: Path) -> None:
        """An absolute filename is honored verbatim rather than routed through the situation macro."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import GetSituationResultSuccess

        situation = SituationTemplate(
            name="save_node_output",
            macro="{outputs}/{sub_dirs?:/}{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        absolute_filename = str(tmp_path / "foo" / "bar" / "output.png")

        with patch(
            HANDLE_REQUEST_PATH, return_value=GetSituationResultSuccess(situation=situation, result_details="ok")
        ):
            dest = ProjectFileDestination.from_situation(absolute_filename, "save_node_output")

        # The resolved path should be the absolute path as-is, not routed under {outputs}.
        assert dest._file.location == absolute_filename
        # Provenance still captures (the record's path/hash/node are the on-disk
        # truth), but its situation block is dropped: the situation macro+variables
        # don't re-resolve to the absolute path we honored verbatim, so recording
        # them would be a lie.
        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is None

    def test_file_metadata_policy_matches_situation(self) -> None:
        """SidecarContent.situation.policy mirrors the situation's policy."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import GetSituationResultSuccess

        situation = SituationTemplate(
            name="save_node_output",
            macro="{outputs}/{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.CREATE_NEW, create_dirs=False),
        )

        with patch(
            HANDLE_REQUEST_PATH, return_value=GetSituationResultSuccess(situation=situation, result_details="ok")
        ):
            dest = ProjectFileDestination.from_situation("data.json", "save_node_output")

        assert dest._file._provenance is not None
        assert dest._file._provenance.situation is not None
        assert dest._file._provenance.situation.policy is not None
        policy = dest._file._provenance.situation.policy
        assert policy.on_collision == SituationFilePolicy.CREATE_NEW
        assert policy.create_dirs is False
