"""Image processing for LLM vision models.

Splits large images vertically with overlap, trims width, and encodes as WebP.
Respects the ImagePreset carried by AIModel.
Includes bridge functions for converting images to LLM ContentParts.
"""

import base64
import logging
from dataclasses import dataclass
from io import BytesIO
from math import ceil
from typing import Literal

from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core._validation import validate_image_content as _validate_image
from ai_pipeline_core._llm_core._validation import validate_pdf_content as _validate_pdf
from ai_pipeline_core._llm_core.types import ContentPart, ImageContent, ImagePreset, PDFContent, TextContent

logger = logging.getLogger(__name__)

__all__ = [
    "ImagePart",
    "ImagePreset",
    "ImageProcessingConfig",
    "ImageProcessingError",
    "ProcessedImage",
    "process_image",
    "validated_binary_parts",
]

PIL_MAX_PIXELS = 500_000_000  # 500MP security limit
Image.MAX_IMAGE_PIXELS = PIL_MAX_PIXELS


class ImageProcessingConfig(BaseModel):
    """Configuration for image processing."""

    model_config = {"frozen": True}

    max_dimension: int = Field(default=3000, ge=100, le=8192)
    max_pixels: int = Field(default=9_000_000, ge=10_000)
    overlap_fraction: float = Field(default=0.20, ge=0.0, le=0.5)
    max_parts: int = Field(default=20, ge=1, le=100)
    webp_quality: int = Field(default=60, ge=10, le=95)

    @classmethod
    def for_preset(cls, preset: ImagePreset) -> ImageProcessingConfig:
        """Create configuration from a model preset."""
        return _PRESETS[preset]


_PRESETS: dict[ImagePreset, ImageProcessingConfig] = {
    ImagePreset.HIGH_RES: ImageProcessingConfig(
        max_dimension=3000,
        max_pixels=9_000_000,
        webp_quality=75,
    ),
    ImagePreset.COMPACT: ImageProcessingConfig(
        max_dimension=1568,
        max_pixels=1_150_000,
        webp_quality=60,
    ),
    ImagePreset.BALANCED: ImageProcessingConfig(
        max_dimension=2048,
        max_pixels=4_000_000,
        webp_quality=70,
    ),
    ImagePreset.DEFAULT: ImageProcessingConfig(
        max_dimension=1000,
        max_pixels=1_000_000,
        webp_quality=75,
    ),
}


class ImagePart(BaseModel):
    """A single processed image part."""

    model_config = {"frozen": True}

    data: bytes = Field(repr=False)
    mime_type: Literal["image/webp"]
    width: int
    height: int
    index: int = Field(ge=0)
    total: int = Field(ge=1)
    source_y: int = Field(ge=0)
    source_height: int = Field(ge=1)

    @property
    def label(self) -> str:
        """Human-readable label for LLM context, 1-indexed."""
        if self.total == 1:
            return "Full image"
        return f"Part {self.index + 1}/{self.total}"


class ProcessedImage(BaseModel):
    """Result of image processing. Use ``.parts`` to iterate over individual parts."""

    model_config = {"frozen": True}

    parts: tuple[ImagePart, ...]
    original_width: int
    original_height: int
    original_bytes: int
    output_bytes: int
    was_trimmed: bool
    warnings: tuple[str, ...] = ()

    @property
    def compression_ratio(self) -> float:
        if self.original_bytes <= 0:
            return 1.0
        return self.output_bytes / self.original_bytes

    def __len__(self) -> int:
        return len(self.parts)

    def __getitem__(self, idx: int) -> ImagePart:
        return self.parts[idx]


class ImageProcessingError(Exception):
    """Image processing failed."""


# ---------------------------------------------------------------------------
# Internal processing functions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SplitPlan:
    """Describes how to split an image into parts."""

    tile_width: int
    tile_height: int
    step_y: int
    num_parts: int
    trim_width: int | None
    warnings: tuple[str, ...]


def _plan_split(
    width: int,
    height: int,
    max_dimension: int,
    max_pixels: int,
    overlap_fraction: float,
    max_parts: int,
) -> _SplitPlan:
    """Calculate how to split an image."""
    warnings: list[str] = []

    tile_size = max_dimension
    while tile_size * tile_size > max_pixels and tile_size > 100:
        tile_size -= 10

    trim_width = tile_size if width > tile_size else None
    effective_width = min(width, tile_size)

    tile_h = tile_size
    while effective_width * tile_h > max_pixels and tile_h > 100:
        tile_h -= 10

    if height <= tile_h:
        return _SplitPlan(
            tile_width=effective_width,
            tile_height=height,
            step_y=0,
            num_parts=1,
            trim_width=trim_width,
            warnings=tuple(warnings),
        )

    overlap_px = int(tile_h * overlap_fraction)
    step = tile_h - overlap_px
    if step <= 0:
        step = 1

    num_parts = 1 + ceil((height - tile_h) / step)

    if num_parts > max_parts:
        warnings.append(f"Image requires {num_parts} parts but max is {max_parts}. Reducing.")
        num_parts = max_parts
        step = (height - tile_h) // (num_parts - 1) if num_parts > 1 else 0

    return _SplitPlan(
        tile_width=effective_width,
        tile_height=tile_h,
        step_y=step,
        num_parts=num_parts,
        trim_width=trim_width,
        warnings=tuple(warnings),
    )


def _load_and_normalize(data: bytes) -> Image.Image:
    """Load image from bytes, apply EXIF orientation, validate size."""
    img = Image.open(BytesIO(data))
    img.load()

    if img.width * img.height > PIL_MAX_PIXELS:
        raise ValueError(f"Image too large: {img.width}x{img.height} pixels (limit: {PIL_MAX_PIXELS:,})")

    return ImageOps.exif_transpose(img)


def _encode_webp(img: Image.Image, quality: int) -> bytes:
    """Encode PIL Image as WebP bytes."""
    if img.mode not in {"RGB", "RGBA", "L", "LA"}:
        img = img.convert("RGB")

    buf = BytesIO()
    img.save(buf, format="WEBP", quality=quality)
    return buf.getvalue()


def _execute_split(
    img: Image.Image,
    plan: _SplitPlan,
    webp_quality: int,
) -> list[tuple[bytes, int, int, int, int]]:
    """Execute a split plan on an image. Returns list of (data, w, h, y, h)."""
    width, height = img.size

    if plan.trim_width is not None and width > plan.trim_width:
        img = img.crop((0, 0, plan.trim_width, height))
        width = plan.trim_width

    if img.mode not in {"RGB", "RGBA", "L", "LA"}:
        img = img.convert("RGB")

    parts: list[tuple[bytes, int, int, int, int]] = []

    for i in range(plan.num_parts):
        y = 0 if plan.num_parts == 1 else min(i * plan.step_y, max(0, height - plan.tile_height))
        h = min(plan.tile_height, height - y)
        tile = img.crop((0, y, width, y + h))
        data = _encode_webp(tile, webp_quality)
        parts.append((data, width, h, y, h))

    return parts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def process_image(
    image: bytes,
    preset: ImagePreset = ImagePreset.DEFAULT,
    config: ImageProcessingConfig | None = None,
) -> ProcessedImage:
    """Process image bytes for LLM vision models. Returns ProcessedImage with split/compressed parts."""
    effective = config if config is not None else ImageProcessingConfig.for_preset(preset)
    raw = image

    if not raw:
        raise ImageProcessingError("Empty image data")

    original_bytes = len(raw)

    try:
        img = _load_and_normalize(raw)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:  # fmt: skip
        raise ImageProcessingError(f"Failed to decode image: {exc}") from exc

    original_width, original_height = img.size

    plan = _plan_split(
        width=original_width,
        height=original_height,
        max_dimension=effective.max_dimension,
        max_pixels=effective.max_pixels,
        overlap_fraction=effective.overlap_fraction,
        max_parts=effective.max_parts,
    )

    raw_parts = _execute_split(img, plan, effective.webp_quality)

    total = len(raw_parts)
    total_output = 0
    parts_list: list[ImagePart] = []

    for idx, (data, w, h, sy, sh) in enumerate(raw_parts):
        total_output += len(data)
        parts_list.append(ImagePart(data=data, mime_type="image/webp", width=w, height=h, index=idx, total=total, source_y=sy, source_height=sh))

    return ProcessedImage(
        parts=tuple(parts_list),
        original_width=original_width,
        original_height=original_height,
        original_bytes=original_bytes,
        output_bytes=total_output,
        was_trimmed=plan.trim_width is not None,
        warnings=plan.warnings,
    )


# ---------------------------------------------------------------------------
# Bridge functions (moved from conversation.py)
# ---------------------------------------------------------------------------

_LLM_SUPPORTED_IMAGE_FORMATS: frozenset[str | None] = frozenset({"JPEG", "PNG", "GIF", "WEBP"})
type _ImageMimeType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]
_FORMAT_TO_MIME: dict[str, _ImageMimeType] = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}


def _image_needs_processing(data: bytes, preset: ImagePreset) -> bool:
    """Check if image exceeds model limits and needs processing."""
    config = ImageProcessingConfig.for_preset(preset)

    try:
        with Image.open(BytesIO(data)) as img:
            w, h = img.size
            fmt = img.format
            return w > config.max_dimension or h > config.max_dimension or w * h > config.max_pixels or fmt not in _LLM_SUPPORTED_IMAGE_FORMATS
    except (OSError, ValueError):  # fmt: skip
        return True


def _process_image_to_parts(data: bytes, preset: ImagePreset) -> list[ContentPart]:
    """Process image bytes and return ContentParts.

    Only re-encodes images that exceed model limits. Adds text labels when
    images are split into multiple parts.
    """
    if not _image_needs_processing(data, preset):
        try:
            with Image.open(BytesIO(data)) as img:
                mime_type: _ImageMimeType = _FORMAT_TO_MIME.get(img.format or "", "image/jpeg")
            return [ImageContent(data=base64.b64encode(data), mime_type=mime_type)]
        except (OSError, ValueError):  # fmt: skip
            pass

    result = process_image(data, preset=preset)
    parts: list[ContentPart] = []

    if len(result.parts) > 1:
        parts.append(TextContent(text=f"[Image split into {len(result.parts)} sequential parts with overlap]\n"))

    for img_part in result.parts:
        if img_part.total > 1:
            parts.append(TextContent(text=f"[{img_part.label}]\n"))
        parts.append(ImageContent(data=base64.b64encode(img_part.data), mime_type=img_part.mime_type))

    return parts


def validated_binary_parts(data: bytes, name: str, is_image: bool, preset: ImagePreset) -> list[ContentPart] | None:
    """Validate and convert binary content to ContentParts. Returns None and logs if invalid."""
    if is_image:
        if err := _validate_image(data):
            logger.warning(
                "Skipping invalid image content '%s' (validation failed: %s). "
                "Verify the bytes are a valid JPEG, PNG, GIF, or WebP file before sending it to the LLM.",
                name,
                err,
            )
            return None
        return _process_image_to_parts(data, preset)
    if err := _validate_pdf(data):
        logger.warning(
            "Skipping invalid PDF content '%s' (validation failed: %s). "
            "Verify the bytes start with a valid PDF header and re-create the Document before sending it to the LLM.",
            name,
            err,
        )
        return None
    return [PDFContent(data=base64.b64encode(data), filename=name)]
