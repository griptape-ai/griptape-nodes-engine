"""Audio artifact provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from griptape_nodes.retained_mode.managers.artifact_providers.base_artifact_provider import (
    BaseArtifactMetadata,
    BaseArtifactProvider,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.artifact_providers.provider_registry import ProviderRegistry


# MPEG audio frame-sync markers used by detect_format below.
# An MPEG audio frame starts with 11 sync bits (byte 0 == 0xFF, top 3 bits of
# byte 1 == 111); bits 1-2 of byte 1 are the "layer" field. Layers I/II/III map
# to MPEG audio (.mp3 etc.); layer=00 is reserved in MPEG audio and is what
# ADTS (.aac) uses to mark its frame header, so the layer bits are the
# unambiguous way to tell ADTS from MP3.
_MPEG_FRAME_SYNC_BYTE = 0xFF
_MPEG_FRAME_SYNC_MASK = 0xE0
_MPEG_FRAME_SYNC_VALUE = 0xE0
_MPEG_LAYER_MASK = 0x06
_MPEG_LAYER_ADTS = 0x00


class AudioArtifactProvider(BaseArtifactProvider):
    """Provider for audio artifacts.

    Currently a minimal provider used only for byte-content sniffing via
    ``detect_format``. It does not perform metadata extraction or preview
    generation; if/when those are needed, add them alongside the byte
    sniffer rather than splitting the responsibility.
    """

    # Minimum bytes needed for the magic-byte sniffer below. ISO BMFF brand
    # sits at offset 8-12; RIFF/Ogg/FLAC/MPEG headers fit inside 12 bytes.
    _SNIFF_MIN_HEADER_BYTES: ClassVar[int] = 12

    def __init__(self, registry: ProviderRegistry) -> None:
        """Initialize the audio artifact provider.

        Args:
            registry: The ProviderRegistry that manages this provider
        """
        super().__init__(registry)

    @classmethod
    def get_friendly_name(cls) -> str:
        return "Audio"

    @classmethod
    def get_supported_formats(cls) -> set[str]:
        return {"mp3", "wav", "flac", "ogg", "opus", "m4a", "m4b", "aac"}

    @classmethod
    def detect_format(cls, data: bytes) -> str | None:  # noqa: C901, PLR0911
        """Magic-byte sniff for common audio container/codec formats."""
        if len(data) < cls._SNIFF_MIN_HEADER_BYTES:
            return None
        head = data[: cls._SNIFF_MIN_HEADER_BYTES]
        if head[:3] == b"ID3":
            return "mp3"
        if head[0] == _MPEG_FRAME_SYNC_BYTE and (head[1] & _MPEG_FRAME_SYNC_MASK) == _MPEG_FRAME_SYNC_VALUE:
            if (head[1] & _MPEG_LAYER_MASK) == _MPEG_LAYER_ADTS:
                return "aac"
            return "mp3"
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            return "wav"
        if head[:4] == b"fLaC":
            return "flac"
        if head[:4] == b"OggS":
            if b"OpusHead" in data[:128]:
                return "opus"
            return "ogg"
        if head[4:8] == b"ftyp":
            if head[8:12] == b"M4A ":
                return "m4a"
            if head[8:12] == b"M4B ":
                return "m4b"
        return None

    @classmethod
    def get_artifact_metadata(cls, source_path: str) -> BaseArtifactMetadata | None:  # noqa: ARG003
        """Audio metadata extraction is not implemented; returns None."""
        return None
