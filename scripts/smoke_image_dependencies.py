"""Check image decoding in a clean engine-only installation."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image import GriptapeImage, GriptapeImagePipeline, ImageFormat
from griptape_nodes.image.image_io import temporary_path


def main() -> None:
    """Fail if Pillow is installed or the engine cannot generate a preview."""
    if importlib.util.find_spec("PIL") is not None:
        msg = "The engine-only smoke environment contains Pillow."
        raise RuntimeError(msg)
    packages = {distribution.metadata["Name"].lower() for distribution in importlib.metadata.distributions()}
    if "pillow" in packages:
        msg = "The engine-only smoke environment contains the Pillow distribution."
        raise RuntimeError(msg)
    pixels = np.full((64, 64, 3), 128, dtype=np.uint8)
    with temporary_path(".png") as path:
        buffer = oiio.ImageBuf(pixels)
        if not buffer.write(str(path)):
            msg = f"The image smoke fixture could not be written: {buffer.geterror()}"
            raise RuntimeError(msg)
        with GriptapeImage.open(path) as image, GriptapeImagePipeline(image).thumbnail(32, 32).end() as preview:
            if not preview.encode(ImageFormat.WEBP):
                msg = "The image smoke preview is empty."
                raise RuntimeError(msg)
    if any(name == "PIL" or name.startswith("PIL.") for name in sys.modules):
        msg = "Engine image processing imported Pillow."
        raise RuntimeError(msg)


if __name__ == "__main__":
    main()
