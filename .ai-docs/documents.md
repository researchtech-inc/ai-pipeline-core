# MODULE: documents
# CLASSES: Attachment, Document, DocumentValidationError, DocumentSizeError, DocumentNameError
# DEPENDS: BaseModel, Exception
# PURPOSE: Document system for AI pipeline flows.
# VERSION: 0.21.1
# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build

## Imports

```python
from ai_pipeline_core import Attachment, Document, DocumentSha256, ensure_extension, find_document, replace_extension, sanitize_url
```

## Types & Constants

```python
MAX_EXTERNAL_SOURCE_URI_BYTES = 8 * 1024

MAX_DESCRIPTION_BYTES = 50 * 1024

MAX_SUMMARY_BYTES = 1024

MAX_TOTAL_DERIVED_FROM_BYTES = 200 * 1024

DocumentSha256 = NewType("DocumentSha256", str)
```

## Public API

```python
class Attachment(BaseModel):
    """Immutable binary attachment for multi-part documents.

    Carries binary content (screenshots, PDFs, supplementary files) without full Document machinery.
    ``mime_type`` is a cached_property — not included in ``model_dump()`` output."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    SERIALIZE_METADATA_KEYS: ClassVar[frozenset[str]] = frozenset({"mime_type", "size"})
    name: str
    content: bytes
    description: str = ""

    @property
    def is_image(self) -> bool:
        """True if MIME type starts with image/."""
        return is_image_mime_type(self.mime_type)

    @property
    def is_pdf(self) -> bool:
        """True if MIME type is application/pdf."""
        return is_pdf_mime_type(self.mime_type)

    @property
    def is_text(self) -> bool:
        """True if MIME type indicates text content."""
        return is_text_mime_type(self.mime_type)

    @cached_property
    def mime_type(self) -> str:
        """Detected MIME type from content and filename. Cached."""
        return detect_mime_type(self.content, self.name)

    @property
    def size(self) -> int:
        """Content size in bytes."""
        return len(self.content)

    @property
    def text(self) -> str:
        """Content decoded as UTF-8. Raises ValueError if not text."""
        if not self.is_text:
            raise ValueError(f"Attachment is not text: {self.name}")
        return self.content.decode("utf-8")


class Document(BaseModel):
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
        Attachments affect the document SHA256 hash."""

    MAX_CONTENT_SIZE: ClassVar[int] = 25 * 1024 * 1024  # Maximum allowed total size in bytes (default 25MB).
    FILES: ClassVar[type[StrEnum] | None] = None  # Allowed filenames enum. Define as nested ``class FILES(StrEnum)`` or assign an external StrEnum subclass.
    publicly_visible: ClassVar[bool] = False  # Whether this document type should be displayed in frontend dashboards.
    name: str
    description: str = ""
    summary: str = ""
    content: bytes
    derived_from: tuple[str, ...] = ()  # Content provenance: documents and references this document's content was directly
    triggered_by: tuple[DocumentSha256, ...] = ()  # Causal provenance: documents that triggered this document's creation without directly
    attachments: tuple[Attachment, ...] = ()
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True, extra="forbid")

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

    @property
    def content_documents(self) -> tuple[str, ...]:
        """Document SHA256 hashes from derived_from (filtered by _is_document_sha256)."""
        return tuple(src for src in self.derived_from if _is_document_sha256(src))

    @property
    def content_references(self) -> tuple[str, ...]:
        """Non-hash reference strings from derived_from (URLs, file paths, etc.)."""
        return tuple(src for src in self.derived_from if not _is_document_sha256(src))

    @final
    @cached_property
    def content_sha256(self) -> str:
        """SHA256 hash of raw content bytes only. Used for content deduplication."""
        return compute_content_sha256(self.content)

    @final
    @property
    def id(self) -> str:
        """First 6 chars of sha256. Used as short document identifier in LLM context."""
        return self.sha256[:6]

    @property
    def is_image(self) -> bool:
        """True if MIME type starts with image/."""
        return is_image_mime_type(self.mime_type)

    @property
    def is_pdf(self) -> bool:
        """True if MIME type is application/pdf."""
        return is_pdf_mime_type(self.mime_type)

    @property
    def is_text(self) -> bool:
        """True if MIME type indicates text content."""
        return is_text_mime_type(self.mime_type)

    @cached_property
    def mime_type(self) -> str:
        """Detected MIME type. Extension-based for known formats, content analysis for others. Cached."""
        return detect_mime_type(self.content, self.name)

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
    @cached_property
    def sha256(self) -> DocumentSha256:
        """Full identity hash (name + description + content + derived_from + triggered_by + attachments).

        Includes description — changing description changes sha256.
        Does not include summary — summary updates do not change identity. BASE32 encoded, cached.
        """
        return compute_document_sha256(self)

    @final
    @property
    def size(self) -> int:
        """Total size of content + attachments in bytes."""
        return len(self.content) + sum(att.size for att in self.attachments)

    @property
    def text(self) -> str:
        """Content decoded as UTF-8. Raises ValueError if not text."""
        if not self.is_text:
            raise ValueError(f"Document is not text: {self.name}")
        return self.content.decode("utf-8")

    @final
    @classmethod
    def content_is_list(cls) -> bool:
        """Return True if declared as Document[list[T]]."""
        return cls._content_is_list

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

    def __copy__(self) -> Self:
        """Blocked: copy.copy() is not supported for Documents."""
        raise TypeError("Document copying is not supported. Use derive() for content transformations or create() for new documents.")

    def __deepcopy__(self, _memo: dict[int, Any] | None = None) -> Self:
        """Blocked: copy.deepcopy() is not supported for Documents."""
        raise TypeError("Document copying is not supported. Use derive() for content transformations or create() for new documents.")

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

    def __reduce__(self) -> Any:
        """Blocked: pickle serialization is not supported for Documents."""
        raise TypeError("Document pickle serialization is not supported. Use JSON serialization (model_dump/model_validate).")

    def __reduce_ex__(self, _protocol: object) -> Any:
        """Blocked: pickle serialization is not supported for Documents."""
        raise TypeError("Document pickle serialization is not supported. Use JSON serialization (model_dump/model_validate).")

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

    def as_yaml(self) -> Any:
        """Parse content as YAML via ruamel.yaml."""
        yaml = YAML()
        return yaml.load(self.text)  # type: ignore[no-untyped-call, no-any-return]

    def has_derived_from(self, source: Document | str) -> bool:
        """Check if a source (Document or string) is in this document's derived_from."""
        if isinstance(source, str):
            return source in self.derived_from
        if not isinstance(source, Document):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"Invalid source type: {type(source).__name__}. Expected Document or str.")  # pyright: ignore[reportUnreachable]
        return source.sha256 in self.derived_from

    @override
    def model_copy(self, *args: Any, **kwargs: Any) -> Self:
        """Blocked: model_copy bypasses Document validation and lifecycle tracking."""
        raise TypeError(
            "Document.model_copy() is not supported — it bypasses validation and lifecycle tracking. "
            "Use derive() for content transformations or create() for new documents."
        )

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


class DocumentValidationError(Exception):
    """Raised when document validation fails."""


class DocumentSizeError(DocumentValidationError):
    """Raised when document content exceeds MAX_CONTENT_SIZE limit."""


class DocumentNameError(DocumentValidationError):
    """Raised when document name contains path traversal, reserved suffixes, or invalid format."""
```

## Functions

```python
def sanitize_url(url: str) -> str:
    """Sanitize URL or query string for use as a filename (max 100 chars)."""
    # Remove protocol if it's a URL
    if url.startswith(("http://", "https://")):
        parsed = urlparse(url)
        # Use domain + path
        url = parsed.netloc + parsed.path

    # Replace invalid filename characters
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", url)

    # Replace multiple underscores with single one
    sanitized = re.sub(r"_+", "_", sanitized)

    # Remove leading/trailing underscores and dots
    sanitized = sanitized.strip("_.")

    # Limit length to prevent too long filenames
    if len(sanitized) > 100:
        sanitized = sanitized[:100]

    # Ensure we have something
    if not sanitized:
        sanitized = "unnamed"

    return sanitized


def ensure_extension(name: str, ext: str) -> str:
    """Ensure a filename has the given extension, adding it only if missing.

    Prevents double-extension bugs like 'report.md.md' from ad-hoc string concatenation.
    """
    if not ext.startswith("."):
        ext = f".{ext}"
    if name.endswith(ext):
        return name
    return name + ext


def replace_extension(name: str, ext: str) -> str:
    """Replace the file extension (or add one if missing).

    Handles compound extensions like '.tar.gz' by replacing only the last extension.
    """
    if not ext.startswith("."):
        ext = f".{ext}"
    dot_pos = name.rfind(".")
    if dot_pos > 0:
        return name[:dot_pos] + ext
    return name + ext


def find_document[T](documents: Sequence[Any], doc_type: type[T]) -> T:
    """Find a document of the given type in a sequence.

    Replaces bare `next(d for d in docs if isinstance(d, T))` which gives opaque
    StopIteration on missing types. Raises DocumentValidationError with a clear message
    listing available document types.
    """
    for doc in documents:
        if isinstance(doc, doc_type):
            return doc
    available = sorted({type(d).__name__ for d in documents})
    raise DocumentValidationError(f"No document of type '{doc_type.__name__}' found. Available types: {', '.join(available) or 'none'}")


def load_document_from_file[D](doc_type: type[D], file_path: Path, *, reason: str = "loaded from file") -> D:
    """Load a local file from disk and wrap it as a typed root Document."""
    content_bytes = file_path.read_bytes()
    try:
        content: str | bytes = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = content_bytes
    return doc_type.create_root(name=file_path.name, content=content, reason=reason)  # type: ignore[return-value]
```

## Examples

**Attachment description affects hash** (`tests/documents/test_hashing.py:68`)

```python
def test_attachment_description_affects_hash(self):
    """Attachment description is included in document_sha256."""
    att1 = Attachment(name="img.png", content=b"\x89PNG", description="screenshot")
    att2 = Attachment(name="img.png", content=b"\x89PNG", description="thumbnail")
    doc1 = HashDoc.create_root(name="a.txt", content="hello", attachments=(att1,), reason="test input")
    doc2 = HashDoc.create_root(name="a.txt", content="hello", attachments=(att2,), reason="test input")
    assert compute_document_sha256(doc1) != compute_document_sha256(doc2)
```

**Attachment mime type** (`tests/documents/test_document_core.py:959`)

```python
def test_attachment_mime_type(self):
    """Attachment.mime_type returns detected MIME type."""
    att = Attachment(name="notes.txt", content=b"Hello")
    assert "text" in att.mime_type
```

**Attachment no detected mime type** (`tests/documents/test_document_core.py:964`)

```python
def test_attachment_no_detected_mime_type(self):
    """Attachment has no detected_mime_type attribute (renamed to mime_type)."""
    att = Attachment(name="notes.txt", content=b"Hello")
    assert not hasattr(att, "detected_mime_type")
```

**Attachment order does not affect hash** (`tests/documents/test_document_attachments.py:67`)

```python
def test_attachment_order_does_not_affect_hash(self):
    """Attachments are sorted by name before hashing, so order doesn't matter."""
    att_x = Attachment(name="x.txt", content=b"xxx")
    att_y = Attachment(name="y.txt", content=b"yyy")
    doc_xy = SampleFlowDoc(name="test.txt", content=b"Hello", attachments=(att_x, att_y))
    doc_yx = SampleFlowDoc(name="test.txt", content=b"Hello", attachments=(att_y, att_x))
    assert doc_xy.sha256 == doc_yx.sha256
```

**Attachment order does not matter** (`tests/documents/test_hashing.py:48`)

```python
def test_attachment_order_does_not_matter(self):
    """Attachments are sorted by name before hashing."""
    att_a = Attachment(name="a.txt", content=b"aaa")
    att_b = Attachment(name="b.txt", content=b"bbb")
    doc1 = HashDoc.create_root(name="doc.txt", content="content", attachments=(att_a, att_b), reason="test input")
    doc2 = HashDoc.create_root(name="doc.txt", content="content", attachments=(att_b, att_a), reason="test input")
    assert compute_document_sha256(doc1) == compute_document_sha256(doc2)
```

**Document list content is list false for scalar** (`tests/documents/test_document_list_content.py:31`)

```python
def test_document_list_content_is_list_false_for_scalar() -> None:
    class ScalarDoc(Document[ItemModel]):
        """Document with scalar content type."""

    assert ScalarDoc.content_is_list() is False
```

**Document mime type** (`tests/documents/test_document_core.py:949`)

```python
def test_document_mime_type(self):
    """Document.mime_type returns detected MIME type."""
    doc = ConcreteTestDocument.create_root(name="data.json", content={"key": "value"}, reason="test input")
    assert doc.mime_type == "application/json"
```


## Error Examples

**Provenance patterns** (`tests/documents/test_document_provenance_patterns.py:72`)

```python
class ResearchSpec(BaseModel):
    categories: dict[str, list[str]]  # category_name -> fields to research


class TrackerItem(BaseModel):
    category: str
    status: str
    findings: str


class GapTracker(BaseModel):
    iteration: int
    items: list[TrackerItem]


class OutputReport(BaseModel):
    sections: dict[str, list[dict[str, str]]]


class UploadManifest(BaseModel):
    uploads: list[dict[str, str]]  # [{filename, drive_url, drive_id}]


class FilterResult(BaseModel):
    excluded: list[str]
    reason: str


class SpecDocument(Document[ResearchSpec]):
    pass


class ResearchDocument(Document):
    pass


class GapTrackerDocument(Document[GapTracker]):
    pass


class OutputDocument(Document[OutputReport]):
    pass


class ManifestDocument(Document[UploadManifest]):
    pass


class FilterResultDocument(Document[FilterResult]):
    pass


class AnalyzedDocument(Document):
    pass


def test_provenance_patterns():
    """Pipeline provenance: when to use derived_from vs triggered_by, derive() vs create().

    Models a research pipeline to demonstrate every provenance pattern.
    The field-tracing test: does the input document's content appear in the output?
    If yes -> derived_from. If no -> triggered_by.
    """

    # ── 1. Root inputs: pipeline entry points with no provenance ──

    spec_doc = SpecDocument.create_root(
        name="spec.json",
        content=ResearchSpec(categories={"financials": ["revenue", "profit"], "team": ["headcount"]}),
        reason="user-provided research specification",
    )
    raw_data_doc = ResearchDocument.create_root(name="raw_data.md", content="Company X had $10M revenue...", reason="uploaded by user")

    assert spec_doc.derived_from == ()
    assert spec_doc.triggered_by == ()

    # ── 2. Content transformation: input content is reused in output -> derived_from ──
    # The research doc's text is parsed and restructured into tracker items.
    # Content flows from input to output — this is derived_from.

    tracker_v1 = GapTrackerDocument.derive(
        name="tracker.json",
        content=GapTracker(
            iteration=1,
            items=[
                TrackerItem(category="financials", status="found", findings="$10M revenue from raw data"),
                TrackerItem(category="team", status="gap", findings=""),
            ],
        ),
        derived_from=(raw_data_doc,),
        triggered_by=(spec_doc,),  # spec says what to look for, but its content isn't in the tracker items
    )

    assert raw_data_doc.sha256 in tracker_v1.derived_from
    assert spec_doc.sha256 in tracker_v1.triggered_by

    # ── 3. State evolution: parse -> mutate -> serialize back -> derived_from ──
    # The new tracker IS the old tracker with modifications.
    # Old tracker content is the structural foundation of the output.

    old_tracker = tracker_v1.parsed
    new_items = []
    for item in old_tracker.items:
        if item.category == "team":
            new_items.append(TrackerItem(category="team", status="found", findings="150 employees from new research"))
        else:
            new_items.append(item)
    updated_tracker = GapTracker(iteration=old_tracker.iteration + 1, items=new_items)

    new_research_doc = ResearchDocument.create_root(name="team_research.md", content="Company X has 150 employees...", reason="web capture")

    tracker_v2 = GapTrackerDocument.derive(
        name="tracker.json",
        content=updated_tracker,
        # tracker_v1 content IS the output's foundation — derived_from, not triggered_by
        # new_research_doc findings flow into item.findings — also derived_from
        derived_from=(tracker_v1, new_research_doc),
    )

    assert tracker_v1.sha256 in tracker_v2.derived_from
    assert new_research_doc.sha256 in tracker_v2.derived_from

    # ── 4. Spec as structure: spec categories become output JSON keys -> derived_from ──
    # When spec.categories are iterated to build the output skeleton,
    # the spec's content (category names, field names) IS the output structure.

    tracker = tracker_v2.parsed
    sections: dict[str, list[dict[str, str]]] = {}
    for cat_name, fields in spec_doc.parsed.categories.items():
        items = [i for i in tracker.items if i.category == cat_name]
        sections[cat_name] = [{f: item.findings for f in fields} for item in items]

    output_doc = OutputDocument.derive(
        name="report.json",
        content=OutputReport(sections=sections),
        # Both tracker and spec content appear in the output:
        # tracker provides item findings, spec provides category/field structure
        derived_from=(tracker_v2, spec_doc),
    )

    assert tracker_v2.sha256 in output_doc.derived_from
    assert spec_doc.sha256 in output_doc.derived_from

    # ── 5. Criteria-driven decision: report provides filter criteria -> triggered_by ──
    # The report tells us what to exclude, but excluded doc names come from analyzed_docs.
    # Report content does NOT appear in the output — only the decision does.

    analyzed_docs = (
        AnalyzedDocument.create_root(name="doc_a.md", content="Relevant company data", reason="analysis input"),
        AnalyzedDocument.create_root(name="doc_b.md", content="Unrelated press release", reason="analysis input"),
    )

    filter_doc = FilterResultDocument.derive(
        name="filter.json",
        content=FilterResult(excluded=["doc_b.md"], reason="not relevant to financials"),
        derived_from=analyzed_docs,  # excluded names come from these documents
        triggered_by=(output_doc,),  # report provides criteria but its content isn't in the filter output
    )

    assert all(d.sha256 in filter_doc.derived_from for d in analyzed_docs)
    assert output_doc.sha256 in filter_doc.triggered_by

    # ── 6. Execution result: new data from external API -> create(), not derive() ──
    # Upload manifest contains Drive URLs and file IDs — new operational data.
    # The report was uploaded but its content doesn't appear in the manifest.

    manifest_doc = ManifestDocument.create(
        name="manifest.json",
        content=UploadManifest(uploads=[{"filename": "report.json", "drive_url": "https://drive.google.com/file/d/abc123", "drive_id": "abc123"}]),
        triggered_by=(output_doc,),  # report caused the upload, but manifest content is from Drive API
    )

    assert output_doc.sha256 in manifest_doc.triggered_by
    assert manifest_doc.derived_from == ()  # no content was derived — all new API data

    # ── 7. Overlap rejection: same document cannot be both derived_from and triggered_by ──

    with pytest.raises(ValueError, match="both derived_from and triggered_by"):
        OutputDocument.derive(
            name="bad.json",
            content=OutputReport(sections={}),
            derived_from=(tracker_v2,),
            triggered_by=(tracker_v2,),  # same doc in both — rejected
        )
```

**Document instantiate base class raises** (`tests/documents/test_document_enforcement.py:49`)

```python
def test_document_instantiate_base_class_raises() -> None:
    with pytest.raises(TypeError, match="Cannot instantiate Document directly"):
        Document(name="test.txt", content=b"data")
```

**Document list rejects list int** (`tests/documents/test_document_list_content.py:114`)

```python
def test_document_list_rejects_list_int() -> None:
    with pytest.raises(TypeError, match="requires T to be a BaseModel subclass"):

        class BadListDoc2(Document[list[int]]):  # type: ignore[type-arg]
            """Bad."""
```

**Document list rejects list str** (`tests/documents/test_document_list_content.py:107`)

```python
def test_document_list_rejects_list_str() -> None:
    with pytest.raises(TypeError, match="requires T to be a BaseModel subclass"):

        class BadListDoc(Document[list[str]]):  # type: ignore[type-arg]
            """Bad."""
```

**Document subclass inheritance is rejected** (`tests/documents/test_typed_content.py:95`)

```python
def test_document_subclass_inheritance_is_rejected(self):
    with pytest.raises(TypeError, match="allows only one level of inheritance"):
        if _typed_doc_inheritance_error is None:
            raise TypeError("Document allows only one level of inheritance")
        raise _typed_doc_inheritance_error
```

**Cannot instantiate document** (`tests/documents/test_document_core.py:138`)

```python
def test_cannot_instantiate_document(self):
    """Test that Document cannot be instantiated directly."""
    with pytest.raises(TypeError, match="Cannot instantiate Document directly"):
        Document(name="test.txt", content=b"test")
```

**Document list rejects non structured extension** (`tests/documents/test_document_list_content.py:70`)

```python
def test_document_list_rejects_non_structured_extension() -> None:
    items = [ItemModel(name="a", value=1)]
    with pytest.raises(ValueError):
        ListDoc.create_root(name="items.txt", content=items, reason="test")
```
