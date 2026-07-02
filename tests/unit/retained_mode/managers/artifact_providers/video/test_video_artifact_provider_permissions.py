"""Coverage for VideoArtifactProvider's codec-permission hooks.

The read/write hooks shell out to ffprobe against real video bytes, which is
too expensive and environment-dependent for unit tests. Every test in this
module monkeypatches ``_run_ffprobe`` to inject a canned payload, so we
exercise the checkpoint assembly + hook-chain evaluation without ever
spawning a subprocess.
"""

from typing import Any

import pytest

from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.artifact_providers.video.video_artifact_provider import (
    VideoArtifactProvider,
)
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    CheckpointDenial,
    CheckpointFailure,
)


def _canned_probe(codec: str) -> dict[str, Any]:
    """Minimal ffprobe payload with a single video stream carrying ``codec``."""
    return {"streams": [{"codec_type": "video", "codec_name": codec}]}


class TestVideoWritePermission:
    """``check_write_permission`` spools bytes to a temp file, probes, and evaluates the checkpoint."""

    def test_denies_when_hook_denies_codec(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
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
            denial = provider.check_write_permission(b"any mp4 payload", "mp4")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_hevc)

        assert denial is not None
        # The checkpoint must carry both id (codec) and container_format so a
        # hook keyed on either dimension can decide correctly.
        assert seen[-1] == {"id": "hevc", "container_format": "mp4"}

    def test_allows_when_hook_allows_codec(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
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
            denial = provider.check_write_permission(b"any mp4 payload", "mp4")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_hevc)

        assert denial is None

    def test_falls_open_when_ffprobe_returns_none(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ffprobe unavailable / hard failure: we cannot identify the codec, so
        # we cannot make a permission decision. The hook must not fire.
        monkeypatch.setattr(VideoArtifactProvider, "_run_ffprobe", classmethod(lambda cls, source_path: None))  # noqa: ARG005

        hook_fired: list[bool] = []

        def sentinel_hook(checkpoint: object) -> CheckpointDenial | None:  # noqa: ARG001
            hook_fired.append(True)
            return CheckpointDenial(failures=(CheckpointFailure(detail="should not fire"),))

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]
        griptape_nodes.EventManager().add_authorization_hook(sentinel_hook)
        try:
            denial = provider.check_write_permission(b"unclassifiable", "mp4")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(sentinel_hook)

        assert denial is None
        assert hook_fired == []

    def test_falls_open_when_probe_has_no_video_stream(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ffprobe ran but found no video stream (audio-only container / broken
        # file). Same fall-open semantics as an ffprobe failure.
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
            denial = provider.check_write_permission(b"audio-only", "mp4")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(sentinel_hook)

        assert denial is None
        assert hook_fired == []


class TestVideoReadPermission:
    """``check_read_permission`` mirrors write but skips the byte-spooling step."""

    def test_denies_when_hook_denies_codec(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
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
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
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

    def test_falls_open_when_ffprobe_returns_none(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
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

        assert denial is None
        assert hook_fired == []


class TestExtractCodecFromBytes:
    """The write-side path spools bytes to a temp file and cleans up after probing."""

    def test_temp_file_is_removed_after_probe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Capture the temp path the spooler picks, then verify it's gone after
        # extraction returns -- whether or not ffprobe succeeded. Without the
        # ``finally`` cleanup a crashing/failing ffprobe would leak files.
        captured_paths: list[str] = []

        def fake_ffprobe(cls, source_path: str) -> dict | None:  # noqa: ANN001, ARG001
            captured_paths.append(source_path)
            return _canned_probe("h264")

        monkeypatch.setattr(VideoArtifactProvider, "_run_ffprobe", classmethod(fake_ffprobe))

        codec = VideoArtifactProvider._extract_codec_from_bytes(b"payload", "mp4")

        assert codec == "h264"
        assert len(captured_paths) == 1
        # The temp file existed while ffprobe ran (fake_ffprobe was called
        # with a real path); it must NOT exist after extraction returns.
        from pathlib import Path

        assert not Path(captured_paths[0]).exists()

    def test_temp_file_is_removed_even_when_ffprobe_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured_paths: list[str] = []

        def failing_ffprobe(cls, source_path: str) -> dict | None:  # noqa: ANN001, ARG001
            captured_paths.append(source_path)
            return None  # ffprobe unavailable / failed

        monkeypatch.setattr(VideoArtifactProvider, "_run_ffprobe", classmethod(failing_ffprobe))

        codec = VideoArtifactProvider._extract_codec_from_bytes(b"payload", "mp4")

        assert codec is None
        assert len(captured_paths) == 1
        from pathlib import Path

        assert not Path(captured_paths[0]).exists()
