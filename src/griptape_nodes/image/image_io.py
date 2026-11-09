"""Reading and writing image files through OpenImageIO."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.operations import GRAYSCALE_ALPHA_CHANNELS, UNASSOCIATED_ALPHA, alpha_channel, checked

if TYPE_CHECKING:
    from collections.abc import Sequence

ICC_PROFILE = "ICCProfile"


class Decoded(NamedTuple):
    buf: oiio.ImageBuf
    file_format_name: str


class Header(NamedTuple):
    spec: oiio.ImageSpec
    file_format_name: str


class temporary_path:  # noqa: N801
    """A closed temporary file path, removed on exit. Closed so OpenImageIO can open it on Windows."""

    def __init__(self, suffix: str) -> None:
        self._suffix = suffix
        self._path: Path | None = None

    def __enter__(self) -> Path:
        handle, name = tempfile.mkstemp(suffix=self._suffix, prefix="gtn-image-")
        os.close(handle)
        self._path = Path(name)
        return self._path

    def __exit__(self, *_exc: object) -> None:
        if self._path is not None:
            self._path.unlink(missing_ok=True)


def read_header_file(path: Path) -> Header:
    """Read the header of the image file at ``path``: dimensions, channels, pixel type, and metadata.

    Raises:
        ValueError: If OpenImageIO cannot read it
    """
    image_input = oiio.ImageInput.open(str(path))
    if image_input is None:
        msg = f"Attempted to read an image. Failed because OpenImageIO could not read it: {oiio.geterror()}"
        raise ValueError(msg)
    try:
        return Header(oiio.ImageSpec(image_input.spec()), image_input.format_name())
    finally:
        image_input.close()


def read_file(path: Path) -> Decoded:
    """Decode the image file at ``path`` into memory, with unassociated alpha.

    Raises:
        ValueError: If OpenImageIO cannot decode it
    """
    config = oiio.ImageSpec()
    config.attribute(UNASSOCIATED_ALPHA, 1)
    buf = oiio.ImageBuf(str(path), 0, 0, config)
    # force pulls the pixels into memory, so the file can go away
    if not buf.read(0, 0, force=True) or buf.has_error:
        msg = f"Attempted to decode an image. Failed because OpenImageIO could not read it: {buf.geterror()}"
        raise ValueError(msg)
    file_format_name = buf.file_format_name
    if buf.nchannels == GRAYSCALE_ALPHA_CHANNELS:
        buf.specmod().channelnames = ("Y", "A")
        buf.specmod().alpha_channel = 1
    elif buf.spec().alpha_channel >= buf.nchannels:
        # The GIF reader names an alpha channel past the last one, so premultiplying reads the next
        # pixel's red as alpha. The channel named "A" is the alpha.
        buf.specmod().alpha_channel = _named_alpha_channel(buf.spec().channelnames)
    # Formats that store associated alpha (EXR, some TIFF) ignore the config; unassociate them so
    # every buffer held is unassociated
    if alpha_channel(buf) is not None and buf.spec().get_int_attribute(UNASSOCIATED_ALPHA, 0) != 1:
        buf = checked(oiio.ImageBufAlgo.unpremult(buf))
    return Decoded(buf, file_format_name)


def _named_alpha_channel(channel_names: Sequence[str]) -> int:
    """Return the index of the channel named "A", or -1 for none."""
    if "A" not in channel_names:
        return -1
    return list(channel_names).index("A")


def spec_icc_profile(spec: oiio.ImageSpec) -> bytes | None:
    """Return the color profile OpenImageIO read into ``spec``, or None if there's none."""
    profile = spec.getattribute(ICC_PROFILE)
    if profile is None or len(profile) == 0:
        return None
    return bytes(profile)


def write_file(buf: oiio.ImageBuf, spec: oiio.ImageSpec, path: Path) -> bytes:
    """Write ``buf``'s pixels to ``path`` under ``spec`` and return the file's bytes.

    Through an ``ImageOutput`` opened with ``spec``, so ``buf``'s own metadata isn't written and
    its pixels aren't copied into a metadata-free buffer first.

    Raises:
        ValueError: If OpenImageIO cannot encode the pixels
    """
    out = oiio.ImageOutput.create(str(path))
    if out is None:
        msg = (
            f"Attempted to save an image. Failed because OpenImageIO has no writer for {path.suffix}: {oiio.geterror()}"
        )
        raise ValueError(msg)
    try:
        if not out.open(str(path), spec) or not buf.write(out):
            msg = f"Attempted to save an image. Failed because OpenImageIO could not encode it: {out.geterror() or buf.geterror()}"
            raise ValueError(msg)
    finally:
        out.close()
    return path.read_bytes()
