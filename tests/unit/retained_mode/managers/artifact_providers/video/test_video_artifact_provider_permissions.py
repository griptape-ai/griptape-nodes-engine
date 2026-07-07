"""Coverage for VideoArtifactProvider's codec-permission hooks.

The read/write hooks shell out to ffprobe against real video bytes, which is
too expensive and environment-dependent for unit tests. Every test in this
module monkeypatches ``_run_ffprobe`` to inject a canned payload, so we
exercise the checkpoint assembly + hook-chain evaluation without ever
spawning a subprocess.

The write hook is now path-based: OSManager stages bytes for the provider,
the provider reads a filesystem path. That means these tests can pass any
stand-in path they like to ``check_write_format_from_path`` and never need
to activate a project template just to exercise the provider.
"""

from pathlib import Path
from typing import Any

import pytest

from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import WriteVettingPolicy
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


class TestVideoWriteVettingPolicy:
    """The video provider declares FROM_PATH so OSManager stages before calling it."""

    def test_policy_is_from_path(self) -> None:
        assert VideoArtifactProvider.get_write_vetting_policy() is WriteVettingPolicy.FROM_PATH


class TestVideoCheckWriteFormatFromPath:
    """``check_write_format_from_path`` probes a caller-provided path and evaluates the checkpoint."""

    def test_denies_when_hook_denies_codec(
        self,
        griptape_nodes: GriptapeNodes,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
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

        staged = tmp_path / "staged.mp4"
        staged.write_bytes(b"stand-in; ffprobe is mocked")

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]
        griptape_nodes.EventManager().add_authorization_hook(deny_hevc)
        try:
            denial = provider.check_write_format_from_path(str(staged), "mp4")
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
        tmp_path: Path,
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

        staged = tmp_path / "staged.mp4"
        staged.write_bytes(b"stand-in; ffprobe is mocked")

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]
        griptape_nodes.EventManager().add_authorization_hook(deny_hevc)
        try:
            denial = provider.check_write_format_from_path(str(staged), "mp4")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(deny_hevc)

        assert denial is None

    def test_fails_closed_when_ffprobe_returns_none(
        self,
        griptape_nodes: GriptapeNodes,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
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

        staged = tmp_path / "broken.mp4"
        staged.write_bytes(b"unclassifiable")

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]
        griptape_nodes.EventManager().add_authorization_hook(sentinel_hook)
        try:
            denial = provider.check_write_format_from_path(str(staged), "mp4")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(sentinel_hook)

        assert denial is not None
        # The denial detail is generic -- OSManager wraps it with the caller's
        # destination filename. The provider is intentionally file-name-agnostic.
        assert "video codec could not be verified" in denial.reason().lower()
        # Policy hook must NOT fire when we couldn't identify the codec --
        # we short-circuited to a synthetic denial before evaluation.
        assert hook_fired == []

    def test_fails_closed_when_probe_has_no_video_stream(
        self,
        griptape_nodes: GriptapeNodes,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
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

        staged = tmp_path / "audio_only.mp4"
        staged.write_bytes(b"audio-only")

        provider = VideoArtifactProvider(registry=None)  # type: ignore[arg-type]
        griptape_nodes.EventManager().add_authorization_hook(sentinel_hook)
        try:
            denial = provider.check_write_format_from_path(str(staged), "mp4")
        finally:
            griptape_nodes.EventManager().remove_authorization_hook(sentinel_hook)

        assert denial is not None
        assert "video codec could not be verified" in denial.reason().lower()
        assert hook_fired == []


class TestVideoReadPermission:
    """``check_read_permission`` mirrors the from-path write path (both funnel through _check_codec)."""

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
        # closed with a synthetic denial. The provider's denial detail is
        # generic -- the read caller (library code) is responsible for
        # framing "which file" if it wants to.
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
        assert "video codec could not be verified" in denial.reason().lower()
        assert hook_fired == []
