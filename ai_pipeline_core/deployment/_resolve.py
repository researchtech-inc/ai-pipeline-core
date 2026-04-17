"""Document input resolution for pipeline deployments.

Provides typed input/output models and the resolver that converts
_DocumentInput (inline content or URL references) into typed Documents.
"""

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Any, ClassVar, Self, cast
from urllib.parse import urlparse

import httpx
from google.cloud import storage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_pipeline_core.documents import Document
from ai_pipeline_core.documents._context import DocumentSha256
from ai_pipeline_core.documents.attachment import Attachment
from ai_pipeline_core.settings import settings

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = re.compile(r"^(https?|gs)://")
_DOWNLOAD_TIMEOUT = 120
_MAX_CONCURRENT_DOWNLOADS = 10


def _string_key_dict(data: Any) -> dict[str, Any]:
    """Normalize arbitrary mapping keys to strings for model validators."""
    if not isinstance(data, dict):
        return {}
    mapping = cast(dict[Any, Any], data)
    normalized: dict[str, Any] = {}
    for key, value in mapping.items():
        normalized[str(key)] = value
    return normalized


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class _InputBase(BaseModel):
    """Shared validators for deployment input models (AttachmentInput, _DocumentInput).

    Subclasses must define STRIP_KEYS and have `url` and `content` fields.
    """

    content: str | None = Field(default=None, description="Inline content string. Mutually exclusive with 'url'.")
    url: str = Field(default="", description="URL to fetch content from. Supported schemes: https://, gs://. Mutually exclusive with 'content'.")

    STRIP_KEYS: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _strip_serialize_metadata(cls, data: Any) -> Any:
        if isinstance(data, dict):
            d = _string_key_dict(data)
            return {k: v for k, v in d.items() if k not in cls.STRIP_KEYS}
        return data

    @model_validator(mode="after")
    def _check_mode(self) -> Self:
        cls_name = type(self).__name__
        if self.url and self.content is not None:
            raise ValueError(f"{cls_name} cannot have both 'url' and 'content'")
        if not self.url and self.content is None:
            raise ValueError(f"{cls_name} must have either 'url' or 'content'")
        return self

    model_config = ConfigDict(frozen=True, extra="forbid")


class AttachmentInput(_InputBase):
    """Attachment provided to a deployment — inline content or a URL reference."""

    name: str = Field(default="", description="Attachment filename. Required for inline content.")
    description: str | None = Field(default=None, description="Human-readable description of the attachment.")

    STRIP_KEYS: ClassVar[frozenset[str]] = frozenset({"mime_type", "size"})


class _DocumentInput(_InputBase):
    """Document provided to a deployment — inline content or a URL reference."""

    name: str = Field(default="", description="Document filename (e.g. 'task.md'). Auto-derived from URL path if omitted.")
    description: str = Field(default="", description="Human-readable description of this document.")
    summary: str = Field(default="", description="Inline summary of the document content.")
    class_name: str = Field(default="", description="Document type class name. Required when the pipeline accepts multiple input types.")

    derived_from: tuple[str, ...] = Field(default=(), description="Content provenance: SHA256 hashes of source documents or URIs.")
    triggered_by: tuple[str, ...] = Field(default=(), description="Causal provenance: SHA256 hashes of triggering documents.")
    attachments: tuple[AttachmentInput, ...] = Field(default=(), description="Secondary content attached to this document.")

    STRIP_KEYS: ClassVar[frozenset[str]] = frozenset({
        "id",
        "sha256",
        "content_sha256",
        "size",
        "mime_type",
    })

    @model_validator(mode="before")
    @classmethod
    def _normalize_serialized_document(cls, data: Any) -> Any:
        if isinstance(data, dict):
            normalized = _string_key_dict(data)
            if normalized.get("description") is None:
                normalized["description"] = ""
            return normalized
        return data


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _is_ip_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address is private/reserved (SSRF protection)."""
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


async def _is_private_ip(hostname: str) -> bool:
    """Check if hostname resolves to a private/reserved IP address (SSRF protection)."""
    try:
        return _is_ip_private(ipaddress.ip_address(hostname))
    except ValueError:
        pass
    try:
        resolved = await asyncio.to_thread(socket.getaddrinfo, hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        return any(_is_ip_private(ipaddress.ip_address(addr[4][0])) for addr in resolved)
    except socket.gaierror, ValueError, OSError:
        return True


async def _validate_url(url: str) -> None:
    """Validate URL scheme and block private/reserved IP ranges (SSRF protection)."""
    if not _ALLOWED_SCHEMES.match(url):
        raise ValueError(f"Only http://, https://, and gs:// URLs are supported, got: {url}")
    if url.startswith("gs://"):
        return
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if await _is_private_ip(hostname):
        raise ValueError(f"URL points to a private/reserved IP address (blocked for security): {hostname}")


def _derive_name(url: str, content_disposition: str | None) -> str:
    """Derive a filename from Content-Disposition or URL path."""
    if content_disposition:
        match = re.search(r'filename="?([^";\s]+)"?', content_disposition)
        if match:
            return match.group(1)
    path_part = url.split("?", maxsplit=1)[0].rsplit("/", maxsplit=1)[-1]
    if path_part and path_part != "/":
        return path_part
    return ""


async def _fetch_http(url: str, client: httpx.AsyncClient) -> tuple[bytes, str | None]:
    """Fetch content via HTTP with streaming and size enforcement.

    Returns (content_bytes, content_disposition_header).
    """
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes(chunk_size=65536):
            total += len(chunk)
            if total > Document.MAX_CONTENT_SIZE:
                raise ValueError(f"Download exceeds {Document.MAX_CONTENT_SIZE // (1024 * 1024)}MB limit: {url}")
            chunks.append(chunk)
        return b"".join(chunks), response.headers.get("content-disposition")


async def _fetch_gcs(url: str) -> bytes:
    """Fetch content from GCS with size enforcement."""
    # Parse gs://bucket/path
    parts = url.removeprefix("gs://").split("/", maxsplit=1)
    if len(parts) != 2 or not parts[1]:
        raise ValueError(f"Invalid GCS URL format: {url}")
    bucket_name, blob_path = parts

    def _download() -> bytes:
        if settings.gcs_service_account_file:
            client = storage.Client.from_service_account_json(settings.gcs_service_account_file)  # pyright: ignore[reportUnknownMemberType]
        else:
            client = storage.Client()
        blob = client.bucket(bucket_name).blob(blob_path)  # pyright: ignore[reportUnknownMemberType]
        blob.reload()  # pyright: ignore[reportUnknownMemberType]
        if blob.size is not None and blob.size > Document.MAX_CONTENT_SIZE:
            raise ValueError(f"GCS blob exceeds {Document.MAX_CONTENT_SIZE // (1024 * 1024)}MB limit: {url}")
        content = blob.download_as_bytes()  # pyright: ignore[reportUnknownMemberType]
        if len(content) > Document.MAX_CONTENT_SIZE:
            raise ValueError(f"Downloaded content exceeds {Document.MAX_CONTENT_SIZE // (1024 * 1024)}MB limit: {url}")
        return content

    return await asyncio.to_thread(_download)


async def _fetch_url(url: str, client: httpx.AsyncClient, semaphore: asyncio.Semaphore) -> tuple[bytes, str | None]:
    """Fetch content from URL (HTTP or GCS) with concurrency limiting."""
    async with semaphore:
        if url.startswith("gs://"):
            return await _fetch_gcs(url), None
        return await _fetch_http(url, client)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


async def resolve_document_inputs(
    inputs: list[_DocumentInput],
    known_types: list[type[Document]],
    start_step_input_types: list[type[Document]] | None = None,
) -> tuple[Document, ...]:
    """Resolve _DocumentInput list into typed Documents.

    Handles both inline content and URL references. URL references are fetched
    in parallel with bounded concurrency.

    Args:
        inputs: List of _DocumentInput from the deployment parameters.
        known_types: All document types from all flows (for explicit class_name matching).
        start_step_input_types: Input types from the start-step flow (for class_name inference).
    """
    if not inputs:
        return ()

    type_map = {t.__name__: t for t in known_types}
    inference_types = {t.__name__: t for t in (start_step_input_types or [])}

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)

    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=False) as client:

        async def _resolve_attachment(att_input: AttachmentInput) -> Attachment:
            if att_input.content is not None:
                name = att_input.name
                if not name:
                    raise ValueError("AttachmentInput with inline content must have a name")
                return Attachment(name=name, content=att_input.content, description=att_input.description)  # pyright: ignore[reportArgumentType]

            # URL attachment
            await _validate_url(att_input.url)
            content, disposition = await _fetch_url(att_input.url, client, semaphore)
            name = att_input.name or _derive_name(att_input.url, disposition)
            if not name:
                raise ValueError(f"Cannot derive attachment name from URL: {att_input.url}")
            return Attachment(name=name, content=content, description=att_input.description)  # pyright: ignore[reportArgumentType]

        async def _resolve_one(doc_input: _DocumentInput) -> Document:
            # Resolve class_name
            class_name = doc_input.class_name
            if not class_name:
                if len(inference_types) == 1:
                    class_name = next(iter(inference_types.keys()))
                elif len(inference_types) == 0:
                    raise ValueError("No input document types discoverable from flows; 'class_name' must be specified")
                else:
                    available = sorted(inference_types.keys())
                    raise ValueError(f"Multiple input types available ({', '.join(available)}); 'class_name' must be specified")

            doc_type = type_map.get(class_name)
            if doc_type is None:
                available = sorted(type_map.keys())
                raise ValueError(f"Unknown class_name '{class_name}'. Available: {', '.join(available)}")

            # Resolve attachments
            attachments: tuple[Attachment, ...] = ()
            if doc_input.attachments:
                att_list = await asyncio.gather(*[_resolve_attachment(a) for a in doc_input.attachments])
                attachments = tuple(att_list)

            # Resolve content: inline or URL fetch
            # Note: inline content is str; Pydantic's _validate_content field validator
            # handles str→bytes conversion including data URI decoding for binary content.
            if doc_input.content is not None:
                content: bytes = cast(bytes, doc_input.content)
                name = doc_input.name
            else:
                await _validate_url(doc_input.url)
                content, disposition = await _fetch_url(doc_input.url, client, semaphore)
                name = doc_input.name or _derive_name(doc_input.url, disposition)
                if not name:
                    raise ValueError(f"Cannot derive document name from URL: {doc_input.url}")

            return doc_type(  # nosemgrep: no-direct-document-init — framework-internal reconstruction from external API input with raw SHA256 provenance
                name=name,
                content=content,
                description=doc_input.description,
                summary=doc_input.summary,
                derived_from=doc_input.derived_from if doc_input.derived_from else (),
                triggered_by=tuple(DocumentSha256(t) for t in doc_input.triggered_by) if doc_input.triggered_by else (),
                attachments=attachments if attachments else (),
            )

        results = await asyncio.gather(*[_resolve_one(inp) for inp in inputs], return_exceptions=True)
        errors = [(i, r) for i, r in enumerate(results) if isinstance(r, BaseException)]
        if errors:
            msgs = [f"  input[{i}]: {type(e).__name__}: {e}" for i, e in errors]
            raise ValueError(f"Failed to resolve {len(errors)}/{len(inputs)} document inputs:\n" + "\n".join(msgs))
        return tuple(r for r in results if isinstance(r, Document))


__all__ = [
    "AttachmentInput",
    "_DocumentInput",
    "resolve_document_inputs",
]
