"""Unit tests for the provenance record schema and its helpers."""

from datetime import UTC, datetime

import pytest

from griptape_nodes.common.project_templates.provenance_settings import (
    ENGINE_DEFAULT_FAILURE_POLICY,
    ENGINE_DEFAULT_PER_ARTIFACT_TYPE,
    ProvenanceCapturePolicy,
    ProvenanceFailurePolicy,
    ProvenanceSettings,
    resolve_capture_policy,
    resolve_failure_policy,
)
from griptape_nodes.retained_mode.file_metadata.provenance_record import (
    RECORD_SCHEMA_VERSION,
    ArtifactIdentity,
    ByHashPointer,
    ProvenancePayload,
    ProvenanceRecord,
    SerializedWorkflowPayload,
    by_hash_relative_path,
    dump_pointer_yaml,
    dump_record_yaml,
    generate_record_id,
    hash_content,
    hash_hex_from_content_hash,
    is_reader_compatible,
    load_pointer_yaml,
    load_record_yaml,
)


class TestRecordId:
    def test_lexicographic_order_matches_chronological_order(self) -> None:
        earlier = generate_record_id(now=datetime(2026, 9, 21, 18, 30, 12, 482913, tzinfo=UTC))
        later = generate_record_id(now=datetime(2026, 9, 21, 18, 30, 12, 482914, tzinfo=UTC))
        assert earlier < later

    def test_same_microsecond_ids_are_distinct(self) -> None:
        id_count = 64
        now = datetime(2026, 9, 21, 18, 30, 12, 482913, tzinfo=UTC)
        ids = {generate_record_id(now=now) for _ in range(id_count)}
        assert len(ids) == id_count

    def test_shape(self) -> None:
        random_suffix_hex_chars = 8
        record_id = generate_record_id(now=datetime(2026, 9, 21, 18, 30, 12, 482913, tzinfo=UTC))
        timestamp_part, random_part = record_id.split("Z-")
        assert timestamp_part == "20260921T183012482913"
        assert len(random_part) == random_suffix_hex_chars
        int(random_part, 16)


class TestContentHash:
    def test_hash_content_round_trips_through_hex_extractor(self) -> None:
        content_hash = hash_content(b"pixels")
        hex_digest = hash_hex_from_content_hash(content_hash)
        assert content_hash == f"blake2b-256:{hex_digest}"

    def test_identical_bytes_hash_identically(self) -> None:
        assert hash_content(b"pixels") == hash_content(b"pixels")
        assert hash_content(b"pixels") != hash_content(b"pixel!")

    def test_unexpected_algorithm_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="does not use the expected"):
            hash_hex_from_content_hash("sha256:abcdef")


class TestStoreLayoutPaths:
    def test_by_hash_path_is_sharded_and_time_sorted(self) -> None:
        content_hash = hash_content(b"pixels")
        hex_digest = hash_hex_from_content_hash(content_hash)
        pointer_path = by_hash_relative_path(content_hash, "20260921T183012482913Z-a3f9c2d1")
        assert pointer_path == f"by-hash/{hex_digest[:2]}/{hex_digest}/20260921T183012482913Z-a3f9c2d1.yaml"


class TestReaderCompatibility:
    def test_same_major_is_compatible(self) -> None:
        assert is_reader_compatible(RECORD_SCHEMA_VERSION)
        assert is_reader_compatible("1.9.3")

    def test_newer_major_is_not_compatible(self) -> None:
        assert not is_reader_compatible("2.0.0")

    def test_malformed_version_is_not_compatible(self) -> None:
        assert not is_reader_compatible("not-a-version")


class TestRecordEnvelope:
    def test_minimal_record_round_trips_through_yaml(self) -> None:
        record = ProvenanceRecord(
            record_id=generate_record_id(),
            saved_at=datetime.now(UTC).isoformat(),
            capture_policy=ProvenanceCapturePolicy.PRODUCING_NODE_ONLY,
            artifact=ArtifactIdentity(
                final_path="/ws/renders/hero.png",
                file_name="hero.png",
                content_hash=hash_content(b"pixels"),
                size_bytes=6,
            ),
        )
        text = dump_record_yaml(record)
        # In-band inspector guidance rides every record.
        assert text.startswith("#")
        assert "faithful representation" in text
        round_tripped = load_record_yaml(text)
        assert round_tripped == record
        assert round_tripped.schema_version == RECORD_SCHEMA_VERSION

    def test_workflow_file_content_embeds_as_readable_block_scalar(self) -> None:
        workflow_text = "# /// script\n# name = 'demo'\n# ///\n\nasync def build_workflow():\n    pass\n"
        record = ProvenanceRecord(
            record_id=generate_record_id(),
            saved_at=datetime.now(UTC).isoformat(),
            capture_policy=ProvenanceCapturePolicy.FULL_WORKFLOW_SNAPSHOT,
            artifact=ArtifactIdentity(
                final_path="/ws/renders/hero.png",
                file_name="hero.png",
                content_hash=hash_content(b"pixels"),
                size_bytes=6,
            ),
            payload=ProvenancePayload(
                serialized_workflow=SerializedWorkflowPayload(
                    workflow_file_content=workflow_text,
                    workflow_file_hash=hash_content(workflow_text.encode("utf-8")),
                ),
            ),
        )
        text = dump_record_yaml(record)
        # Block scalar: the workflow lines appear verbatim, not as escaped \n soup.
        # (Keys are double-quoted per house YAML convention.)
        assert '"workflow_file_content": |' in text
        assert "async def build_workflow():" in text
        round_tripped = load_record_yaml(text)
        assert round_tripped.payload.serialized_workflow is not None
        assert round_tripped.payload.serialized_workflow.workflow_file_content == workflow_text

    def test_pointer_round_trips_through_yaml(self) -> None:
        pointer = ByHashPointer(record="by-path/renders/hero.png/x.yaml", artifact_path_at_save="/ws/renders/hero.png")
        assert load_pointer_yaml(dump_pointer_yaml(pointer)) == pointer


class TestPolicyResolution:
    def test_explicit_request_wins_over_the_table(self) -> None:
        settings = ProvenanceSettings(
            per_artifact_type={"image": ProvenanceCapturePolicy.NO_PROVENANCE_RECORDED},
        )
        resolved = resolve_capture_policy(
            ProvenanceCapturePolicy.FULL_WORKFLOW_SNAPSHOT, settings, artifact_kind="image"
        )
        assert resolved == ProvenanceCapturePolicy.FULL_WORKFLOW_SNAPSHOT

    def test_none_and_inherit_both_resolve_through_the_table(self) -> None:
        settings = ProvenanceSettings(
            per_artifact_type={"video": ProvenanceCapturePolicy.PRODUCING_NODE_ONLY},
        )
        for requested in (None, ProvenanceCapturePolicy.INHERIT_PROJECT_POLICY):
            assert (
                resolve_capture_policy(requested, settings, artifact_kind="video")
                == ProvenanceCapturePolicy.PRODUCING_NODE_ONLY
            )
            # A kind not in the list records nothing.
            assert (
                resolve_capture_policy(requested, settings, artifact_kind="image")
                == ProvenanceCapturePolicy.NO_PROVENANCE_RECORDED
            )
            # An unclaimed format can never be in a kind-keyed table.
            assert (
                resolve_capture_policy(requested, settings, artifact_kind=None)
                == ProvenanceCapturePolicy.NO_PROVENANCE_RECORDED
            )
        assert resolve_failure_policy(None, None) == ENGINE_DEFAULT_FAILURE_POLICY

    def test_no_project_block_uses_engine_default_table(self) -> None:
        for kind, policy in ENGINE_DEFAULT_PER_ARTIFACT_TYPE.items():
            assert resolve_capture_policy(None, None, artifact_kind=kind) == policy
        assert resolve_capture_policy(None, None, artifact_kind="3d") == (
            ProvenanceCapturePolicy.NO_PROVENANCE_RECORDED
        )

    def test_block_without_table_keeps_the_engine_default_table(self) -> None:
        # A block that only sets failure policy must not silently kill capture.
        settings = ProvenanceSettings(default_failure_policy=ProvenanceFailurePolicy.WARN_AND_CONTINUE)
        assert settings.per_artifact_type == ENGINE_DEFAULT_PER_ARTIFACT_TYPE

    def test_per_type_keys_normalize_and_reject_inherit(self) -> None:
        settings = ProvenanceSettings(per_artifact_type={"Image": ProvenanceCapturePolicy.PRODUCING_NODE_ONLY})
        assert settings.per_artifact_type == {"image": ProvenanceCapturePolicy.PRODUCING_NODE_ONLY}
        with pytest.raises(ValueError, match="per_artifact_type"):
            ProvenanceSettings(per_artifact_type={"image": ProvenanceCapturePolicy.INHERIT_PROJECT_POLICY})

    def test_settings_reject_inherit_failure_default(self) -> None:
        with pytest.raises(ValueError, match="default_failure_policy"):
            ProvenanceSettings(default_failure_policy=ProvenanceFailurePolicy.INHERIT_PROJECT_POLICY)
