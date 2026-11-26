"""Tests for the Pillow image artifact provider."""

from pathlib import Path

from PIL import Image

from griptape_nodes.retained_mode.managers.artifact_providers.image.image_artifact_provider import (
    ImageArtifactMetadata,
    ImageArtifactProvider,
)


def test_source_metadata_accounts_for_exif_orientation(tmp_path: Path) -> None:
    """Report oriented dimensions and the Pillow mode as colour space."""
    source = tmp_path / "phone.jpeg"
    exif = Image.Exif()
    exif[274] = 6
    with Image.new("RGB", (8, 4)) as image:
        image.save(source, exif=exif)

    metadata = ImageArtifactProvider.get_artifact_metadata(str(source))

    assert metadata == ImageArtifactMetadata(
        width=4, height=8, format="JPEG", channels=3, color_space="RGB", file_size=source.stat().st_size
    )
