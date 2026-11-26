"""Tests for the OpenImageIO image artifact provider."""

from pathlib import Path

import pytest
from PIL import Image
from pytest_mock import MockerFixture

from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactMetadata,
)

try:
    import OpenImageIO as oiio

    from griptape_nodes.retained_mode.managers.artifact_providers.image.openimageio_artifact_provider import (
        OpenImageIOArtifactProvider,
    )
except ModuleNotFoundError as error:
    if error.name != "OpenImageIO":
        raise
    pytest.skip("OpenImageIO is optional", allow_module_level=True)


def test_source_metadata_accounts_for_exif_orientation(tmp_path: Path) -> None:
    """Report oriented dimensions and the OpenImageIO colour-space tag."""
    source = tmp_path / "phone.jpeg"
    exif = Image.Exif()
    exif[274] = 6
    with Image.new("RGB", (8, 4)) as image:
        image.save(source, exif=exif)

    metadata = OpenImageIOArtifactProvider.get_artifact_metadata(str(source))

    assert metadata == ImageArtifactMetadata(
        width=4, height=8, format="JPEG", channels=3, color_space="srgb_rec709_scene", file_size=source.stat().st_size
    )


@pytest.mark.parametrize("exists", [False, True], ids=["missing", "unreadable"])
def test_openimageio_metadata_unavailable_for_invalid_file(tmp_path: Path, *, exists: bool) -> None:
    """Missing files and invalid headers return no metadata."""
    source = tmp_path / "invalid.png"
    if exists:
        source.write_bytes(b"not a PNG")

    assert OpenImageIOArtifactProvider.get_artifact_metadata(str(source)) is None


def test_openimageio_metadata_uses_header_only(tmp_path: Path, mocker: MockerFixture) -> None:
    """Read only the header, close the input, and report absent colour information."""
    source = tmp_path / "header.png"
    source.touch()
    reader = mocker.Mock(spec=oiio.ImageInput)
    reader.spec.return_value = oiio.ImageSpec(8, 4, 1, oiio.UINT8)
    reader.format_name.return_value = "png"
    mocker.patch.object(oiio.ImageInput, "open", return_value=reader)

    metadata = OpenImageIOArtifactProvider.get_artifact_metadata(str(source))

    assert metadata == ImageArtifactMetadata(
        width=8, height=4, format="PNG", channels=1, color_space="Unknown", file_size=0
    )
    assert reader.mock_calls == [mocker.call.spec(), mocker.call.format_name(), mocker.call.close()]
