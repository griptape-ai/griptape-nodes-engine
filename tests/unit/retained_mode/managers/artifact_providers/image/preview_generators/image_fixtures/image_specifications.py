"""Validated test-only image specifications. Loading never generates image files."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator
from ruamel.yaml import YAML

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_][a-zA-Z0-9_-]*$")]
SampleType = Literal["uint1", "uint8", "uint16", "float16", "float32"]
MetadataValue = str | int | FiniteFloat


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Dimensions(Model):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class DataOrigin(Model):
    x: int = 0
    y: int = 0


class Window(DataOrigin):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class Position(Model):
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class Region(Position):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


Colour = Annotated[list[FiniteFloat], Field(min_length=1)]


class SolidPattern(Model):
    pattern: Literal["solid"]
    colour: Colour

    def channel_values(self) -> list[list[float]]:
        return [self.colour]


class GradientPattern(Model):
    pattern: Literal["gradient"]
    axis: Literal["x", "y"]
    start: Colour
    end: Colour

    def channel_values(self) -> list[list[float]]:
        return [self.start, self.end]


class ColourBarsPattern(Model):
    pattern: Literal["colour_bars"]
    axis: Literal["x", "y"]
    colours: list[Colour] = Field(min_length=1)

    def channel_values(self) -> list[list[float]]:
        return self.colours


class CheckerboardPattern(Model):
    pattern: Literal["checkerboard"]
    cell_dimensions: Dimensions
    colours: list[Colour] = Field(min_length=2, max_length=2)

    def channel_values(self) -> list[list[float]]:
        return self.colours


class QuadrantsPattern(Model):
    pattern: Literal["quadrants"]
    colours: list[Colour] = Field(min_length=4, max_length=4)

    def channel_values(self) -> list[list[float]]:
        return self.colours


Pattern = Annotated[
    SolidPattern | GradientPattern | ColourBarsPattern | CheckerboardPattern | QuadrantsPattern,
    Field(discriminator="pattern"),
]


class Source(Model):
    dimensions: Dimensions
    data_origin: DataOrigin = Field(default_factory=DataOrigin)
    display_window: Window | None = None
    channels: list[str] = Field(min_length=1)
    colour_space: str | None = None
    alpha_association: Literal["straight", "premultiplied"] | None = None
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)
    pixels: Pattern

    @property
    def data_window(self) -> Window:
        return Window(
            x=self.data_origin.x,
            y=self.data_origin.y,
            width=self.dimensions.width,
            height=self.dimensions.height,
        )

    @property
    def resolved_display_window(self) -> Window:
        if self.display_window is None:
            return self.data_window
        return self.display_window

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if len(set(self.channels)) != len(self.channels):
            msg = "Channel names must be unique"
            raise ValueError(msg)
        if self.alpha_association is not None and "A" not in self.channels:
            msg = "Alpha association requires an A channel"
            raise ValueError(msg)
        if any(len(value) != len(self.channels) for value in self.pixels.channel_values()):
            msg = "Pattern colours must match source channels"
            raise ValueError(msg)
        return self


class MetadataChecks(Model):
    equals: dict[str, MetadataValue] = Field(default_factory=dict)
    present: list[str] = Field(default_factory=list)
    absent: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_checks(self) -> Self:
        if set(self.absent) & (set(self.present) | set(self.equals)):
            msg = "Metadata cannot be both present and absent"
            raise ValueError(msg)
        return self


class PixelCheckFields(Model):
    colour: Colour
    max_absolute_error: FiniteFloat = Field(ge=0)


class AllPixelsCheck(PixelCheckFields):
    kind: Literal["all_pixels"]


class SampleCheck(PixelCheckFields):
    kind: Literal["sample"]
    position: Position


class RegionCheck(PixelCheckFields):
    kind: Literal["region"]
    region: Region


PixelCheck = Annotated[AllPixelsCheck | SampleCheck | RegionCheck, Field(discriminator="kind")]


class PixelChecks(Model):
    checks: list[PixelCheck] = Field(min_length=1)


class EncodingChecks(Model):
    quantization: dict[int, list[int]] | None = None
    sampling: int | None = None
    progressive: bool | None = None

    @model_validator(mode="after")
    def validate_encoding(self) -> Self:
        if self.quantization is None and self.sampling is None and self.progressive is None:
            message = "Encoding expectations must contain an assertion"
            raise ValueError(message)
        return self


class Expectations(Model):
    dimensions: Dimensions | None = None
    data_window: Window | None = None
    display_window: Window | None = None
    sample_type: SampleType | None = None
    channels: list[str] | None = Field(default=None, min_length=1)
    metadata: MetadataChecks = Field(default_factory=MetadataChecks)
    pixels: PixelChecks | None = None
    encoding: EncodingChecks | None = None

    @model_validator(mode="after")
    def validate_expectations(self) -> Self:
        if not any(
            (
                self.dimensions,
                self.data_window,
                self.display_window,
                self.sample_type,
                self.channels,
                self.pixels,
                self.encoding,
                self.metadata.equals,
                self.metadata.present,
                self.metadata.absent,
            )
        ):
            message = "Expected results must contain at least one assertion"
            raise ValueError(message)
        return self


class VariantFields(Model):
    id: Identifier
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)
    encoding: dict[str, MetadataValue] = Field(default_factory=dict)
    expect: Expectations | None = None


class PngVariant(VariantFields):
    format: Literal["png"]
    sample_type: Literal["uint1", "uint8", "uint16"]


class JpegVariant(VariantFields):
    format: Literal["jpg", "jpeg"]
    sample_type: Literal["uint8"]


class WebpVariant(VariantFields):
    format: Literal["webp"]
    sample_type: Literal["uint8"]


class TiffVariant(VariantFields):
    format: Literal["tiff", "tif"]
    sample_type: Literal["uint8", "uint16", "float32"]


class ExrVariant(VariantFields):
    format: Literal["exr"]
    sample_type: Literal["float16", "float32"]


class HdrVariant(VariantFields):
    format: Literal["hdr"]
    sample_type: Literal["float32"]


class DpxVariant(VariantFields):
    format: Literal["dpx"]
    sample_type: Literal["uint8", "uint16"]


Variant = Annotated[
    PngVariant | JpegVariant | WebpVariant | TiffVariant | ExrVariant | HdrVariant | DpxVariant,
    Field(discriminator="format"),
]


class Output(Model):
    format: Literal["png", "jpg", "jpeg", "webp"]
    expect: Expectations


class Parameters(Model):
    max_width: int = Field(default=1024, gt=0, le=8192)
    max_height: int = Field(default=1024, gt=0, le=8192)


class Case(Model):
    id: Identifier
    operation: Literal["generate_thumbnail"]
    inputs: list[Identifier] = Field(min_length=1)
    parameters: Parameters = Field(default_factory=Parameters)
    workflow_name: str | None = None
    inject_metadata: bool = False
    preserve_source_metadata: bool = False
    outputs: list[Output] = Field(min_length=1)


class ImageSpecification(Model):
    id: Identifier
    description: str = ""
    source: Source
    variants: list[Variant] = Field(min_length=1)
    cases: list[Case] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_image(self) -> Self:  # noqa: C901 - Validate the finite format/channel capability table together.
        variants = {variant.id for variant in self.variants}
        if len(variants) != len(self.variants) or len({case.id for case in self.cases}) != len(self.cases):
            msg = "Variant and case identifiers must be unique"
            raise ValueError(msg)
        count = len(self.source.channels)
        for variant in self.variants:
            if variant.format != "exr" and {"data_origin", "display_window"} & self.source.model_fields_set:
                msg = "Explicit source windows require EXR variants"
                raise ValueError(msg)
            if variant.format in {"jpg", "jpeg"} and count not in {1, 3}:
                msg = "JPEG sources require grayscale or RGB"
                raise ValueError(msg)
            if variant.format == "webp" and count not in {3, 4}:
                msg = "WebP sources require RGB or RGBA"
                raise ValueError(msg)
            if variant.format == "png" and count > len("RGBA"):
                msg = "PNG sources support at most four channels"
                raise ValueError(msg)
            if variant.sample_type == "uint1" and (count != 1 or self.source.pixels.pattern != "solid"):
                msg = "One-bit sources require a solid grayscale image"
                raise ValueError(msg)
            if variant.sample_type.startswith("uint") and any(
                value < 0 or value > 1 for colour in self.source.pixels.channel_values() for value in colour
            ):
                msg = "Integer source values must be in [0, 1]"
                raise ValueError(msg)
            if variant.format in {"hdr", "dpx"} and count != len("RGB"):
                msg = "HDR and DPX sources require RGB"
                raise ValueError(msg)
        for case in self.cases:
            if not set(case.inputs) <= variants or len(set(case.inputs)) != len(case.inputs):
                msg = "Case inputs must reference unique declared variants"
                raise ValueError(msg)
            if len({output.format for output in case.outputs}) != len(case.outputs):
                msg = "Output formats must be unique within a case"
                raise ValueError(msg)
        return self


class Specification(Model):
    schema_version: Literal[1]
    images: list[ImageSpecification]

    @model_validator(mode="after")
    def validate_identifiers(self) -> Self:
        if len({image.id for image in self.images}) != len(self.images):
            msg = "Image identifiers must be unique"
            raise ValueError(msg)
        return self


@dataclass(frozen=True)
class ExpandedCase:
    image: ImageSpecification
    variant: Variant
    case: Case
    output: Output

    @property
    def id(self) -> str:
        return f"{self.image.id}/{self.variant.id}/{self.case.id}/{self.output.format}"


def load_specifications(path: Path) -> Specification:
    """Load specifications."""
    yaml = YAML(typ="safe")
    yaml.allow_duplicate_keys = False
    with path.open(encoding="utf-8") as stream:
        data = yaml.load(stream)
    return Specification.model_validate(data)


def expand_cases(specification: Specification) -> list[ExpandedCase]:
    """Expand cases."""
    return [
        ExpandedCase(image, variant, case, output)
        for image in specification.images
        for case in image.cases
        for variant in image.variants
        if variant.id in case.inputs
        for output in case.outputs
    ]
