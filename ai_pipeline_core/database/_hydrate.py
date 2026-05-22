"""Shared document hydration helpers for stored document records."""

from ai_pipeline_core.database._types import HydratedDocument
from ai_pipeline_core.documents._context import DocumentSha256
from ai_pipeline_core.documents.attachment import Attachment
from ai_pipeline_core.documents.document import Document

__all__ = [
    "hydrate_document",
]


def hydrate_document(document_cls: type[Document], hydrated: HydratedDocument) -> Document:
    """Build a concrete Document instance from hydrated metadata and blobs.

    Verifies the recomputed sha256 matches the stored sha256 to catch
    data corruption (e.g. blob encoding issues, field normalization drift).
    """
    doc = document_cls(
        name=hydrated.record.name,
        content=hydrated.content,
        description=hydrated.record.description,
        summary=hydrated.record.summary,
        derived_from=hydrated.record.derived_from,
        triggered_by=tuple(DocumentSha256(sha) for sha in hydrated.record.triggered_by),
        attachments=_hydrate_attachments(hydrated),
    )
    if doc.sha256 != hydrated.record.document_sha256:
        raise ValueError(
            f"Document integrity check failed: '{hydrated.record.name}' ({hydrated.record.document_type}) "
            f"stored sha256={hydrated.record.document_sha256}, recomputed sha256={doc.sha256}. "
            "This indicates data corruption during storage or retrieval. "
            "Check blob content integrity and ClickHouse String encoding settings."
        )
    return doc


def _hydrate_attachments(hydrated: HydratedDocument) -> tuple[Attachment, ...]:
    built_attachments: list[Attachment] = []
    for name, description, content_sha256 in zip(
        hydrated.record.attachment_names,
        hydrated.record.attachment_descriptions,
        hydrated.record.attachment_content_sha256s,
        strict=True,
    ):
        content = hydrated.attachment_contents.get(content_sha256)
        if content is None:
            raise ValueError(
                f"Document {hydrated.record.document_sha256[:12]}... is missing "
                f"attachment blob {content_sha256[:12]}... for {name!r}. "
                "Persist every attachment blob before hydrating this stored document."
            )
        built_attachments.append(
            Attachment(
                name=name,
                content=content,
                description=description,
            )
        )
    return tuple(built_attachments)
