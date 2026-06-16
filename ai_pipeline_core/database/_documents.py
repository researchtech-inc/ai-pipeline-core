"""Document reconstruction and serialization for span-era database records."""

import logging

from ai_pipeline_core.database._hydrate import hydrate_document
from ai_pipeline_core.database._protocol import DatabaseReader, DatabaseWriter
from ai_pipeline_core.database._types import DocumentRecord, HydratedDocument, SpanRecord, SpanStatus, _BlobRecord
from ai_pipeline_core.documents._context import DocumentSha256
from ai_pipeline_core.documents._hashing import compute_content_sha256
from ai_pipeline_core.documents.document import Document, _class_name_registry

__all__ = [
    "document_to_blobs",
    "document_to_record",
    "get_latest_completed_deployment_by_run_id",
    "get_latest_result_documents_by_run_ids",
    "load_documents_from_database",
    "load_latest_documents_by_run_ids",
    "store_document",
]

logger = logging.getLogger(__name__)


def document_to_record(document: Document) -> DocumentRecord:
    """Convert a Document instance to a DocumentRecord for database storage."""
    return DocumentRecord(
        document_sha256=document.sha256,
        content_sha256=compute_content_sha256(document.content),
        document_type=type(document).__name__,
        name=document.name,
        description=document.description,
        mime_type=document.mime_type,
        size_bytes=document.size,
        summary=document.summary,
        derived_from=document.derived_from,
        triggered_by=document.triggered_by,
        attachment_names=tuple(att.name for att in document.attachments),
        attachment_descriptions=tuple(att.description for att in document.attachments),
        attachment_content_sha256s=tuple(compute_content_sha256(att.content) for att in document.attachments),
        attachment_mime_types=tuple(att.mime_type for att in document.attachments),
        attachment_size_bytes=tuple(att.size for att in document.attachments),
        publicly_visible=getattr(type(document), "publicly_visible", False),
    )


def document_to_blobs(document: Document) -> list[_BlobRecord]:
    """Extract all _BlobRecords (primary content + attachments) from a Document."""
    blobs = [_BlobRecord(content_sha256=compute_content_sha256(document.content), content=document.content)]
    for att in document.attachments:
        blobs.append(_BlobRecord(content_sha256=compute_content_sha256(att.content), content=att.content))
    return blobs


async def store_document(database: DatabaseWriter, document: Document) -> None:
    """Save a Document to the database, decomposing it into BlobRecords and a DocumentRecord.

    Blobs are saved first to ensure referential integrity.
    """
    blobs = document_to_blobs(document)
    record = document_to_record(document)
    await database.save_blob_batch(blobs)
    await database.save_document(record)


def _find_document_class(class_name: str) -> type[Document] | None:
    """Find a Document subclass by name from the registry, falling back to subclass search for test classes."""
    registered = _class_name_registry.get(class_name)
    if registered is not None:
        return registered

    # Test-defined Document subclasses are excluded from the registry by __init_subclass__.
    # Walk the subclass tree to find them.
    queue: list[type[Document]] = list(Document.__subclasses__())
    while queue:
        cls = queue.pop()
        if cls.__name__ == class_name:
            return cls
        queue.extend(cls.__subclasses__())

    return None


def _reconstruct_document(
    record: DocumentRecord,
    content: bytes,
    attachment_contents: dict[str, bytes],
) -> Document | None:
    doc_cls = _find_document_class(record.document_type)
    if doc_cls is None:
        logger.warning(
            "Cannot reconstruct document '%s': Document subclass '%s' not found. "
            "Import the module that defines this Document subclass.",
            record.name,
            record.document_type,
        )
        return None

    try:
        return hydrate_document(
            doc_cls,
            HydratedDocument(
                record=record,
                content=content,
                attachment_contents=attachment_contents,
            ),
        )
    except (TypeError, ValueError) as exc:  # fmt: skip
        logger.warning("Cannot reconstruct document '%s': %s", record.name, exc)
        return None


def _filtered_records(
    records: dict[str, DocumentRecord],
    filter_types: list[type[Document]] | None,
) -> list[DocumentRecord]:
    if filter_types is None:
        return list(records.values())
    filter_type_names = {document_type.__name__ for document_type in filter_types}
    return [record for record in records.values() if record.document_type in filter_type_names]


def _attachment_contents_for_record(
    record: DocumentRecord,
    blobs: dict[str, _BlobRecord],
) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    missing: list[str] = []
    for attachment_sha in record.attachment_content_sha256s:
        attachment_blob = blobs.get(attachment_sha)
        if attachment_blob is None:
            missing.append(attachment_sha)
        else:
            contents[attachment_sha] = attachment_blob.content
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(
            f"Document '{record.name}' ({record.document_sha256}) references missing "
            f"attachment blob(s): {missing_list}. "
            "These blobs are missing from storage. "
            "Persist every attachment blob before reading the document."
        )
    return contents


def _reconstruct_documents(
    records: list[DocumentRecord],
    blobs: dict[str, _BlobRecord],
) -> list[Document]:
    result: list[Document] = []
    for record in records:
        blob = blobs.get(record.content_sha256)
        if blob is None:
            logger.warning(
                "Content blob not found for document '%s' (content_sha256=%s...)",
                record.name,
                record.content_sha256[:12],
            )
            continue
        try:
            attachment_contents = _attachment_contents_for_record(record, blobs)
        except ValueError:
            logger.warning(
                "Skipping document '%s' (%s...) because one or more attachment blobs are missing.",
                record.name,
                record.document_sha256[:12],
            )
            continue
        document = _reconstruct_document(record, blob.content, attachment_contents)
        if document is not None:
            result.append(document)
    return result


async def load_documents_from_database(
    reader: DatabaseReader,
    sha256s: set[str],
    *,
    filter_types: list[type[Document]] | None = None,
) -> list[Document]:
    """Load and reconstruct typed Document instances from the database."""
    if not sha256s:
        return []

    sha256_list = [str(DocumentSha256(sha256)) for sha256 in sha256s]
    records = await reader.get_documents_batch(sha256_list)
    if not records:
        return []

    filtered_records = _filtered_records(records, filter_types)
    if not filtered_records:
        return []

    required_blob_shas = {record.content_sha256 for record in filtered_records}
    for record in filtered_records:
        required_blob_shas.update(record.attachment_content_sha256s)

    blobs = await reader.get_blobs_batch(sorted(required_blob_shas))
    return _reconstruct_documents(filtered_records, blobs)


async def get_latest_completed_deployment_by_run_id(
    reader: DatabaseReader,
    run_id: str,
    *,
    statuses: tuple[str, ...] = (SpanStatus.COMPLETED,),
) -> SpanRecord | None:
    """Return the latest root deployment for one run_id, filtered by status."""
    return (await reader.list_latest_completed_deployments_by_run_ids([run_id], statuses=statuses)).get(run_id)


async def get_latest_result_documents_by_run_ids(
    reader: DatabaseReader,
    run_ids: list[str],
    *,
    document_type: str | None = None,
    statuses: tuple[str, ...] = (SpanStatus.COMPLETED,),
) -> dict[str, tuple[DocumentRecord, ...]]:
    """Return result document records from each winning root deployment span.

    Reads each latest completed root deployment's ``output_document_shas`` in the
    stored SHA order, which reflects the durable record for that run's deliverable
    documents. If ``document_type`` is provided, only records of that type are
    returned. Resolved runs with no matching result documents map to an empty tuple.
    """
    deployments = await reader.list_latest_completed_deployments_by_run_ids(run_ids, statuses=statuses)
    output_shas = sorted({sha for deployment in deployments.values() for sha in deployment.output_document_shas})
    records = await reader.get_documents_batch(output_shas) if output_shas else {}

    result: dict[str, tuple[DocumentRecord, ...]] = {}
    for run_id, deployment in deployments.items():
        ordered = tuple(
            records[sha]
            for sha in deployment.output_document_shas
            if sha in records and (document_type is None or records[sha].document_type == document_type)
        )
        result[run_id] = ordered
    return result


async def load_latest_documents_by_run_ids(
    reader: DatabaseReader,
    run_ids: list[str],
    *,
    filter_types: list[type[Document]] | None = None,
    statuses: tuple[str, ...] = (SpanStatus.COMPLETED,),
) -> dict[str, tuple[Document, ...]]:
    """Hydrate latest result documents per run_id and require complete recovery.

    A resolved run's expected output documents must all rehydrate successfully.
    Silent omission would hide corrupted or incomplete persistence for the
    canonical latest outputs a caller explicitly requested.
    """
    deployments = await reader.list_latest_completed_deployments_by_run_ids(run_ids, statuses=statuses)
    output_shas = sorted({sha for deployment in deployments.values() for sha in deployment.output_document_shas})
    records = await reader.get_documents_batch(output_shas) if output_shas else {}

    allowed_type_names = (
        {document_type.__name__ for document_type in filter_types} if filter_types is not None else None
    )
    expected_by_run: dict[str, tuple[str, ...]] = {}
    expected_shas: set[str] = set()
    for run_id, deployment in deployments.items():
        missing_records = [sha for sha in deployment.output_document_shas if sha not in records]
        if missing_records:
            missing_list = ", ".join(missing_records)
            raise ValueError(
                f"Could not fully resolve latest output documents for run_id {run_id!r}: {missing_list}. "
                "Persist every referenced document record before loading latest outputs."
            )
        filtered_shas = tuple(
            sha
            for sha in deployment.output_document_shas
            if allowed_type_names is None or records[sha].document_type in allowed_type_names
        )
        expected_by_run[run_id] = filtered_shas
        expected_shas.update(filtered_shas)

    loaded = await load_documents_from_database(reader, expected_shas, filter_types=filter_types)
    loaded_by_sha = {str(document.sha256): document for document in loaded}

    result: dict[str, tuple[Document, ...]] = {}
    for run_id, expected_shas_for_run in expected_by_run.items():
        missing = [sha for sha in expected_shas_for_run if sha not in loaded_by_sha]
        if missing:
            missing_list = ", ".join(missing)
            raise ValueError(
                f"Could not fully hydrate latest output documents for run_id {run_id!r}: {missing_list}. "
                "Persist every referenced document record and blob, and import the corresponding Document classes "
                "before loading latest outputs."
            )
        result[run_id] = tuple(loaded_by_sha[sha] for sha in expected_shas_for_run)
    return result
