"""Tests for provenance capture through the write pipeline.

Covers: opt-in gating, record layout (by-path + by-hash), immutable history on
overwrite, collision-walk keying, failure-policy behavior (fail vs warn),
legacy-template inactivity, and parent linking.
"""

import base64
import io
import logging
import pickle
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.common.project_templates.default_project_template import (
    DEFAULT_PROJECT_TEMPLATE,
    DEFAULT_PROJECT_TEMPLATE_V0,
)
from griptape_nodes.common.project_templates.provenance_settings import (
    ProvenanceCapturePolicy,
    ProvenanceFailurePolicy,
    ProvenanceSettings,
)
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
)
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.artifact_events import RegisterArtifactProviderRequest
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.context_events import EnsureWorkflowAndFlowRequest
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    FileIOFailureReason,
    WriteFileRequest,
    WriteFileResultFailure,
    WriteFileResultSuccess,
)
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    MacroPath,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.provenance_events import (
    GetProvenanceForArtifactRequest,
    GetProvenanceForArtifactResultFailure,
    GetProvenanceForArtifactResultSuccess,
    GetProvenancePayloadRequest,
    GetProvenancePayloadResultSuccess,
    ListProvenancedArtifactsRequest,
    ListProvenancedArtifactsResultSuccess,
    ListProvenanceRecordsForArtifactRequest,
    ListProvenanceRecordsForArtifactResultSuccess,
    ListProvenanceRecordsForHashRequest,
    ListProvenanceRecordsForHashResultSuccess,
    ProvenanceMatchOrigin,
    ProvenanceQueryFailureReason,
)
from griptape_nodes.retained_mode.file_metadata.provenance_record import (
    ProducingNodeIdentity,
    ProvenanceContent,
    ProvenanceRecord,
    hash_content,
    load_record_yaml,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)
from griptape_nodes.retained_mode.managers.os_manager import FileWriteAttemptResult
from griptape_nodes.retained_mode.managers.provenance_manager import ProvenanceCaptureResult

PROVENANCE_STORE_DIR = "griptape-nodes-provenance"


def _sample_provenance() -> ProvenanceContent:
    return ProvenanceContent(
        capture_policy=ProvenanceCapturePolicy.PRODUCING_NODE_ONLY,
        failure_policy=ProvenanceFailurePolicy.FAIL_ARTIFACT_SAVE,
        producing_node=ProducingNodeIdentity(name="SaveImage_1", node_type="SaveImage"),
    )


def _tiny_png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color="red").save(buffer, format="PNG")
    return buffer.getvalue()


def _record_dir_for(workspace: Path, file_name: str) -> Path:
    return workspace / PROVENANCE_STORE_DIR / "by-path" / file_name


def _load_single_record(record_dir: Path) -> ProvenanceRecord:
    record_files = sorted(record_dir.glob("*.yaml"))
    assert len(record_files) == 1
    return load_record_yaml(record_files[0].read_text(encoding="utf-8"))


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary workspace directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir).resolve()


@pytest.fixture(autouse=True)
def project_workspace(temp_dir: Path, engine: Engine) -> Generator[None, None, None]:
    """Point the workspace at temp_dir and load the default (v1) project template."""
    config_manager = engine.config_manager
    original_workspace = config_manager.workspace_path
    config_manager.set_config_value("workspace_directory", str(temp_dir))

    # Plain default template: pipeline tests elect capture EXPLICITLY per save,
    # and explicit elections win over the per-type table, so .txt writes can
    # exercise capture mechanics without widening any project policy.
    project_yml = temp_dir / "project_template.yml"
    project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
    load_result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
    assert isinstance(load_result, LoadProjectTemplateResultSuccess)
    engine.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

    yield

    engine.handle_request(SetCurrentProjectRequest(project_id=None))
    config_manager.set_config_value("workspace_directory", str(original_workspace))


class TestCaptureGating:
    def test_no_provenance_field_writes_no_store(self, engine: Engine, temp_dir: Path) -> None:
        result = engine.handle_request(WriteFileRequest(file_path=str(temp_dir / "plain.txt"), content="hello"))
        assert isinstance(result, WriteFileResultSuccess)
        assert result.provenance is None
        assert not (temp_dir / PROVENANCE_STORE_DIR).exists()

    def test_no_provenance_recorded_policy_writes_no_store(self, engine: Engine, temp_dir: Path) -> None:
        provenance = _sample_provenance()
        provenance.capture_policy = ProvenanceCapturePolicy.NO_PROVENANCE_RECORDED
        result = engine.handle_request(
            WriteFileRequest(file_path=str(temp_dir / "plain.txt"), content="hello", provenance=provenance)
        )
        assert isinstance(result, WriteFileResultSuccess)
        assert result.provenance is None
        assert not (temp_dir / PROVENANCE_STORE_DIR).exists()

    def test_legacy_v0_template_is_inactive_and_never_fails_saves(self, engine: Engine, temp_dir: Path) -> None:
        # A v0 project has no provenance situation: capture must resolve to
        # nothing recorded, and the fail_artifact_save default must NOT fire.
        project_yml = temp_dir / "legacy_project.yml"
        project_yml.write_text(DEFAULT_PROJECT_TEMPLATE_V0.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE_V0))
        load_result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        engine.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

        result = engine.handle_request(
            WriteFileRequest(file_path=str(temp_dir / "legacy.txt"), content="hello", provenance=_sample_provenance())
        )
        assert isinstance(result, WriteFileResultSuccess)
        assert result.provenance is None
        assert not (temp_dir / PROVENANCE_STORE_DIR).exists()


class TestCaptureResolution:
    def _inherit_provenance(self) -> ProvenanceContent:
        return ProvenanceContent(producing_node=ProducingNodeIdentity(name="SaveImage_1", node_type="SaveImage"))

    def test_unrecognized_formats_record_nothing_by_default(self, engine: Engine, temp_dir: Path) -> None:
        # INHERIT on a .txt (no provider claims it): the shipped default skips it.
        result = engine.handle_request(
            WriteFileRequest(
                file_path=str(temp_dir / "notes.txt"), content="words", provenance=self._inherit_provenance()
            )
        )
        assert isinstance(result, WriteFileResultSuccess)
        assert result.provenance is None
        assert not _record_dir_for(temp_dir, "notes.txt").exists()

    def test_recognized_formats_record_by_default(self, engine: Engine, temp_dir: Path) -> None:
        engine.handle_request(RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider))
        result = engine.handle_request(
            WriteFileRequest(
                file_path=str(temp_dir / "hero.png"), content=_tiny_png_bytes(), provenance=self._inherit_provenance()
            )
        )
        assert isinstance(result, WriteFileResultSuccess)
        assert result.provenance is not None
        assert _record_dir_for(temp_dir, "hero.png").is_dir()

    def test_explicit_election_beats_the_table(self, engine: Engine, temp_dir: Path) -> None:
        # A deliberate per-save election records even for an unrecognized format.
        result = engine.handle_request(
            WriteFileRequest(file_path=str(temp_dir / "notes.txt"), content="words", provenance=_sample_provenance())
        )
        assert isinstance(result, WriteFileResultSuccess)
        assert result.provenance is not None

    def test_per_artifact_type_entry_overrides_project_default(self, engine: Engine, temp_dir: Path) -> None:
        engine.handle_request(RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider))
        quiet_images_template = DEFAULT_PROJECT_TEMPLATE.model_copy(
            update={
                "provenance": ProvenanceSettings(
                    per_artifact_type={"image": ProvenanceCapturePolicy.NO_PROVENANCE_RECORDED}
                )
            }
        )
        project_yml = temp_dir / "quiet_images_project.yml"
        project_yml.write_text(quiet_images_template.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
        load_result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
        assert isinstance(load_result, LoadProjectTemplateResultSuccess)
        engine.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

        result = engine.handle_request(
            WriteFileRequest(
                file_path=str(temp_dir / "hero.png"), content=_tiny_png_bytes(), provenance=self._inherit_provenance()
            )
        )
        assert isinstance(result, WriteFileResultSuccess)
        assert result.provenance is None

    def test_system_temp_saves_never_capture(self, engine: Engine) -> None:
        # The engine's own scratch space: records there would be
        # guaranteed-dangling. Recognized format, elected save -- still skipped.
        engine.handle_request(RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider))
        with tempfile.TemporaryDirectory() as system_temp_subdir:
            staging_path = Path(system_temp_subdir) / "staged.png"
            result = engine.handle_request(
                WriteFileRequest(
                    file_path=str(staging_path), content=_tiny_png_bytes(), provenance=_sample_provenance()
                )
            )
            assert isinstance(result, WriteFileResultSuccess)
            assert result.provenance is None


class TestRecordContents:
    def test_record_written_with_expected_envelope(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        result = engine.handle_request(
            WriteFileRequest(file_path=str(file_path), content="pixels", provenance=_sample_provenance())
        )
        assert isinstance(result, WriteFileResultSuccess)
        assert result.provenance is not None
        assert result.provenance.capture_policy == ProvenanceCapturePolicy.PRODUCING_NODE_ONLY

        record = _load_single_record(_record_dir_for(temp_dir, "hero.txt"))
        assert record.record_id == result.provenance.record_id
        assert record.artifact.path_at_save == str(file_path)
        assert record.artifact.file_name == "hero.txt"
        assert record.artifact.content_hash == hash_content(b"pixels")
        assert record.artifact.size_bytes == len(b"pixels")
        assert record.producing_node is not None
        assert record.producing_node.name == "SaveImage_1"
        assert record.capture_policy == ProvenanceCapturePolicy.PRODUCING_NODE_ONLY
        assert record.summary_line is not None

    def test_by_hash_pointer_resolves_to_canonical_record(self, engine: Engine, temp_dir: Path) -> None:
        result = engine.handle_request(
            WriteFileRequest(file_path=str(temp_dir / "hero.txt"), content="pixels", provenance=_sample_provenance())
        )
        assert isinstance(result, WriteFileResultSuccess)
        assert result.provenance is not None

        content_hash_hex = result.provenance.content_hash.split(":", 1)[1]
        pointer_dir = temp_dir / PROVENANCE_STORE_DIR / "by-hash" / content_hash_hex[:2] / content_hash_hex
        pointer_files = list(pointer_dir.glob("*.yaml"))
        assert len(pointer_files) == 1
        assert pointer_files[0].stem == result.provenance.record_id

    def test_overwriting_same_path_accumulates_immutable_history(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        first = engine.handle_request(
            WriteFileRequest(file_path=str(file_path), content="take one", provenance=_sample_provenance())
        )
        second = engine.handle_request(
            WriteFileRequest(file_path=str(file_path), content="take two", provenance=_sample_provenance())
        )
        assert isinstance(first, WriteFileResultSuccess)
        assert isinstance(second, WriteFileResultSuccess)
        assert first.provenance is not None
        assert second.provenance is not None

        expected_record_count = 2
        record_files = sorted(_record_dir_for(temp_dir, "hero.txt").glob("*.yaml"))
        assert len(record_files) == expected_record_count
        # Lexicographic max is the latest save.
        assert record_files[-1].stem == second.provenance.record_id

        latest = engine.provenance_manager.find_latest_record_for_path(file_path)
        assert latest is not None
        assert latest.record_id == second.provenance.record_id
        assert latest.artifact.content_hash == hash_content(b"take two")

    def test_collision_walk_keys_record_on_final_name(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        file_path.write_text("already here")

        result = engine.handle_request(
            WriteFileRequest(
                file_path=str(file_path),
                content="walked",
                existing_file_policy=ExistingFilePolicy.CREATE_NEW,
                provenance=_sample_provenance(),
            )
        )
        assert isinstance(result, WriteFileResultSuccess)
        assert result.final_file_path.endswith("hero_1.txt")

        record = _load_single_record(_record_dir_for(temp_dir, "hero_1.txt"))
        assert record.artifact.file_name == "hero_1.txt"


class TestFailurePolicies:
    def test_fail_policy_rolls_back_exclusive_create(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        failure = ProvenanceCaptureResult(error_message="record store unavailable")
        with patch.object(type(engine.provenance_manager), "record_artifact_save", return_value=failure):
            result = engine.handle_request(
                WriteFileRequest(
                    file_path=str(file_path),
                    content="pixels",
                    existing_file_policy=ExistingFilePolicy.FAIL,
                    provenance=_sample_provenance(),
                )
            )
        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.PROVENANCE_WRITE_FAILED
        assert not file_path.exists()

    def test_fail_policy_on_overwrite_leaves_prior_file_untouched(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        file_path.write_text("prior content")

        failure = ProvenanceCaptureResult(error_message="record store unavailable")
        with patch.object(type(engine.provenance_manager), "record_artifact_save", return_value=failure):
            result = engine.handle_request(
                WriteFileRequest(file_path=str(file_path), content="new content", provenance=_sample_provenance())
            )
        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.PROVENANCE_WRITE_FAILED
        assert file_path.read_text() == "prior content"

    def test_warn_policy_keeps_the_save(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        provenance = _sample_provenance()
        provenance.failure_policy = ProvenanceFailurePolicy.WARN_AND_CONTINUE

        failure = ProvenanceCaptureResult(error_message="record store unavailable")
        with patch.object(type(engine.provenance_manager), "record_artifact_save", return_value=failure):
            result = engine.handle_request(
                WriteFileRequest(file_path=str(file_path), content="pixels", provenance=provenance)
            )
        assert isinstance(result, WriteFileResultSuccess)
        assert result.provenance is None
        assert file_path.read_text() == "pixels"

    def test_failed_overwrite_rolls_back_the_pre_captured_record(self, engine: Engine, temp_dir: Path) -> None:
        """Atomic-overwrite ordering: record pre-captured, replace fails, record rolled back.

        No record may describe a save that never happened, and the prior file
        must be untouched.
        """
        file_path = temp_dir / "hero.txt"
        file_path.write_text("prior content")

        disk_full = FileWriteAttemptResult(
            bytes_written=None,
            failure_reason=FileIOFailureReason.IO_ERROR,
            error_message=f"Attempted to write to file '{file_path}'. Failed due to: disk full.",
        )
        with patch.object(type(engine.os_manager), "_attempt_atomic_file_write", return_value=disk_full):
            result = engine.handle_request(
                WriteFileRequest(file_path=str(file_path), content="new content", provenance=_sample_provenance())
            )
        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.IO_ERROR
        assert file_path.read_text() == "prior content"
        # Both the pre-captured record AND its by-hash pointer are gone.
        store_root = temp_dir / PROVENANCE_STORE_DIR
        assert not list(store_root.rglob("*.yaml"))

    def test_append_preflight_failure_stops_the_save_before_any_bytes(self, engine: Engine, temp_dir: Path) -> None:
        """Appends cannot be rolled back, so an unwritable record location stops the append."""
        file_path = temp_dir / "log.txt"
        file_path.write_text("line one\n")
        # A file squatting where the store's by-path tree must go makes the
        # record directory uncreatable -- a real filesystem failure, not a mock.
        store_root = temp_dir / PROVENANCE_STORE_DIR
        store_root.mkdir()
        (store_root / "by-path").write_text("squatter")

        result = engine.handle_request(
            WriteFileRequest(
                file_path=str(file_path), content="line two\n", append=True, provenance=_sample_provenance()
            )
        )
        assert isinstance(result, WriteFileResultFailure)
        assert result.failure_reason == FileIOFailureReason.PROVENANCE_WRITE_FAILED
        assert file_path.read_text() == "line one\n"

    def test_append_record_failure_after_write_degrades_to_warning(self, engine: Engine, temp_dir: Path) -> None:
        """A record failure AFTER an append keeps the save: appends cannot be rolled back.

        Even under fail_artifact_save the write stands; the failure surfaces as
        a warning on the success result instead.
        """
        file_path = temp_dir / "log.txt"
        file_path.write_text("line one\n")

        record_error = f"Attempted to write a provenance record for '{file_path.name}'. Failed due to: disk full."
        with patch.object(type(engine.provenance_manager), "_write_record_files", return_value=record_error):
            result = engine.handle_request(
                WriteFileRequest(
                    file_path=str(file_path), content="line two\n", append=True, provenance=_sample_provenance()
                )
            )
        assert isinstance(result, WriteFileResultSuccess)
        assert result.provenance is None
        assert file_path.read_text() == "line one\nline two\n"
        assert isinstance(result.result_details, ResultDetails)
        detail = result.result_details.result_details[0]
        assert "Provenance record failed" in detail.message
        assert detail.level == logging.WARNING


class TestClassificationIsTypeDriven:
    """Classification is type-driven; value-shape sniffing never happens.

    The type system testifies two ways: the value's runtime type (UrlArtifact)
    or the holding parameter's DECLARED type (a string in an image-declared
    parameter is a file reference by declaration).

    Regression history: a ~4KB prompt was once probed as a filesystem path and
    stat() raised ENAMETOOLONG (errno 63), rolling back a real save. Strings in
    str-typed parameters are never probed; strings in artifact-declared
    parameters are probed with the exception-guarded check (ruling 2026-09-23).
    """

    @staticmethod
    def _str_param() -> Parameter:
        return Parameter(name="prompt", type="str", tooltip="t", allowed_modes={ParameterMode.PROPERTY})

    @staticmethod
    def _image_param() -> Parameter:
        return Parameter(
            name="input_images",
            type="ImageUrlArtifact",
            input_types=["ImageUrlArtifact", "ImageArtifact", "str"],
            tooltip="t",
            allowed_modes={ParameterMode.INPUT},
        )

    def test_multi_kb_prompt_in_str_param_is_never_classified(self, engine: Engine) -> None:
        prompt = "A left-handed warrior of tremendous strength, " * 100 + "painted CRPG art"
        assert engine.provenance_manager._classify_file_backed_value(prompt, self._str_param()) is None

    def test_real_path_string_in_str_param_is_never_classified(self, engine: Engine, temp_dir: Path) -> None:
        real_file = temp_dir / "hero.png"
        real_file.write_bytes(_tiny_png_bytes())
        assert engine.provenance_manager._classify_file_backed_value(str(real_file), self._str_param()) is None

    def test_path_string_in_artifact_declared_param_classifies(self, engine: Engine, temp_dir: Path) -> None:
        real_file = temp_dir / "reference.png"
        real_file.write_bytes(_tiny_png_bytes())
        resolved = engine.provenance_manager._classify_file_backed_value(str(real_file), self._image_param())
        assert resolved == real_file

    def test_junk_in_artifact_declared_param_degrades_safely(self, engine: Engine) -> None:
        # Even in a declared-artifact parameter, an over-long value must degrade
        # to "not a parent" via the guarded probe, never raise out.
        junk = "A left-handed warrior, " * 200 + "art.png"
        assert engine.provenance_manager._classify_file_backed_value(junk, self._image_param()) is None
        assert engine.provenance_manager._classify_file_backed_value("", self._image_param()) is None

    def test_url_artifact_classifies_to_its_backing_file(self, engine: Engine, temp_dir: Path) -> None:
        from griptape.artifacts import ImageUrlArtifact

        real_file = temp_dir / "hero.png"
        real_file.write_bytes(_tiny_png_bytes())
        artifact = ImageUrlArtifact("http://localhost:8124/workspace/hero.png")
        resolved = engine.provenance_manager._classify_file_backed_value(artifact, self._str_param())
        assert resolved == real_file


class TestSourceLinking:
    def test_input_with_record_links_and_matches_latest(self, engine: Engine, temp_dir: Path) -> None:
        input_path = temp_dir / "input.txt"
        input_result = engine.handle_request(
            WriteFileRequest(file_path=str(input_path), content="source", provenance=_sample_provenance())
        )
        assert isinstance(input_result, WriteFileResultSuccess)
        assert input_result.provenance is not None

        link = engine.provenance_manager._link_source(input_path, "image")
        assert link.record_id == input_result.provenance.record_id
        assert link.content_hash == hash_content(b"source")
        assert link.matches_latest_record is True
        assert link.summary is not None

    def test_input_without_record_is_a_chain_break(self, engine: Engine, temp_dir: Path) -> None:
        input_path = temp_dir / "foreign.txt"
        input_path.write_text("no record")

        link = engine.provenance_manager._link_source(input_path, "image")
        assert link.record_id is None
        assert link.content_hash == hash_content(b"no record")

    def test_renamed_input_recovers_via_by_hash(self, engine: Engine, temp_dir: Path) -> None:
        original_path = temp_dir / "original.txt"
        result = engine.handle_request(
            WriteFileRequest(file_path=str(original_path), content="movable", provenance=_sample_provenance())
        )
        assert isinstance(result, WriteFileResultSuccess)
        assert result.provenance is not None

        renamed_path = temp_dir / "renamed_by_hand.txt"
        original_path.rename(renamed_path)

        link = engine.provenance_manager._link_source(renamed_path, "image")
        assert link.record_id == result.provenance.record_id
        assert link.matches_latest_record is True


class _ProvProbeNode(DataNode):
    """Minimal node with a prompt and a declared-image input, for payload + lineage coverage."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="prompt",
                tooltip="Prompt",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="reference_image",
                tooltip="Reference",
                type="ImageUrlArtifact",
                input_types=["ImageUrlArtifact", "str"],
                default_value=None,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )

    def process(self) -> None:
        pass


class TestFaithfulPayload:
    """The payload must be the FULL serialize-node protocol output.

    Commands + indirect set-value commands + the pickled unique-values dict. A
    bare serialize call (the original bug) records commands whose value
    references point at a dict that was never captured.
    """

    _LIBRARY = "provenance-payload-test-library"

    @pytest.fixture
    def producing_node_name(self, engine: Engine) -> Generator[str, None, None]:
        """Register a tiny library, stand up a workflow/flow context, create one node."""
        LibraryRegistry._clear()
        schema = LibrarySchema(
            name=self._LIBRARY,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="t", description="d", library_version="0.1.0", engine_version="0.0.0", tags=[]
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(schema)
        library.register_new_node_type(
            _ProvProbeNode, NodeMetadata(category="t", description="d", display_name="Probe")
        )
        engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_name="prov_wf", flow_name="prov_flow"))
        create_result = engine.handle_request(
            CreateNodeRequest(node_type="_ProvProbeNode", specific_library_name=self._LIBRARY, node_name="Probe_1")
        )
        assert isinstance(create_result, CreateNodeResultSuccess)
        node_name = create_result.node_name
        node = engine.object_manager.attempt_get_object_by_name_as_type(node_name, _ProvProbeNode)
        assert node is not None
        node.set_parameter_value("prompt", "a left-handed warrior of tremendous strength")
        reference = engine.config_manager.workspace_path / "kb_reference.png"
        reference.write_bytes(_tiny_png_bytes())
        # A plain STRING path in a declared-image parameter: the field case
        # (reference libraries feed paths, not artifact objects).
        node.set_parameter_value("reference_image", str(reference))
        try:
            yield node_name
        finally:
            engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
            LibraryRegistry._clear()

    def test_full_capture_payload_is_rehydratable_and_embeds_workflow_file(
        self, engine: Engine, temp_dir: Path, producing_node_name: str
    ) -> None:
        provenance = ProvenanceContent(
            capture_policy=ProvenanceCapturePolicy.FULL_WORKFLOW_SNAPSHOT,
            failure_policy=ProvenanceFailurePolicy.FAIL_ARTIFACT_SAVE,
            producing_node=ProducingNodeIdentity(name=producing_node_name, node_type="_ProvProbeNode"),
        )
        result = engine.handle_request(
            WriteFileRequest(file_path=str(temp_dir / "hero.txt"), content="pixels", provenance=provenance)
        )
        assert isinstance(result, WriteFileResultSuccess)
        record = _load_single_record(_record_dir_for(temp_dir, "hero.txt"))

        payload = record.payload
        # No 'kind' discriminator: sub-object presence IS the answer.
        assert payload.serialized_node is not None
        assert payload.serialized_node.serialized_node_commands
        assert payload.serialized_node.set_parameter_value_commands
        unique_values = pickle.loads(  # noqa: S301
            base64.b64decode(payload.serialized_node.pickled_parameter_values)
        )
        assert isinstance(unique_values, dict)
        assert unique_values

        # The preview is the lossy display layer; the prompt is readable there.
        assert record.parameters_preview is not None
        assert record.parameters_preview["prompt"] == "a left-handed warrior of tremendous strength"

        # Lineage: the string reference in the declared-image parameter became a
        # source link -- a chain break (never engine-written) carrying path + hash.
        assert len(record.sources) == 1
        source = record.sources[0]
        assert source.parameter_name == "reference_image"
        assert source.path_at_use.endswith("kb_reference.png")
        assert source.record_id is None
        assert source.content_hash == hash_content(_tiny_png_bytes())

        # The embedded workflow snapshot is a real workflow .py: versioned header and all.
        workflow = payload.serialized_workflow
        assert workflow is not None
        assert "# /// script" in workflow.workflow_file_content
        assert workflow.workflow_file_hash == hash_content(workflow.workflow_file_content.encode("utf-8"))

        # PERSISTENCE COUPLING GUARD: the stored FAITHFUL form must rehydrate to
        # live serialize-node types -- including CONCRETE element command types
        # (element_modification_commands is polymorphic; a generic dict
        # restructure silently degrades them to bare RequestPayload, which is
        # exactly the bug this guard exists to catch). If the protocol's shapes
        # change without keeping records rehydratable (or bumping the record
        # schema), THIS is the test that fails.
        from griptape_nodes.retained_mode.events.base_events import RequestPayload
        from griptape_nodes.retained_mode.events.event_converter import converter
        from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, SerializedNodeCommands

        assert payload.serialized_node.pickled_node_commands is not None
        rehydrated_commands = pickle.loads(  # noqa: S301
            base64.b64decode(payload.serialized_node.pickled_node_commands)
        )
        assert isinstance(rehydrated_commands, SerializedNodeCommands)
        assert rehydrated_commands.node_uuid
        assert isinstance(rehydrated_commands.create_node_command, CreateNodeRequest)
        for element_command in rehydrated_commands.element_modification_commands:
            assert type(element_command) is not RequestPayload, "element command lost its concrete type"

        # The indirect set-value commands are concrete types; the converter handles them.
        rehydrated_set_commands = [
            converter.structure(entry, SerializedNodeCommands.IndirectSetParameterValueCommand)
            for entry in payload.serialized_node.set_parameter_value_commands
        ]
        assert rehydrated_set_commands
        stored_uuids = {entry.unique_value_uuid for entry in rehydrated_set_commands}
        assert stored_uuids <= set(unique_values.keys())

        # THE WIRE DIET: query results carry the header with the payload
        # summarized -- sizes and hashes, never the embedded workflow itself.
        get_result = engine.handle_request(GetProvenanceForArtifactRequest(macro_path=str(temp_dir / "hero.txt")))
        assert isinstance(get_result, GetProvenanceForArtifactResultSuccess)
        wire_payload = get_result.record.payload
        assert wire_payload.has_serialized_node
        assert wire_payload.has_serialized_workflow
        assert wire_payload.workflow_file_chars == len(workflow.workflow_file_content)
        assert wire_payload.workflow_file_hash == workflow.workflow_file_hash
        assert not hasattr(wire_payload, "workflow_file_content")

        # The full payload is an explicit opt-in via its own request.
        payload_result = engine.handle_request(
            GetProvenancePayloadRequest(macro_path=str(temp_dir / "hero.txt"), record_id=record.record_id)
        )
        assert isinstance(payload_result, GetProvenancePayloadResultSuccess)
        assert payload_result.payload.serialized_workflow is not None
        assert payload_result.payload.serialized_workflow.workflow_file_content == workflow.workflow_file_content


class TestIngestionCopies:
    """Normalization uploads (reference-image ingestion into staticfiles/) are provenance-free.

    Ruling 2026-09-23: the staticfiles copy is the engine normalizing an INPUT,
    not producing an artifact. The consumer's parent link still carries the
    copy's path and content hash (record_id null: a documented ingestion boundary).
    """

    def test_normalization_upload_records_nothing(self, engine: Engine, temp_dir: Path) -> None:
        from griptape.artifacts import ImageUrlArtifact

        from griptape_nodes.retained_mode.engine import engine_scope
        from griptape_nodes.utils.artifact_normalization import normalize_artifact_input

        engine.handle_request(RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider))
        source = temp_dir / "reference.png"
        source.write_bytes(_tiny_png_bytes())

        with engine_scope(engine):
            artifact = normalize_artifact_input(str(source), ImageUrlArtifact)

        assert isinstance(artifact, ImageUrlArtifact)
        # The copy landed in staticfiles/ ...
        staticfiles_copies = list(temp_dir.rglob("staticfiles/reference.png"))
        assert staticfiles_copies
        # ... and NO record exists anywhere in the store for it.
        store = temp_dir / PROVENANCE_STORE_DIR
        ingestion_records = list(store.rglob("*reference.png*/*.yaml")) if store.exists() else []
        assert not ingestion_records


class TestQueryRequests:
    """The request family: the ONLY provenance interface for anything outside the engine."""

    def _write(self, engine: Engine, path: Path, content: bytes | str) -> WriteFileResultSuccess:
        result = engine.handle_request(
            WriteFileRequest(file_path=str(path), content=content, provenance=_sample_provenance())
        )
        assert isinstance(result, WriteFileResultSuccess)
        return result

    def test_get_latest_by_absolute_and_relative_spelling(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        self._write(engine, file_path, "take one")
        latest = self._write(engine, file_path, "take two")
        assert latest.provenance is not None

        macro_spelling = MacroPath(ParsedMacro("{workspace_dir}/hero.txt"), {})
        for spelling in (str(file_path), "hero.txt", macro_spelling):
            result = engine.handle_request(GetProvenanceForArtifactRequest(macro_path=spelling))
            assert isinstance(result, GetProvenanceForArtifactResultSuccess), spelling
            assert result.record.record_id == latest.provenance.record_id
            assert result.matched_by == ProvenanceMatchOrigin.PATH
            assert result.is_stale is False
            assert result.record_path.endswith(f"{latest.provenance.record_id}.yaml")

    def test_get_specific_record_and_staleness(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        first = self._write(engine, file_path, "take one")
        self._write(engine, file_path, "take two")
        assert first.provenance is not None

        result = engine.handle_request(
            GetProvenanceForArtifactRequest(macro_path=str(file_path), record_id=first.provenance.record_id)
        )
        assert isinstance(result, GetProvenanceForArtifactResultSuccess)
        # The live file was overwritten by take two: iteration one is stale.
        assert result.is_stale is True

        missing = engine.handle_request(
            GetProvenanceForArtifactRequest(macro_path=str(file_path), record_id="20200101T000000000000Z-00000000")
        )
        assert isinstance(missing, GetProvenanceForArtifactResultFailure)
        assert missing.failure_reason == ProvenanceQueryFailureReason.RECORD_NOT_FOUND

    def test_get_falls_back_to_hash_for_renamed_files(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "original.txt"
        written = self._write(engine, file_path, "movable")
        assert written.provenance is not None
        renamed = temp_dir / "renamed_by_hand.txt"
        file_path.rename(renamed)

        result = engine.handle_request(GetProvenanceForArtifactRequest(macro_path=str(renamed)))
        assert isinstance(result, GetProvenanceForArtifactResultSuccess)
        assert result.record.record_id == written.provenance.record_id
        assert result.matched_by == ProvenanceMatchOrigin.HASH

    def test_no_records_failure(self, engine: Engine, temp_dir: Path) -> None:
        bare = temp_dir / "no_history.txt"
        bare.write_text("nothing")
        result = engine.handle_request(GetProvenanceForArtifactRequest(macro_path=str(bare)))
        assert isinstance(result, GetProvenanceForArtifactResultFailure)
        assert result.failure_reason == ProvenanceQueryFailureReason.NO_PROVENANCE_RECORDS

    def test_list_for_artifact_returns_history_newest_first(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        first = self._write(engine, file_path, "take one")
        second = self._write(engine, file_path, "take two")
        assert first.provenance is not None
        assert second.provenance is not None

        result = engine.handle_request(ListProvenanceRecordsForArtifactRequest(macro_path=str(file_path)))
        assert isinstance(result, ListProvenanceRecordsForArtifactResultSuccess)
        assert [r.record_id for r in result.records] == [
            second.provenance.record_id,
            first.provenance.record_id,
        ]

    def test_list_for_hash_finds_identical_content_across_paths(self, engine: Engine, temp_dir: Path) -> None:
        a = self._write(engine, temp_dir / "copy_a.txt", "same bytes")
        b = self._write(engine, temp_dir / "copy_b.txt", "same bytes")
        assert a.provenance is not None
        assert b.provenance is not None
        assert a.provenance.content_hash == b.provenance.content_hash

        result = engine.handle_request(ListProvenanceRecordsForHashRequest(content_hash=a.provenance.content_hash))
        assert isinstance(result, ListProvenanceRecordsForHashResultSuccess)
        found_ids = {r.record_id for r in result.records}
        assert found_ids == {a.provenance.record_id, b.provenance.record_id}

    def test_inventory_lists_and_sorts_by_kind(self, engine: Engine, temp_dir: Path) -> None:
        engine.handle_request(RegisterArtifactProviderRequest(provider_class=ImageArtifactProvider))
        self._write(engine, temp_dir / "zeta.txt", "words")
        self._write(engine, temp_dir / "hero.png", _tiny_png_bytes())
        self._write(engine, temp_dir / "hero.png", _tiny_png_bytes())  # second iteration

        result = engine.handle_request(ListProvenancedArtifactsRequest())
        assert isinstance(result, ListProvenancedArtifactsResultSuccess)
        expected_artifact_count = 2
        assert len(result.artifacts) == expected_artifact_count
        # Sorted by kind (image before kind-less txt), then path.
        image_entry, text_entry = result.artifacts
        assert image_entry.artifact_kind == "image"
        assert image_entry.file_name == "hero.png"
        image_record_count = 2
        assert image_entry.record_count == image_record_count
        assert text_entry.artifact_kind is None
        assert text_entry.file_name == "zeta.txt"
        assert text_entry.record_count == 1
        # Latest record id is the newest save.
        assert image_entry.latest_record_id == max(f.stem for f in _record_dir_for(temp_dir, "hero.png").glob("*.yaml"))
