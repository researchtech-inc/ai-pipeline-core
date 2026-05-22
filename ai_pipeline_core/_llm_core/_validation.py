"""Content validation for LLM multimodal inputs.

Validation runs at message-prep time inside ``_transport`` so invalid bytes
are dropped from the request body and reported via a warning, instead of
being forwarded to the provider where they would either error out or be
silently rendered as garbage.
"""

from io import BytesIO

from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PdfReadError

_MIN_IMAGE_DIMENSION = 1
_MAX_IMAGE_DIMENSION = 10_000


def validate_image_content(data: bytes) -> str | None:
    """Return an error message if ``data`` is not a usable image, else None.

    Rejects empty bytes, non-image bytes, zero-pixel images, and images whose
    width or height exceeds 10,000 px.
    """
    if not data:
        return "empty image content"
    try:
        with Image.open(BytesIO(data)) as img:
            width, height = img.size
            img.verify()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:  # fmt: skip
        return f"invalid image: {exc}"
    if width < _MIN_IMAGE_DIMENSION or height < _MIN_IMAGE_DIMENSION:
        return f"zero-pixel image ({width}x{height})"
    if width > _MAX_IMAGE_DIMENSION or height > _MAX_IMAGE_DIMENSION:
        return f"image dimensions {width}x{height} exceed {_MAX_IMAGE_DIMENSION}x{_MAX_IMAGE_DIMENSION} cap"
    return None


def validate_pdf_content(data: bytes) -> str | None:
    """Return an error message if ``data`` is not a usable PDF, else None.

    Rejects empty bytes, missing ``%PDF-`` header, password-protected PDFs,
    and PDFs with zero pages.
    """
    if not data:
        return "empty PDF content"
    if not data.lstrip().startswith(b"%PDF-"):
        return "missing %PDF- header"
    try:
        reader = PdfReader(BytesIO(data))
    except (PdfReadError, OSError, ValueError) as exc:  # fmt: skip
        return f"corrupted PDF: {exc}"
    if reader.is_encrypted:
        return "password-protected PDF"
    if len(reader.pages) == 0:
        return "PDF has no pages"
    return None
