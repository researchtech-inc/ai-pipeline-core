"""Tests for ai_pipeline_core.images public API — process_image."""

from io import BytesIO

import pytest
from PIL import Image

from ai_pipeline_core.llm._images import (
    ImagePart,
    ImagePreset,
    ImageProcessingConfig,
    ImageProcessingError,
    ProcessedImage,
    process_image,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_image_bytes(width: int, height: int, fmt: str = "PNG", color: tuple[int, int, int] = (128, 128, 128)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format=fmt)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# ImagePreset / ImageProcessingConfig
# ---------------------------------------------------------------------------


class TestConfig:
    """Tests for configuration and presets."""

    def test_all_presets_exist(self):
        for preset in ImagePreset:
            config = ImageProcessingConfig.for_preset(preset)
            assert config.max_dimension > 0
            assert config.max_pixels > 0
            assert config.webp_quality > 0

    def test_high_res_preset_values(self):
        config = ImageProcessingConfig.for_preset(ImagePreset.HIGH_RES)
        assert config.max_dimension == 3000
        assert config.max_pixels == 9_000_000
        assert config.webp_quality == 75

    def test_compact_preset_values(self):
        config = ImageProcessingConfig.for_preset(ImagePreset.COMPACT)
        assert config.max_dimension == 1568
        assert config.max_pixels == 1_150_000
        assert config.webp_quality == 60

    def test_balanced_preset_values(self):
        config = ImageProcessingConfig.for_preset(ImagePreset.BALANCED)
        assert config.max_dimension == 2048
        assert config.max_pixels == 4_000_000
        assert config.webp_quality == 70

    def test_default_preset_values(self):
        config = ImageProcessingConfig.for_preset(ImagePreset.DEFAULT)
        assert config.max_dimension == 1000
        assert config.max_pixels == 1_000_000
        assert config.webp_quality == 75

    def test_custom_config(self):
        config = ImageProcessingConfig(max_dimension=2000, max_pixels=3_000_000, webp_quality=80)
        assert config.max_dimension == 2000
        assert config.max_pixels == 3_000_000
        assert config.webp_quality == 80
        assert config.overlap_fraction == pytest.approx(0.20)
        assert config.max_parts == 20

    def test_config_is_frozen(self):
        config = ImageProcessingConfig.for_preset(ImagePreset.HIGH_RES)
        with pytest.raises(Exception):
            config.max_dimension = 1000  # type: ignore[misc]  # frozen model mutation negative test


# ---------------------------------------------------------------------------
# process_image — small images (no split)
# ---------------------------------------------------------------------------


class TestProcessImageSmall:
    """Tests for process_image with images that don't need splitting."""

    def test_small_rgb_png(self):
        raw = make_image_bytes(800, 600)
        result = process_image(raw)
        assert len(result) == 1
        assert result.original_width == 800
        assert result.original_height == 600
        assert result.original_bytes == len(raw)
        assert result.output_bytes > 0
        assert not result.was_trimmed
        assert result.warnings == ()

    def test_single_part_properties(self):
        result = process_image(make_image_bytes(800, 600))
        part = result[0]
        assert part.index == 0
        assert part.total == 1
        assert part.label == "Full image"
        assert part.width == 800
        assert part.height == 600
        assert part.source_y == 0

    def test_compression_ratio(self):
        # Use a larger, non-uniform image so WebP is actually smaller than PNG
        import random

        random.seed(42)
        img = Image.new("RGB", (1200, 900))
        img.putdata([(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)) for _ in range(1200 * 900)])
        buf = BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()
        result = process_image(raw)
        assert 0.0 < result.compression_ratio < 1.0  # WebP smaller than noisy PNG

    def test_output_is_webp(self):
        result = process_image(make_image_bytes(800, 600))
        img = Image.open(BytesIO(result[0].data))
        assert img.format == "WEBP"

    def test_iterable_protocol(self):
        result = process_image(make_image_bytes(800, 600))
        parts = list(result.parts)
        assert len(parts) == 1
        assert isinstance(parts[0], ImagePart)

    def test_getitem(self):
        result = process_image(make_image_bytes(800, 600))
        assert result[0].index == 0


# ---------------------------------------------------------------------------
# process_image — tall images (splitting)
# ---------------------------------------------------------------------------


class TestProcessImageTall:
    """Tests for process_image with tall images that get split."""

    def test_splits_tall_image(self):
        raw = make_image_bytes(1000, 7000)
        result = process_image(raw)
        assert len(result) > 1

    def test_part_labels_sequential(self):
        result = process_image(make_image_bytes(1000, 7000))
        total = len(result)
        for i, part in enumerate(result.parts):
            assert part.index == i
            assert part.total == total
            assert part.label == f"Part {i + 1}/{total}"

    def test_all_parts_valid_webp(self):
        result = process_image(make_image_bytes(1000, 7000))
        for part in result.parts:
            img = Image.open(BytesIO(part.data))
            assert img.format == "WEBP"
            assert img.size[0] == part.width
            assert img.size[1] == part.height

    def test_coverage_first_to_last(self):
        result = process_image(make_image_bytes(1000, 7000))
        # First part starts at top
        assert result[0].source_y == 0
        # Last part reaches bottom
        last = result[-1]
        assert last.source_y + last.source_height == 7000

    def test_parts_monotonically_ordered(self):
        result = process_image(make_image_bytes(1000, 7000))
        for i in range(1, len(result)):
            assert result[i].source_y >= result[i - 1].source_y

    @pytest.mark.parametrize(
        ("width", "height"),
        [
            (500, 30000),  # very tall, narrow — one dimension far exceeds WebP 16383 limit
            (30000, 500),  # very wide, short — same, but horizontal
            (2000, 20000),  # tall screenshot — common real-world case
        ],
        ids=["tall_30k", "wide_30k", "tall_screenshot"],
    )
    def test_extreme_dimensions_stay_under_webp_limit(self, width, height):
        """Images exceeding WebP's 16383px limit are split/trimmed so all tiles fit."""
        WEBP_MAX_DIMENSION = 16383
        raw = make_image_bytes(width, height)
        for preset in ImagePreset:
            result = process_image(raw, preset=preset)
            assert len(result) >= 1
            for part in result.parts:
                assert part.width <= WEBP_MAX_DIMENSION, f"{preset}: tile width {part.width} exceeds WebP limit"
                assert part.height <= WEBP_MAX_DIMENSION, f"{preset}: tile height {part.height} exceeds WebP limit"
                # verify each tile is valid WebP
                img = Image.open(BytesIO(part.data))
                assert img.format == "WEBP"


# ---------------------------------------------------------------------------
# process_image — wide images (trimming)
# ---------------------------------------------------------------------------


class TestProcessImageWide:
    """Tests for width trimming."""

    def test_trims_wide_image(self):
        result = process_image(make_image_bytes(5000, 600))
        assert result.was_trimmed
        assert result[0].width <= 3000

    def test_narrow_not_trimmed(self):
        result = process_image(make_image_bytes(800, 600))
        assert not result.was_trimmed


# ---------------------------------------------------------------------------
# process_image — presets and custom config
# ---------------------------------------------------------------------------


class TestProcessImagePresets:
    """Tests for preset and custom config handling."""

    def test_default_preset_handles_small_image_without_trimming(self):
        result = process_image(make_image_bytes(800, 600))
        # Default preset accepts an 800px image without trimming.
        assert not result.was_trimmed

    def test_compact_preset_trims_1920(self):
        result = process_image(make_image_bytes(1920, 1080), preset=ImagePreset.COMPACT)
        assert result.was_trimmed

    def test_custom_config_overrides_preset(self):
        config = ImageProcessingConfig(max_dimension=500, max_pixels=250_000, webp_quality=50)
        result = process_image(make_image_bytes(800, 600), config=config)
        assert result.was_trimmed
        assert result[0].width <= 500


# ---------------------------------------------------------------------------
# process_image — error handling
# ---------------------------------------------------------------------------


class TestProcessImageErrors:
    """Tests for error cases."""

    def test_empty_bytes(self):
        with pytest.raises(ImageProcessingError, match="Empty image data"):
            process_image(b"")

    def test_invalid_bytes(self):
        with pytest.raises(ImageProcessingError, match="Failed to decode"):
            process_image(b"not an image at all")


# ---------------------------------------------------------------------------
# ProcessedImage / ImagePart models
# ---------------------------------------------------------------------------


class TestModels:
    """Tests for result model properties."""

    def test_processed_image_frozen(self):
        result = process_image(make_image_bytes(800, 600))
        with pytest.raises(Exception):
            result.original_width = 999  # type: ignore[misc]  # frozen model mutation negative test

    def test_image_part_frozen(self):
        result = process_image(make_image_bytes(800, 600))
        with pytest.raises(Exception):
            result[0].width = 999  # type: ignore[misc]  # frozen model mutation negative test

    def test_compression_ratio_with_zero_bytes(self):
        # Edge case: original_bytes <= 0
        result = ProcessedImage(
            parts=[],
            original_width=0,
            original_height=0,
            original_bytes=0,
            output_bytes=0,
            was_trimmed=False,
        )
        assert result.compression_ratio == pytest.approx(1.0)
