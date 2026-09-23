"""Tests for the serialized-artifact-dict branch of `normalize_artifact_input`.

The dict branch reuses `_normalize_string_input`, so these only cover inputs it can
resolve without a configured engine: HTTP URLs, and paths it fails to resolve. Workspace
path resolution and static-storage upload need a configured engine and are not covered.
"""

from typing import Any

import pytest
from griptape.artifacts import AudioUrlArtifact, ImageArtifact, ImageUrlArtifact
from griptape.artifacts.video_url_artifact import VideoUrlArtifact

from griptape_nodes.utils.artifact_normalization import normalize_artifact_input, normalize_artifact_list

# The shape the editor sends for a stored video: an artifact dict plus the display
# metadata it tracks alongside it.
EDITOR_VIDEO_DICT = {
    "type": "VideoUrlArtifact",
    "value": "http://example.com/clip.mp4",
    "name": "clip.mp4",
    "width": 1920,
    "height": 1080,
    "duration": 12,
}


@pytest.mark.parametrize(
    ("artifact_dict", "artifact_type"),
    [
        pytest.param(EDITOR_VIDEO_DICT, VideoUrlArtifact, id="video-with-metadata"),
        pytest.param(
            {"type": "VideoUrlArtifact", "value": "http://example.com/clip.mp4"}, VideoUrlArtifact, id="video"
        ),
        pytest.param(
            {"type": "ImageUrlArtifact", "value": "http://example.com/frame.png", "width": 64},
            ImageUrlArtifact,
            id="image-with-metadata",
        ),
        pytest.param(
            {"type": "AudioUrlArtifact", "value": "http://example.com/take.mp3", "duration": 3},
            AudioUrlArtifact,
            id="audio-with-metadata",
        ),
    ],
)
def test_artifact_dict_becomes_the_artifact(artifact_dict: dict, artifact_type: type) -> None:
    """A serialized artifact dict becomes the artifact, display metadata and all.

    The extra keys are why this branch exists: the editor sends `width` / `height` /
    `duration` alongside the value, and only the value is needed to build the artifact.
    """
    result = normalize_artifact_input(dict(artifact_dict), artifact_type)

    assert isinstance(result, artifact_type)
    assert result.value == artifact_dict["value"]


def test_unresolvable_path_still_becomes_an_artifact() -> None:
    """A dict must never degrade into a bare string.

    `_normalize_string_input` returns its own input when a path cannot be resolved or
    uploaded — a project macro path, or a file outside the workspace. The dict already
    declared its artifact type, so the value is wrapped in that type rather than handed
    back as a string; callers that received a dict expect an artifact-shaped value.
    """
    macro_path_dict = {"type": "VideoUrlArtifact", "value": "{inputs}/clip.mp4", "duration": 6}

    result = normalize_artifact_input(dict(macro_path_dict), VideoUrlArtifact)

    assert isinstance(result, VideoUrlArtifact)
    assert result.value == "{inputs}/clip.mp4"


def test_dict_for_another_artifact_type_passes_through() -> None:
    """An image dict on a video parameter is left alone.

    The declared type is what distinguishes a path from a payload, so a dict naming a
    different type is not safe to unwrap. Handing back the input keeps the node's own
    validation responsible for reporting the mismatch.
    """
    image_dict = {"type": "ImageUrlArtifact", "value": "http://example.com/frame.png"}

    result = normalize_artifact_input(dict(image_dict), VideoUrlArtifact)

    assert result == image_dict


def test_raw_artifact_dict_passes_through() -> None:
    """Documents a gap rather than asserting a desirable outcome.

    A raw `ImageArtifact` holds base64 bytes in `value`, not a path, so it cannot go
    through the string branch — unwrapping it would produce an artifact whose "URL" is a
    base64 payload. Such dicts are left untouched, exactly as before this branch existed.
    Supporting them means reconstructing the artifact from its schema, which is a
    different job from normalizing a path.
    """
    raw_dict = ImageArtifact(b"\x89PNG", format="png", width=64, height=64).to_dict()

    result = normalize_artifact_input(dict(raw_dict), ImageUrlArtifact, accepted_types=(ImageArtifact,))

    assert result == raw_dict


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({}, id="empty"),
        pytest.param({"foo": "bar"}, id="no-type-key"),
        pytest.param({"type": "NotARealArtifactType", "value": "whatever"}, id="unknown-type"),
        pytest.param({"type": "video/mp4", "value": "AAAA"}, id="mime-type-not-a-class"),
        pytest.param({"type": "VideoUrlArtifact"}, id="no-value"),
        pytest.param({"type": "VideoUrlArtifact", "value": ""}, id="empty-value"),
    ],
)
def test_dict_that_is_not_a_usable_artifact_dict_passes_through(value: dict) -> None:
    """A parameter accepting any input type can be handed a dict that is not an artifact at all."""
    result = normalize_artifact_input(dict(value), VideoUrlArtifact)

    assert result == value


def test_existing_artifact_is_returned_unchanged() -> None:
    """An artifact of the requested type is handed back as the same object."""
    artifact = VideoUrlArtifact("http://example.com/clip.mp4")

    assert normalize_artifact_input(artifact, VideoUrlArtifact) is artifact


@pytest.mark.parametrize("value", [None, 3, True], ids=["none", "int", "bool"])
def test_non_dict_non_string_values_pass_through(value: Any) -> None:
    """There is nothing to normalize in a scalar, so it survives untouched."""
    assert normalize_artifact_input(value, VideoUrlArtifact) is value


def test_list_of_artifact_dicts_is_normalized_element_wise() -> None:
    """List parameters get the same treatment per element, mixed contents included."""
    incoming = [dict(EDITOR_VIDEO_DICT), {"foo": "bar"}]

    result = normalize_artifact_list(incoming, VideoUrlArtifact)

    assert isinstance(result[0], VideoUrlArtifact)
    assert result[1] == {"foo": "bar"}
