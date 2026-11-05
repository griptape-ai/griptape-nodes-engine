"""EXIF tags, parsed from and serialized to the TIFF-structured block that image containers store."""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Any, NamedTuple

import numpy as np

from griptape_nodes.image.exif_tags import EXIF_IFD_TAGS

if TYPE_CHECKING:
    from collections.abc import ItemsView

EXIF_HEADER = b"Exif\x00\x00"

EXIF_ORIENTATION_TAG = 0x0112
EXIF_OFFSET_TAG = 0x8769
EXIF_GPSINFO_TAG = 0x8825
EXIF_INTEROP_TAG = 0xA005
EXIF_ICC_PROFILE_TAG = 0x8773
EXIF_USER_COMMENT_TAG = 0x9286
EXIF_PIXEL_X_DIMENSION_TAG = 0xA002
EXIF_PIXEL_Y_DIMENSION_TAG = 0xA003

# EXIF orientations that turn the image a quarter turn (or transpose it), swapping width and height
TRANSPOSING_ORIENTATIONS = frozenset({5, 6, 7, 8})

_POINTER_TAGS = frozenset({EXIF_OFFSET_TAG, EXIF_GPSINFO_TAG, EXIF_INTEROP_TAG})
_THUMBNAIL_OFFSET_TAG = 0x0201
_THUMBNAIL_LENGTH_TAG = 0x0202
_STRIP_OFFSETS_TAG = 0x0111
_TIFF_MAGIC = 42
_HEADER_BYTES = 8
_ENTRY_BYTES = 12
_INLINE_BYTES = 4
_UPRIGHT = 1
_MAX_ORIENTATION = 8

# TIFF field types
_BYTE = 1
_ASCII = 2
_SHORT = 3
_LONG = 4
_RATIONAL = 5
_SBYTE = 6
_UNDEFINED = 7
_SSHORT = 8
_SLONG = 9
_SRATIONAL = 10
_FLOAT = 11
_DOUBLE = 12
_IFD = 13

# Field types whose values read back as bytes rather than numbers
_BYTES_TYPES = frozenset({_BYTE, _ASCII, _UNDEFINED})
_INTEGER_TYPES = frozenset({_SHORT, _LONG, _SBYTE, _SSHORT, _SLONG, _IFD})
_RATIONAL_TYPES = frozenset({_RATIONAL, _SRATIONAL})
# Field type -> struct code of one value. A rational is two of them.
_STRUCT_CODES = {
    _SHORT: "H",
    _LONG: "I",
    _RATIONAL: "I",
    _SBYTE: "b",
    _SSHORT: "h",
    _SLONG: "i",
    _SRATIONAL: "i",
    _FLOAT: "f",
    _DOUBLE: "d",
    _IFD: "I",
}
_TYPE_BYTES = {
    _BYTE: 1,
    _ASCII: 1,
    _SHORT: 2,
    _LONG: 4,
    _RATIONAL: 8,
    _SBYTE: 1,
    _UNDEFINED: 1,
    _SSHORT: 2,
    _SLONG: 4,
    _SRATIONAL: 8,
    _FLOAT: 4,
    _DOUBLE: 8,
    _IFD: 4,
}
_INTEGER_RANGES = {
    _SHORT: range(0x10000),
    _LONG: range(0x100000000),
    _SSHORT: range(-0x8000, 0x8000),
    _SLONG: range(-0x80000000, 0x80000000),
}
# The range of a rational's numerator and denominator
_RATIONAL_PART_RANGES = {_RATIONAL: _INTEGER_RANGES[_LONG], _SRATIONAL: _INTEGER_RANGES[_SLONG]}

# UserComment starts with an 8-byte character code naming the encoding of the rest
_USER_COMMENT_ASCII = b"ASCII\x00\x00\x00"
_USER_COMMENT_UNICODE = b"UNICODE\x00"
_USER_COMMENT_CODE_BYTES = 8

# Tags of a TIFF file's IFD0 that describe its pixel layout rather than the picture. They belong
# to the file, not to the EXIF it carries.
_TIFF_STRUCTURE_TAGS = frozenset(
    {
        0x00FE,  # NewSubfileType
        0x00FF,  # SubfileType
        0x0100,  # ImageWidth
        0x0101,  # ImageLength
        0x0102,  # BitsPerSample
        0x0103,  # Compression
        0x0106,  # PhotometricInterpretation
        0x0107,  # Threshholding
        0x0108,  # CellWidth
        0x0109,  # CellLength
        0x010A,  # FillOrder
        0x0111,  # StripOffsets
        0x0115,  # SamplesPerPixel
        0x0116,  # RowsPerStrip
        0x0117,  # StripByteCounts
        0x0118,  # MinSampleValue
        0x0119,  # MaxSampleValue
        0x011C,  # PlanarConfiguration
        0x0120,  # FreeOffsets
        0x0121,  # FreeByteCounts
        0x0122,  # GrayResponseUnit
        0x0123,  # GrayResponseCurve
        0x0124,  # T4Options
        0x0125,  # T6Options
        0x013D,  # Predictor
        0x0140,  # ColorMap
        0x0141,  # HalftoneHints
        0x0142,  # TileWidth
        0x0143,  # TileLength
        0x0144,  # TileOffsets
        0x0145,  # TileByteCounts
        0x014A,  # SubIFDs
        0x014C,  # InkSet
        0x0152,  # ExtraSamples
        0x0153,  # SampleFormat
        0x0154,  # SMinSampleValue
        0x0155,  # SMaxSampleValue
        0x015B,  # JPEGTables
        *range(0x0200, 0x020A),  # Old-style JPEG compression
        0x0211,  # YCbCrCoefficients
        0x0212,  # YCbCrSubSampling
        0x0214,  # ReferenceBlackWhite
        0x8773,  # ICC profile, carried as ImageMetadata.icc_profile
    }
)

_TIFF_XMP_TAG = 0x02BC
_TIFF_IPTC_TAG = 0x83BB
_TIFF_PHOTOSHOP_TAG = 0x8649
_TIFF_IMAGE_SOURCE_DATA_TAG = 0x935C

# Tags of a TIFF file's IFD0 holding other metadata standards (XMP, IPTC) and Photoshop's
# resources and layers. They aren't EXIF, and are often too large for a JPEG's EXIF segment.
_TIFF_NON_EXIF_TAGS = frozenset({_TIFF_XMP_TAG, _TIFF_IPTC_TAG, _TIFF_PHOTOSHOP_TAG, _TIFF_IMAGE_SOURCE_DATA_TAG})

# OpenImageIO stamps every TIFF it writes with an XMP packet holding only xmp:CreateDate, the time
# of writing. A packet that small whose only property is that one says nothing about the picture.
_XMP_STAMP_MAX_BYTES = 4096
_XMP_STAMP_PROPERTIES = frozenset({b"xmp:CreateDate"})
# Prefixes of the packet's own structure (x:xmpmeta, rdf:Description, xmlns:...), not properties
_XMP_STRUCTURE_PREFIXES = frozenset({b"x", b"rdf", b"xmlns", b"xml"})
_XMP_QUALIFIED_NAME = re.compile(rb"([A-Za-z_][\w.-]*):[A-Za-z_][\w.-]*")
# Attribute values, dropped before looking for names, so a URI or date in one isn't taken for one
_XMP_QUOTED = re.compile(rb"\"[^\"]*\"|'[^']*'")

# Tag -> name of the metadata it holds, as ``tiff_extra_metadata_kinds`` reports it
_TIFF_EXTRA_METADATA_KINDS = {
    _TIFF_XMP_TAG: "XMP",
    _TIFF_IPTC_TAG: "IPTC",
    _TIFF_PHOTOSHOP_TAG: "Photoshop data",
    _TIFF_IMAGE_SOURCE_DATA_TAG: "Photoshop data",
}


@dataclass(frozen=True)
class ExifRational:
    """A RATIONAL or SRATIONAL value. ``float()`` of one with a zero denominator is NaN."""

    numerator: int
    denominator: int

    def __float__(self) -> float:
        if self.denominator == 0:
            return math.nan
        return self.numerator / self.denominator

    def __str__(self) -> str:
        return str(float(self))


class _Entry(NamedTuple):
    """One tag's value as stored: its field type and packed value bytes, in the block's byte order.

    Numeric values are unpacked only when read, so a tag holding millions of values costs its
    packed size rather than a tuple of Python numbers.
    """

    field_type: int
    raw: bytes


class _Directory(NamedTuple):
    entries: dict[int, _Entry]
    next_offset: int


class _TiffHeader(NamedTuple):
    byte_order: str
    ifd0_offset: int


class _EntryLocation(NamedTuple):
    """Where an IFD entry lies: its tag, field type, value count, and the position of the entry."""

    tag: int
    field_type: int
    count: int
    position: int


class TiffMetadata(NamedTuple):
    """The EXIF and color profile a TIFF file carries, from one parse of its IFDs."""

    exif: Exif
    icc_profile: bytes | None


class Exif:
    """Parsed EXIF tags, keyed by the tag numbers the EXIF and TIFF specs assign.

    Lookups and ``items`` cover the primary IFD (IFD0) and the Exif sub-IFD together; ``gps``
    holds the GPS sub-IFD. Values come back parsed: ``str`` for text and UserComment, ``int`` and
    ``float``, ``ExifRational``, tuples for multi-valued tags, and ``bytes`` for undefined data.

    Tags this class doesn't change keep their values through a read-modify-write, maker notes and
    unknown tags included. Serializing recomputes every offset, so a maker note that locates its
    own data by offsets into the block can stop resolving once its position moves.

    Values stay packed until read. Parsing keeps at most as many value bytes as the block holds,
    so entries that overlap to describe more values than there is data are skipped rather than
    expanded.
    """

    __hash__ = None  # type: ignore[assignment]  # Mutable

    def __init__(self) -> None:
        # "<" (little-endian, "II") or ">" (big-endian, "MM"); kept from a parsed block
        self._byte_order = "<"
        self._ifd0: dict[int, _Entry] = {}
        self._exif_ifd: dict[int, _Entry] = {}
        self._gps_ifd: dict[int, _Entry] = {}
        self._interop_ifd: dict[int, _Entry] = {}
        # The embedded thumbnail's directory and JPEG bytes
        self._ifd1: dict[int, _Entry] = {}
        self._thumbnail: bytes | None = None

    def __bool__(self) -> bool:
        return any((self._ifd0, self._exif_ifd, self._gps_ifd, self._interop_ifd, self._ifd1))

    def __contains__(self, tag: int) -> bool:
        return self._entry(tag) is not None

    def __getitem__(self, tag: int) -> Any:
        entry = self._entry(tag)
        if entry is None:
            raise KeyError(tag)
        return _decoded(tag, entry, self._byte_order)

    def __setitem__(self, tag: int, value: Any) -> None:
        """Set a tag of IFD0 or the Exif sub-IFD, where the EXIF spec places it.

        Raises:
            ValueError: If ``tag`` is a sub-IFD pointer or ``value`` has no EXIF representation
        """
        if tag in _POINTER_TAGS:
            msg = f"Attempted to set EXIF tag {tag:#06x}. Failed because that tag locates other tags and is written automatically."
            raise ValueError(msg)
        existing = self._entry(tag)
        entry = _encoded(tag, value, existing, self._byte_order)
        if tag in EXIF_IFD_TAGS:
            self._ifd0.pop(tag, None)
            self._exif_ifd[tag] = entry
            return
        if tag in self._exif_ifd:
            self._exif_ifd[tag] = entry
            return
        self._ifd0[tag] = entry

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Exif):
            return NotImplemented
        # Entries are packed in their own block's byte order; compare them in one
        converted = [
            _converted_directory(directory, other._byte_order, self._byte_order) for directory in other._directories()
        ]
        return self._directories() == converted and self._thumbnail == other._thumbnail

    @property
    def orientation(self) -> int:
        """The Orientation tag (1-8), or 1 (upright) when absent or invalid."""
        orientation = self.get(EXIF_ORIENTATION_TAG, _UPRIGHT)
        if not isinstance(orientation, int) or not _UPRIGHT <= orientation <= _MAX_ORIENTATION:
            return _UPRIGHT
        return orientation

    @property
    def swaps_dimensions(self) -> bool:
        """True if displaying the image upright swaps its width and height."""
        return self.orientation in TRANSPOSING_ORIENTATIONS

    @property
    def gps(self) -> dict[int, Any]:
        """Tags of the GPS sub-IFD, keyed by GPS tag number. Empty if there is none."""
        return {tag: _decoded(tag, entry, self._byte_order) for tag, entry in self._gps_ifd.items()}

    def get(self, tag: int, default: Any = None) -> Any:
        entry = self._entry(tag)
        if entry is None:
            return default
        return _decoded(tag, entry, self._byte_order)

    def items(self) -> ItemsView[int, Any]:
        """Tags of IFD0 and the Exif sub-IFD, less the pointers that locate the sub-IFDs."""
        merged = {}
        for directory in (self._ifd0, self._exif_ifd):
            for tag, entry in directory.items():
                if tag not in _POINTER_TAGS:
                    merged[tag] = _decoded(tag, entry, self._byte_order)
        return merged.items()

    def copy(self) -> Exif:
        """Return an independent copy."""
        copied = Exif()
        copied._byte_order = self._byte_order
        copied._ifd0 = dict(self._ifd0)
        copied._exif_ifd = dict(self._exif_ifd)
        copied._gps_ifd = dict(self._gps_ifd)
        copied._interop_ifd = dict(self._interop_ifd)
        copied._ifd1 = dict(self._ifd1)
        copied._thumbnail = self._thumbnail
        return copied

    def remove_thumbnail(self) -> None:
        """Drop the embedded thumbnail, as one that no longer matches the picture should be."""
        self._ifd1 = {}
        self._thumbnail = None

    def to_bytes(self) -> bytes:
        """Serialize to a TIFF-structured EXIF block, without the leading "Exif" marker."""
        marker = b"II"
        if self._byte_order == ">":
            marker = b"MM"
        header = marker + struct.pack(f"{self._byte_order}HI", _TIFF_MAGIC, _HEADER_BYTES)
        return header + self._packed(self._ifd0, self._byte_order, _HEADER_BYTES, include_thumbnail=True)

    def _entry(self, tag: int) -> _Entry | None:
        entry = self._ifd0.get(tag)
        if entry is not None and tag not in _POINTER_TAGS:
            return entry
        return self._exif_ifd.get(tag)

    def _directories(self) -> list[dict[int, _Entry]]:
        return [self._ifd0, self._exif_ifd, self._gps_ifd, self._interop_ifd, self._ifd1]

    def _packed(self, ifd0: dict[int, _Entry], byte_order: str, base: int, *, include_thumbnail: bool) -> bytes:
        """Serialize ``ifd0`` and the sub-IFDs to start at offset ``base`` of the TIFF structure.

        Offsets are computed in two passes: sizes first, with each sub-IFD pointer a placeholder,
        then the pointers are filled in and each directory is packed where it lands.
        """
        exif_ifd = {tag: entry for tag, entry in self._exif_ifd.items() if tag not in _POINTER_TAGS}
        primary = {tag: entry for tag, entry in ifd0.items() if tag not in _POINTER_TAGS}
        placeholder = _long_entry(0, byte_order)
        if self._interop_ifd:
            exif_ifd[EXIF_INTEROP_TAG] = placeholder
        if exif_ifd:
            primary[EXIF_OFFSET_TAG] = placeholder
        if self._gps_ifd:
            primary[EXIF_GPSINFO_TAG] = placeholder

        thumbnail_ifd: dict[int, _Entry] = {}
        if include_thumbnail and self._thumbnail is not None and self._ifd1:
            thumbnail_ifd = dict(self._ifd1)
            thumbnail_ifd[_THUMBNAIL_OFFSET_TAG] = placeholder
            thumbnail_ifd[_THUMBNAIL_LENGTH_TAG] = _long_entry(len(self._thumbnail), byte_order)

        primary_offset = base
        exif_offset = primary_offset + _directory_size(primary)
        interop_offset = exif_offset + _directory_size(exif_ifd)
        gps_offset = interop_offset + _directory_size(self._interop_ifd)
        thumbnail_ifd_offset = gps_offset + _directory_size(self._gps_ifd)
        thumbnail_offset = thumbnail_ifd_offset + _directory_size(thumbnail_ifd)

        if EXIF_OFFSET_TAG in primary:
            primary[EXIF_OFFSET_TAG] = _long_entry(exif_offset, byte_order)
        if EXIF_GPSINFO_TAG in primary:
            primary[EXIF_GPSINFO_TAG] = _long_entry(gps_offset, byte_order)
        if EXIF_INTEROP_TAG in exif_ifd:
            exif_ifd[EXIF_INTEROP_TAG] = _long_entry(interop_offset, byte_order)
        next_offset = 0
        if thumbnail_ifd:
            thumbnail_ifd[_THUMBNAIL_OFFSET_TAG] = _long_entry(thumbnail_offset, byte_order)
            next_offset = thumbnail_ifd_offset

        packed = bytearray(_packed_directory(primary, byte_order, primary_offset, next_offset))
        packed += _packed_directory(exif_ifd, byte_order, exif_offset, 0)
        packed += _packed_directory(self._interop_ifd, byte_order, interop_offset, 0)
        packed += _packed_directory(self._gps_ifd, byte_order, gps_offset, 0)
        packed += _packed_directory(thumbnail_ifd, byte_order, thumbnail_ifd_offset, 0)
        if thumbnail_ifd and self._thumbnail is not None:
            packed += self._thumbnail
        return bytes(packed)

    @classmethod
    def from_bytes(cls, block: bytes) -> Exif:
        """Parse a TIFF-structured EXIF block, with or without the leading "Exif" marker.

        A block that isn't valid TIFF parses as empty. Damage inside a valid block (offsets past
        the end, directories that loop) loses only the tags it touches.
        """
        data = block.removeprefix(EXIF_HEADER)
        exif = cls()
        header = _tiff_header(data)
        if header is None:
            return exif

        exif._byte_order = header.byte_order
        reader = _BlockReader(data, header.byte_order)
        ifd0 = reader.directory(header.ifd0_offset)
        exif._ifd0 = ifd0.entries
        exif._exif_ifd = reader.sub_directory(exif._ifd0, EXIF_OFFSET_TAG)
        exif._gps_ifd = reader.sub_directory(exif._ifd0, EXIF_GPSINFO_TAG)
        exif._interop_ifd = reader.sub_directory(exif._exif_ifd, EXIF_INTEROP_TAG)
        if ifd0.next_offset:
            exif._read_thumbnail(data, reader.directory(ifd0.next_offset).entries)
        return exif

    def _read_thumbnail(self, data: bytes, ifd1: dict[int, _Entry]) -> None:
        offset = _single_integer(ifd1.get(_THUMBNAIL_OFFSET_TAG), self._byte_order)
        length = _single_integer(ifd1.get(_THUMBNAIL_LENGTH_TAG), self._byte_order)
        # An uncompressed thumbnail is located by strip offsets, which can't be moved with it
        if offset is None or length is None or _STRIP_OFFSETS_TAG in ifd1:
            return
        if offset + length > len(data):
            return
        self._ifd1 = ifd1
        self._thumbnail = data[offset : offset + length]


def tiff_metadata(data: bytes) -> TiffMetadata:
    """Return the EXIF and ICC profile a TIFF file carries.

    The EXIF is its IFD0, less the tags describing pixel layout and those holding other metadata
    standards (XMP, IPTC, Photoshop's), and its sub-IFDs.
    """
    exif = Exif.from_bytes(data)
    icc_entry = exif._ifd0.get(EXIF_ICC_PROFILE_TAG)
    icc_profile = None
    if icc_entry is not None and icc_entry.field_type in _BYTES_TYPES and icc_entry.raw:
        icc_profile = icc_entry.raw
    exif._ifd0 = {tag: entry for tag, entry in exif._ifd0.items() if _is_tiff_exif_tag(tag)}
    # A second directory in a TIFF is another page, not an EXIF thumbnail
    exif.remove_thumbnail()
    return TiffMetadata(exif, icc_profile)


def tiff_extra_metadata_kinds(data: bytes) -> list[str]:
    """Name the metadata a TIFF file's IFD0 holds besides EXIF and its color profile ("XMP", "IPTC", ...).

    Reads the directory's tag numbers, and the XMP packet only when it's small enough to be the
    write-time stamp OpenImageIO adds to every TIFF, which isn't reported.
    """
    header = _tiff_header(data)
    if header is None:
        return []
    kinds = []
    for location in _directory_locations(data, header):
        kind = _TIFF_EXTRA_METADATA_KINDS.get(location.tag)
        if kind is None or kind in kinds:
            continue
        if location.tag == _TIFF_XMP_TAG and _is_xmp_stamp(data, header, location):
            continue
        kinds.append(kind)
    return kinds


def replace_tiff_exif(data: bytes, exif: Exif) -> bytes | None:
    """Return a single-page TIFF file with ``exif`` written into its IFD0, or None if it isn't classic TIFF.

    The file's own layout tags (strips, sample format, ICC profile) are kept and win over ``exif``;
    its other tags are replaced where ``exif`` sets them, except XMP, IPTC, and Photoshop's, which
    aren't EXIF. The new directories are appended and the
    header pointed at them, so the pixel data and its offsets stay where they are.
    """
    file_exif = Exif.from_bytes(data)
    if not file_exif._ifd0:
        return None
    byte_order = file_exif._byte_order
    ifd0 = {tag: entry for tag, entry in file_exif._ifd0.items() if tag not in _POINTER_TAGS}
    for tag, entry in exif._ifd0.items():
        if _is_tiff_exif_tag(tag):
            ifd0[tag] = _converted(entry, exif._byte_order, byte_order)
    written = Exif()
    written._byte_order = byte_order
    written._exif_ifd = _converted_directory(exif._exif_ifd, exif._byte_order, byte_order)
    written._gps_ifd = _converted_directory(exif._gps_ifd, exif._byte_order, byte_order)
    written._interop_ifd = _converted_directory(exif._interop_ifd, exif._byte_order, byte_order)

    padding = b"\x00" * (len(data) & 1)
    base = len(data) + len(padding)
    directories = written._packed(ifd0, byte_order, base, include_thumbnail=False)
    header = data[:4] + struct.pack(f"{byte_order}I", base)
    return header + data[_HEADER_BYTES:] + padding + directories


class _BlockReader:
    """Reads the IFDs of one TIFF-structured block.

    Each directory is read at most once, so directories that point at each other end the walk.
    The value bytes kept across all directories are capped at the block's own size: a well-formed
    block stores each value once, so only entries overlapping one another can pass the cap.
    """

    def __init__(self, data: bytes, byte_order: str) -> None:
        self._data = data
        self._byte_order = byte_order
        self._visited: set[int] = set()
        self._remaining_value_bytes = len(data)

    def sub_directory(self, parent: dict[int, _Entry], pointer_tag: int) -> dict[int, _Entry]:
        offset = _single_integer(parent.pop(pointer_tag, None), self._byte_order)
        if offset is None:
            return {}
        return self.directory(offset).entries

    def directory(self, offset: int) -> _Directory:
        """Read the IFD at ``offset``, skipping entries whose type is unknown or whose data lies outside the block."""
        data = self._data
        byte_order = self._byte_order
        if offset in self._visited or offset < _HEADER_BYTES or offset + 2 > len(data):
            return _Directory({}, 0)
        self._visited.add(offset)
        (count,) = struct.unpack(f"{byte_order}H", data[offset : offset + 2])
        entries_start = offset + 2
        # A count running past the end keeps the entries that fit
        count = min(count, (len(data) - entries_start) // _ENTRY_BYTES)
        entries: dict[int, _Entry] = {}
        for index in range(count):
            position = entries_start + index * _ENTRY_BYTES
            tag, field_type, value_count = struct.unpack(f"{byte_order}HHI", data[position : position + 8])
            unit_bytes = _TYPE_BYTES.get(field_type)
            if unit_bytes is None:
                continue
            size = unit_bytes * value_count
            if size > self._remaining_value_bytes:
                continue
            if size <= _INLINE_BYTES:
                raw = data[position + 8 : position + 8 + size]
            else:
                (value_offset,) = struct.unpack(f"{byte_order}I", data[position + 8 : position + 12])
                if value_offset + size > len(data):
                    continue
                raw = data[value_offset : value_offset + size]
            self._remaining_value_bytes -= size
            entries[tag] = _Entry(field_type, raw)

        next_position = entries_start + count * _ENTRY_BYTES
        next_offset = 0
        if next_position + 4 <= len(data):
            (next_offset,) = struct.unpack(f"{byte_order}I", data[next_position : next_position + 4])
        return _Directory(entries, next_offset)


def _tiff_header(data: bytes) -> _TiffHeader | None:
    """Read a TIFF structure's byte order and IFD0 offset, or None if ``data`` isn't classic TIFF."""
    if len(data) < _HEADER_BYTES:
        return None
    if data[:2] == b"II":
        byte_order = "<"
    elif data[:2] == b"MM":
        byte_order = ">"
    else:
        return None
    magic, ifd0_offset = struct.unpack(f"{byte_order}HI", data[2:_HEADER_BYTES])
    if magic != _TIFF_MAGIC:
        return None
    return _TiffHeader(byte_order, ifd0_offset)


def _directory_locations(data: bytes, header: _TiffHeader) -> list[_EntryLocation]:
    """Locate the IFD0 entries that fit in ``data``, without reading their values."""
    offset = header.ifd0_offset
    if offset < _HEADER_BYTES or offset + 2 > len(data):
        return []
    (count,) = struct.unpack(f"{header.byte_order}H", data[offset : offset + 2])
    entries_start = offset + 2
    count = min(count, (len(data) - entries_start) // _ENTRY_BYTES)
    locations = []
    for index in range(count):
        position = entries_start + index * _ENTRY_BYTES
        tag, field_type, value_count = struct.unpack(f"{header.byte_order}HHI", data[position : position + 8])
        locations.append(_EntryLocation(tag, field_type, value_count, position))
    return locations


def _is_xmp_stamp(data: bytes, header: _TiffHeader, location: _EntryLocation) -> bool:
    """Whether an XMP entry is a small packet holding only the properties OpenImageIO stamps on write."""
    unit_bytes = _TYPE_BYTES.get(location.field_type)
    if unit_bytes != 1 or location.count > _XMP_STAMP_MAX_BYTES:
        return False
    if location.count <= _INLINE_BYTES:
        packet = data[location.position + 8 : location.position + 8 + location.count]
    else:
        (value_offset,) = struct.unpack(f"{header.byte_order}I", data[location.position + 8 : location.position + 12])
        packet = data[value_offset : value_offset + location.count]
    properties = {
        match.group(0)
        for match in _XMP_QUALIFIED_NAME.finditer(_XMP_QUOTED.sub(b"", packet))
        if match.group(1) not in _XMP_STRUCTURE_PREFIXES
    }
    return properties <= _XMP_STAMP_PROPERTIES


def _is_tiff_exif_tag(tag: int) -> bool:
    return tag not in _TIFF_STRUCTURE_TAGS and tag not in _TIFF_NON_EXIF_TAGS


def _long_entry(value: int, byte_order: str) -> _Entry:
    return _Entry(_LONG, struct.pack(f"{byte_order}I", value))


def _numbers(entry: _Entry, byte_order: str) -> tuple[int | float, ...]:
    """Unpack a numeric entry's values. Rationals come back flattened to numerator, denominator pairs."""
    code = _STRUCT_CODES[entry.field_type]
    count = len(entry.raw) // struct.calcsize(f"={code}")
    return struct.unpack(f"{byte_order}{count}{code}", entry.raw)


def _value_count(entry: _Entry) -> int:
    return len(entry.raw) // _TYPE_BYTES[entry.field_type]


def _directory_size(entries: dict[int, _Entry]) -> int:
    if not entries:
        return 0
    size = 2 + _ENTRY_BYTES * len(entries) + 4
    for entry in entries.values():
        value_bytes = len(entry.raw)
        if value_bytes > _INLINE_BYTES:
            size += value_bytes + (value_bytes & 1)
    return size


def _packed_directory(entries: dict[int, _Entry], byte_order: str, offset: int, next_offset: int) -> bytes:
    """Pack one IFD to sit at ``offset``, its out-of-line values following its entries, word-aligned."""
    if not entries:
        return b""
    data_offset = offset + 2 + _ENTRY_BYTES * len(entries) + 4
    table = bytearray(struct.pack(f"{byte_order}H", len(entries)))
    values = bytearray()
    for tag in sorted(entries):
        entry = entries[tag]
        raw = entry.raw
        if len(raw) <= _INLINE_BYTES:
            field = raw.ljust(_INLINE_BYTES, b"\x00")
        else:
            field = struct.pack(f"{byte_order}I", data_offset + len(values))
            values += raw + b"\x00" * (len(raw) & 1)
        table += struct.pack(f"{byte_order}HHI", tag, entry.field_type, _value_count(entry)) + field
    table += struct.pack(f"{byte_order}I", next_offset)
    return bytes(table + values)


def _converted(entry: _Entry, from_order: str, to_order: str) -> _Entry:
    """Return ``entry`` for a block in ``to_order``: numbers byte-swapped, UTF-16 UserComment text re-encoded."""
    if from_order == to_order:
        return entry
    if entry.field_type not in _BYTES_TYPES:
        number_bytes = struct.calcsize(f"={_STRUCT_CODES[entry.field_type]}")
        swapped = np.frombuffer(entry.raw, dtype=f"u{number_bytes}").byteswap().tobytes()
        return _Entry(entry.field_type, swapped)
    if entry.raw.startswith(_USER_COMMENT_UNICODE):
        text = entry.raw[_USER_COMMENT_CODE_BYTES:].decode(_utf16(from_order), errors="replace")
        return _Entry(entry.field_type, _USER_COMMENT_UNICODE + text.encode(_utf16(to_order)))
    return entry


def _converted_directory(entries: dict[int, _Entry], from_order: str, to_order: str) -> dict[int, _Entry]:
    return {tag: _converted(entry, from_order, to_order) for tag, entry in entries.items()}


def _single_integer(entry: _Entry | None, byte_order: str) -> int | None:
    if entry is None or entry.field_type not in _INTEGER_TYPES or _value_count(entry) != 1:
        return None
    return int(_numbers(entry, byte_order)[0])


def _decoded(tag: int, entry: _Entry, byte_order: str) -> Any:
    """Return a stored value as callers read it."""
    if entry.field_type in _BYTES_TYPES:
        if entry.field_type == _ASCII:
            return _text(entry.raw.rstrip(b"\x00"))
        if tag == EXIF_USER_COMMENT_TAG:
            return _user_comment_text(entry.raw, byte_order)
        return entry.raw
    values = _numbers(entry, byte_order)
    decoded: tuple[Any, ...] = values
    if entry.field_type in _RATIONAL_TYPES:
        decoded = tuple(ExifRational(int(values[i]), int(values[i + 1])) for i in range(0, len(values), 2))
    if len(decoded) == 1:
        return decoded[0]
    return decoded


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _user_comment_text(raw: bytes, byte_order: str) -> str:
    code = raw[:_USER_COMMENT_CODE_BYTES]
    body = raw[_USER_COMMENT_CODE_BYTES:]
    if code == _USER_COMMENT_UNICODE:
        return body.decode(_utf16(byte_order), errors="replace").rstrip("\x00")
    if len(raw) >= _USER_COMMENT_CODE_BYTES and (code in (_USER_COMMENT_ASCII, b"\x00" * 8) or code.isalpha()):
        return _text(body.rstrip(b"\x00 "))
    # No character code: text written straight into the tag
    return _text(raw.rstrip(b"\x00"))


def _utf16(byte_order: str) -> str:
    if byte_order == ">":
        return "utf-16-be"
    return "utf-16-le"


def _encoded(tag: int, value: Any, existing: _Entry | None, byte_order: str) -> _Entry:
    """Return how to store ``value``, keeping the existing entry's type where the value fits it.

    Raises:
        ValueError: If ``value`` has no EXIF representation
    """
    if isinstance(value, str):
        if tag == EXIF_USER_COMMENT_TAG:
            return _Entry(_UNDEFINED, _user_comment_bytes(value, byte_order))
        return _Entry(_ASCII, value.encode("utf-8") + b"\x00")
    if isinstance(value, bytes | bytearray):
        if existing is not None and existing.field_type == _BYTE:
            return _Entry(_BYTE, bytes(value))
        return _Entry(_UNDEFINED, bytes(value))
    values: tuple[Any, ...] = (value,)
    if isinstance(value, tuple | list):
        values = tuple(value)
    if not values:
        msg = f"Attempted to set EXIF tag {tag:#06x}. Failed because the value is empty."
        raise ValueError(msg)
    if all(isinstance(item, int) for item in values):
        field_type = _integer_type(tag, values, existing)
        return _Entry(field_type, _packed_numbers(field_type, values, byte_order))
    if all(isinstance(item, ExifRational | Fraction | float) for item in values):
        return _rational_or_float_entry(tag, values, existing, byte_order)
    msg = f"Attempted to set EXIF tag {tag:#06x}. Failed because {type(value).__name__} values can't be stored in EXIF."
    raise ValueError(msg)


def _integer_type(tag: int, values: tuple[int, ...], existing: _Entry | None) -> int:
    """Return the existing integer type if every value fits it, else the narrowest that holds them.

    Raises:
        ValueError: If a value is too large for any EXIF integer type
    """
    if existing is not None and existing.field_type in _INTEGER_RANGES:
        kept_range = _INTEGER_RANGES[existing.field_type]
        if all(item in kept_range for item in values):
            return existing.field_type
    for field_type in (_SHORT, _LONG, _SLONG):
        if all(item in _INTEGER_RANGES[field_type] for item in values):
            return field_type
    msg = f"Attempted to set EXIF tag {tag:#06x}. Failed because the value is too large for EXIF to store."
    raise ValueError(msg)


def _rational_or_float_entry(tag: int, values: tuple[Any, ...], existing: _Entry | None, byte_order: str) -> _Entry:
    """Return a FLOAT or DOUBLE entry if the existing one is, else a RATIONAL, or SRATIONAL for negative values.

    Raises:
        ValueError: If a value is out of range for the type, or not finite where a rational is needed
    """
    if existing is not None and existing.field_type in (_FLOAT, _DOUBLE):
        try:
            raw = _packed_numbers(existing.field_type, tuple(float(item) for item in values), byte_order)
        except OverflowError as err:
            msg = f"Attempted to set EXIF tag {tag:#06x}. Failed because the value is too large for EXIF to store."
            raise ValueError(msg) from err
        return _Entry(existing.field_type, raw)
    pairs = [_rational(tag, item) for item in values]
    field_type = _RATIONAL
    if any(pair.numerator < 0 or pair.denominator < 0 for pair in pairs):
        field_type = _SRATIONAL
    part_range = _RATIONAL_PART_RANGES[field_type]
    if any(pair.numerator not in part_range or pair.denominator not in part_range for pair in pairs):
        msg = f"Attempted to set EXIF tag {tag:#06x}. Failed because the value is too large for EXIF to store."
        raise ValueError(msg)
    flattened: list[int] = []
    for pair in pairs:
        flattened.extend((pair.numerator, pair.denominator))
    return _Entry(field_type, _packed_numbers(field_type, tuple(flattened), byte_order))


def _packed_numbers(field_type: int, numbers: tuple[int | float, ...], byte_order: str) -> bytes:
    """Pack range-checked numbers of a numeric field type. Rationals are numerator, denominator pairs.

    Raises:
        OverflowError: If a float is too large for a FLOAT
    """
    return struct.pack(f"{byte_order}{len(numbers)}{_STRUCT_CODES[field_type]}", *numbers)


def _rational(tag: int, value: ExifRational | Fraction | float) -> ExifRational:
    """Return ``value`` as a numerator and denominator. An ``ExifRational`` is kept as written, unreduced.

    Raises:
        ValueError: If ``value`` is infinite or NaN
    """
    if isinstance(value, ExifRational):
        return value
    if isinstance(value, Fraction):
        return ExifRational(value.numerator, value.denominator)
    if not math.isfinite(value):
        msg = f"Attempted to set EXIF tag {tag:#06x}. Failed because {value} has no EXIF rational form."
        raise ValueError(msg)
    # A negative value is stored as SRATIONAL, whose denominator is signed
    max_denominator = _RATIONAL_PART_RANGES[_RATIONAL].stop - 1
    if value < 0:
        max_denominator = _RATIONAL_PART_RANGES[_SRATIONAL].stop - 1
    fraction = Fraction(value).limit_denominator(max_denominator)
    return ExifRational(fraction.numerator, fraction.denominator)


def _user_comment_bytes(text: str, byte_order: str) -> bytes:
    if text.isascii():
        return _USER_COMMENT_ASCII + text.encode("ascii")
    return _USER_COMMENT_UNICODE + text.encode(_utf16(byte_order))
