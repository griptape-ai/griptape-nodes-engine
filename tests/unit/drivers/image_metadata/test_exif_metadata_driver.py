import json
from io import BytesIO

import pytest
from PIL import Image

from griptape_nodes.drivers.image_metadata.exif_metadata_driver import EXIF_USERCOMMENT_TAG, ExifMetadataDriver


def _image(image_format: str, user_comment: str | None = None) -> Image.Image:
    buf = BytesIO()
    exif = Image.Exif()
    if user_comment is not None:
        exif[EXIF_USERCOMMENT_TAG] = user_comment
    Image.new("RGB", (4, 4), color="red").save(buf, image_format, exif=exif)
    return Image.open(BytesIO(buf.getvalue()))


def _user_comment(data: bytes) -> dict:
    return json.loads(Image.open(BytesIO(data)).getexif()[EXIF_USERCOMMENT_TAG])


class TestExifMetadataDriver:
    @pytest.mark.parametrize("image_format", ["JPEG", "TIFF", "WEBP"])
    def test_inject_merges_over_existing_user_comment(self, image_format: str) -> None:
        driver = ExifMetadataDriver()

        first = driver.inject_metadata(_image(image_format), {"old": "keep", "k1": "v1"})
        second = driver.inject_metadata(Image.open(BytesIO(first)), {"k1": "replaced", "k2": "v2"})

        assert _user_comment(second) == {"old": "keep", "k1": "replaced", "k2": "v2"}

    def test_inject_keeps_json_types_of_existing_values(self) -> None:
        written = ExifMetadataDriver().inject_metadata(_image("JPEG", '{"count":3,"tags":["a"]}'), {"k1": "v1"})

        assert _user_comment(written) == {"count": 3, "tags": ["a"], "k1": "v1"}

    @pytest.mark.parametrize(
        "user_comment",
        ["shot on a phone", '["not", "an", "object"]', "[" * 20000, '{"a":' + "1" * 5000 + "}"],
    )
    def test_inject_replaces_user_comment_that_is_not_a_json_object(self, user_comment: str) -> None:
        written = ExifMetadataDriver().inject_metadata(_image("JPEG", user_comment), {"k1": "v1"})

        assert _user_comment(written) == {"k1": "v1"}

    def test_extract_returns_user_comment_values_as_strings(self) -> None:
        metadata = ExifMetadataDriver().extract_metadata(_image("JPEG", '{"count":3,"k1":"v1"}'))

        assert metadata["count"] == "3"
        assert metadata["k1"] == "v1"
