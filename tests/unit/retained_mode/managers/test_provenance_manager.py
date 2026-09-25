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
    GetProvenancePayloadResultFailure,
    GetProvenancePayloadResultSuccess,
    ListProvenancedArtifactsRequest,
    ListProvenancedArtifactsResultSuccess,
    ListProvenanceRecordsForArtifactRequest,
    ListProvenanceRecordsForArtifactResultFailure,
    ListProvenanceRecordsForArtifactResultSuccess,
    ListProvenanceRecordsForHashRequest,
    ListProvenanceRecordsForHashResultFailure,
    ListProvenanceRecordsForHashResultSuccess,
    ProvenanceMatchOrigin,
    ProvenanceQueryFailureReason,
)
from griptape_nodes.retained_mode.file_metadata.provenance_record import (
    ArtifactIdentity,
    ProducingNodeIdentity,
    ProducingNodeIdentitySource,
    ProvenanceContent,
    ProvenanceRecord,
    ProvenanceWriteDetails,
    by_hash_relative_path,
    dump_record_yaml,
    hash_content,
    load_record_yaml,
)
from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactProvider,
)
from griptape_nodes.retained_mode.managers.os_manager import FileWriteAttemptResult
from griptape_nodes.retained_mode.managers.provenance_manager import ArtifactWriteFacts, ProvenanceCaptureResult

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


class TestSummaryExcerptSource:
    """The summary excerpt prefers prompt, then the longest substantial string, then anything."""

    def test_empty_preview_yields_nothing(self, engine: Engine) -> None:
        assert engine.provenance_manager._summary_excerpt_source(None) is None
        assert engine.provenance_manager._summary_excerpt_source({}) is None

    def test_prompt_wins_over_longer_strings(self, engine: Engine) -> None:
        preview = {"negative_prompt": "x" * 50, "prompt": "hero"}
        assert engine.provenance_manager._summary_excerpt_source(preview) == ("prompt", "hero")

    def test_longest_substantial_string_wins_without_prompt(self, engine: Engine) -> None:
        preview = {"style": "noir", "description": "a fortress carved into a cliff face"}
        excerpt = engine.provenance_manager._summary_excerpt_source(preview)
        assert excerpt == ("description", "a fortress carved into a cliff face")

    def test_first_entry_is_the_last_resort(self, engine: Engine) -> None:
        preview = {"api_key_provider": True, "steps": 30}
        assert engine.provenance_manager._summary_excerpt_source(preview) == ("api_key_provider", True)


class TestReconstructArtifactPath:
    """Inverting the by-path mirror for the inventory: heuristic, never guesses wrong."""

    def test_drive_mirror_reconstructs_a_drive_anchored_path(self, engine: Engine) -> None:
        reconstructed = engine.provenance_manager._reconstruct_artifact_path(Path("C:") / "renders" / "out.png")
        assert reconstructed == Path("C:/renders/out.png")

    def test_rooted_mirror_reconstructs_when_the_root_location_exists(self, engine: Engine, temp_dir: Path) -> None:
        # A mirror of a real rooted directory: the workspace-relative candidate
        # does not exist, so reconstruction re-roots it at / and finds it there.
        mirror = temp_dir.relative_to(Path(temp_dir.anchor))
        reconstructed = engine.provenance_manager._reconstruct_artifact_path(mirror)
        assert reconstructed == temp_dir

    def test_deleted_artifact_falls_back_to_the_workspace_shape(self, engine: Engine, temp_dir: Path) -> None:
        reconstructed = engine.provenance_manager._reconstruct_artifact_path(Path("ghost/gone.png"))
        assert reconstructed == temp_dir / "ghost" / "gone.png"


class TestRecordLoadingSkips:
    """The store is user-editable: unreadable or newer-major records are skipped, not raised."""

    def _saved_record_dir(self, engine: Engine, temp_dir: Path) -> Path:
        result = engine.handle_request(
            WriteFileRequest(file_path=str(temp_dir / "hero.txt"), content="pixels", provenance=_sample_provenance())
        )
        assert isinstance(result, WriteFileResultSuccess)
        return _record_dir_for(temp_dir, "hero.txt")

    def test_unreadable_record_is_skipped(self, engine: Engine, temp_dir: Path) -> None:
        record_dir = self._saved_record_dir(engine, temp_dir)
        good_record = _load_single_record(record_dir)
        # Sorts after every timestamped record id, so it is tried FIRST.
        (record_dir / "99990101T000000000000Z-aaaaaaaa.yaml").write_text("{{{ not yaml")

        latest = engine.provenance_manager.find_latest_record_for_path(temp_dir / "hero.txt")
        assert latest is not None
        assert latest.record_id == good_record.record_id

    def test_newer_major_schema_record_is_skipped(self, engine: Engine, temp_dir: Path) -> None:
        record_dir = self._saved_record_dir(engine, temp_dir)
        good_record = _load_single_record(record_dir)
        from_the_future = good_record.model_copy(update={"schema_version": "99.0.0"})
        (record_dir / "99990101T000000000000Z-bbbbbbbb.yaml").write_text(dump_record_yaml(from_the_future))

        latest = engine.provenance_manager.find_latest_record_for_path(temp_dir / "hero.txt")
        assert latest is not None
        assert latest.record_id == good_record.record_id


class TestProducingNodeInference:
    """No attested identity: the resolving-node heuristic fills in, flagged as inferred."""

    def test_resolving_node_is_inferred_and_flagged(self, engine: Engine) -> None:
        engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_name="inf_wf", flow_name="inf_flow"))
        try:
            with patch.object(type(engine.flow_manager), "flow_state", return_value=(set(), ["GhostNode"], set())):
                identity = engine.provenance_manager._resolve_producing_node(ProvenanceContent())
        finally:
            engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        assert identity is not None
        assert identity.name == "GhostNode"
        # The node is not in the object manager, so the type falls back to the name.
        assert identity.node_type == "GhostNode"
        assert identity.identity_source == ProducingNodeIdentitySource.INFERRED_RESOLVING_NODE

    def test_no_resolving_nodes_means_no_identity(self, engine: Engine) -> None:
        engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_name="inf_wf2", flow_name="inf_flow2"))
        try:
            with patch.object(type(engine.flow_manager), "flow_state", return_value=(set(), [], set())):
                identity = engine.provenance_manager._resolve_producing_node(ProvenanceContent())
        finally:
            engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        assert identity is None


class TestScratchPathEdges:
    def test_non_temp_absolute_path_is_not_scratch(self, engine: Engine) -> None:
        assert engine.provenance_manager._is_engine_scratch_path(Path("/definitely/not/temp/out.png")) is False

    def test_temp_path_outside_workspace_is_scratch(self, engine: Engine) -> None:
        scratch = Path(tempfile.gettempdir()) / "gtn_scratch_probe" / "out.png"
        assert engine.provenance_manager._is_engine_scratch_path(scratch) is True


class TestRollbackBestEffort:
    def test_rollback_tolerates_undeletable_files(self, engine: Engine, temp_dir: Path) -> None:
        """Rollback is best-effort: an OSError on either unlink warns instead of raising."""
        content_hash = hash_content(b"doomed")
        # Directories where the files should be: unlink raises OSError on both.
        record_path = temp_dir / "record_squatter"
        record_path.mkdir()
        store_root = temp_dir / PROVENANCE_STORE_DIR
        pointer_path = store_root / by_hash_relative_path(content_hash, "rid")
        pointer_path.mkdir(parents=True)

        details = ProvenanceWriteDetails(
            record_id="rid",
            record_path=str(record_path),
            content_hash=content_hash,
            capture_policy=ProvenanceCapturePolicy.PRODUCING_NODE_ONLY,
        )
        engine.provenance_manager.rollback_record(details)
        assert record_path.exists()
        assert pointer_path.exists()


class TestQueryFailureBranches:
    def test_payload_request_with_unresolvable_path_fails(self, engine: Engine) -> None:
        result = engine.handle_request(GetProvenancePayloadRequest(macro_path=""))
        assert isinstance(result, GetProvenancePayloadResultFailure)
        assert result.failure_reason == ProvenanceQueryFailureReason.PATH_UNRESOLVABLE

    def test_payload_request_without_records_fails(self, engine: Engine, temp_dir: Path) -> None:
        (temp_dir / "plain.txt").write_text("no history")
        result = engine.handle_request(GetProvenancePayloadRequest(macro_path=str(temp_dir / "plain.txt")))
        assert isinstance(result, GetProvenancePayloadResultFailure)
        assert result.failure_reason == ProvenanceQueryFailureReason.NO_PROVENANCE_RECORDS

    def test_payload_request_for_missing_record_id_fails(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        write_result = engine.handle_request(
            WriteFileRequest(file_path=str(file_path), content="pixels", provenance=_sample_provenance())
        )
        assert isinstance(write_result, WriteFileResultSuccess)
        result = engine.handle_request(
            GetProvenancePayloadRequest(macro_path=str(file_path), record_id="20200101T000000000000Z-deadbeef")
        )
        assert isinstance(result, GetProvenancePayloadResultFailure)
        assert result.failure_reason == ProvenanceQueryFailureReason.RECORD_NOT_FOUND

    def test_hash_listing_rejects_an_unknown_hash_algorithm(self, engine: Engine) -> None:
        result = engine.handle_request(ListProvenanceRecordsForHashRequest(content_hash="sha256:abcdef"))
        assert isinstance(result, ListProvenanceRecordsForHashResultFailure)
        assert result.failure_reason == ProvenanceQueryFailureReason.PATH_UNRESOLVABLE

    def test_hash_listing_for_unknown_content_is_empty_success(self, engine: Engine) -> None:
        result = engine.handle_request(ListProvenanceRecordsForHashRequest(content_hash=hash_content(b"nobody")))
        assert isinstance(result, ListProvenanceRecordsForHashResultSuccess)
        assert result.records == []


class TestCaptureNeverRaises:
    def test_capture_blowup_becomes_a_failed_result(self, engine: Engine, temp_dir: Path) -> None:
        plan = engine.provenance_manager.plan_capture(_sample_provenance(), temp_dir / "hero.txt")
        facts = ArtifactWriteFacts(final_file_path=temp_dir / "hero.txt", final_content_bytes=b"pixels")
        with patch.object(
            type(engine.provenance_manager), "_record_artifact_save", side_effect=RuntimeError("meteor strike")
        ):
            capture = engine.provenance_manager.record_artifact_save(facts, _sample_provenance(), plan)
        assert capture.failed
        assert capture.error_message is not None
        assert "meteor strike" in capture.error_message


class TestPathSpellingResolution:
    """The `str | MacroPath` request surface: every spelling resolves or fails readably."""

    def test_unresolvable_macro_string_fails_the_lookup(self, engine: Engine) -> None:
        result = engine.handle_request(
            ListProvenanceRecordsForArtifactRequest(macro_path="{no_such_macro_anywhere}/x.png")
        )
        assert isinstance(result, ListProvenanceRecordsForArtifactResultFailure)
        assert result.failure_reason == ProvenanceQueryFailureReason.PATH_UNRESOLVABLE

    def test_empty_path_fails_the_lookup(self, engine: Engine) -> None:
        result = engine.handle_request(ListProvenanceRecordsForArtifactRequest(macro_path=""))
        assert isinstance(result, ListProvenanceRecordsForArtifactResultFailure)
        assert result.failure_reason == ProvenanceQueryFailureReason.PATH_UNRESOLVABLE
        get_result = engine.handle_request(GetProvenanceForArtifactRequest(macro_path=""))
        assert isinstance(get_result, GetProvenanceForArtifactResultFailure)
        assert get_result.failure_reason == ProvenanceQueryFailureReason.PATH_UNRESOLVABLE

    def test_macro_syntax_error_degrades_to_a_plain_relative_path(self, engine: Engine) -> None:
        # "{unclosed" is not a macro; it is treated as a workspace-relative path.
        result = engine.handle_request(ListProvenanceRecordsForArtifactRequest(macro_path="{unclosed/x.png"))
        assert isinstance(result, ListProvenanceRecordsForArtifactResultSuccess)
        assert result.records == []

    def test_relative_path_resolves_against_the_workspace(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "rel.txt"
        write_result = engine.handle_request(
            WriteFileRequest(file_path=str(file_path), content="pixels", provenance=_sample_provenance())
        )
        assert isinstance(write_result, WriteFileResultSuccess)
        result = engine.handle_request(ListProvenanceRecordsForArtifactRequest(macro_path="rel.txt"))
        assert isinstance(result, ListProvenanceRecordsForArtifactResultSuccess)
        assert len(result.records) == 1


class TestLookupDegradation:
    def test_missing_artifact_with_no_records_lists_empty(self, engine: Engine, temp_dir: Path) -> None:
        # No record dir AND unreadable live bytes: the hash fallback degrades to empty.
        result = engine.handle_request(
            ListProvenanceRecordsForArtifactRequest(macro_path=str(temp_dir / "never_existed.png"))
        )
        assert isinstance(result, ListProvenanceRecordsForArtifactResultSuccess)
        assert result.records == []

    def test_unreadable_by_hash_pointer_is_skipped(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        write_result = engine.handle_request(
            WriteFileRequest(file_path=str(file_path), content="pixels", provenance=_sample_provenance())
        )
        assert isinstance(write_result, WriteFileResultSuccess)
        content_hash = hash_content(b"pixels")
        pointer_dir = (temp_dir / PROVENANCE_STORE_DIR / by_hash_relative_path(content_hash, "_")).parent
        (pointer_dir / "99990101T000000000000Z-cccccccc.yaml").write_text("{{{ not yaml")

        result = engine.handle_request(ListProvenanceRecordsForHashRequest(content_hash=content_hash))
        assert isinstance(result, ListProvenanceRecordsForHashResultSuccess)
        assert len(result.records) == 1

    def test_find_record_by_hash_with_no_pointers_is_none(self, engine: Engine) -> None:
        assert engine.provenance_manager._find_record_by_hash(hash_content(b"never saved")) is None

    def test_staleness_is_unjudgeable_when_the_artifact_vanished(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        write_result = engine.handle_request(
            WriteFileRequest(file_path=str(file_path), content="pixels", provenance=_sample_provenance())
        )
        assert isinstance(write_result, WriteFileResultSuccess)
        file_path.unlink()

        result = engine.handle_request(GetProvenanceForArtifactRequest(macro_path=str(file_path)))
        assert isinstance(result, GetProvenanceForArtifactResultSuccess)
        assert result.is_stale is None

    def test_all_unreadable_records_yield_no_latest(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        write_result = engine.handle_request(
            WriteFileRequest(file_path=str(file_path), content="pixels", provenance=_sample_provenance())
        )
        assert isinstance(write_result, WriteFileResultSuccess)
        record_dir = _record_dir_for(temp_dir, "hero.txt")
        for record_file in record_dir.glob("*.yaml"):
            record_file.write_text("{{{ not yaml")
        assert engine.provenance_manager.find_latest_record_for_path(file_path) is None

    def test_payload_request_without_record_id_returns_the_latest(self, engine: Engine, temp_dir: Path) -> None:
        file_path = temp_dir / "hero.txt"
        write_result = engine.handle_request(
            WriteFileRequest(file_path=str(file_path), content="pixels", provenance=_sample_provenance())
        )
        assert isinstance(write_result, WriteFileResultSuccess)
        assert write_result.provenance is not None
        result = engine.handle_request(GetProvenancePayloadRequest(macro_path=str(file_path)))
        assert isinstance(result, GetProvenancePayloadResultSuccess)
        assert result.record_id == write_result.provenance.record_id


class TestClassificationGuards:
    def test_empty_declared_string_is_not_a_source(self, engine: Engine, temp_dir: Path) -> None:
        assert engine.provenance_manager._classify_declared_path_string("", temp_dir) is None

    def test_hostile_length_string_probe_degrades_to_no_source(self, engine: Engine, temp_dir: Path) -> None:
        # The ENAMETOOLONG regression: an over-long name raises from stat()
        # instead of answering "no"; the guard answers "not a source".
        assert engine.provenance_manager._classify_declared_path_string("x" * 4096, temp_dir) is None

    def test_remote_url_artifact_is_not_a_local_source(self, engine: Engine, temp_dir: Path) -> None:
        from griptape.artifacts import ImageUrlArtifact

        artifact = ImageUrlArtifact("https://example.com/render.png")
        assert engine.provenance_manager._classify_url_artifact(artifact, temp_dir) is None

    def test_parameter_value_enumeration_degrades_per_parameter(self, engine: Engine) -> None:
        node = _ProvProbeNode("Probe_Z")
        node.set_parameter_value("prompt", "fine")
        with patch.object(type(node), "get_parameter_value", side_effect=RuntimeError("resolver exploded")):
            assert engine.provenance_manager._iter_input_values(node) == []

    def test_dict_values_flatten_one_level(self, engine: Engine) -> None:
        node = _ProvProbeNode("Probe_D")
        node.set_parameter_value("prompt", "fine")
        with patch.object(type(node), "get_parameter_value", return_value={"a": "one", "b": "two"}):
            entries = engine.provenance_manager._iter_input_values(node)
        values = [value for _, value in entries]
        assert values.count("one") > 0
        assert values.count("two") > 0


class TestWorkflowAttachmentGuards:
    def test_no_current_flow_leaves_the_payload_without_a_workflow(self, engine: Engine, temp_dir: Path) -> None:
        record = ProvenanceRecord(
            record_id="20260924T000000000000Z-abcd1234",
            saved_at="2026-09-24T00:00:00+00:00",
            capture_policy=ProvenanceCapturePolicy.FULL_WORKFLOW_SNAPSHOT,
            artifact=ArtifactIdentity(
                path_at_save=str(temp_dir / "hero.txt"),
                file_name="hero.txt",
                content_hash=hash_content(b"pixels"),
                size_bytes=6,
            ),
        )
        engine.provenance_manager._attach_workflow_file(record)
        assert record.payload.serialized_workflow is None


class TestSourceDiscoveryDegradation:
    """Source discovery never fails a save: every per-source error degrades."""

    def test_unfindable_node_yields_no_sources(self, engine: Engine) -> None:
        identity = ProducingNodeIdentity(name="Nobody_1", node_type="Nobody")
        assert engine.provenance_manager._discover_sources(identity) == []

    def test_node_lookup_blowup_yields_no_sources(self, engine: Engine) -> None:
        identity = ProducingNodeIdentity(name="Nobody_1", node_type="Nobody")
        with patch.object(
            type(engine.object_manager), "attempt_get_object_by_name_as_type", side_effect=RuntimeError("boom")
        ):
            assert engine.provenance_manager._discover_sources(identity) == []

    def test_link_failure_becomes_a_chain_break_with_the_cause(self, engine: Engine, temp_dir: Path) -> None:
        node = _ProvProbeNode("Probe_X")
        reference = temp_dir / "ref.png"
        reference.write_bytes(_tiny_png_bytes())
        node.set_parameter_value("reference_image", str(reference))
        identity = ProducingNodeIdentity(name=node.name, node_type="_ProvProbeNode")

        with (
            patch.object(type(engine.object_manager), "attempt_get_object_by_name_as_type", return_value=node),
            patch.object(type(engine.provenance_manager), "_link_source", side_effect=RuntimeError("store on fire")),
        ):
            sources = engine.provenance_manager._discover_sources(identity)
        assert len(sources) == 1
        assert sources[0].parameter_name == "reference_image"
        assert sources[0].discovery_error is not None
        assert "store on fire" in sources[0].discovery_error

    def test_classification_blowup_skips_the_value(self, engine: Engine, temp_dir: Path) -> None:
        node = _ProvProbeNode("Probe_Y")
        reference = temp_dir / "ref2.png"
        reference.write_bytes(_tiny_png_bytes())
        node.set_parameter_value("reference_image", str(reference))
        identity = ProducingNodeIdentity(name=node.name, node_type="_ProvProbeNode")

        with (
            patch.object(type(engine.object_manager), "attempt_get_object_by_name_as_type", return_value=node),
            patch.object(
                type(engine.provenance_manager),
                "_classify_file_backed_value",
                side_effect=OSError("ENAMETOOLONG"),
            ),
        ):
            sources = engine.provenance_manager._discover_sources(identity)
        assert sources == []
