"""Regression tests for verified document reconstruction bugs."""

from ai_pipeline_core.database._documents import _reconstruct_documents, document_to_blobs, document_to_record
from ai_pipeline_core.documents import Attachment, Document


class _BatchDoc(Document):
    """Document type used for reconstruction regression tests."""


def test_reconstruct_documents_skips_only_corrupt_attachment_record() -> None:
    good = _BatchDoc.create_root(name="good.txt", content="good", reason="test")
    bad = _BatchDoc.create_root(
        name="bad.txt",
        content="bad",
        reason="test",
        attachments=(Attachment(name="proof.txt", content=b"proof"),),
    )

    good_record = document_to_record(good)
    bad_record = document_to_record(bad)

    blobs = {}
    for blob in document_to_blobs(good):
        blobs[blob.content_sha256] = blob
    missing_attachment_sha = bad_record.attachment_content_sha256s[0]
    for blob in document_to_blobs(bad):
        if blob.content_sha256 != missing_attachment_sha:
            blobs[blob.content_sha256] = blob

    reconstructed = _reconstruct_documents([bad_record, good_record], blobs)

    assert [doc.name for doc in reconstructed] == ["good.txt"]
