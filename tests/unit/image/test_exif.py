"""Parsing and writing EXIF blocks with Exif."""

import math
import struct
import tracemalloc

import pytest

from griptape_nodes.image import EXIF_GPSINFO_TAG, EXIF_ORIENTATION_TAG, Exif, ExifRational
from griptape_nodes.image.exif import (
    EXIF_HEADER,
    EXIF_OFFSET_TAG,
    EXIF_USER_COMMENT_TAG,
    replace_tiff_exif,
    tiff_extra_metadata_kinds,
    tiff_metadata,
)
from griptape_nodes.image.exif_tags import EXIF_IFD_TAGS, GPS_TAGS, TAGS
from tests.unit.image.sample_images import SRGB_PROFILE, encode, gray_tiff, read_spec, solid

_MAKE = 0x010F
_X_RESOLUTION = 0x011A
_EXPOSURE_TIME = 0x829A
_EXPOSURE_BIAS = 0x9204
_DATETIME_ORIGINAL = 0x9003
_MAKER_NOTE = 0x927C
_UNKNOWN_TAG = 0xC7FF
_IMAGE_WIDTH = 0x0100
_STRIP_OFFSETS = 0x0111
_GPS_LATITUDE_REF = 0x0001
_GPS_LATITUDE = 0x0002
_ICC_PROFILE = 0x8773
_XMP = 0x02BC
_IPTC = 0x83BB
_PHOTOSHOP = 0x8649
_IMAGE_SOURCE_DATA = 0x935C

_BYTE = 1

_ASCII = 2
_SHORT = 3
_LONG = 4
_RATIONAL = 5
_UNDEFINED = 7
_FLOAT = 11

_MAKER_NOTE_BYTES = bytes(range(40))
_HEADER_BYTES = 8


def _entry(tag: int, field_type: int, count: int, value: bytes) -> tuple[int, int, int, bytes]:
    return (tag, field_type, count, value)


def _block(
    byte_order: str,
    ifd0: list[tuple[int, int, int, bytes]],
    exif_ifd: list[tuple[int, int, int, bytes]] | None = None,
    gps_ifd: list[tuple[int, int, int, bytes]] | None = None,
) -> bytes:
    """A TIFF-structured EXIF block laid out as cameras write it: IFD0, then the sub-IFDs, values after each."""
    marker = b"II"
    if byte_order == ">":
        marker = b"MM"
    header = marker + struct.pack(f"{byte_order}HI", 42, 8)
    directories = [list(ifd0)]
    pointer_tags = []
    if exif_ifd is not None:
        pointer_tags.append(EXIF_OFFSET_TAG)
        directories.append(list(exif_ifd))
    if gps_ifd is not None:
        pointer_tags.append(EXIF_GPSINFO_TAG)
        directories.append(list(gps_ifd))
    # Placeholder pointers, so IFD0's size is known before the sub-IFD offsets are
    for tag in pointer_tags:
        directories[0].append(_entry(tag, _LONG, 1, bytes(4)))

    offsets = []
    offset = len(header)
    for directory in directories:
        offsets.append(offset)
        offset += _directory_size(directory)
    for index, tag in enumerate(pointer_tags):
        directories[0] = [
            _entry(tag, _LONG, 1, struct.pack(f"{byte_order}I", offsets[index + 1])) if entry[0] == tag else entry
            for entry in directories[0]
        ]
    return header + b"".join(
        _directory(directory, byte_order, start) for directory, start in zip(directories, offsets, strict=True)
    )


def _directory_size(entries: list[tuple[int, int, int, bytes]]) -> int:
    size = 2 + 12 * len(entries) + 4
    for _, _, _, value in entries:
        if len(value) > 4:  # noqa: PLR2004
            size += len(value) + len(value) % 2
    return size


def _directory(entries: list[tuple[int, int, int, bytes]], byte_order: str, start: int) -> bytes:
    entries = sorted(entries)
    values_offset = start + 2 + 12 * len(entries) + 4
    table = struct.pack(f"{byte_order}H", len(entries))
    values = b""
    for tag, field_type, count, value in entries:
        if len(value) > 4:  # noqa: PLR2004
            table += struct.pack(f"{byte_order}HHII", tag, field_type, count, values_offset + len(values))
            values += value + bytes(len(value) % 2)
        else:
            table += struct.pack(f"{byte_order}HHI", tag, field_type, count) + value.ljust(4, b"\x00")
    return table + struct.pack(f"{byte_order}I", 0) + values


def _ascii(tag: int, text: str) -> tuple[int, int, int, bytes]:
    data = text.encode() + b"\x00"
    return _entry(tag, _ASCII, len(data), data)


def _camera_block(byte_order: str) -> bytes:
    def short(value: int) -> bytes:
        return struct.pack(f"{byte_order}H", value)

    def rationals(*values: tuple[int, int]) -> bytes:
        return b"".join(struct.pack(f"{byte_order}II", *value) for value in values)

    return _block(
        byte_order,
        ifd0=[
            _ascii(_MAKE, "Camera Maker"),
            _entry(EXIF_ORIENTATION_TAG, _SHORT, 1, short(6)),
            _entry(_X_RESOLUTION, _RATIONAL, 1, rationals((300, 1))),
            _entry(_UNKNOWN_TAG, _SHORT, 3, short(1) + short(2) + short(3)),
        ],
        exif_ifd=[
            _entry(_EXPOSURE_TIME, _RATIONAL, 1, rationals((1, 250))),
            _ascii(_DATETIME_ORIGINAL, "2024:01:02 03:04:05"),
            _entry(_MAKER_NOTE, _UNDEFINED, len(_MAKER_NOTE_BYTES), _MAKER_NOTE_BYTES),
        ],
        gps_ifd=[
            _ascii(_GPS_LATITUDE_REF, "N"),
            _entry(_GPS_LATITUDE, _RATIONAL, 3, rationals((40, 1), (26, 1), (4630, 100))),
        ],
    )


@pytest.mark.parametrize("byte_order", ["<", ">"])
class TestParsing:
    def test_reads_ifd0_exif_and_gps_tags(self, byte_order: str) -> None:
        exif = Exif.from_bytes(_camera_block(byte_order))

        assert exif[_MAKE] == "Camera Maker"
        assert exif.orientation == 6  # noqa: PLR2004
        assert exif.swaps_dimensions
        assert exif[_X_RESOLUTION] == ExifRational(300, 1)
        assert exif[_EXPOSURE_TIME] == ExifRational(1, 250)
        assert float(exif[_EXPOSURE_TIME]) == pytest.approx(0.004)
        assert exif[_DATETIME_ORIGINAL] == "2024:01:02 03:04:05"
        assert exif[_MAKER_NOTE] == _MAKER_NOTE_BYTES
        assert exif[_UNKNOWN_TAG] == (1, 2, 3)
        assert exif.gps == {
            _GPS_LATITUDE_REF: "N",
            _GPS_LATITUDE: (ExifRational(40, 1), ExifRational(26, 1), ExifRational(4630, 100)),
        }

    def test_round_trips_unknown_tags_and_maker_notes(self, byte_order: str) -> None:
        exif = Exif.from_bytes(_camera_block(byte_order))

        reread = Exif.from_bytes(exif.to_bytes())

        assert reread == exif
        assert reread[_MAKER_NOTE] == _MAKER_NOTE_BYTES
        assert reread[_UNKNOWN_TAG] == (1, 2, 3)
        assert reread.gps == exif.gps

    def test_keeps_the_byte_order(self, byte_order: str) -> None:
        exif = Exif.from_bytes(_camera_block(byte_order))
        exif[_MAKE] = "Other Maker"

        written = exif.to_bytes()

        expected = b"II"
        if byte_order == ">":
            expected = b"MM"
        assert written[:2] == expected

    def test_accepts_the_exif_marker(self, byte_order: str) -> None:
        exif = Exif.from_bytes(EXIF_HEADER + _camera_block(byte_order))

        assert exif[_MAKE] == "Camera Maker"

    def test_user_comment_round_trips_unicode(self, byte_order: str) -> None:
        exif = Exif.from_bytes(_camera_block(byte_order))
        exif[EXIF_USER_COMMENT_TAG] = '{"prompt": "café ☕"}'

        reread = Exif.from_bytes(exif.to_bytes())

        assert reread[EXIF_USER_COMMENT_TAG] == '{"prompt": "café ☕"}'

    def test_equal_whatever_the_byte_order(self, byte_order: str) -> None:
        other_order = "<"
        if byte_order == "<":
            other_order = ">"

        assert Exif.from_bytes(_camera_block(byte_order)) == Exif.from_bytes(_camera_block(other_order))

    def test_keeps_a_long_array_packed(self, byte_order: str) -> None:
        # One SHORT tag holding a million values: as Python ints they'd take ~18x the block's size
        values = struct.pack(f"{byte_order}1000000H", *([1000] * 1_000_000))
        block = _block(byte_order, [_ascii(_MAKE, "Camera Maker"), _entry(_UNKNOWN_TAG, _SHORT, 1_000_000, values)])

        tracemalloc.start()
        try:
            exif = Exif.from_bytes(block)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert peak < 2 * len(block)
        assert exif[_MAKE] == "Camera Maker"
        assert exif[_UNKNOWN_TAG][:2] == (1000, 1000)
        assert len(exif[_UNKNOWN_TAG]) == 1_000_000  # noqa: PLR2004
        assert Exif.from_bytes(exif.to_bytes()) == exif

    def test_other_readers_see_the_written_tags(self, byte_order: str) -> None:
        exif = Exif.from_bytes(_camera_block(byte_order))
        exif[EXIF_USER_COMMENT_TAG] = "hello"
        jpeg = encode(solid(4, 4), "jpg")
        jpeg = jpeg[:2] + _app1(EXIF_HEADER + exif.to_bytes()) + jpeg[2:]

        spec = read_spec(jpeg)

        assert spec.getattribute("Make") == "Camera Maker"
        assert spec.getattribute("Exif:DateTimeOriginal") == "2024:01:02 03:04:05"
        assert spec.getattribute("GPS:LatitudeRef") == "N"


def _app1(payload: bytes) -> bytes:
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


class TestUserComment:
    def test_ascii_comment_reads_as_text(self) -> None:
        comment = b"ASCII\x00\x00\x00hello"
        exif = Exif.from_bytes(
            _block("<", [], exif_ifd=[_entry(EXIF_USER_COMMENT_TAG, _UNDEFINED, len(comment), comment)])
        )

        assert exif[EXIF_USER_COMMENT_TAG] == "hello"

    def test_unicode_comment_reads_in_the_blocks_byte_order(self) -> None:
        comment = b"UNICODE\x00" + "hé".encode("utf-16-be")
        exif = Exif.from_bytes(
            _block(">", [], exif_ifd=[_entry(EXIF_USER_COMMENT_TAG, _UNDEFINED, len(comment), comment)])
        )

        assert exif[EXIF_USER_COMMENT_TAG] == "hé"

    def test_written_comment_lands_in_the_exif_ifd(self) -> None:
        exif = Exif()
        exif[EXIF_USER_COMMENT_TAG] = "hello"

        reread = Exif.from_bytes(exif.to_bytes())

        assert EXIF_USER_COMMENT_TAG in reread._exif_ifd
        assert EXIF_USER_COMMENT_TAG not in reread._ifd0
        assert reread[EXIF_USER_COMMENT_TAG] == "hello"


class TestEditing:
    def test_new_exif_is_empty(self) -> None:
        exif = Exif()

        assert not exif
        assert exif.orientation == 1
        assert Exif.from_bytes(exif.to_bytes()) == exif

    def test_sets_numbers_text_and_rationals(self) -> None:
        exif = Exif()
        exif[_MAKE] = "Camera Maker"
        exif[EXIF_ORIENTATION_TAG] = 3
        exif[_X_RESOLUTION] = ExifRational(72, 1)

        reread = Exif.from_bytes(exif.to_bytes())

        assert dict(reread.items()) == {
            _MAKE: "Camera Maker",
            EXIF_ORIENTATION_TAG: 3,
            _X_RESOLUTION: ExifRational(72, 1),
        }

    def test_refuses_to_set_a_sub_ifd_pointer(self) -> None:
        with pytest.raises(ValueError, match="locates other tags"):
            Exif()[EXIF_GPSINFO_TAG] = 0

    @pytest.mark.parametrize(
        "value",
        [
            1e10,
            math.inf,
            math.nan,
            ExifRational(2**33, 1),
            # Signed, so the denominator must fit a signed 32-bit integer
            ExifRational(-1, 2**32 - 1),
            (ExifRational(1, 2), ExifRational(-(2**31) - 1, 1)),
        ],
    )
    def test_refuses_a_rational_out_of_range(self, value: object) -> None:
        exif = Exif()
        exif[_MAKE] = "Camera Maker"

        with pytest.raises(ValueError, match="Attempted to set EXIF tag"):
            exif[_X_RESOLUTION] = value

        assert _X_RESOLUTION not in exif
        assert Exif.from_bytes(exif.to_bytes()) == exif

    def test_refuses_a_value_too_large_for_an_existing_float_tag(self) -> None:
        exif = Exif.from_bytes(_block("<", [_entry(_X_RESOLUTION, _FLOAT, 1, struct.pack("<f", 1.5))]))

        with pytest.raises(ValueError, match="too large"):
            exif[_X_RESOLUTION] = 1e40

        assert exif[_X_RESOLUTION] == 1.5  # noqa: PLR2004
        exif.to_bytes()

    def test_stores_a_negative_float_as_a_signed_rational(self) -> None:
        exif = Exif()
        exif[_EXPOSURE_BIAS] = -0.3333

        reread = Exif.from_bytes(exif.to_bytes())

        assert float(reread[_EXPOSURE_BIAS]) == pytest.approx(-0.3333)

    def test_copy_is_independent(self) -> None:
        exif = Exif.from_bytes(_camera_block("<"))
        copied = exif.copy()
        copied[_MAKE] = "Other Maker"

        assert exif[_MAKE] == "Camera Maker"
        assert copied != exif

    def test_missing_tag(self) -> None:
        exif = Exif()

        assert exif.get(_MAKE) is None
        assert _MAKE not in exif
        with pytest.raises(KeyError):
            _ = exif[_MAKE]


class TestGarbage:
    @pytest.mark.parametrize(
        "block",
        [
            b"",
            b"not a TIFF block",
            b"II*\x00",
            b"II\x2b\x00\x08\x00\x00\x00",
            # IFD0 offset past the end
            b"II*\x00\xff\xff\x00\x00",
            # An entry count claiming more entries than there are bytes
            b"II*\x00\x08\x00\x00\x00\xff\xff",
        ],
    )
    def test_reads_as_empty(self, block: bytes) -> None:
        assert not Exif.from_bytes(block)

    def test_overlapping_entries_decode_no_more_than_the_block_holds(self) -> None:
        # Thousands of entries each claiming most of the block as its values, as a hostile block can
        entry_count = 5000
        values_per_entry = 25_000
        table = struct.pack("<H", entry_count) + b"".join(
            struct.pack("<HHII", 0x1000 + index, _SHORT, values_per_entry, _HEADER_BYTES)
            for index in range(entry_count)
        )
        block = b"II*\x00" + struct.pack("<I", _HEADER_BYTES) + table + struct.pack("<I", 0)

        exif = Exif.from_bytes(block)

        decoded = sum(len(value) for _, value in exif.items())
        assert 0 < decoded <= len(block) // 2

    def test_skips_an_entry_whose_value_is_out_of_bounds(self) -> None:
        block = bytearray(_block("<", [_ascii(_MAKE, "Camera Maker"), _ascii(0x0110, "Model Name")]))
        # Point Make's value past the end
        struct.pack_into("<I", block, 8 + 2 + 8, 10_000)

        exif = Exif.from_bytes(bytes(block))

        assert _MAKE not in exif
        assert exif[0x0110] == "Model Name"

    def test_stops_at_an_ifd_that_points_to_itself(self) -> None:
        block = bytearray(_block("<", [_ascii(_MAKE, "Camera Maker")]))
        # IFD0's next-IFD offset points back at IFD0
        next_offset = 8 + 2 + 12
        struct.pack_into("<I", block, next_offset, 8)

        exif = Exif.from_bytes(bytes(block))

        assert exif[_MAKE] == "Camera Maker"

    def test_ignores_a_sub_ifd_pointer_out_of_bounds(self) -> None:
        block = _block(
            "<", [_ascii(_MAKE, "Camera Maker"), _entry(EXIF_OFFSET_TAG, _LONG, 1, struct.pack("<I", 10_000))]
        )

        exif = Exif.from_bytes(block)

        assert exif[_MAKE] == "Camera Maker"


class TestTiff:
    def test_reads_tags_of_a_tiff_file_without_its_structure(self) -> None:
        tiff = encode(solid(8, 4), "tif", Make="Camera Maker")

        exif = tiff_metadata(tiff).exif

        assert exif[_MAKE] == "Camera Maker"
        # Width, strip layout, and the like describe the file, not the picture
        assert _IMAGE_WIDTH not in exif
        assert _STRIP_OFFSETS not in exif

    def test_replaces_the_tags_of_a_tiff_file(self) -> None:
        tiff = encode(solid(8, 4), "tif", Make="Old Maker")
        exif = Exif.from_bytes(_camera_block(">"))

        rewritten = replace_tiff_exif(tiff, exif)

        assert rewritten is not None
        spec = read_spec(rewritten)
        assert (spec.width, spec.height) == (8, 4)
        assert spec.getattribute("Make") == "Camera Maker"
        assert spec.getattribute("Exif:DateTimeOriginal") == "2024:01:02 03:04:05"
        assert tiff_metadata(rewritten).exif.gps == exif.gps

    def test_refuses_data_that_is_not_a_classic_tiff(self) -> None:
        assert replace_tiff_exif(b"II\x2b\x00" + bytes(12), Exif()) is None

    def test_reads_the_icc_profile_with_the_exif(self) -> None:
        tiff = encode(solid(8, 4), "tif", icc_profile=SRGB_PROFILE, Make="Camera Maker")

        metadata = tiff_metadata(tiff)

        assert metadata.icc_profile == SRGB_PROFILE
        assert metadata.exif[_MAKE] == "Camera Maker"
        assert _ICC_PROFILE not in metadata.exif

    def test_leaves_other_metadata_standards_out_of_the_exif(self) -> None:
        other_tags = {
            _XMP: (_BYTE, b"<x:xmpmeta/>" * 6000),
            _IPTC: (_UNDEFINED, b"\x1c\x02\x00\x00\x02\x00\x04"),
            _PHOTOSHOP: (_BYTE, b"8BIM" + bytes(20)),
            _IMAGE_SOURCE_DATA: (_UNDEFINED, b"Adobe Photoshop Document Data Block\x00"),
        }
        tiff = gray_tiff({_MAKE: (_ASCII, b"Camera Maker\x00"), **other_tags})

        exif = tiff_metadata(tiff).exif

        assert exif[_MAKE] == "Camera Maker"
        assert not any(tag in exif for tag in other_tags)
        assert tiff_extra_metadata_kinds(tiff) == ["XMP", "IPTC", "Photoshop data"]

    def test_writes_no_other_metadata_standards_from_the_exif(self) -> None:
        exif = Exif()
        exif[_MAKE] = "Camera Maker"
        exif[_XMP] = b"<x:xmpmeta/>"

        rewritten = replace_tiff_exif(gray_tiff({}), exif)

        assert rewritten is not None
        assert tiff_metadata(rewritten).exif[_MAKE] == "Camera Maker"
        assert tiff_extra_metadata_kinds(rewritten) == []

    def test_leaves_out_the_xmp_stamp_openimageio_writes(self) -> None:
        stamp = (
            b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?> <x:xmpmeta xmlns:x="adobe:ns:meta/">'
            b' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"> <rdf:Description rdf:about=""'
            b' xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:CreateDate="2026:01:02 03:04:05"/> </rdf:RDF>'
            b' </x:xmpmeta> <?xpacket end="w"?>'
        )
        titled = stamp.replace(b'xmp:CreateDate="2026:01:02 03:04:05"', b'xmp:CreateDate="2026:01:02" dc:title="A"')

        assert tiff_extra_metadata_kinds(gray_tiff({_XMP: (_BYTE, stamp)})) == []
        assert tiff_extra_metadata_kinds(gray_tiff({_XMP: (_BYTE, titled)})) == ["XMP"]
        assert tiff_extra_metadata_kinds(encode(solid(8, 4), "tif")) == []

    def test_names_no_other_metadata_for_data_that_is_not_tiff(self) -> None:
        assert tiff_extra_metadata_kinds(b"not a tiff") == []


class TestTagTables:
    def test_names_common_tags(self) -> None:
        assert TAGS[_MAKE] == "Make"
        assert TAGS[_DATETIME_ORIGINAL] == "DateTimeOriginal"
        assert GPS_TAGS[_GPS_LATITUDE] == "GPSLatitude"

    def test_exif_ifd_tags_exclude_ifd0_tags_and_pointers(self) -> None:
        assert _EXPOSURE_TIME in EXIF_IFD_TAGS
        assert EXIF_USER_COMMENT_TAG in EXIF_IFD_TAGS
        assert _MAKE not in EXIF_IFD_TAGS
        assert EXIF_GPSINFO_TAG not in EXIF_IFD_TAGS
