"""Coverage for VideoArtifactProvider's codec-permission hooks.

The read/write hooks shell out to ffprobe against real video bytes, which is
too expensive and environment-dependent for unit tests. Every test in this
module monkeypatches ``_run_ffprobe`` to inject a canned payload, so we
exercise the checkpoint assembly + hook-chain evaluation without ever
spawning a subprocess.

The write path additionally issues a ``WriteTempFileRequest`` which resolves
the project's ``SAVE_TEMP_FILE`` situation macro. That needs a real project
workspace, so the write-permission tests load ``DEFAULT_PROJECT_TEMPLATE``
into a temp directory before running.
"""

from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.artifact_providers.video.video_artifact_provider import (
    VideoArtifactProvider,
)
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    CheckpointDenial,
    CheckpointFailure,
)

if TYPE_CHECKING:
    from griptape_nodes.common.macro_parser import MacroVariables


def _canned_probe(codec: str) -> dict[str, Any]:
    """Minimal ffprobe payload with a single video stream carrying ``codec``."""
    return {"streams": [{"codec_type": "video", "codec_name": codec}]}


@pytest.fixture
def _project_workspace(griptape_nodes: GriptapeNodes, tmp_path: Path) -> Generator[Path, None, None]:
    """Set workspace to tmp_path and load DEFAULT_PROJECT_TEMPLATE so SAVE_TEMP_FILE resolves.

    ``check_write_permission`` issues a ``WriteTempFileRequest`` which needs a
    resolvable project template. Point the workspace at a scratch dir and
    activate a project template there for the duration of each test.
    """
    config_manager = griptape_nodes.ConfigManager()
    original_workspace = config_manager.workspace_path
    config_manager.set_config_value("workspace_directory", str(tmp_path))

    project_yml = tmp_path / "project_template.yml"
    project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
    load_result = GriptapeNodes.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
    if isinstance(load_result, LoadProjectTemplateResultSuccess):
        GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))

    yield tmp_path

    GriptapeNodes.handle_request(SetCurrentProjectRequest(project_id=None))
    config_manager.set_config_value("workspace_directory", str(original_workspace))


class TestVideoWritePermission:
    """``check_write_permission`` stages bytes via WriteTempFileRequest, probes, and evaluates the checkpoint."""

    def test_denies_when_hook_denies_codec(
        self,
        griptape_nodes: GriptapeNodes,
        monkeypatch: pytest.MonkeyPatch,
        _project_workspace: Path,  # noqa: PT019
    ) -> None:
        # Force ffprobe to report hevc without actually running the binary.
        # Signature: classmethod _run_ffprobe(cls, source_path) -> dict | None.
        monkeypatch.setattr(
            VideoArtifactProvider,
            "_run_ffprobe",
            classmethod(lambda cls, source_path: _canned_probe("hevc")),  # noqa: ARG005
        )

        seen: list[dict[str, Any]] = []

        def deny_hevc(checkpoint: object) -> CheckpointDenial | None:
            seen.append(dict(checkpoint.attributes))  # type: ignore[attr-defined]
            if checkpoint.attributes.get("id") == "hevc":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="hevc is not licensed."),))
            return None

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]
        griptape_nodes.EventManager().add_authorization_hook(deny_hevc)
        try:
            denial = provider.check_write_permission(b"any mp4 payload", "mp4", file_name="clip.mp4")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_hevc)

        assert denial is not None
        # The checkpoint must carry both id (codec) and container_format so a
        # hook keyed on either dimension can decide correctly.
        assert seen[-1] == {"id": "hevc", "container_format": "mp4"}

    def test_allows_when_hook_allows_codec(
        self,
        griptape_nodes: GriptapeNodes,
        monkeypatch: pytest.MonkeyPatch,
        _project_workspace: Path,  # noqa: PT019
    ) -> None:
        monkeypatch.setattr(
            VideoArtifactProvider,
            "_run_ffprobe",
            classmethod(lambda cls, source_path: _canned_probe("h264")),  # noqa: ARG005
        )

        def deny_hevc(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("id") == "hevc":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="hevc is not licensed."),))
            return None

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]
        griptape_nodes.EventManager().add_authorization_hook(deny_hevc)
        try:
            denial = provider.check_write_permission(b"any mp4 payload", "mp4", file_name="clip.mp4")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_hevc)

        assert denial is None

    def test_fails_closed_when_ffprobe_returns_none(
        self,
        griptape_nodes: GriptapeNodes,
        monkeypatch: pytest.MonkeyPatch,
        _project_workspace: Path,  # noqa: PT019
    ) -> None:
        # ffprobe unavailable / hard failure: we cannot identify the codec.
        # For a gate that exists to protect against legally-encumbered codecs,
        # defaulting to permit would silently bless disallowed writes whenever
        # ffprobe hiccups. Fail closed with a synthetic denial instead.
        monkeypatch.setattr(VideoArtifactProvider, "_run_ffprobe", classmethod(lambda cls, source_path: None))  # noqa: ARG005

        hook_fired: list[bool] = []

        def sentinel_hook(checkpoint: object) -> CheckpointDenial | None:  # noqa: ARG001
            hook_fired.append(True)
            return CheckpointDenial(failures=(CheckpointFailure(detail="should not fire"),))

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]
        griptape_nodes.EventManager().add_authorization_hook(sentinel_hook)
        try:
            denial = provider.check_write_permission(b"unclassifiable", "mp4", file_name="broken.mp4")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(sentinel_hook)

        assert denial is not None
        # The synthetic denial names the destination filename so an artist
        # sees which save was blocked.
        assert "broken.mp4" in denial.reason()
        # Policy hook must NOT fire when we couldn't identify the codec --
        # we short-circuited to a synthetic denial before evaluation.
        assert hook_fired == []

    def test_fails_closed_when_probe_has_no_video_stream(
        self,
        griptape_nodes: GriptapeNodes,
        monkeypatch: pytest.MonkeyPatch,
        _project_workspace: Path,  # noqa: PT019
    ) -> None:
        # ffprobe ran but found no video stream (audio-only container / broken
        # file). Same fail-closed semantics as an ffprobe failure.
        monkeypatch.setattr(
            VideoArtifactProvider,
            "_run_ffprobe",
            classmethod(lambda cls, source_path: {"streams": [{"codec_type": "audio", "codec_name": "aac"}]}),  # noqa: ARG005
        )

        hook_fired: list[bool] = []

        def sentinel_hook(checkpoint: object) -> CheckpointDenial | None:  # noqa: ARG001
            hook_fired.append(True)
            return CheckpointDenial(failures=(CheckpointFailure(detail="should not fire"),))

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]
        griptape_nodes.EventManager().add_authorization_hook(sentinel_hook)
        try:
            denial = provider.check_write_permission(b"audio-only", "mp4", file_name="audio_only.mp4")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(sentinel_hook)

        assert denial is not None
        assert "audio_only.mp4" in denial.reason()
        assert hook_fired == []


class TestVideoReadPermission:
    """``check_read_permission`` mirrors write but skips the byte-staging step."""

    def test_denies_when_hook_denies_codec(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            VideoArtifactProvider,
            "_run_ffprobe",
            classmethod(lambda cls, source_path: _canned_probe("hevc")),  # noqa: ARG005
        )

        seen: list[dict[str, Any]] = []

        def deny_hevc(checkpoint: object) -> CheckpointDenial | None:
            seen.append(dict(checkpoint.attributes))  # type: ignore[attr-defined]
            if checkpoint.attributes.get("id") == "hevc":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="hevc is not licensed."),))
            return None

        # A real path so the container-format extraction (Path(source_path).suffix)
        # produces a non-"unknown" value the hook can see.
        source_path = tmp_path / "clip.mp4"
        source_path.write_bytes(b"stand-in; ffprobe is mocked")

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]
        griptape_nodes.EventManager().add_authorization_hook(deny_hevc)
        try:
            denial = provider.check_read_permission(str(source_path))
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_hevc)

        assert denial is not None
        assert seen[-1] == {"id": "hevc", "container_format": "mp4"}

    def test_container_format_unknown_when_path_has_no_extension(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A caller can pass a path with no extension (e.g. a temp file). The
        # provider still probes the bytes for codec but tags the checkpoint
        # with container_format="unknown" so a hook keying strictly on the
        # container knows the sniff couldn't determine one.
        monkeypatch.setattr(
            VideoArtifactProvider,
            "_run_ffprobe",
            classmethod(lambda cls, source_path: _canned_probe("h264")),  # noqa: ARG005
        )

        seen: list[dict[str, Any]] = []

        def record(checkpoint: object) -> None:
            seen.append(dict(checkpoint.attributes))  # type: ignore[attr-defined]

        source_path = tmp_path / "no_extension"
        source_path.write_bytes(b"stand-in")

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]
        griptape_nodes.EventManager().add_authorization_hook(record)
        try:
            provider.check_read_permission(str(source_path))
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(record)

        assert seen[-1]["container_format"] == "unknown"

    def test_fails_closed_when_ffprobe_returns_none(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # ffprobe unavailable: read cannot be verified as compliant. Fail
        # closed with a synthetic denial that names the file the artist
        # tried to load.
        monkeypatch.setattr(VideoArtifactProvider, "_run_ffprobe", classmethod(lambda cls, source_path: None))  # noqa: ARG005

        hook_fired: list[bool] = []

        def sentinel_hook(checkpoint: object) -> CheckpointDenial | None:  # noqa: ARG001
            hook_fired.append(True)
            return CheckpointDenial(failures=(CheckpointFailure(detail="should not fire"),))

        source_path = tmp_path / "clip.mp4"
        source_path.write_bytes(b"stand-in")

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]
        griptape_nodes.EventManager().add_authorization_hook(sentinel_hook)
        try:
            denial = provider.check_read_permission(str(source_path))
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(sentinel_hook)

        assert denial is not None
        assert "clip.mp4" in denial.reason()
        assert hook_fired == []


class TestCallerVariablesThreading:
    """Caller-supplied macro variables reach the temp filename via the vet.

    ``OSManager.on_write_file_request`` passes a MacroPath's variables through
    to ``check_write_permission`` as ``caller_variables``. The video provider
    merges those with its own required overrides (uuid ``file_name_base`` +
    sniffed ``file_extension``) before issuing ``WriteTempFileRequest``, so
    the temp filename carries origin context (``node_name``) without
    sacrificing collision safety.
    """

    def test_caller_variables_flow_into_temp_filename(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _project_workspace: Path,  # noqa: PT019
    ) -> None:
        # The default SAVE_TEMP_FILE macro is
        # ``{temp}/{node_name?:_}{file_name_base}{_index?:03}.{file_extension}``.
        # Passing ``node_name`` should land in the staged filename before the
        # uuid stem (the ``?:_`` suffixes it with an underscore).
        captured_paths: list[str] = []

        def fake_ffprobe(cls, source_path: str) -> dict | None:  # noqa: ANN001, ARG001
            captured_paths.append(source_path)
            return _canned_probe("h264")

        monkeypatch.setattr(VideoArtifactProvider, "_run_ffprobe", classmethod(fake_ffprobe))

        VideoArtifactProvider._extract_codec_via_staging(
            b"payload",
            "mp4",
            file_name="clip.mp4",
            caller_variables={"node_name": "MyVideoNode"},
        )

        assert len(captured_paths) == 1
        staged_name = Path(captured_paths[0]).name
        # ``node_name`` should be in the filename for observability.
        assert "MyVideoNode" in staged_name
        # And ``file_extension=mp4`` (the vet override) wins over any suffix.
        assert staged_name.endswith(".mp4")

    def test_vet_overrides_caller_file_name_base_for_collision_safety(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _project_workspace: Path,  # noqa: PT019
    ) -> None:
        """A caller-supplied ``file_name_base`` must not defeat the vet's uuid.

        If a caller happens to bind ``file_name_base`` in their MacroPath and
        two callers use the same value, the vet's uuid override ensures they
        still stage to distinct paths.
        """
        captured_paths: list[str] = []

        def fake_ffprobe(cls, source_path: str) -> dict | None:  # noqa: ANN001, ARG001
            captured_paths.append(source_path)
            return _canned_probe("h264")

        monkeypatch.setattr(VideoArtifactProvider, "_run_ffprobe", classmethod(fake_ffprobe))

        colliding: MacroVariables = {"file_name_base": "identical-stem"}
        VideoArtifactProvider._extract_codec_via_staging(
            b"payload-a",
            "mp4",
            file_name="a.mp4",
            caller_variables=colliding,
        )
        VideoArtifactProvider._extract_codec_via_staging(
            b"payload-b",
            "mp4",
            file_name="b.mp4",
            caller_variables=colliding,
        )

        # Two calls with the same caller-supplied ``file_name_base`` still
        # produce two distinct staged paths -- the uuid override wins.
        assert len(captured_paths) == 2  # noqa: PLR2004
        assert captured_paths[0] != captured_paths[1]

    def test_vet_overrides_caller_file_extension(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _project_workspace: Path,  # noqa: PT019
    ) -> None:
        """Caller can't lie to ffprobe about the container via ``file_extension``.

        Even if the caller supplies ``file_extension="wav"``, the vet's
        sniffed-container override wins so the staged filename ends in
        ``.mp4`` -- letting ffprobe's extension-based dispatch pick the right
        demuxer.
        """
        captured_paths: list[str] = []

        def fake_ffprobe(cls, source_path: str) -> dict | None:  # noqa: ANN001, ARG001
            captured_paths.append(source_path)
            return _canned_probe("h264")

        monkeypatch.setattr(VideoArtifactProvider, "_run_ffprobe", classmethod(fake_ffprobe))

        VideoArtifactProvider._extract_codec_via_staging(
            b"payload",
            "mp4",
            file_name="clip.mp4",
            caller_variables={"file_extension": "wav"},  # Deliberately wrong.
        )

        assert len(captured_paths) == 1
        # Sniffed container wins.
        assert captured_paths[0].endswith(".mp4")


class TestStagingCleanup:
    """Zero-out + delete cleanup runs on every path (approve, deny, ffprobe fail, hook absent).

    The temp file exists between the stage and the finally block; after
    _extract_codec_via_staging returns, whatever landed on disk during
    staging must either be gone (delete succeeded) or all zeros (delete
    failed but zero-out ran). Either way, no leftover file usable as video.
    """

    def test_staged_file_is_removed_after_successful_probe(
        self,
        griptape_nodes: GriptapeNodes,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
        _project_workspace: Path,  # noqa: PT019
    ) -> None:
        captured_paths: list[str] = []

        def fake_ffprobe(cls, source_path: str) -> dict | None:  # noqa: ANN001, ARG001
            # File must exist at probe time so this test also covers the
            # invariant that we don't delete BEFORE probing.
            assert Path(source_path).exists()
            captured_paths.append(source_path)
            return _canned_probe("h264")

        monkeypatch.setattr(VideoArtifactProvider, "_run_ffprobe", classmethod(fake_ffprobe))

        codec = VideoArtifactProvider._extract_codec_via_staging(
            b"payload", "mp4", file_name="probe.mp4", caller_variables=None
        )

        assert codec == "h264"
        assert len(captured_paths) == 1
        assert not Path(captured_paths[0]).exists()

    def test_staged_file_is_removed_after_ffprobe_failure(
        self,
        griptape_nodes: GriptapeNodes,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
        _project_workspace: Path,  # noqa: PT019
    ) -> None:
        captured_paths: list[str] = []

        def failing_ffprobe(cls, source_path: str) -> dict | None:  # noqa: ANN001, ARG001
            captured_paths.append(source_path)
            return None  # ffprobe unavailable / failed

        monkeypatch.setattr(VideoArtifactProvider, "_run_ffprobe", classmethod(failing_ffprobe))

        codec = VideoArtifactProvider._extract_codec_via_staging(
            b"payload", "mp4", file_name="probe.mp4", caller_variables=None
        )

        assert codec is None
        assert len(captured_paths) == 1
        assert not Path(captured_paths[0]).exists()

    def test_staged_file_is_zeroed_out_when_delete_fails(
        self,
        griptape_nodes: GriptapeNodes,
        monkeypatch: pytest.MonkeyPatch,
        _project_workspace: Path,  # noqa: PT019
    ) -> None:
        """Belt-and-suspenders: if the delete somehow doesn't remove the file, its bytes are zeros.

        Simulate a delete failure by replacing the DeleteFileRequest handler
        in the EventManager's registry with a no-op that returns Success but
        does not touch disk. Zero-out must have already run before the delete
        attempt, so the file left on disk is all null bytes.
        """
        from griptape_nodes.retained_mode.events.os_events import (
            DeleteFileRequest,
            DeleteFileResultSuccess,
            DeletionOutcome,
        )

        captured_paths: list[str] = []

        def fake_ffprobe(cls, source_path: str) -> dict | None:  # noqa: ANN001, ARG001
            captured_paths.append(source_path)
            return _canned_probe("h264")

        monkeypatch.setattr(VideoArtifactProvider, "_run_ffprobe", classmethod(fake_ffprobe))

        def noop_delete_handler(request: DeleteFileRequest) -> DeleteFileResultSuccess:  # noqa: ARG001
            return DeleteFileResultSuccess(
                deleted_path="",
                was_directory=False,
                deleted_paths=[],
                outcome=DeletionOutcome.PERMANENTLY_DELETED,
                result_details="delete skipped (test)",
            )

        # Swap in the noop DeleteFile handler by rebinding the EventManager's
        # request-type registry entry. ``monkeypatch`` snapshots the dict
        # and restores it at test teardown, so no cleanup needed here.
        event_manager = griptape_nodes.EventManager()
        registry = event_manager._request_type_to_manager
        monkeypatch.setitem(registry, DeleteFileRequest, noop_delete_handler)

        original_payload = b"some non-zero payload bytes"
        codec = VideoArtifactProvider._extract_codec_via_staging(
            original_payload, "mp4", file_name="probe.mp4", caller_variables=None
        )

        assert codec == "h264"
        assert len(captured_paths) == 1
        # File is still there (our noop delete kept it) -- but zero-out ran
        # first so its contents are all null bytes, matching the original size.
        leftover = Path(captured_paths[0])
        assert leftover.exists(), "test setup: fake delete should have kept the file"
        contents = leftover.read_bytes()
        assert contents == b"\x00" * len(original_payload)
