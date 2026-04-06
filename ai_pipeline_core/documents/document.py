"""Document abstraction layer for AI pipeline flows.

Immutable Pydantic models wrapping binary content with metadata, MIME detection,
SHA256 hashing, and serialization. All documents must be concrete subclasses of Document.
"""

import base64
import json
import logging
from collections.abc import Sequence
from contextvars import ContextVar
from enum import StrEnum
from functools import cached_property
from io import BytesIO
from typing import (
    Any,
    ClassVar,
    Self,
    TypeVar,
    cast,
    final,
    get_args,
    get_origin,
    overload,
    override,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)
from ruamel.yaml import YAML

from ai_pipeline_core._token_estimates import estimate_binary_tokens, estimate_image_tokens, estimate_pdf_tokens, estimate_text_tokens
from ai_pipeline_core.documents._context import DocumentSha256
from ai_pipeline_core.documents._hashing import compute_content_sha256, compute_document_sha256
from ai_pipeline_core.documents.exceptions import DocumentNameError, DocumentSizeError
from ai_pipeline_core.documents.utils import _DATA_URI_PATTERN, _is_document_sha256, _serialize_content_bytes

from ._mime_type import (
    detect_mime_type,
    is_image_mime_type,
    is_pdf_mime_type,
    is_text_mime_type,
    is_yaml_mime_type,
)
from .attachment import Attachment

__all__ = [
    "Document",
]

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

_STRUCTURED_EXTENSIONS: frozenset[str] = frozenset({".json", ".yaml", ".yml"})

MAX_EXTERNAL_SOURCE_URI_BYTES = 8 * 1024
"""Maximum size for a single external source URI in derived_from (8 KB)."""

MAX_DESCRIPTION_BYTES = 50 * 1024
"""Maximum size for the description field (50 KB). Description is part of document identity (sha256)."""

MAX_SUMMARY_BYTES = 1024
"""Maximum size for the summary field (1 KB). Summary is NOT part of document identity."""

MAX_TOTAL_DERIVED_FROM_BYTES = 200 * 1024
"""Maximum combined size of all derived_from entries (200 KB)."""

# Registry of class __name__ -> Document subclass for collision detection.
# Only non-test classes are registered. Test modules (tests.*, conftest, etc.) are skipped.
_class_name_registry: dict[str, type[Document]] = {}  # nosemgrep: no-mutable-module-globals

# Metadata keys added by serialize_model() that should be stripped before validation.
_DOCUMENT_SERIALIZE_METADATA_KEYS: frozenset[str] = frozenset({
    "id",
    "sha256",
    "content_sha256",
    "size",
    "mime_type",
    "class_name",
})

# Context variable to allow from_dict() to call model_validate() internally.
_from_dict_active: ContextVar[bool] = ContextVar("_from_dict_active", default=False)


def _is_test_module(cls: type) -> bool:
    """Check if a class is defined in a test module (skip collision detection)."""
    module = getattr(cls, "__module__", "") or ""
    parts = module.split(".")
    return any(p == "tests" or p.startswith("test_") or p == "conftest" for p in parts)


def _enforce_content_type_rules(cls: type[Document]) -> None:
    """Enforce content type rules on a Document subclass at definition time.

    Co-location: the root content model must be defined in the exact same module as the
    Document subclass. This prevents the "Model Explosion" anti-pattern where content models
    drift into shared ``models/`` packages.

    Tests and framework internals are exempt from co-location enforcement.
    """
    if _is_test_module(cls):
        return

    # Co-location enforcement: content model must live in same module as the Document subclass
    ct = cls._content_type
    if ct is not None and ct.__module__ != cls.__module__:
        raise TypeError(
            f"Document subclass '{cls.__name__}' and its content model '{ct.__name__}' "
            f"must be defined in the same module. "
            f"'{cls.__name__}' is in '{cls.__module__}' but '{ct.__name__}' is in '{ct.__module__}'. "
            f"Move '{ct.__name__}' to '{cls.__module__}' or move '{cls.__name__}' to '{ct.__module__}'."
        )

    # Structured-only FILES without Document[T]: likely missing generic parameter
    if ct is None:
        expected = cls.get_expected_files()
        if expected and all(any(f.endswith(ext) for ext in _STRUCTURED_EXTENSIONS) for f in expected):
            logger.warning(
                "Document subclass '%s' has structured-only FILES (%s) but no Document[T] generic parameter. "
                "Declare Document[ModelClass] for creation-time schema validation and typed .parsed access.",
                cls.__name__,
                ", ".join(expected),
            )


def _serialize_structured(name: str, data: Any) -> bytes:
    """Serialize dict/list to JSON or YAML based on file extension."""
    name_lower = name.lower()
    if name_lower.endswith((".yaml", ".yml")):
        yaml = YAML()
        stream = BytesIO()
        yaml.dump(data, stream)  # pyright: ignore[reportUnknownMemberType]
        return stream.getvalue()
    if name_lower.endswith(".json"):
        return json.dumps(data, indent=2).encode("utf-8")
    raise ValueError(f"Structured content ({type(data).__name__}) requires .json or .yaml extension, got: {name}")


def _convert_content(name: str, content: str | bytes | dict[str, Any] | list[Any] | BaseModel) -> bytes:
    """Convert any supported content type to bytes. Dispatch by isinstance."""
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, dict):
        return _serialize_structured(name, content)
    if isinstance(content, BaseModel):
        return _serialize_structured(name, content.model_dump(mode="json"))
    if isinstance(content, list):  # pyright: ignore[reportUnnecessaryIsInstance]
        data = [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in content]
        return _serialize_structured(name, data)
    raise ValueError(f"Unsupported content type: {type(content)}")  # pyright: ignore[reportUnreachable]


def _validate_content_schema(
    content_type: type[BaseModel],
    original_content: str | bytes | dict[str, Any] | list[Any] | BaseModel,
    content_bytes: bytes,
    name: str,
    *,
    is_list: bool = False,
) -> None:
    """Validate content against declared schema. Called from create()/create_root().

    When ``is_list`` is True, validates each item in the list against ``content_type``.
    Fast path for BaseModel instances (isinstance check, no parsing).
    Falls back to byte-level parse+validate for structured formats (JSON, YAML).
    """
    if is_list:
        _validate_list_content_schema(content_type, original_content, content_bytes, name)
        return
    if isinstance(original_content, BaseModel):
        if not isinstance(original_content, content_type):
            raise TypeError(f"Expected content of type {content_type.__name__}, got {type(original_content).__name__}")
        return
    name_lower = name.lower()
    try:
        if name_lower.endswith((".yaml", ".yml")):
            yaml = YAML()
            raw = yaml.load(content_bytes.decode("utf-8"))  # type: ignore[no-untyped-call]
            content_type.model_validate(raw)
        elif name_lower.endswith(".json"):
            content_type.model_validate_json(content_bytes)
    except Exception as e:
        raise TypeError(f"Content does not validate against {content_type.__name__}: {e}") from e


def _validate_list_items(item_type: type[BaseModel], items: list[Any]) -> None:
    """Validate each item in a list against ``item_type``."""
    for i, item in enumerate(items):
        if isinstance(item, BaseModel) and not isinstance(item, item_type):
            raise TypeError(f"List item [{i}]: expected {item_type.__name__}, got {type(item).__name__}")
        if isinstance(item, dict):
            try:
                item_type.model_validate(item)
            except Exception as e:
                raise TypeError(f"List item [{i}] does not validate against {item_type.__name__}: {e}") from e
        elif not isinstance(item, BaseModel):
            raise TypeError(f"List item [{i}]: expected {item_type.__name__} or dict, got {type(item).__name__}")


def _parse_list_bytes(content_bytes: bytes, name: str, item_type: type[BaseModel]) -> list[Any]:
    """Parse structured content bytes as a list by extension."""
    name_lower = name.lower()
    if name_lower.endswith((".yaml", ".yml")):
        yaml = YAML()
        raw = yaml.load(content_bytes.decode("utf-8"))  # type: ignore[no-untyped-call]
    elif name_lower.endswith(".json"):
        raw = json.loads(content_bytes)
    else:
        raise TypeError(
            f"Document[list[{item_type.__name__}]] requires .json or .yaml extension for list content, got: '{name}'. Use a structured file extension."
        )
    if not isinstance(raw, list):
        raise TypeError(f"Expected list content for Document[list[{item_type.__name__}]], got {type(raw).__name__}")
    return raw  # type: ignore[no-any-return]


def _validate_list_content_schema(
    item_type: type[BaseModel],
    original_content: str | bytes | dict[str, Any] | list[Any] | BaseModel,
    content_bytes: bytes,
    name: str,
) -> None:
    """Validate list content where each item must match ``item_type``."""
    if isinstance(original_content, list):
        _validate_list_items(item_type, original_content)
        return
    raw = _parse_list_bytes(content_bytes, name, item_type)
    _validate_list_items(item_type, raw)


def _extract_content_type(cls: type) -> None:
    """Extract content type from Document[T] or Document[list[T]] generic parameter.

    When inheriting from Document[T], Pydantic creates a concrete intermediate class.
    The type info is in the parent's ``__pydantic_generic_metadata__``.
    Supports both ``Document[ModelType]`` and ``Document[list[ModelType]]``.
    """
    for base in cls.__bases__:
        meta = getattr(base, "__pydantic_generic_metadata__", None)
        if not (meta and meta.get("origin") is Document and meta.get("args")):
            continue
        ct = meta["args"][0]
        if get_origin(ct) is list:
            list_args = get_args(ct)
            if not list_args or not isinstance(list_args[0], type) or not issubclass(list_args[0], BaseModel):
                inner = repr(list_args[0]) if list_args else "?"
                raise TypeError(
                    f"Document subclass '{cls.__name__}' generic parameter list[T] requires T to be a BaseModel subclass, got list[{inner}]. "
                    f"Use Document[list[MyModel]] where MyModel is a BaseModel subclass."
                )
            cls._content_type = list_args[0]
            cls._content_is_list = True
            return
        if not isinstance(ct, type) or not issubclass(ct, BaseModel):
            raise TypeError(f"Document subclass '{cls.__name__}' generic parameter must be a BaseModel subclass or list[BaseModel], got {ct!r}")
        cls._content_type = ct
        cls._content_is_list = False
        return


class Document[TContent: BaseModel = Any](BaseModel):
    """Immutable base class for all pipeline documents. Cannot be instantiated directly — must be subclassed.

    Content is stored as bytes. Use `create()` for automatic conversion from str/dict/list/BaseModel.
    Use `parse()` to reverse the conversion. Serialization is extension-driven (.json → JSON, .yaml → YAML).

    Constructor selection:
        - ``create_root()``: genuine pipeline inputs with no prior pipeline provenance
        - ``derive()``: content transformations where this content was produced from other documents
        - ``create()``: new content caused by another document, but not derived from its content
        - ``create_external()``: content fetched from external URIs; use triggered_by when another
          document caused the fetch

    Provenance:
        - ``derived_from``: content sources (document SHA256 hashes or external URLs)
        - ``triggered_by``: causal provenance (document SHA256 hashes only)
        - ``create()`` requires at least one provenance field. Use ``create_root()`` for pipeline inputs.

    Identity (sha256):
        The document hash includes: name, description, content, derived_from, triggered_by, attachments.
        ``description`` is part of document identity — changing it changes sha256.
        ``summary`` is NOT part of document identity — it can be updated without changing the hash.

    Size limits:
        - External source URIs in derived_from: 8 KB per URI
        - description: 50 KB (identity-bearing, keep concise)
        - summary: 1 KB (short synopsis only)

    Attachments:
        Secondary content bundled with the primary document. The primary content lives in ``content``,
        while ``attachments`` carries supplementary material of the same logical document — e.g. a webpage
        stored as HTML in ``content`` with its screenshot in an attachment, or a report with embedded images.
        Attachments affect the document SHA256 hash.
    """

    MAX_CONTENT_SIZE: ClassVar[int] = 25 * 1024 * 1024
    """Maximum allowed total size in bytes (default 25MB)."""

    _content_type: ClassVar[type[BaseModel] | None] = None
    """Content schema declared via generic parameter (Document[ModelType] or Document[list[ModelType]]). None for untyped documents."""

    _content_is_list: ClassVar[bool] = False
    """True when declared as Document[list[ModelType]] — content is validated/parsed as a list of items."""

    FILES: ClassVar[type[StrEnum] | None] = None
    """Allowed filenames enum. Define as nested ``class FILES(StrEnum)`` or assign an external StrEnum subclass."""

    publicly_visible: ClassVar[bool] = False
    """Whether this document type should be displayed in frontend dashboards.
    Override to ``True`` in subclasses whose content is meant for end-user consumption."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Validate subclass at definition time. Cannot start with 'Test', cannot add custom fields."""
        super().__init_subclass__(**kwargs)

        # Skip Pydantic-generated parameterized classes (e.g. Document[ResearchDefinition]).
        # Same pattern as PromptSpec (spec.py:130-131).
        if "[" in cls.__name__:
            return

        # Extract content type from generic parameter.
        _extract_content_type(cls)

        # Single-level inheritance: Document subclasses cannot be further subclassed.
        # This prevents AI agents from creating bypass hierarchies like PreviousLoopDocument(LoopDocument)
        # to circumvent return discipline checks. Every document type must inherit directly from Document.
        concrete_doc_parents = [
            base for base in cls.__bases__ if base is not Document and isinstance(base, type) and issubclass(base, Document) and "[" not in base.__name__
        ]
        if concrete_doc_parents:
            parent_name = concrete_doc_parents[0].__name__
            raise TypeError(
                f"Document subclass '{cls.__name__}' inherits from '{parent_name}', which is already a Document subclass. "
                f"Document allows only one level of inheritance. "
                f"Define '{cls.__name__}' as a direct Document subclass instead: "
                f"class {cls.__name__}(Document): ... or class {cls.__name__}(Document[MyModel]): ..."
            )

        if cls.__name__.startswith("Test"):
            raise TypeError(
                f"Document subclass '{cls.__name__}' cannot start with 'Test' prefix. "
                "This causes conflicts with pytest test discovery. "
                "Please use a different name (e.g., 'SampleDocument', 'ExampleDocument')."
            )
        if "FILES" in cls.__dict__:
            files_attr = cls.__dict__["FILES"]
            if not isinstance(files_attr, type) or not issubclass(files_attr, StrEnum):
                raise TypeError(f"Document subclass '{cls.__name__}'.FILES must be an Enum of string values")

        _enforce_content_type_rules(cls)

        # Check that the Document's model_fields only contain the allowed fields
        # It prevents AI models from adding additional fields to documents
        allowed = {"name", "description", "summary", "content", "derived_from", "attachments", "triggered_by"}
        current = set(getattr(cls, "model_fields", {}).keys())
        extras = current - allowed
        if extras:
            raise TypeError(
                f"Document subclass '{cls.__name__}' cannot declare additional fields: "
                f"{', '.join(sorted(extras))}. Only {', '.join(sorted(allowed))} are allowed."
            )

        # Class name collision detection (production classes only)
        if not _is_test_module(cls):
            name = cls.__name__
            existing = _class_name_registry.get(name)
            if existing is not None and existing is not cls:
                if existing.__module__ == cls.__module__ and existing.__qualname__ == cls.__qualname__:
                    _class_name_registry[name] = cls
                    return
                raise TypeError(
                    f"Document subclass '{name}' (in {cls.__module__}) collides with "
                    f"existing class in {existing.__module__}. "
                    f"Class names must be unique across the framework."
                )
            _class_name_registry[name] = cls

    @classmethod
    def create_root(
        cls,
        *,
        name: str,
        content: str | bytes | dict[str, Any] | list[Any] | BaseModel,
        reason: str,
        description: str | None = None,
        summary: str = "",
        attachments: tuple[Attachment, ...] | None = None,
    ) -> Self:
        """Create a root document (pipeline input) with no provenance.

        This is the explicit escape hatch for deployment-boundary inputs.
        The reason is logged for auditability and not stored on the document.
        """
        if not reason.strip():
            raise ValueError(f"{cls.__name__}.create_root(reason=...) requires a non-empty reason.")

        content_bytes = _convert_content(name, content)
        if cls._content_type is not None:
            _validate_content_schema(cls._content_type, content, content_bytes, name, is_list=cls._content_is_list)
        logger.info("Creating root document '%s' (%s): %s", name, cls.__name__, reason)
        return cls(
            name=name,
            content=content_bytes,
            description=description,
            summary=summary,
            derived_from=(),
            triggered_by=(),
            attachments=attachments,
        )

    @classmethod
    def create(
        cls,
        *,
        name: str,
        content: str | bytes | dict[str, Any] | list[Any] | BaseModel,
        triggered_by: Sequence[Document],
        derived_from: Sequence[Document] | None = None,
        description: str | None = None,
        summary: str = "",
        attachments: tuple[Attachment, ...] | None = None,
    ) -> Self:
        """Create a document triggered by other documents (causal provenance).

        Use when something triggered the creation of new content — e.g. a research plan
        triggers webpage captures. ``triggered_by`` is required. Optionally provide
        ``derived_from`` for content provenance.

        For content transformations (summaries, analyses), use ``derive()`` instead.
        For external URI sources (URLs, MCP), use ``create_external()``.
        For pipeline root inputs with no provenance, use ``create_root(reason='...')``.
        """
        if not triggered_by:
            raise ValueError(
                f"{cls.__name__}.create() requires at least one triggered_by document. "
                f"For content transformations use derive(). For root inputs use create_root(reason='...')."
            )
        content_bytes = _convert_content(name, content)
        if cls._content_type is not None:
            _validate_content_schema(cls._content_type, content, content_bytes, name, is_list=cls._content_is_list)
        return cls(
            name=name,
            content=content_bytes,
            description=description,
            summary=summary,
            derived_from=tuple(d.sha256 for d in derived_from) if derived_from else None,
            triggered_by=tuple(DocumentSha256(d.sha256) for d in triggered_by),
            attachments=attachments,
        )

    @classmethod
    def derive(
        cls,
        *,
        name: str,
        content: str | bytes | dict[str, Any] | list[Any] | BaseModel,
        derived_from: Sequence[Document],
        triggered_by: Sequence[Document] | None = None,
        description: str | None = None,
        summary: str = "",
        attachments: tuple[Attachment, ...] | None = None,
    ) -> Self:
        """Create a document derived from other documents (content provenance).

        The primary API path for content transformations: summaries, analyses, reviews.
        ``derived_from`` is required — the documents whose content was used.
        Optionally provide ``triggered_by`` for causal provenance.
        """
        if not derived_from:
            raise ValueError(
                f"{cls.__name__}.derive() requires at least one derived_from document. "
                f"For triggered creation use create(). For root inputs use create_root(reason='...')."
            )
        content_bytes = _convert_content(name, content)
        if cls._content_type is not None:
            _validate_content_schema(cls._content_type, content, content_bytes, name, is_list=cls._content_is_list)
        return cls(
            name=name,
            content=content_bytes,
            description=description,
            summary=summary,
            derived_from=tuple(d.sha256 for d in derived_from),
            triggered_by=tuple(DocumentSha256(d.sha256) for d in triggered_by) if triggered_by else None,
            attachments=attachments,
        )

    @classmethod
    def create_external(
        cls,
        *,
        name: str,
        content: str | bytes | dict[str, Any] | list[Any] | BaseModel,
        from_sources: Sequence[str],
        triggered_by: Sequence[Document] | None = None,
        description: str | None = None,
        summary: str = "",
        attachments: tuple[Attachment, ...] | None = None,
    ) -> Self:
        """Create a document from external URI sources (websites, APIs, MCP responses).

        ``from_sources`` accepts URI strings (https://, search://, email://, mcp://, etc.)
        validated by the presence of ``://``. This is the only constructor that accepts
        URI strings as provenance.
        """
        if not from_sources:
            raise ValueError(f"{cls.__name__}.create_external() requires at least one URI in from_sources.")
        for src in from_sources:
            if "://" not in src:
                raise ValueError(
                    f"{cls.__name__}.create_external() from_sources entries must be URIs containing '://'. "
                    f"Got: {src!r}. For document-to-document provenance use derive() or create()."
                )
        content_bytes = _convert_content(name, content)
        if cls._content_type is not None:
            _validate_content_schema(cls._content_type, content, content_bytes, name, is_list=cls._content_is_list)
        return cls(
            name=name,
            content=content_bytes,
            description=description,
            summary=summary,
            derived_from=tuple(from_sources),
            triggered_by=tuple(DocumentSha256(d.sha256) for d in triggered_by) if triggered_by else None,
            attachments=attachments,
        )

    def __init__(
        self,
        *,
        name: str,
        content: bytes,
        description: str | None = None,
        summary: str = "",
        derived_from: tuple[str, ...] | None = None,
        triggered_by: tuple[DocumentSha256, ...] | None = None,
        attachments: tuple[Attachment, ...] | None = None,
    ) -> None:
        """Initialize with raw bytes content. Most users should use `create()` or `derive()` instead."""
        if type(self) is Document or "[" in type(self).__name__:
            raise TypeError("Cannot instantiate Document directly — define a named subclass")

        # None → default conversions for convenience (callers can pass None).
        # Field validators handle description (None → "").
        super().__init__(
            name=name,
            content=content,
            description=description,
            summary=summary,
            derived_from=derived_from if derived_from is not None else (),
            triggered_by=triggered_by if triggered_by is not None else (),
            attachments=attachments if attachments is not None else (),
        )

    name: str
    description: str = ""
    summary: str = ""
    content: bytes
    derived_from: tuple[str, ...] = ()
    """Content provenance: documents and references this document's content was directly
    derived from. Can be document SHA256 hashes (for pipeline documents) or external
    references (URLs, file paths). Answers: 'what content was this derived from?'

    A summary derived from input documents has derived_from=(input_doc.sha256,).
    A webpage capture has derived_from=("https://example.com",)."""

    triggered_by: tuple[DocumentSha256, ...] = ()
    """Causal provenance: documents that triggered this document's creation without directly
    contributing to its content. Always document SHA256 hashes.
    Answers: 'what triggered the creation of this document?'

    A research plan triggers 10 webpages to be captured. Each webpage's derived_from is its
    URL (content provenance), its triggered_by is the research plan (causal — the plan
    triggered the capture but didn't contribute to the webpage's content).

    A SHA256 hash must not appear in both derived_from and triggered_by for the same document."""
    attachments: tuple[Attachment, ...] = ()

    # Pydantic configuration
    model_config = ConfigDict(
        frozen=True,
        arbitrary_types_allowed=True,
        extra="forbid",
    )

    @final
    @classmethod
    def get_expected_files(cls) -> list[str] | None:
        """Return allowed filenames from FILES enum, or None if unrestricted."""
        if not hasattr(cls, "FILES"):
            return None
        files_attr = cls.FILES
        if files_attr is None:
            return None
        if not isinstance(files_attr, type) or not issubclass(files_attr, StrEnum):  # pyright: ignore[reportUnnecessaryIsInstance]
            return None
        try:
            values = [str(member.value) for member in files_attr]
        except TypeError:
            raise DocumentNameError(f"{cls.__name__}.FILES must be an Enum of string values") from None
        if len(values) == 0:
            return None
        return values

    @classmethod
    def _validate_file_name(cls, name: str) -> None:
        """Validate filename against FILES enum. Override only for custom validation beyond FILES."""
        allowed = cls.get_expected_files()
        if not allowed:
            return

        if name not in allowed:
            allowed_str = ", ".join(sorted(allowed))
            files_enum = getattr(cls, "FILES", None)
            hint = ""
            if files_enum is not None:
                members = [f"{cls.__name__}.FILES.{m.name}" for m in files_enum]
                if len(members) == 1:
                    hint = f"\nFIX: Use name={members[0]} in create()/create_root()/derive()."
                else:
                    hint = f"\nFIX: Use one of: {', '.join(members)}"
            raise DocumentNameError(f"Invalid filename '{name}' for {cls.__name__}. Allowed: {allowed_str}{hint}")

    @field_validator("description", mode="before")
    @classmethod
    def _normalize_description(cls, v: str | None) -> str:
        """Normalize None to empty string and enforce size limit. description is always str, never None."""
        if v is None:
            return ""
        byte_len = len(v.encode("utf-8"))
        if byte_len > MAX_DESCRIPTION_BYTES:
            raise ValueError(
                f"Document description is {byte_len} bytes, exceeds {MAX_DESCRIPTION_BYTES}-byte limit. "
                f"Move large explanatory content into 'content' or an attachment."
            )
        return v

    @field_validator("summary")
    @classmethod
    def _validate_summary_length(cls, v: str) -> str:
        """Enforce size limit on summary. Summary is a short synopsis, not full content."""
        byte_len = len(v.encode("utf-8"))
        if byte_len > MAX_SUMMARY_BYTES:
            raise ValueError(
                f"Document summary is {byte_len} bytes, exceeds {MAX_SUMMARY_BYTES}-byte limit. Summaries are short overviews, not full document content."
            )
        return v

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        """Reject path traversal, whitespace issues, reserved suffixes. Must match FILES enum if defined."""
        if ".." in v or "\\" in v or "/" in v:
            raise DocumentNameError(f"Invalid filename - contains path traversal characters: {v}")

        if not v or v.startswith(" ") or v.endswith(" "):
            raise DocumentNameError(f"Invalid filename format: {v}")

        if v.endswith(".meta.json"):
            raise DocumentNameError(f"Document names cannot end with .meta.json (reserved): {v}")

        # Detect double extensions like .md.md, .json.json, .yaml.yaml
        dot_pos = v.rfind(".")
        if dot_pos > 0:
            ext = v[dot_pos:]
            prefix = v[:dot_pos]
            if prefix.endswith(ext):
                raise DocumentNameError(f"Double extension detected in '{v}' — use ensure_extension() to prevent this")

        cls._validate_file_name(v)

        return v

    @field_validator("content", mode="before")
    @classmethod
    def _validate_content(cls, v: Any, info: ValidationInfo) -> bytes:
        """Convert content to bytes. Enforces MAX_CONTENT_SIZE.

        Handles:
        1. bytes — passed through directly
        2. str with data URI prefix — base64-decoded to bytes
        3. str (plain text) — UTF-8 encoded to bytes
        4. dict/list/BaseModel — serialized via _convert_content
        """
        if isinstance(v, bytes):
            pass
        elif isinstance(v, str):
            # Data URIs are produced by serialize_content() for binary content only (failed UTF-8 decode).
            # Text content starting with "data:<mime>;base64," would be misinterpreted here, but this is
            # accepted by design — real documents never start with a bare data URI on the first byte.
            if _DATA_URI_PATTERN.match(v):
                _, payload = v.split(",", 1)
                v = base64.b64decode(payload, validate=True)
            else:
                v = v.encode("utf-8")
        else:
            name = info.data.get("name", "") if hasattr(info, "data") else ""
            v = _convert_content(name, v)
        if len(v) > cls.MAX_CONTENT_SIZE:
            raise DocumentSizeError(f"Document size ({len(v)} bytes) exceeds maximum allowed size ({cls.MAX_CONTENT_SIZE} bytes)")
        return v

    @field_validator("derived_from")
    @classmethod
    def _validate_derived_from(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        """derived_from must be document SHA256 hashes or URLs. URIs are size-capped."""
        for src in v:
            if _is_document_sha256(src):
                continue
            if "://" not in src:
                raise ValueError(f"derived_from entry must be a document SHA256 hash or a URL (containing '://'), got: {src!r}")
            byte_len = len(src.encode("utf-8"))
            if byte_len > MAX_EXTERNAL_SOURCE_URI_BYTES:
                raise ValueError(
                    f"External source URI is {byte_len} bytes, exceeds {MAX_EXTERNAL_SOURCE_URI_BYTES}-byte limit. "
                    f"Store content in 'content' and keep from_sources for identifiers/URLs only."
                )
        total_bytes = sum(len(src.encode("utf-8")) for src in v)
        if total_bytes > MAX_TOTAL_DERIVED_FROM_BYTES:
            raise ValueError(
                f"Total derived_from size is {total_bytes} bytes, exceeds {MAX_TOTAL_DERIVED_FROM_BYTES}-byte limit. "
                f"Reduce the number or size of provenance entries."
            )
        return v

    @field_validator("triggered_by")
    @classmethod
    def _validate_triggered_by(cls, v: tuple[DocumentSha256, ...]) -> tuple[DocumentSha256, ...]:
        """triggered_by must be valid document SHA256 hashes."""
        for trigger in v:
            if not _is_document_sha256(trigger):
                raise ValueError(f"triggered_by entry must be a document SHA256 hash, got: {trigger}")
        return v

    @model_validator(mode="after")
    def _validate_no_provenance_overlap(self) -> Self:
        """Reject documents where the same SHA256 appears in both derived_from and triggered_by."""
        derived_sha256s = {src for src in self.derived_from if _is_document_sha256(src)}
        if derived_sha256s:
            overlap = derived_sha256s & set(self.triggered_by)
            if overlap:
                sample = next(iter(overlap))
                raise ValueError(
                    f"SHA256 hash {sample[:12]}... appears in both derived_from and triggered_by. "
                    f"A document reference must be either derived_from (content provenance) "
                    f"or triggered_by (causal provenance), not both."
                )
        return self

    @model_validator(mode="after")
    def _validate_total_size(self) -> Self:
        """Validate that total document size (content + attachments) is within limits."""
        total = self.size
        if total > self.MAX_CONTENT_SIZE:
            raise DocumentSizeError(f"Total document size ({total} bytes) including attachments exceeds maximum allowed size ({self.MAX_CONTENT_SIZE} bytes)")
        return self

    @field_serializer("content")
    def _serialize_content(self, v: bytes) -> str:
        """Serialize content: plain string for text, data URI (RFC 2397) for binary."""
        return _serialize_content_bytes(v, self.mime_type)

    @final
    @property
    def id(self) -> str:
        """First 6 chars of sha256. Used as short document identifier in LLM context."""
        return self.sha256[:6]

    @final
    @cached_property
    def sha256(self) -> DocumentSha256:
        """Full identity hash (name + description + content + derived_from + triggered_by + attachments).

        Includes description — changing description changes sha256.
        Does not include summary — summary updates do not change identity. BASE32 encoded, cached.
        """
        return compute_document_sha256(self)

    @final
    @cached_property
    def content_sha256(self) -> str:
        """SHA256 hash of raw content bytes only. Used for content deduplication."""
        return compute_content_sha256(self.content)

    @final
    @property
    def size(self) -> int:
        """Total size of content + attachments in bytes."""
        return len(self.content) + sum(att.size for att in self.attachments)

    @cached_property
    def mime_type(self) -> str:
        """Detected MIME type. Extension-based for known formats, content analysis for others. Cached."""
        return detect_mime_type(self.content, self.name)

    @property
    def is_text(self) -> bool:
        """True if MIME type indicates text content."""
        return is_text_mime_type(self.mime_type)

    @property
    def is_pdf(self) -> bool:
        """True if MIME type is application/pdf."""
        return is_pdf_mime_type(self.mime_type)

    @property
    def is_image(self) -> bool:
        """True if MIME type starts with image/."""
        return is_image_mime_type(self.mime_type)

    @property
    def text(self) -> str:
        """Content decoded as UTF-8. Raises ValueError if not text."""
        if not self.is_text:
            raise ValueError(f"Document is not text: {self.name}")
        return self.content.decode("utf-8")

    @cached_property
    def approximate_tokens_count(self) -> int:
        """Approximate token count across primary content and attachments."""
        if self.is_text:
            total = estimate_text_tokens(self.text)
        elif self.is_image:
            total = estimate_image_tokens()
        elif self.is_pdf:
            total = estimate_pdf_tokens()
        else:
            total = estimate_binary_tokens()

        for att in self.attachments:
            if att.is_image:
                total += estimate_image_tokens()
            elif att.is_pdf:
                total += estimate_pdf_tokens()
            elif att.is_text:
                total += estimate_text_tokens(att.text)
            else:
                total += estimate_binary_tokens()

        return total

    def as_yaml(self) -> Any:
        """Parse content as YAML via ruamel.yaml."""
        yaml = YAML()
        return yaml.load(self.text)  # type: ignore[no-untyped-call, no-any-return]

    def as_json(self) -> Any:
        """Parse content as JSON."""
        return json.loads(self.text)

    @overload
    def as_pydantic_model(self, model_type: type[TModel]) -> TModel: ...

    @overload
    def as_pydantic_model(self, model_type: type[list[TModel]]) -> list[TModel]: ...

    def as_pydantic_model(self, model_type: type[TModel] | type[list[TModel]]) -> TModel | list[TModel]:
        """Parse JSON/YAML content and validate against a Pydantic model. Supports single and list types."""
        data = self.as_yaml() if is_yaml_mime_type(self.mime_type) else self.as_json()

        if get_origin(model_type) is list:
            if not isinstance(data, list):
                raise ValueError(f"Expected list data for {model_type}, got {type(data)}")
            item_type = get_args(model_type)[0]
            if not (isinstance(item_type, type) and issubclass(item_type, BaseModel)):
                raise TypeError(f"List item type must be a BaseModel subclass, got {item_type}")
            result_list = [item_type.model_validate(entry) for entry in cast(list[Any], data)]
            return cast(list[TModel], result_list)

        # At this point model_type must be type[TModel], not type[list[TModel]]
        single_model = cast(type[TModel], model_type)
        return single_model.model_validate(data)

    @final
    @cached_property
    def parsed(self) -> TContent:
        """Content parsed against the declared generic type parameter. Cached.

        Returns the Pydantic model declared via Document[ModelType], or a list
        of models for Document[list[ModelType]].
        Raises TypeError if the Document subclass has no declared content type.
        Use parse(ModelType) for explicit parsing on untyped documents.
        """
        content_type = self.__class__._content_type
        if content_type is None:
            raise TypeError(f"{self.__class__.__name__} has no declared content type. Use parse(ModelType) for explicit parsing.")
        if self.__class__._content_is_list:
            return cast(TContent, self.as_pydantic_model(list[content_type]))
        return cast(TContent, self.as_pydantic_model(content_type))

    @final
    @classmethod
    def get_content_type(cls) -> type[BaseModel] | None:
        """Return the declared content type from the generic parameter, or None.

        For ``Document[list[T]]``, returns T (the item type). Use ``content_is_list``
        to check whether the content type is a list.
        """
        return cls._content_type

    @final
    @classmethod
    def content_is_list(cls) -> bool:
        """Return True if declared as Document[list[T]]."""
        return cls._content_is_list

    def _parse_structured(self) -> Any:
        """Parse content as JSON or YAML based on extension. Strict — no guessing."""
        name_lower = self.name.lower()
        if name_lower.endswith(".json"):
            return self.as_json()
        if name_lower.endswith((".yaml", ".yml")):
            return self.as_yaml()
        raise ValueError(f"Cannot parse '{self.name}' as structured data — use .json or .yaml extension")

    def parse(self, type_: type[Any]) -> Any:
        """Parse content to the requested type. Reverses create() conversion. Extension-based dispatch, no guessing."""
        if type_ is bytes:
            return self.content
        if type_ is str:
            return self.text if self.content else ""
        if type_ is dict or type_ is list:
            data = self._parse_structured()
            if not isinstance(data, type_):
                raise ValueError(f"Expected {type_.__name__} but got {type(data).__name__}")
            return data  # pyright: ignore[reportUnknownVariableType]
        if isinstance(type_, type) and issubclass(type_, BaseModel):  # pyright: ignore[reportUnnecessaryIsInstance]
            return self.as_pydantic_model(type_)
        raise ValueError(f"Unsupported parse type: {type_}")

    @property
    def content_documents(self) -> tuple[str, ...]:
        """Document SHA256 hashes from derived_from (filtered by _is_document_sha256)."""
        return tuple(src for src in self.derived_from if _is_document_sha256(src))

    @property
    def content_references(self) -> tuple[str, ...]:
        """Non-hash reference strings from derived_from (URLs, file paths, etc.)."""
        return tuple(src for src in self.derived_from if not _is_document_sha256(src))

    def has_derived_from(self, source: Document | str) -> bool:
        """Check if a source (Document or string) is in this document's derived_from."""
        if isinstance(source, str):
            return source in self.derived_from
        if not isinstance(source, Document):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"Invalid source type: {type(source).__name__}. Expected Document or str.")  # pyright: ignore[reportUnreachable]
        return source.sha256 in self.derived_from

    @final
    def serialize_model(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict for storage/transmission. Roundtrips with from_dict().

        Delegates to model_dump() for content serialization (unified format), then adds metadata.
        """
        # Get base serialization from Pydantic (uses field_serializer for content)
        result = self.model_dump(mode="json")

        # Add metadata not present in standard model_dump (keys must match _DOCUMENT_SERIALIZE_METADATA_KEYS, used by from_dict() to strip them)
        result["id"] = self.id
        result["sha256"] = self.sha256
        result["content_sha256"] = self.content_sha256
        result["size"] = self.size
        result["mime_type"] = self.mime_type
        result["class_name"] = self.__class__.__name__

        # Add metadata to attachments
        for att_dict, att_obj in zip(result.get("attachments", []), self.attachments, strict=False):
            att_dict["mime_type"] = att_obj.mime_type
            att_dict["size"] = att_obj.size

        return result

    @override
    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> Self:
        """Blocked: model_validate bypasses Document lifecycle tracking and enables cross-type casting.

        Use from_dict() for deserialization, or create()/derive()/create_root()/create_external() for new documents.
        """
        if not _from_dict_active.get():
            raise TypeError(
                f"{cls.__name__}.model_validate() is not supported — it bypasses Document lifecycle tracking "
                "and enables cross-type casting between Document subclasses. "
                "Use from_dict() for deserialization, or create()/derive()/create_root()/create_external() for new documents."
            )
        return super().model_validate(obj, *args, **kwargs)

    @final
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialize from dict produced by serialize_model(). Roundtrip guarantee.

        Delegates to model_validate() which handles content decoding via field_validator.
        Metadata keys are stripped before validation since custom __init__ receives raw data.
        """
        # Reject cross-type casting: if serialized data carries a class_name, it must match
        stored_class = data.get("class_name")
        if stored_class is not None and stored_class != cls.__name__:
            raise TypeError(
                f"Cannot deserialize '{stored_class}' as '{cls.__name__}' — document type casting is not allowed. "
                f"Use the original document type's from_dict(), or create a new document with derive()/create()."
            )

        # Strip metadata keys added by serialize_model() (model_validator mode="before"
        # doesn't work with custom __init__ - Pydantic passes raw data to __init__ first)
        cleaned = {k: v for k, v in data.items() if k not in _DOCUMENT_SERIALIZE_METADATA_KEYS}

        # Strip attachment metadata added by serialize_model()
        if cleaned.get("attachments"):
            cleaned["attachments"] = [{k: v for k, v in att.items() if k not in Attachment.SERIALIZE_METADATA_KEYS} for att in cleaned["attachments"]]

        token = _from_dict_active.set(True)
        try:
            return cls.model_validate(cleaned)
        finally:
            _from_dict_active.reset(token)

    @override
    def model_copy(self, *args: Any, **kwargs: Any) -> Self:
        """Blocked: model_copy bypasses Document validation and lifecycle tracking."""
        raise TypeError(
            "Document.model_copy() is not supported — it bypasses validation and lifecycle tracking. "
            "Use derive() for content transformations or create() for new documents."
        )

    def __reduce__(self) -> Any:
        """Blocked: pickle serialization is not supported for Documents."""
        raise TypeError("Document pickle serialization is not supported. Use JSON serialization (model_dump/model_validate).")

    def __reduce_ex__(self, _protocol: object) -> Any:
        """Blocked: pickle serialization is not supported for Documents."""
        raise TypeError("Document pickle serialization is not supported. Use JSON serialization (model_dump/model_validate).")

    def __copy__(self) -> Self:
        """Blocked: copy.copy() is not supported for Documents."""
        raise TypeError("Document copying is not supported. Use derive() for content transformations or create() for new documents.")

    def __deepcopy__(self, _memo: dict[int, Any] | None = None) -> Self:
        """Blocked: copy.deepcopy() is not supported for Documents."""
        raise TypeError("Document copying is not supported. Use derive() for content transformations or create() for new documents.")
