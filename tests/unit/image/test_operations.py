"""Editing pixels through GriptapeImagePipeline: geometry, color, filters, compositing, and drawing."""

import numpy as np
import pytest

from griptape_nodes.image import (
    GriptapeImage,
    GriptapeImagePipeline,
    ImageFormat,
    ImageMetadata,
    ResizeFilter,
    TextAlign,
    text_size,
)
from tests.unit.image.sample_images import BLUE, RED, SRGB_PROFILE, WHITE, decode, encode, noise, solid

_BLACK = (0, 0, 0)


def _image(pixels: np.ndarray) -> GriptapeImage:
    return GriptapeImage.from_array(pixels)


def _edited(pixels: np.ndarray, edit: object) -> np.ndarray:
    """Run ``edit`` (a function of the pipeline) over ``pixels`` and return the result's pixels."""
    with _image(pixels) as image, edit(GriptapeImagePipeline(image)).end() as result:  # type: ignore[operator]
        return result.to_array()


class TestArrays:
    def test_from_array_and_to_array_round_trip(self) -> None:
        pixels = noise(5, 3, 4)
        with GriptapeImage.from_array(pixels) as image:
            assert (image.width, image.height, image.channels) == (5, 3, 4)
            assert np.array_equal(image.to_array(), pixels)

    def test_from_array_accepts_two_dimensional_gray(self) -> None:
        with GriptapeImage.from_array(np.full((3, 4), 7, dtype=np.uint8)) as image:
            assert image.channels == 1
            assert image.to_array().shape == (3, 4, 1)

    def test_from_array_copies_the_pixels(self) -> None:
        pixels = solid(2, 2, RED)
        with GriptapeImage.from_array(pixels) as image:
            pixels[:] = 0
            assert tuple(image.to_array()[0, 0]) == RED

    @pytest.mark.parametrize("shape", [(2,), (2, 2, 5), (2, 2, 0)])
    def test_from_array_rejects_other_shapes(self, shape: tuple[int, ...]) -> None:
        with pytest.raises(ValueError, match="Failed because"):
            GriptapeImage.from_array(np.zeros(shape, dtype=np.uint8))

    def test_from_array_keeps_float_pixels(self) -> None:
        pixels = np.full((2, 2, 3), 0.25, dtype=np.float32)
        with GriptapeImage.from_array(pixels) as image:
            assert np.allclose(image.to_array(), 0.25)

    def test_new_fills_with_one_color(self) -> None:
        with GriptapeImage.new(3, 2, (*BLUE, 128)) as image:
            assert (image.width, image.height, image.channels) == (3, 2, 4)
            assert tuple(image.to_array()[1, 2]) == (*BLUE, 128)

    def test_new_encodes(self) -> None:
        with GriptapeImage.new(3, 2, RED) as image:
            assert tuple(decode(image.encode(ImageFormat.PNG))[0, 0]) == RED

    def test_new_rejects_out_of_range_colors(self) -> None:
        with pytest.raises(ValueError, match="Failed because"):
            GriptapeImage.new(2, 2, (300, 0, 0))

    def test_from_array_accepts_big_endian_pixels(self) -> None:
        pixels = np.full((2, 2, 3), 1000, dtype=">u2")
        with GriptapeImage.from_array(pixels) as image:
            assert np.all(image.to_array() == 1000)  # noqa: PLR2004


class TestGeometry:
    def test_resize_changes_dimensions(self) -> None:
        result = _edited(solid(40, 20, RED), lambda p: p.resize(10, 30))
        assert result.shape == (30, 10, 3)
        assert tuple(result[15, 5]) == RED

    def test_nearest_resize_keeps_exact_values(self) -> None:
        pixels = noise(4, 4)
        result = _edited(pixels, lambda p: p.resize(8, 8, ResizeFilter.NEAREST))
        assert set(map(tuple, result.reshape(-1, 3))) <= set(map(tuple, pixels.reshape(-1, 3)))

    def test_resize_keeps_transparent_color_out_of_edges(self) -> None:
        pixels = solid(8, 8, (*RED, 255))
        pixels[:, 4:] = (0, 255, 0, 0)
        result = _edited(pixels, lambda p: p.resize(16, 16, ResizeFilter.BILINEAR))
        # Color next to the transparent half is still red, not tinted green
        red, green, _, alpha = result[8, 7]
        assert alpha > 0
        assert red > 200 > green  # noqa: PLR2004

    def test_crop_takes_the_box(self) -> None:
        pixels = noise(10, 8)
        result = _edited(pixels, lambda p: p.crop(2, 1, 6, 5))
        assert np.array_equal(result, pixels[1:5, 2:6])

    def test_crop_outside_the_image_fills_with_zero(self) -> None:
        result = _edited(solid(4, 4, RED), lambda p: p.crop(-2, 0, 4, 4))
        assert result.shape == (4, 6, 3)
        assert tuple(result[0, 0]) == _BLACK
        assert tuple(result[0, 3]) == RED

    def test_crop_rejects_an_empty_box(self) -> None:
        with pytest.raises(ValueError, match="Failed because"):
            _edited(solid(4, 4), lambda p: p.crop(3, 0, 3, 4))

    @pytest.mark.parametrize(("degrees", "corner"), [(90, (0, 0)), (180, (1, 0)), (270, (3, 1)), (-90, (3, 1))])
    def test_quarter_turns_are_lossless_and_counterclockwise(self, degrees: int, corner: tuple[int, int]) -> None:
        pixels = solid(4, 2, BLUE)
        pixels[0, 3] = RED
        result = _edited(pixels, lambda p: p.rotate(degrees, expand=True))
        assert result.shape[:2] == ((2, 4) if degrees % 180 == 0 else (4, 2))
        assert tuple(result[corner]) == RED
        assert int((result == RED).all(axis=2).sum()) == 1

    def test_rotate_without_expand_keeps_size_and_fills_corners(self) -> None:
        result = _edited(solid(20, 10, RED), lambda p: p.rotate(30, fill=WHITE))
        assert result.shape == (10, 20, 3)
        assert tuple(result[0, 0]) == WHITE
        assert tuple(result[5, 10]) == RED

    def test_rotate_with_expand_fits_the_turned_image(self) -> None:
        result = _edited(solid(20, 10, (*RED, 255)), lambda p: p.rotate(45, expand=True))
        # ceil of 20 * cos 45 + 10 * sin 45
        assert result.shape[:2] == (22, 22)
        assert result[0, 0, 3] == 0
        assert tuple(result[11, 11]) == (*RED, 255)

    @pytest.mark.parametrize(
        ("width", "height", "degrees", "expected"),
        [(37, 23, 30, (39, 45)), (37, 23, -30, (39, 45)), (36, 22, 30, (38, 44)), (37, 22, 10, (30, 41))],
    )
    def test_rotate_with_expand_sizes_the_canvas_as_pillow(
        self, width: int, height: int, degrees: int, expected: tuple[int, int]
    ) -> None:
        result = _edited(solid(width, height, RED), lambda p: p.rotate(degrees, expand=True))
        assert result.shape[:2] == expected

    def test_rotate_fill_shows_only_past_the_turned_image(self) -> None:
        result = _edited(
            solid(21, 21, (*RED, 128)), lambda p: p.rotate(45, fill=(*BLUE, 255), resize_filter=ResizeFilter.NEAREST)
        )
        # Inside, the image keeps its own alpha; outside, the opaque fill
        assert tuple(result[10, 10]) == (*RED, 128)
        assert tuple(result[0, 0]) == (*BLUE, 255)

    def test_rotate_rejects_a_non_finite_angle(self) -> None:
        with pytest.raises(ValueError, match="Failed because"):
            _edited(solid(4, 4), lambda p: p.rotate(float("nan")))

    def test_float_resize_keeps_alpha_in_range(self) -> None:
        pixels = np.zeros((9, 15, 4), dtype=np.float32)
        pixels[:, ::2] = (1.0, 0.5, 0.0, 1.0)
        result = _edited(pixels, lambda p: p.resize(31, 17, ResizeFilter.LANCZOS))
        assert result[:, :, 3].min() >= 0.0
        assert result[:, :, 3].max() <= 1.0
        assert result[:, :, :3].min() >= 0.0
        assert result[:, :, :3].max() <= 1.0

    @pytest.mark.parametrize("resize_filter", [ResizeFilter.BILINEAR, ResizeFilter.LANCZOS])
    def test_resize_gray_with_alpha(self, resize_filter: ResizeFilter) -> None:
        result = _edited(solid(9, 9, (200, 128)), lambda p: p.resize(4, 4, resize_filter))
        assert result.shape == (4, 4, 2)
        assert tuple(result[2, 2]) == (200, 128)

    def test_rotate_turns_counterclockwise(self) -> None:
        pixels = solid(21, 21, BLUE)
        pixels[10, 15:] = RED
        result = _edited(pixels, lambda p: p.rotate(30))
        # The red ray to the right of center turns up and to the right
        red, _, blue = result[7, 16]
        assert red > blue

    def test_flips(self) -> None:
        pixels = noise(5, 3)
        assert np.array_equal(_edited(pixels, lambda p: p.flip_horizontal()), pixels[:, ::-1])
        assert np.array_equal(_edited(pixels, lambda p: p.flip_vertical()), pixels[::-1])

    def test_pad_adds_a_border(self) -> None:
        result = _edited(solid(2, 2, RED), lambda p: p.pad(1, 2, 3, 4, WHITE))
        assert result.shape == (8, 6, 3)
        assert tuple(result[0, 0]) == WHITE
        assert tuple(result[2, 1]) == RED

    def test_pad_rejects_negative_sizes(self) -> None:
        with pytest.raises(ValueError, match="Failed because"):
            _edited(solid(2, 2), lambda p: p.pad(-1, 0, 0, 0))


class TestColor:
    def test_to_gray_uses_luma(self) -> None:
        result = _edited(solid(2, 2, RED), lambda p: p.to_gray())
        assert result.shape == (2, 2, 1)
        assert int(result[0, 0, 0]) == round(255 * 0.299)

    def test_to_gray_discards_alpha(self) -> None:
        result = _edited(solid(2, 2, (*WHITE, 100)), lambda p: p.to_gray())
        assert tuple(result[0, 0]) == (255,)

    def test_brightness_scales_and_keeps_alpha(self) -> None:
        result = _edited(solid(2, 2, (100, 50, 200, 77)), lambda p: p.brightness(1.5))
        assert tuple(result[0, 0]) == (150, 75, 255, 77)

    def test_contrast_zero_is_the_mean_gray(self) -> None:
        pixels = solid(2, 2, _BLACK)
        pixels[0] = WHITE
        result = _edited(pixels, lambda p: p.contrast(0))
        assert np.all(result == 128)  # noqa: PLR2004

    def test_saturation_zero_is_gray(self) -> None:
        result = _edited(solid(2, 2, RED), lambda p: p.saturation(0))
        assert len(set(result[0, 0])) == 1

    def test_invert_keeps_alpha(self) -> None:
        result = _edited(solid(2, 2, (10, 20, 30, 40)), lambda p: p.invert())
        assert tuple(result[0, 0]) == (245, 235, 225, 40)

    def test_to_uint8_converts_sixteen_bit(self) -> None:
        pixels = np.full((2, 2, 3), 65535, dtype=np.uint16)
        result = _edited(pixels, lambda p: p.to_uint8())
        assert result.dtype == np.uint8
        assert np.all(result == 255)  # noqa: PLR2004


class TestFilters:
    def test_gaussian_blur_softens_an_edge(self) -> None:
        pixels = solid(20, 4, _BLACK)
        pixels[:, 10:] = WHITE
        result = _edited(pixels, lambda p: p.gaussian_blur(2))
        assert 0 < int(result[2, 9, 0]) < 128 < int(result[2, 10, 0]) < 255  # noqa: PLR2004
        assert tuple(result[2, 0]) == _BLACK

    def test_gaussian_blur_keeps_a_flat_image(self) -> None:
        assert np.array_equal(_edited(solid(8, 8, RED), lambda p: p.gaussian_blur(3)), solid(8, 8, RED))

    def test_dilate_and_erode(self) -> None:
        pixels = solid(7, 7, 0)
        pixels[3, 3] = 255
        dilated = _edited(pixels, lambda p: p.dilate(3))
        assert int((dilated == 255).sum()) == 9  # noqa: PLR2004
        assert int((_edited(dilated, lambda p: p.erode(3)) == 255).sum()) == 1  # noqa: PLR2004

    @pytest.mark.parametrize("size", [0, 2, -3])
    def test_dilate_rejects_sizes_that_are_not_odd_and_positive(self, size: int) -> None:
        with pytest.raises(ValueError, match="Failed because"):
            _edited(solid(4, 4), lambda p: p.dilate(size))

    def test_find_edges_leaves_the_border(self) -> None:
        pixels = noise(6, 6)
        result = _edited(pixels, lambda p: p.find_edges())
        assert np.array_equal(result[0], pixels[0])
        assert np.array_equal(result[:, -1], pixels[:, -1])

    def test_find_edges_is_zero_on_flat_color(self) -> None:
        pixels = solid(8, 8, _BLACK)
        pixels[:, 4:] = WHITE
        result = _edited(pixels, lambda p: p.find_edges())
        assert tuple(result[4, 0]) == _BLACK
        assert tuple(result[4, 4]) == WHITE


class TestCompositing:
    def test_composite_places_the_overlay(self) -> None:
        with GriptapeImage.new(2, 2, RED) as overlay:
            result = _edited(solid(6, 6, BLUE), lambda p: p.composite(overlay, 3, 1))
        assert tuple(result[1, 3]) == RED
        assert tuple(result[0, 0]) == BLUE

    def test_composite_blends_by_alpha_and_opacity(self) -> None:
        with GriptapeImage.new(1, 1, (*WHITE, 255)) as overlay:
            result = _edited(solid(1, 1, _BLACK), lambda p: p.composite(overlay, opacity=0.5))
        assert tuple(result[0, 0]) == (128, 128, 128)

    def test_composite_clips_to_the_base(self) -> None:
        with GriptapeImage.new(4, 4, RED) as overlay:
            result = _edited(solid(3, 3, BLUE), lambda p: p.composite(overlay, -2, -2))
        assert tuple(result[1, 1]) == RED
        assert tuple(result[2, 2]) == BLUE

    def test_set_alpha_from_a_mask(self) -> None:
        mask = solid(2, 1, 0)
        mask[0, 1] = 200
        with GriptapeImage.from_array(mask) as alpha:
            result = _edited(solid(2, 1, RED), lambda p: p.set_alpha(alpha))
        assert tuple(result[0, 0]) == (*RED, 0)
        assert tuple(result[0, 1]) == (*RED, 200)

    def test_set_alpha_from_a_value(self) -> None:
        result = _edited(solid(1, 1, RED), lambda p: p.set_alpha(0.5))
        assert tuple(result[0, 0]) == (*RED, 128)

    def test_set_alpha_rejects_a_mask_of_another_size(self) -> None:
        with GriptapeImage.new(3, 3, (0,)) as alpha, pytest.raises(ValueError, match="Failed because"):
            _edited(solid(2, 2, RED), lambda p: p.set_alpha(alpha))

    def test_composite_matches_a_linear_overlay_to_a_display_base(self) -> None:
        # Linear 0.5 is display value 188, as it encodes on its own
        overlay_pixels = np.full((1, 1, 3), 0.5, dtype=np.float32)
        with GriptapeImage.from_array(overlay_pixels) as overlay:
            result = _edited(solid(1, 1, _BLACK), lambda p: p.composite(overlay))
        assert tuple(result[0, 0]) == (188, 188, 188)


class TestDrawing:
    def test_draw_rectangle_fills_and_outlines(self) -> None:
        result = _edited(solid(10, 10, _BLACK), lambda p: p.draw_rectangle(2, 2, 8, 8, fill=RED, outline=WHITE))
        assert tuple(result[2, 2]) == WHITE
        assert tuple(result[5, 5]) == RED
        assert tuple(result[8, 8]) == _BLACK

    def test_draw_line(self) -> None:
        result = _edited(solid(10, 5, _BLACK), lambda p: p.draw_line(0, 2, 9, 2, color=RED))
        assert all(tuple(result[2, x]) == RED for x in range(10))
        assert tuple(result[0, 0]) == _BLACK

    def test_draw_text_paints_inside_its_measured_box(self) -> None:
        size = text_size("Hi", 20)
        result = _edited(solid(100, 50, _BLACK), lambda p: p.draw_text("Hi", 10, 10, size=20, color=RED))
        painted = np.argwhere(result[:, :, 0] > 0)
        assert painted.size > 0
        top, left = painted.min(axis=0)
        bottom, right = painted.max(axis=0)
        assert left >= 10  # noqa: PLR2004
        assert top >= 10  # noqa: PLR2004
        assert right < 10 + size.width + 1
        assert bottom < 10 + size.height + 1

    def test_text_size_grows_with_lines(self) -> None:
        one = text_size("ab", 16)
        two = text_size("ab\nab", 16, spacing=4)
        assert two.width == one.width
        assert two.height == 2 * one.height + 4

    def test_centered_lines_center_in_the_block(self) -> None:
        text = "WWWWWW\no"
        block = text_size(text, 20)
        result = _edited(
            solid(200, 80, _BLACK), lambda p: p.draw_text(text, 10, 5, size=20, color=WHITE, align=TextAlign.CENTER)
        )
        # The short second line sits under the middle of the long first one
        second_line = result[5 + block.height // 2 + 2 :, :, 0]
        columns = np.argwhere(second_line > 0)[:, 1]
        assert abs((columns.min() + columns.max()) / 2 - (10 + block.width / 2)) <= 3  # noqa: PLR2004


class TestApply:
    def test_apply_runs_the_function_on_the_pixels(self) -> None:
        result = _edited(solid(2, 2, RED), lambda p: p.apply(lambda pixels: pixels[:, :, ::-1].copy()))
        assert tuple(result[0, 0]) == BLUE

    def test_apply_rejects_a_non_array(self) -> None:
        with pytest.raises(ValueError, match="Failed because"):
            _edited(solid(2, 2), lambda p: p.apply(lambda _: None))


class TestMetadataCarriesThrough:
    def test_text_and_profile_survive_edits(self) -> None:
        metadata = ImageMetadata(text={"gtn_workflow_name": "demo"}, icc_profile=SRGB_PROFILE)
        with (
            GriptapeImage.from_array(solid(20, 10, RED), metadata) as image,
            GriptapeImagePipeline(image).resize(10, 5).rotate(90, expand=True).brightness(1.1).end() as result,
        ):
            assert result.metadata.text == {"gtn_workflow_name": "demo"}
            assert result.metadata.icc_profile == SRGB_PROFILE
            with GriptapeImage.open(result.encode(ImageFormat.PNG)) as reread:
                assert reread.metadata.text["gtn_workflow_name"] == "demo"


class TestPreviewFormats:
    @pytest.mark.parametrize("image_format", [ImageFormat.WEBP, ImageFormat.GIF])
    def test_gray_encodes_to_color_only_formats(self, image_format: ImageFormat) -> None:
        with GriptapeImage.open(encode(solid(4, 4, 90), "png")) as image:
            pixels = decode(image.encode(image_format))
        assert pixels.shape[2] >= 3  # noqa: PLR2004
        assert tuple(pixels[0, 0, :3]) == (90, 90, 90)

    def test_transparent_gif_keeps_its_color(self) -> None:
        pixels = solid(4, 4, (*RED, 255))
        pixels[0, 0] = (0, 0, 0, 0)
        with GriptapeImage.open(encode(pixels, "gif")) as image:
            assert image.channels == 4  # noqa: PLR2004
            reread = decode(image.encode(ImageFormat.PNG))
        assert tuple(reread[2, 2]) == (*RED, 255)
