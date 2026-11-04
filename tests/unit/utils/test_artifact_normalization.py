"""Tests for `normalize_artifact_input` and `normalize_artifact_list`."""

import base64
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from griptape.artifacts import AudioUrlArtifact, ImageArtifact, ImageUrlArtifact
from griptape.artifacts.video_url_artifact import VideoUrlArtifact

from griptape_nodes.utils import artifact_normalization
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

# Long enough that, read as a path under the workspace, it exceeds the OS file name limit.
LARGE_PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(bytes(5000)).decode()


@pytest.fixture
def project_macros(monkeypatch: pytest.MonkeyPatch) -> dict[Path, str]:
    """Absolute paths the project maps to macro paths. Empty means no project directory matches."""
    macros: dict[Path, str] = {}
    monkeypatch.setattr(artifact_normalization.project_file, "_attempt_map_to_project", macros.get)
    return macros


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, project_macros: dict[Path, str]) -> Path:  # noqa: ARG001
    """Give path resolution a workspace, so relative values are looked up on disk.

    The engine has no static files manager: normalization must not need a static server.
    """
    engine = MagicMock()
    engine.config_manager.workspace_path = tmp_path
    del engine.static_files_manager
    monkeypatch.setattr(artifact_normalization, "current_engine", lambda: engine)
    return tmp_path


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


@pytest.mark.usefixtures("workspace")
def test_data_uri_dict_becomes_the_artifact() -> None:
    """A data URI is not a path, but it is a value the declared type can hold.

    It is looked up on disk as a workspace-relative path first, and a real image's worth of
    base64 is longer than the OS allows a file name to be. That lookup must answer "not a
    file" rather than raise, so the value is wrapped like any other unresolvable one.
    """
    data_uri_dict = {"type": "ImageUrlArtifact", "value": LARGE_PNG_DATA_URI, "width": 64}

    result = normalize_artifact_input(dict(data_uri_dict), ImageUrlArtifact)

    assert isinstance(result, ImageUrlArtifact)
    assert result.value == LARGE_PNG_DATA_URI


@pytest.mark.usefixtures("workspace")
def test_data_uri_string_passes_through() -> None:
    """The string branch hands back what it cannot resolve, a data URI included."""
    assert normalize_artifact_input(LARGE_PNG_DATA_URI, ImageUrlArtifact) == LARGE_PNG_DATA_URI


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


@pytest.mark.parametrize("relative", [True, False], ids=["relative", "absolute"])
def test_workspace_path_becomes_a_workspace_dir_macro_path(workspace: Path, relative: bool) -> None:  # noqa: FBT001
    """A workspace file outside every project directory is stored under `{workspace_dir}`."""
    file_path = workspace / "renders" / "image.jpg"
    file_path.parent.mkdir()
    file_path.write_bytes(b"data")
    artifact_input = "renders/image.jpg" if relative else str(file_path)

    result = normalize_artifact_input(artifact_input, ImageUrlArtifact)

    assert isinstance(result, ImageUrlArtifact)
    assert result.value == "{workspace_dir}/renders/image.jpg"


def test_workspace_path_in_a_project_directory_uses_its_macro(workspace: Path, project_macros: dict[Path, str]) -> None:
    """A workspace file inside a project directory is stored as that directory's macro path."""
    file_path = workspace / "inputs" / "image.jpg"
    file_path.parent.mkdir()
    file_path.write_bytes(b"data")
    project_macros[file_path.resolve()] = "{inputs}/image.jpg"

    result = normalize_artifact_input(str(file_path), ImageUrlArtifact)

    assert isinstance(result, ImageUrlArtifact)
    assert result.value == "{inputs}/image.jpg"


@pytest.mark.usefixtures("workspace")
def test_project_directory_outside_the_workspace_uses_its_macro(
    tmp_path_factory: pytest.TempPathFactory, project_macros: dict[Path, str]
) -> None:
    """A project directory outside the workspace still gets its macro path."""
    file_path = tmp_path_factory.mktemp("project") / "image.jpg"
    file_path.write_bytes(b"data")
    project_macros[file_path.resolve()] = "{inputs}/image.jpg"

    result = normalize_artifact_input(str(file_path), ImageUrlArtifact)

    assert isinstance(result, ImageUrlArtifact)
    assert result.value == "{inputs}/image.jpg"


@pytest.mark.usefixtures("workspace")
def test_external_path_is_kept(tmp_path_factory: pytest.TempPathFactory) -> None:
    """A file outside the workspace keeps its own path."""
    file_path = tmp_path_factory.mktemp("external") / "image.jpg"
    file_path.write_bytes(b"data")

    result = normalize_artifact_input(str(file_path), ImageUrlArtifact)

    assert isinstance(result, ImageUrlArtifact)
    assert result.value == str(file_path)


def test_workspace_static_server_url_becomes_its_path(workspace: Path) -> None:
    """A static server URL saved by an earlier session becomes a macro path for the file it serves."""
    file_path = workspace / "staticfiles" / "image.jpg"
    file_path.parent.mkdir()
    file_path.write_bytes(b"data")

    result = normalize_artifact_input(
        "http://localhost:53999/workspace/staticfiles/image.jpg?v=stale", ImageUrlArtifact
    )

    assert isinstance(result, ImageUrlArtifact)
    assert result.value == "{workspace_dir}/staticfiles/image.jpg"


@pytest.mark.usefixtures("workspace")
def test_external_static_server_url_becomes_its_path(tmp_path_factory: pytest.TempPathFactory) -> None:
    """An `/external/` URL becomes the path of the file outside the workspace it serves."""
    file_path = tmp_path_factory.mktemp("external") / "image.jpg"
    file_path.write_bytes(b"data")
    url = f"http://localhost:8124/external/{file_path.as_posix().removeprefix('/')}?v=1"

    result = normalize_artifact_input(url, ImageUrlArtifact)

    assert isinstance(result, ImageUrlArtifact)
    assert result.value == str(file_path)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://localhost:8124/workspace/staticfiles/missing.jpg?v=1", id="missing-workspace-file"),
        pytest.param("http://localhost:8124/external/nowhere/missing.jpg", id="missing-external-file"),
        pytest.param("http://localhost:8124/api/health", id="not-a-static-file-url"),
        pytest.param("https://example.com/image.jpg", id="remote"),
    ],
)
@pytest.mark.usefixtures("workspace")
def test_url_that_names_no_local_file_is_wrapped_as_is(url: str) -> None:
    """A URL with no local file behind it is kept verbatim."""
    result = normalize_artifact_input(url, ImageUrlArtifact)

    assert isinstance(result, ImageUrlArtifact)
    assert result.value == url


@pytest.mark.usefixtures("workspace")
def test_missing_path_is_returned_unchanged() -> None:
    """A path to nothing is handed back for the node's own validation to report."""
    assert normalize_artifact_input("missing.jpg", ImageUrlArtifact) == "missing.jpg"
