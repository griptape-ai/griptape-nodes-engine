"""Reading and writing image files through OpenImageIO."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import NamedTuple

import numpy as np
import OpenImageIO as oiio  # noqa: N813

from griptape_nodes.image.operations import GRAYSCALE_ALPHA_CHANNELS, alpha_channel, checked

# Keeps alpha as stored in the file. OpenImageIO otherwise premultiplies on read and divides it
# back out on write, which rounds 8-bit color under partial alpha.
UNASSOCIATED_ALPHA = "oiio:UnassociatedAlpha"

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
    """Read the header of the image file at ``path`` without decoding pixels or populating ImageCache.

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


def read_cached_file(path: Path) -> Decoded:
    """Read an image file at ``path`` backed by OpenImageIO's shared ImageCache.

    Persistent local files take this route so tile memory and open file handles follow the user's
    configured ``openimageio.*`` settings.

    Raises:
        ValueError: If OpenImageIO cannot decode it
    """
    oiio.attribute("imagebuf:use_imagecache", 1)
    config = oiio.ImageSpec()
    config.attribute(UNASSOCIATED_ALPHA, 1)
    buf = oiio.ImageBuf(str(path), 0, 0, config)
    if not buf.read(0, 0, force=False) or buf.has_error:
        msg = f"Attempted to decode an image. Failed because OpenImageIO could not read it: {buf.geterror()}"
        raise ValueError(msg)
    file_format_name = buf.file_format_name
    return _normalized_decoded(buf, file_format_name)


def read_ephemeral_file(path: Path) -> Decoded:
    """Decode a temporary staging file directly via ``ImageInput``, bypassing ``ImageCache``.

    Temporary files created for in-memory ``bytes`` payloads will be unlinked as soon as the read
    finishes; bypassing ``ImageCache`` keeps ephemeral paths out of cache RAM and closes the file
    handle immediately on Windows.

    Raises:
        ValueError: If OpenImageIO cannot decode it
    """
    config = oiio.ImageSpec()
    config.attribute(UNASSOCIATED_ALPHA, 1)
    image_input = oiio.ImageInput.open(str(path), config)
    if image_input is None:
        msg = f"Attempted to decode an image. Failed because OpenImageIO could not read it: {oiio.geterror()}"
        raise ValueError(msg)
    try:
        spec = oiio.ImageSpec(image_input.spec())
        file_format_name = image_input.format_name()
        pixels = image_input.read_image(spec.format)
        if not isinstance(pixels, np.ndarray):
            msg = (
                f"Attempted to decode an image. Failed because OpenImageIO could not read it: {image_input.geterror()}"
            )
            raise ValueError(msg)  # noqa: TRY004
    finally:
        image_input.close()

    buf = oiio.ImageBuf(np.ascontiguousarray(pixels))
    buf_spec = buf.specmod()
    buf_spec.channelnames = spec.channelnames
    buf_spec.alpha_channel = spec.alpha_channel
    for attrib in spec.extra_attribs:
        buf_spec.attribute(attrib.name, attrib.type, attrib.value)
    return _normalized_decoded(buf, file_format_name)


def _normalized_decoded(buf: oiio.ImageBuf, file_format_name: str) -> Decoded:
    if buf.nchannels == GRAYSCALE_ALPHA_CHANNELS:
        buf.make_writable(keep_cache_type=True)
        buf.specmod().channelnames = ("Y", "A")
        buf.specmod().alpha_channel = 1
    # Formats that store associated alpha (EXR, some TIFF) ignore the config; unassociate them so
    # every buffer held is unassociated.
    if alpha_channel(buf) is not None and buf.spec().get_int_attribute(UNASSOCIATED_ALPHA, 0) != 1:
        buf = checked(oiio.ImageBufAlgo.unpremult(buf))
    return Decoded(buf, file_format_name)


def spec_icc_profile(spec: oiio.ImageSpec) -> bytes | None:
    """Return the color profile OpenImageIO read into ``spec``, or None if none is present."""
    profile = spec.getattribute(ICC_PROFILE)
    if profile is None or len(profile) == 0:
        return None
    return bytes(profile)


def write_file(buf: oiio.ImageBuf, spec: oiio.ImageSpec, path: Path) -> bytes:
    """Write ``buf``'s pixels to ``path`` under ``spec`` and return the file's bytes.

    Through an ``ImageOutput`` opened with ``spec``, so ``buf``'s own metadata is omitted without
    copying pixels into a metadata-free buffer first.

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
