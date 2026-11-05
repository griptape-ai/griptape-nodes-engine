# ruff: noqa: S108

"""Tests for reducing a media parameter value to one string.

``coerce_media_url_or_data_uri`` must pass HTTP(S) URLs, ``data:`` URIs, project macro paths like
``{inputs}/foo.png``, and plain filesystem paths through without wrapping them as raw base64. Wrapping
is only applied to artifact ``.base64`` payloads (which are genuinely raw bytes) or to serialized
``<Kind>Artifact`` dicts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from griptape.artifacts import ImageUrlArtifact
from griptape.artifacts.video_url_artifact import VideoUrlArtifact

from griptape_nodes.utils.media_values import MediaKind, coerce_media_url_or_data_uri

# ---------------------------------------------------------------------------
# IMAGE
# ---------------------------------------------------------------------------


class TestCoerceImage:
    kind: MediaKind = "image"

    def test_none_returns_none(self) -> None:
        assert coerce_media_url_or_data_uri(None, kind=self.kind) is None

    @pytest.mark.parametrize("value", ["", "   ", "\t\n"])
    def test_empty_string_returns_none(self, value: str) -> None:
        assert coerce_media_url_or_data_uri(value, kind=self.kind) is None

    @pytest.mark.parametrize(
        "value",
        [
            "http://example.com/foo.png",
            "https://example.com/foo.png",
            "data:image/png;base64,QUJD",
            "data:image/jpeg;base64,QUJD",
        ],
    )
    def test_known_uri_strings_pass_through(self, value: str) -> None:
        assert coerce_media_url_or_data_uri(value, kind=self.kind) == value

    def test_strings_are_stripped(self) -> None:
        assert (
            coerce_media_url_or_data_uri("  https://example.com/foo.png  ", kind=self.kind)
            == "https://example.com/foo.png"
        )

    @pytest.mark.parametrize(
        "value",
        ["{inputs}/foo.png", "{outputs}/color_bars.png", "{workspace}/scene/frame.jpeg"],
    )
    def test_macro_paths_pass_through_unwrapped(self, value: str) -> None:
        assert coerce_media_url_or_data_uri(value, kind=self.kind) == value

    @pytest.mark.parametrize(
        "value",
        ["/tmp/foo.png", "/abs/path/to/image.jpg", "relative/path/foo.webp", "image.png"],
    )
    def test_filesystem_paths_pass_through_unwrapped(self, value: str) -> None:
        assert coerce_media_url_or_data_uri(value, kind=self.kind) == value

    def test_raw_string_is_not_wrapped_as_base64(self) -> None:
        assert coerce_media_url_or_data_uri("QUJDREVG", kind=self.kind) == "QUJDREVG"

    def test_image_url_artifact_with_http_url(self) -> None:
        artifact = ImageUrlArtifact("https://example.com/foo.png")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "https://example.com/foo.png"

    def test_image_url_artifact_with_macro_path(self) -> None:
        artifact = ImageUrlArtifact("{inputs}/foo.png")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "{inputs}/foo.png"

    def test_image_url_artifact_with_filesystem_path(self) -> None:
        artifact = ImageUrlArtifact("/abs/path/foo.png")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "/abs/path/foo.png"

    def test_artifact_value_is_stripped(self) -> None:
        artifact = SimpleNamespace(value="  {inputs}/foo.png  ", base64=None)
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "{inputs}/foo.png"

    def test_artifact_base64_raw_is_wrapped_as_data_uri(self) -> None:
        artifact = SimpleNamespace(value=None, base64="RAW_IMAGE_B64")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "data:image/png;base64,RAW_IMAGE_B64"

    def test_artifact_base64_already_data_uri_passes_through(self) -> None:
        artifact = SimpleNamespace(value=None, base64="data:image/jpeg;base64,RAW")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "data:image/jpeg;base64,RAW"

    def test_artifact_value_takes_precedence_over_base64(self) -> None:
        artifact = SimpleNamespace(value="https://example.com/foo.png", base64="UNUSED")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "https://example.com/foo.png"

    def test_empty_artifact_returns_none(self) -> None:
        artifact = SimpleNamespace(value="", base64="")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) is None

    def test_artifact_with_no_known_attrs_returns_none(self) -> None:
        assert coerce_media_url_or_data_uri(SimpleNamespace(), kind=self.kind) is None

    def test_artifact_with_non_string_value_falls_back_to_base64(self) -> None:
        artifact = SimpleNamespace(value=123, base64="RAW")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "data:image/png;base64,RAW"


# ---------------------------------------------------------------------------
# VIDEO
# ---------------------------------------------------------------------------


class TestCoerceVideo:
    kind: MediaKind = "video"

    def test_none_returns_none(self) -> None:
        assert coerce_media_url_or_data_uri(None, kind=self.kind) is None

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_string_returns_none(self, value: str) -> None:
        assert coerce_media_url_or_data_uri(value, kind=self.kind) is None

    @pytest.mark.parametrize(
        "value",
        [
            "http://example.com/foo.mp4",
            "https://example.com/foo.mp4",
            "data:video/mp4;base64,QUJD",
            "data:video/webm;base64,QUJD",
        ],
    )
    def test_known_uri_strings_pass_through(self, value: str) -> None:
        assert coerce_media_url_or_data_uri(value, kind=self.kind) == value

    @pytest.mark.parametrize(
        "value",
        ["{inputs}/foo.mp4", "{outputs}/render.mp4", "/tmp/clip.mov", "relative/clip.mp4"],
    )
    def test_macro_paths_and_filesystem_paths_pass_through_unwrapped(self, value: str) -> None:
        assert coerce_media_url_or_data_uri(value, kind=self.kind) == value

    def test_video_url_artifact_with_http_url(self) -> None:
        artifact = VideoUrlArtifact("https://example.com/foo.mp4")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "https://example.com/foo.mp4"

    def test_video_url_artifact_with_macro_path(self) -> None:
        artifact = VideoUrlArtifact("{inputs}/foo.mp4")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "{inputs}/foo.mp4"

    def test_artifact_base64_raw_is_wrapped_as_video_data_uri(self) -> None:
        artifact = SimpleNamespace(value=None, base64="RAW_VIDEO_B64")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "data:video/mp4;base64,RAW_VIDEO_B64"

    def test_artifact_base64_already_data_uri_passes_through(self) -> None:
        artifact = SimpleNamespace(value=None, base64="data:video/webm;base64,RAW")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "data:video/webm;base64,RAW"

    def test_strings_are_stripped(self) -> None:
        assert (
            coerce_media_url_or_data_uri("  https://example.com/foo.mp4  ", kind=self.kind)
            == "https://example.com/foo.mp4"
        )

    def test_artifact_with_no_known_attrs_returns_none(self) -> None:
        assert coerce_media_url_or_data_uri(SimpleNamespace(), kind=self.kind) is None


# ---------------------------------------------------------------------------
# AUDIO
# ---------------------------------------------------------------------------


class TestCoerceAudio:
    kind: MediaKind = "audio"

    def test_none_returns_none(self) -> None:
        assert coerce_media_url_or_data_uri(None, kind=self.kind) is None

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_string_returns_none(self, value: str) -> None:
        assert coerce_media_url_or_data_uri(value, kind=self.kind) is None

    @pytest.mark.parametrize(
        "value",
        [
            "http://example.com/foo.mp3",
            "https://example.com/foo.wav",
            "data:audio/mpeg;base64,QUJD",
            "data:audio/wav;base64,QUJD",
        ],
    )
    def test_known_uri_strings_pass_through(self, value: str) -> None:
        assert coerce_media_url_or_data_uri(value, kind=self.kind) == value

    @pytest.mark.parametrize(
        "value",
        ["{inputs}/foo.mp3", "{outputs}/voice.wav", "/tmp/clip.mp3", "relative/clip.wav"],
    )
    def test_macro_paths_and_filesystem_paths_pass_through_unwrapped(self, value: str) -> None:
        assert coerce_media_url_or_data_uri(value, kind=self.kind) == value

    def test_audio_url_artifact_with_macro_path(self) -> None:
        artifact = SimpleNamespace(value="{inputs}/voice.mp3", base64=None)
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "{inputs}/voice.mp3"

    def test_artifact_base64_raw_is_wrapped_as_audio_data_uri(self) -> None:
        artifact = SimpleNamespace(value=None, base64="RAW_AUDIO_B64")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "data:audio/mpeg;base64,RAW_AUDIO_B64"

    def test_artifact_base64_already_data_uri_passes_through(self) -> None:
        artifact = SimpleNamespace(value=None, base64="data:audio/wav;base64,RAW")
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "data:audio/wav;base64,RAW"

    def test_strings_are_stripped(self) -> None:
        assert (
            coerce_media_url_or_data_uri("  https://example.com/foo.mp3  ", kind=self.kind)
            == "https://example.com/foo.mp3"
        )

    def test_artifact_value_with_path_passes_through(self) -> None:
        artifact = SimpleNamespace(value="/abs/path/voice.wav", base64=None)
        assert coerce_media_url_or_data_uri(artifact, kind=self.kind) == "/abs/path/voice.wav"


# ---------------------------------------------------------------------------
# Serialized-dict path
# ---------------------------------------------------------------------------


class TestCoerceImageDictPath:
    """Serialized image artifact dicts must round-trip cleanly for any caller."""

    def test_dict_with_image_url_artifact_type_passes_value_through(self) -> None:
        result = coerce_media_url_or_data_uri(
            {"type": "ImageUrlArtifact", "value": "https://example.com/foo.png"}, kind="image"
        )
        assert result == "https://example.com/foo.png"

    def test_dict_with_image_url_artifact_type_passes_macro_path_through(self) -> None:
        result = coerce_media_url_or_data_uri({"type": "ImageUrlArtifact", "value": "{inputs}/foo.png"}, kind="image")
        assert result == "{inputs}/foo.png"

    def test_dict_with_image_url_artifact_type_strips_whitespace(self) -> None:
        result = coerce_media_url_or_data_uri(
            {"type": "ImageUrlArtifact", "value": "  {inputs}/foo.png  "}, kind="image"
        )
        assert result == "{inputs}/foo.png"

    def test_dict_with_url_value_passes_through_regardless_of_type(self) -> None:
        result = coerce_media_url_or_data_uri(
            {"type": "SomethingElse", "value": "https://example.com/foo.png"}, kind="image"
        )
        assert result == "https://example.com/foo.png"

    def test_dict_with_data_uri_value_passes_through(self) -> None:
        result = coerce_media_url_or_data_uri(
            {"type": "ImageArtifact", "value": "data:image/png;base64,RAW"}, kind="image"
        )
        assert result == "data:image/png;base64,RAW"

    def test_dict_with_image_artifact_wraps_with_format(self) -> None:
        result = coerce_media_url_or_data_uri({"type": "ImageArtifact", "value": "RAW", "format": "jpeg"}, kind="image")
        assert result == "data:image/jpeg;base64,RAW"

    def test_dict_with_image_artifact_defaults_to_png_format(self) -> None:
        result = coerce_media_url_or_data_uri({"type": "ImageArtifact", "value": "RAW"}, kind="image")
        assert result == "data:image/png;base64,RAW"

    def test_dict_with_image_artifact_lowercases_format(self) -> None:
        result = coerce_media_url_or_data_uri({"type": "ImageArtifact", "value": "RAW", "format": "JPEG"}, kind="image")
        assert result == "data:image/jpeg;base64,RAW"

    def test_dict_with_unknown_type_and_macro_path_passes_through(self) -> None:
        result = coerce_media_url_or_data_uri({"type": "RandomShape", "value": "{inputs}/foo.png"}, kind="image")
        assert result == "{inputs}/foo.png"

    def test_dict_with_unknown_type_and_filesystem_path_passes_through(self) -> None:
        result = coerce_media_url_or_data_uri({"type": "RandomShape", "value": "/tmp/foo.png"}, kind="image")
        assert result == "/tmp/foo.png"

    def test_dict_with_no_type_and_macro_path_passes_through(self) -> None:
        result = coerce_media_url_or_data_uri({"value": "{inputs}/foo.png"}, kind="image")
        assert result == "{inputs}/foo.png"

    def test_dict_with_empty_value_returns_none(self) -> None:
        assert coerce_media_url_or_data_uri({"type": "ImageArtifact", "value": ""}, kind="image") is None
        assert coerce_media_url_or_data_uri({"type": "ImageArtifact", "value": "   "}, kind="image") is None

    def test_dict_with_non_string_value_returns_none(self) -> None:
        assert coerce_media_url_or_data_uri({"type": "ImageArtifact", "value": 123}, kind="image") is None
        assert coerce_media_url_or_data_uri({"type": "ImageArtifact", "value": None}, kind="image") is None

    def test_dict_with_no_value_key_returns_none(self) -> None:
        assert coerce_media_url_or_data_uri({"type": "ImageArtifact"}, kind="image") is None

    def test_dict_falls_back_to_url_key_when_value_is_missing(self) -> None:
        # Some serialized URL-artifact shapes carry the URL on a top-level ``url``
        # key rather than ``value``.
        result = coerce_media_url_or_data_uri({"url": "https://example.com/foo.png"}, kind="image")
        assert result == "https://example.com/foo.png"

    def test_dict_falls_back_to_url_key_when_value_is_empty_string(self) -> None:
        result = coerce_media_url_or_data_uri({"value": "", "url": "https://example.com/foo.png"}, kind="image")
        assert result == "https://example.com/foo.png"

    def test_dict_value_takes_precedence_over_url(self) -> None:
        result = coerce_media_url_or_data_uri(
            {"value": "{inputs}/foo.png", "url": "https://example.com/unused.png"}, kind="image"
        )
        assert result == "{inputs}/foo.png"

    def test_object_with_to_dict_routes_through_dict_path(self) -> None:
        class Fake:
            def to_dict(self) -> dict:
                return {"type": "ImageUrlArtifact", "value": "{inputs}/foo.png"}

        assert coerce_media_url_or_data_uri(Fake(), kind="image") == "{inputs}/foo.png"

    def test_object_to_dict_returning_non_dict_falls_back_to_attrs(self) -> None:
        class Fake:
            def to_dict(self) -> str:
                return "not-a-dict"

            value = "{inputs}/fallback.png"
            base64 = None

        assert coerce_media_url_or_data_uri(Fake(), kind="image") == "{inputs}/fallback.png"

    def test_object_with_failing_to_dict_returns_none_safely(self) -> None:
        class Fake:
            def to_dict(self) -> dict:
                msg = "boom"
                raise RuntimeError(msg)

        assert coerce_media_url_or_data_uri(Fake(), kind="image") is None

    def test_plain_string_macro_path_passes_through(self) -> None:
        assert coerce_media_url_or_data_uri("{inputs}/foo.png", kind="image") == "{inputs}/foo.png"

    def test_object_whose_dict_names_nothing_falls_back_to_its_value(self) -> None:
        class Fake:
            value = "{inputs}/fallback.png"

            def to_dict(self) -> dict:
                return {"type": "ImageUrlArtifact", "value": ""}

        assert coerce_media_url_or_data_uri(Fake(), kind="image") == "{inputs}/fallback.png"

    def test_object_whose_value_property_raises_returns_none_safely(self) -> None:
        class Fake:
            @property
            def value(self) -> str:
                msg = "the artifact has no value loaded"
                raise RuntimeError(msg)

        assert coerce_media_url_or_data_uri(Fake(), kind="image") is None

    def test_object_whose_base64_property_raises_returns_none_safely(self) -> None:
        class Fake:
            value = None

            @property
            def base64(self) -> str:
                msg = "the artifact's bytes can't be encoded"
                raise ValueError(msg)

        assert coerce_media_url_or_data_uri(Fake(), kind="image") is None


class TestCoerceAudioDictPath:
    """The dict path also handles ``AudioArtifact`` shapes."""

    def test_dict_with_audio_artifact_wraps_with_format(self) -> None:
        result = coerce_media_url_or_data_uri({"type": "AudioArtifact", "value": "RAW", "format": "wav"}, kind="audio")
        assert result == "data:audio/wav;base64,RAW"

    def test_dict_with_audio_artifact_defaults_to_mpeg_format(self) -> None:
        result = coerce_media_url_or_data_uri({"type": "AudioArtifact", "value": "RAW"}, kind="audio")
        assert result == "data:audio/mpeg;base64,RAW"

    def test_dict_with_audio_url_artifact_passes_through(self) -> None:
        result = coerce_media_url_or_data_uri(
            {"type": "AudioUrlArtifact", "value": "https://example.com/foo.mp3"}, kind="audio"
        )
        assert result == "https://example.com/foo.mp3"
