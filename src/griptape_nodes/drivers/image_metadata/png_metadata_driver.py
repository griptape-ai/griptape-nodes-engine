"""PNG metadata injection and extraction driver using text chunks."""

import zlib

from PIL import Image, PngImagePlugin

from griptape_nodes.drivers.image_metadata.base_image_metadata_driver import BaseImageMetadataDriver

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TEXT_CHUNK_TYPES = {b"tEXt", b"zTXt", b"iTXt"}
# Each chunk is a 4-byte length, a 4-byte type, the data, and a 4-byte CRC.
_CHUNK_OVERHEAD = 12

# Text longer than this, such as an embedded workflow, is stored compressed. Shorter text stays
# uncompressed, where image viewers that only show plain text chunks can still read it.
_COMPRESS_TEXT_LONGER_THAN = 1024


def _should_compress_text(text: str) -> bool:
    """Decide whether a text chunk can be compressed and still be read back.

    Pillow rejects a zTXt/iTXt chunk whose decompressed size exceeds
    PngImagePlugin.MAX_TEXT_CHUNK, raising ValueError from Image.open. Mirror the
    encoding add_text falls back to (latin-1, then utf-8) to check the size Pillow
    will actually decompress.
    """
    if len(text) <= _COMPRESS_TEXT_LONGER_THAN:
        return False
    try:
        encoded_length = len(text.encode("latin-1"))
    except UnicodeEncodeError:
        encoded_length = len(text.encode("utf-8"))
    return encoded_length <= PngImagePlugin.MAX_TEXT_CHUNK


class PngMetadataDriver(BaseImageMetadataDriver):
    """Bidirectional driver for PNG metadata using text chunks.

    Supports both reading and writing metadata in PNG text chunks.
    Preserves existing text chunks when writing new metadata.
    All PNG-specific logic is encapsulated in this driver.
    """

    def get_supported_formats(self) -> list[str]:
        """Return list of PIL format strings this driver supports.

        Returns:
            List containing "PNG"
        """
        return ["PNG"]

    def inject_metadata(self, data: bytes, pil_image: Image.Image, metadata: dict[str, str]) -> bytes:  # noqa: ARG002
        """Inject metadata into PNG text chunks without re-encoding the image.

        Inserts the new text chunks before the first IDAT chunk and copies every other
        chunk, and anything after IEND, byte for byte, so pixel data, bit depth, and
        color type are unchanged. Before IDAT is where Image.open reads text chunks
        without loading the pixels.
        An existing text chunk whose key is in metadata is dropped; the new value
        replaces it.

        Args:
            data: Encoded PNG bytes that pil_image was opened from
            pil_image: PIL Image opened from data (unused; the chunks are read from data)
            metadata: Dictionary of key-value pairs to inject

        Returns:
            Image bytes with metadata injected

        Raises:
            ValueError: If data is not a well-formed PNG
        """
        if not data.startswith(_PNG_SIGNATURE):
            msg = "Attempted to add metadata to a PNG. Failed because the data does not start with a PNG signature."
            raise ValueError(msg)

        png_info = PngImagePlugin.PngInfo()
        for key, value in metadata.items():
            text = str(value)
            png_info.add_text(key, text, zip=_should_compress_text(text))
        new_text_chunks = b"".join(
            _encode_chunk(chunk_type, chunk_data) for chunk_type, chunk_data, _ in png_info.chunks
        )

        output = bytearray(_PNG_SIGNATURE)
        inserted = False
        position = len(_PNG_SIGNATURE)
        while position < len(data):
            chunk_end = position + _CHUNK_OVERHEAD + int.from_bytes(data[position : position + 4], "big")
            if chunk_end > len(data):
                msg = "Attempted to add metadata to a PNG. Failed because the file ends partway through a chunk."
                raise ValueError(msg)
            chunk_type = data[position + 4 : position + 8]
            chunk_data = data[position + 8 : chunk_end - 4]
            if chunk_type == b"IEND":
                # Decoders stop at IEND, so anything after it is copied as is.
                output += data[position:]
                break
            if chunk_type == b"IDAT" and not inserted:
                output += new_text_chunks
                inserted = True
            if not _is_text_chunk_for_any_key(chunk_type, chunk_data, metadata):
                output += data[position:chunk_end]
            position = chunk_end

        if not inserted:
            msg = "Attempted to add metadata to a PNG. Failed because the file has no image data."
            raise ValueError(msg)
        return bytes(output)

    def extract_metadata(self, pil_image: Image.Image) -> dict[str, str]:
        """Extract all text chunks from PNG image.

        Args:
            pil_image: PIL Image to extract metadata from

        Returns:
            Dictionary of all PNG text chunks, empty dict if none found
        """
        # PIL unpacks PNG text chunks directly into pil_image.info
        return {key: value for key, value in pil_image.info.items() if isinstance(key, str) and isinstance(value, str)}


def _encode_chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
    """Encode one PNG chunk: length, type, data, and CRC over type and data."""
    length = len(chunk_data).to_bytes(4, "big")
    crc = zlib.crc32(chunk_type + chunk_data).to_bytes(4, "big")
    return length + chunk_type + chunk_data + crc


def _is_text_chunk_for_any_key(chunk_type: bytes, chunk_data: bytes, keys: dict[str, str]) -> bool:
    """Whether a chunk is a tEXt/zTXt/iTXt chunk whose keyword is one of keys.

    All three text chunk types start with a latin-1 keyword terminated by a null byte.
    """
    if chunk_type not in _TEXT_CHUNK_TYPES:
        return False
    keyword = chunk_data.split(b"\0", 1)[0].decode("latin-1")
    return keyword in keys
