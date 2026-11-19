"""Reading and writing image files through OpenImageIO."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import NamedTuple

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

    Passing no per-call ``config`` spec keeps ``ImageBuf`` backed by ``ImageCache`` tiles rather
    than copying into a local buffer; ``unassociatedalpha`` is set directly on the shared cache.

    Raises:
        ValueError: If OpenImageIO cannot decode it
    """
    oiio.attribute("imagebuf:use_imagecache", 1)
    oiio.ImageCache(shared=True).attribute("unassociatedalpha", 1)
    buf = oiio.ImageBuf(str(path))
    if not buf.read(0, 0, force=False) or buf.has_error:
        msg = f"Attempted to decode an image. Failed because OpenImageIO could not read it: {buf.geterror()}"
        raise ValueError(msg)
    file_format_name = buf.file_format_name
    return _normalized_decoded(buf, file_format_name)


def read_ephemeral_file(path: Path) -> Decoded:
    """Decode a temporary staging file into a local C++ ``ImageBuf`` and evict its temp path from ``ImageCache``.

    ``force=True`` decodes directly in C++ without a Python NumPy round-trip, and invalidating the
    staging path afterward closes any open ``ImageInput`` handle before the temp file is unlinked.

    Raises:
        ValueError: If OpenImageIO cannot decode it
    """
    config = oiio.ImageSpec()
    config.attribute(UNASSOCIATED_ALPHA, 1)
    buf = oiio.ImageBuf(str(path), 0, 0, config)
    try:
        if not buf.read(0, 0, force=True) or buf.has_error:
            msg = f"Attempted to decode an image. Failed because OpenImageIO could not read it: {buf.geterror()}"
            raise ValueError(msg)
        file_format_name = buf.file_format_name
    finally:
        oiio.ImageCache(shared=True).invalidate(str(path), True)
    return _normalized_decoded(buf, file_format_name)


def _normalized_decoded(buf: oiio.ImageBuf, file_format_name: str) -> Decoded:
    if buf.nchannels == GRAYSCALE_ALPHA_CHANNELS:
        buf.make_writable(keep_cache_type=True)
        buf.specmod().channelnames = ("Y", "A")
        buf.specmod().alpha_channel = 1
    # Formats that store associated alpha (EXR, some TIFF) ignore the config; unassociate them so
    # every buffer held is unassociated.
    if (
        file_format_name in {"openexr", "tiff"}
        and alpha_channel(buf) is not None
        and buf.spec().get_int_attribute(UNASSOCIATED_ALPHA, 0) != 1
    ):
        source = buf
        buf = checked(oiio.ImageBufAlgo.unpremult(source), source)
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
        _write_pixels(out, buf, spec, path)
    except BaseException:
        out.close()
        raise
    closed = out.close()
    error = out.geterror()
    checked(buf)
    if not closed or error:
        msg = f"Attempted to save an image. Failed while finishing the image file: {error}"
        raise ValueError(msg)
    return path.read_bytes()


def _write_pixels(out: oiio.ImageOutput, buf: oiio.ImageBuf, spec: oiio.ImageSpec, path: Path) -> None:
    opened = out.open(str(path), spec)
    error = out.geterror()
    if not opened or error:
        msg = f"Attempted to save an image. Failed while opening the image file: {error}"
        raise ValueError(msg)
    written = buf.write(out)
    error = out.geterror() or buf.geterror()
    if not written or error:
        msg = f"Attempted to save an image. Failed while writing the image pixels: {error}"
        raise ValueError(msg)
