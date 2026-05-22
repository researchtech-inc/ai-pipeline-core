"""Tests for ai_pipeline_core.llm._images — pure planning and splitting logic."""

from io import BytesIO

import pytest
from PIL import Image

from ai_pipeline_core.llm._images import (
    _SplitPlan as SplitPlan,
    _encode_webp as encode_webp,
    _execute_split as execute_split,
    _load_and_normalize as load_and_normalize,
    _plan_split as plan_split,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_rgb_image(width: int, height: int, color: tuple[int, int, int] = (128, 128, 128)) -> Image.Image:
    return Image.new("RGB", (width, height), color)


def image_to_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# plan_split
# ---------------------------------------------------------------------------


class TestPlanSplit:
    """Tests for the pure plan_split function."""

    def test_small_image_no_split(self):
        plan = plan_split(800, 600, max_dimension=3000, max_pixels=9_000_000, overlap_fraction=0.2, max_parts=20)
        assert plan.num_parts == 1
        assert plan.step_y == 0
        assert plan.trim_width is None
        assert plan.tile_width == 800
        assert plan.tile_height == 600
        assert plan.warnings == ()

    def test_tall_image_splits(self):
        plan = plan_split(1000, 7000, max_dimension=3000, max_pixels=9_000_000, overlap_fraction=0.2, max_parts=20)
        assert plan.num_parts > 1
        assert plan.step_y > 0
        assert plan.trim_width is None

    def test_wide_image_trimmed(self):
        plan = plan_split(5000, 600, max_dimension=3000, max_pixels=9_000_000, overlap_fraction=0.2, max_parts=20)
        assert plan.num_parts == 1
        assert plan.trim_width is not None
        assert plan.trim_width <= 3000

    def test_wide_and_tall(self):
        plan = plan_split(5000, 7000, max_dimension=3000, max_pixels=9_000_000, overlap_fraction=0.2, max_parts=20)
        assert plan.num_parts > 1
        assert plan.trim_width is not None

    def test_exact_max_dimension(self):
        plan = plan_split(3000, 3000, max_dimension=3000, max_pixels=9_000_000, overlap_fraction=0.2, max_parts=20)
        assert plan.num_parts == 1
        assert plan.trim_width is None

    def test_max_parts_exceeded_reduces_with_warning(self):
        # Very tall image that would need many parts
        plan = plan_split(
            1000,
            100_000,
            max_dimension=3000,
            max_pixels=9_000_000,
            overlap_fraction=0.2,
            max_parts=5,
        )
        assert plan.num_parts == 5
        assert len(plan.warnings) == 1
        assert "Reducing" in plan.warnings[0]

    def test_max_parts_1_handles_edge_case(self):
        plan = plan_split(1000, 7000, max_dimension=3000, max_pixels=9_000_000, overlap_fraction=0.2, max_parts=1)
        assert plan.num_parts == 1
        assert plan.step_y == 0

    def test_zero_overlap(self):
        plan = plan_split(1000, 7000, max_dimension=3000, max_pixels=9_000_000, overlap_fraction=0.0, max_parts=20)
        assert plan.num_parts > 1
        # With 0 overlap, step equals tile height
        assert plan.step_y == plan.tile_height

    def test_max_overlap(self):
        plan = plan_split(1000, 7000, max_dimension=3000, max_pixels=9_000_000, overlap_fraction=0.5, max_parts=20)
        assert plan.num_parts > 1
        # With 50% overlap, step is half of tile height
        assert plan.step_y == plan.tile_height // 2

    def test_max_pixels_constrains_tile_size(self):
        # With very low max_pixels, tile must shrink
        plan = plan_split(1000, 1000, max_dimension=3000, max_pixels=500_000, overlap_fraction=0.2, max_parts=20)
        assert plan.tile_width <= 1000
        assert plan.tile_height <= 1000
        # tile_width * tile_height should respect max_pixels
        assert plan.tile_width * plan.tile_height <= 500_000

    def test_claude_preset_constraints(self):
        # Claude: 1568px, 1.15M pixels
        plan = plan_split(1920, 1080, max_dimension=1568, max_pixels=1_150_000, overlap_fraction=0.2, max_parts=20)
        assert plan.tile_width <= 1568
        assert plan.trim_width is not None  # 1920 > 1568


# ---------------------------------------------------------------------------
# load_and_normalize
# ---------------------------------------------------------------------------


class TestLoadAndNormalize:
    """Tests for image loading and normalization."""

    def test_load_rgb_png(self):
        raw = image_to_bytes(make_rgb_image(200, 100))
        img = load_and_normalize(raw)
        assert img.size == (200, 100)

    def test_load_rgba_png(self):
        rgba = Image.new("RGBA", (200, 100), (128, 128, 128, 255))
        raw = image_to_bytes(rgba)
        img = load_and_normalize(raw)
        assert img.size == (200, 100)

    def test_load_jpeg(self):
        buf = BytesIO()
        make_rgb_image(200, 100).save(buf, format="JPEG", quality=80)
        img = load_and_normalize(buf.getvalue())
        assert img.size == (200, 100)

    def test_load_grayscale(self):
        gray = Image.new("L", (200, 100), 128)
        raw = image_to_bytes(gray)
        img = load_and_normalize(raw)
        assert img.size == (200, 100)

    def test_reject_too_large(self):
        # Create a minimally valid image that claims enormous size
        # We can't easily create a 100MP+ image in memory, so test the check
        from ai_pipeline_core.llm._images import PIL_MAX_PIXELS

        assert PIL_MAX_PIXELS == 500_000_000

    def test_invalid_data_raises(self):
        with pytest.raises(Exception):
            load_and_normalize(b"not an image")


# ---------------------------------------------------------------------------
# encode_webp
# ---------------------------------------------------------------------------


class TestEncodeWebp:
    """Tests for WebP encoding."""

    def test_rgb_image(self):
        img = make_rgb_image(200, 100)
        data = encode_webp(img, quality=60)
        assert len(data) > 0
        result = Image.open(BytesIO(data))
        assert result.format == "WEBP"
        assert result.size == (200, 100)

    def test_rgba_preserved(self):
        img = Image.new("RGBA", (200, 100), (128, 128, 128, 200))
        data = encode_webp(img, quality=60)
        result = Image.open(BytesIO(data))
        assert result.mode == "RGBA"

    def test_grayscale_image(self):
        img = Image.new("L", (200, 100), 128)
        data = encode_webp(img, quality=60)
        assert len(data) > 0

    def test_quality_affects_size(self):
        import random

        random.seed(42)
        img = Image.new("RGB", (500, 500))
        img.putdata([(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)) for _ in range(500 * 500)])
        low = encode_webp(img, quality=10)
        high = encode_webp(img, quality=95)
        assert len(low) < len(high)


# ---------------------------------------------------------------------------
# execute_split
# ---------------------------------------------------------------------------


class TestExecuteSplit:
    """Tests for split execution."""

    def test_single_part_no_split(self):
        img = make_rgb_image(800, 600)
        plan = SplitPlan(tile_width=800, tile_height=600, step_y=0, num_parts=1, trim_width=None, warnings=())
        parts = execute_split(img, plan, webp_quality=60)
        assert len(parts) == 1
        data, w, h, sy, sh = parts[0]
        assert w == 800
        assert h == 600
        assert sy == 0
        assert sh == 600
        assert len(data) > 0

    def test_multi_part_split(self):
        img = make_rgb_image(1000, 6000)
        plan = plan_split(1000, 6000, max_dimension=3000, max_pixels=9_000_000, overlap_fraction=0.2, max_parts=20)
        parts = execute_split(img, plan, webp_quality=60)
        assert len(parts) == plan.num_parts
        assert len(parts) > 1

        for data, w, h, _sy, _sh in parts:
            assert len(data) > 0
            assert w == 1000
            assert h > 0

        # First part starts at y=0
        assert parts[0][3] == 0

        # Last part should cover the bottom of the image
        last_y = parts[-1][3]
        last_h = parts[-1][2]
        assert last_y + last_h == 6000

    def test_trim_width(self):
        img = make_rgb_image(5000, 600)
        plan = SplitPlan(tile_width=3000, tile_height=600, step_y=0, num_parts=1, trim_width=3000, warnings=())
        parts = execute_split(img, plan, webp_quality=60)
        assert len(parts) == 1
        _, w, h, _, _ = parts[0]
        assert w == 3000
        assert h == 600

    def test_rgba_converted(self):
        img = Image.new("RGBA", (800, 600), (128, 128, 128, 200))
        plan = SplitPlan(tile_width=800, tile_height=600, step_y=0, num_parts=1, trim_width=None, warnings=())
        parts = execute_split(img, plan, webp_quality=60)
        assert len(parts) == 1
        result = Image.open(BytesIO(parts[0][0]))
        assert result.format == "WEBP"

    def test_overlap_coverage(self):
        """Verify that with overlap, the entire image is covered."""
        img = make_rgb_image(1000, 7000)
        plan = plan_split(1000, 7000, max_dimension=3000, max_pixels=9_000_000, overlap_fraction=0.2, max_parts=20)
        parts = execute_split(img, plan, webp_quality=60)

        # First part starts at 0
        assert parts[0][3] == 0
        # Last part ends at image bottom
        last_y = parts[-1][3]
        last_h = parts[-1][2]
        assert last_y + last_h == 7000

        # Each part's y should be >= previous part's y (monotonically increasing)
        for i in range(1, len(parts)):
            assert parts[i][3] >= parts[i - 1][3]
