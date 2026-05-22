"""Unit tests for ai_pipeline_core.deployment._resolve — input resolution, SSRF, URL validation."""

import ipaddress
from unittest.mock import AsyncMock, patch

import pytest

from ai_pipeline_core.deployment._resolve import (
    AttachmentInput,
    _derive_name,
    _is_ip_private,
    resolve_document_inputs,
)
from ai_pipeline_core.deployment._resolve import _DocumentInput
from ai_pipeline_core.documents import Document


class ResolveDoc(Document):
    pass


class OtherDoc(Document):
    pass


@pytest.fixture(autouse=True)
def suppress_doc_registration():
    return


# ---------------------------------------------------------------------------
# DocumentInput / AttachmentInput validation
# ---------------------------------------------------------------------------


class TestDocumentInput:
    def test_url_mode(self):
        di = _DocumentInput(url="https://example.com/doc.txt", name="doc.txt")
        assert di.url == "https://example.com/doc.txt"
        assert di.content is None

    def test_content_mode(self):
        di = _DocumentInput(content="hello world", name="doc.txt")
        assert di.content == "hello world"
        assert di.url == ""

    def test_both_raises(self):
        with pytest.raises(ValueError, match="cannot have both"):
            _DocumentInput(url="https://example.com", content="hello", name="d")

    def test_neither_raises(self):
        with pytest.raises(ValueError, match="must have either"):
            _DocumentInput(name="d")


class TestAttachmentInput:
    def test_strips_metadata(self):
        data = {"content": "text", "name": "att.txt", "mime_type": "text/plain", "size": 100}
        ai = AttachmentInput(**data)
        assert ai.name == "att.txt"
        assert not hasattr(ai, "mime_type")
        assert not hasattr(ai, "size")


# ---------------------------------------------------------------------------
# URL validation helpers
# ---------------------------------------------------------------------------


class TestIsIpPrivate:
    def test_loopback(self):
        assert _is_ip_private(ipaddress.ip_address("127.0.0.1")) is True

    def test_public(self):
        assert _is_ip_private(ipaddress.ip_address("8.8.8.8")) is False

    def test_private_range(self):
        assert _is_ip_private(ipaddress.ip_address("192.168.1.1")) is True

    def test_ipv6_loopback(self):
        assert _is_ip_private(ipaddress.ip_address("::1")) is True


class TestValidateUrl:
    async def test_rejects_ftp(self):
        from ai_pipeline_core.deployment._resolve import _validate_url

        with pytest.raises(ValueError, match="Only http://"):
            await _validate_url("ftp://example.com/file.txt")

    async def test_allows_gs(self):
        from ai_pipeline_core.deployment._resolve import _validate_url

        await _validate_url("gs://bucket/path/file.txt")

    @patch("ai_pipeline_core.deployment._resolve._is_private_ip", new_callable=AsyncMock, return_value=True)
    async def test_rejects_private(self, mock_private):
        from ai_pipeline_core.deployment._resolve import _validate_url

        with pytest.raises(ValueError, match="private/reserved"):
            await _validate_url("https://internal.local/doc")


# ---------------------------------------------------------------------------
# _derive_name
# ---------------------------------------------------------------------------


class TestDeriveName:
    def test_from_content_disposition(self):
        name = _derive_name("https://example.com/path", 'attachment; filename="report.pdf"')
        assert name == "report.pdf"

    def test_from_url_path(self):
        name = _derive_name("https://example.com/docs/report.pdf?token=abc", None)
        assert name == "report.pdf"

    def test_empty_path(self):
        name = _derive_name("https://example.com/", None)
        assert name == ""


# ---------------------------------------------------------------------------
# resolve_document_inputs
# ---------------------------------------------------------------------------


class TestResolveDocumentInputs:
    async def test_empty_inputs(self):
        result = await resolve_document_inputs([], [ResolveDoc])
        assert result == ()

    async def test_inline_content(self):
        inputs = [_DocumentInput(content="hello", name="doc.txt", class_name="ResolveDoc")]
        result = await resolve_document_inputs(inputs, [ResolveDoc])
        assert len(result) == 1
        assert isinstance(result[0], ResolveDoc)
        assert result[0].name == "doc.txt"

    async def test_infers_single_type(self):
        inputs = [_DocumentInput(content="data", name="doc.txt")]
        result = await resolve_document_inputs(inputs, [ResolveDoc], start_step_input_types=[ResolveDoc])
        assert len(result) == 1
        assert isinstance(result[0], ResolveDoc)

    async def test_ambiguous_raises(self):
        inputs = [_DocumentInput(content="data", name="doc.txt")]
        with pytest.raises(ValueError, match="Multiple input types"):
            await resolve_document_inputs(inputs, [ResolveDoc, OtherDoc], start_step_input_types=[ResolveDoc, OtherDoc])

    async def test_unknown_class_raises(self):
        inputs = [_DocumentInput(content="data", name="doc.txt", class_name="NonExistent")]
        with pytest.raises(ValueError, match="Unknown class_name"):
            await resolve_document_inputs(inputs, [ResolveDoc])

    async def test_with_inline_attachments(self):
        att = AttachmentInput(content="attachment data", name="att.txt")
        inputs = [_DocumentInput(content="main content", name="doc.txt", class_name="ResolveDoc", attachments=(att,))]
        result = await resolve_document_inputs(inputs, [ResolveDoc])
        assert len(result) == 1
        assert len(result[0].attachments) == 1
        assert result[0].attachments[0].name == "att.txt"

    async def test_attachment_no_name_inline_raises(self):
        att = AttachmentInput(content="data", name="")
        inputs = [_DocumentInput(content="main", name="d.txt", class_name="ResolveDoc", attachments=(att,))]
        with pytest.raises(ValueError, match="must have a name"):
            await resolve_document_inputs(inputs, [ResolveDoc])

    @patch("ai_pipeline_core.deployment._resolve._validate_url", new_callable=AsyncMock)
    @patch("ai_pipeline_core.deployment._resolve._fetch_url", new_callable=AsyncMock, return_value=(b"fetched", None))
    async def test_url_fetch(self, mock_fetch, mock_validate):
        inputs = [_DocumentInput(url="https://example.com/doc.txt", name="doc.txt", class_name="ResolveDoc")]
        result = await resolve_document_inputs(inputs, [ResolveDoc])
        assert len(result) == 1
        assert result[0].content == b"fetched"
        mock_validate.assert_called_once()

    async def test_aggregates_errors(self):
        inputs = [
            _DocumentInput(content="ok", name="ok.txt", class_name="NonExistent"),
            _DocumentInput(content="ok", name="ok2.txt", class_name="AlsoNonExistent"),
        ]
        with pytest.raises(ValueError, match="Failed to resolve 2/2"):
            await resolve_document_inputs(inputs, [ResolveDoc])

    async def test_no_inference_types_raises(self):
        inputs = [_DocumentInput(content="data", name="doc.txt")]
        with pytest.raises(ValueError, match="No input document types"):
            await resolve_document_inputs(inputs, [ResolveDoc], start_step_input_types=[])

    async def test_resolve_preserves_derived_from(self):
        """Documents with derived_from should survive the serialize → resolve roundtrip."""
        root = ResolveDoc.create_root(name="root.txt", content="root content", reason="test")
        derived = ResolveDoc.derive(derived_from=(root,), name="derived.txt", content="derived content")

        serialized = derived.serialize_model()
        doc_input = _DocumentInput.model_validate(serialized)

        assert doc_input.derived_from == (root.sha256,), "derived_from lost during _DocumentInput creation"

        result = await resolve_document_inputs([doc_input], [ResolveDoc])
        assert len(result) == 1
        assert result[0].derived_from == (root.sha256,), "derived_from lost during resolve"

    async def test_resolve_preserves_triggered_by(self):
        """Documents with triggered_by should survive the serialize → resolve roundtrip."""
        trigger = ResolveDoc.create_root(name="trigger.txt", content="trigger", reason="test")
        created = ResolveDoc.create(triggered_by=(trigger,), name="created.txt", content="created content")

        serialized = created.serialize_model()
        doc_input = _DocumentInput.model_validate(serialized)

        assert doc_input.triggered_by == (trigger.sha256,), "triggered_by lost during _DocumentInput creation"

        result = await resolve_document_inputs([doc_input], [ResolveDoc])
        assert len(result) == 1
        assert result[0].triggered_by == (trigger.sha256,), "triggered_by lost during resolve"

    async def test_resolve_preserves_both_provenance_fields(self):
        """Documents with both derived_from and triggered_by should survive roundtrip."""
        source = ResolveDoc.create_root(name="source.txt", content="source", reason="test")
        trigger = OtherDoc.create_root(name="trigger.txt", content="trigger", reason="test")
        doc = ResolveDoc.derive(
            derived_from=(source,),
            triggered_by=(trigger,),
            name="both.txt",
            content="has both provenance fields",
        )

        serialized = doc.serialize_model()
        doc_input = _DocumentInput.model_validate(serialized)

        result = await resolve_document_inputs([doc_input], [ResolveDoc])
        assert len(result) == 1
        assert result[0].derived_from == (source.sha256,)
        assert result[0].triggered_by == (trigger.sha256,)


# ---------------------------------------------------------------------------
# _is_private_ip async tests
# ---------------------------------------------------------------------------


class TestIsPrivateIpAsync:
    async def test_direct_ip_private(self):
        from ai_pipeline_core.deployment._resolve import _is_private_ip

        result = await _is_private_ip("127.0.0.1")
        assert result is True

    async def test_direct_ip_public(self):
        from ai_pipeline_core.deployment._resolve import _is_private_ip

        result = await _is_private_ip("8.8.8.8")
        assert result is False

    @patch("ai_pipeline_core.deployment._resolve.socket.getaddrinfo", side_effect=OSError("dns fail"))
    async def test_dns_failure_returns_false(self, mock_dns):
        from ai_pipeline_core.deployment._resolve import _is_private_ip

        result = await _is_private_ip("nonexistent.invalid")
        assert result is True


# ---------------------------------------------------------------------------
# URL document fetch with name derivation
# ---------------------------------------------------------------------------


class TestResolveUrlDocumentNameDerivation:
    @patch("ai_pipeline_core.deployment._resolve._validate_url", new_callable=AsyncMock)
    @patch("ai_pipeline_core.deployment._resolve._fetch_url", new_callable=AsyncMock, return_value=(b"data", 'attachment; filename="derived.pdf"'))
    async def test_derives_name_from_disposition(self, mock_fetch, mock_validate):
        inputs = [_DocumentInput(url="https://example.com/path", class_name="ResolveDoc")]
        result = await resolve_document_inputs(inputs, [ResolveDoc])
        assert len(result) == 1
        assert result[0].name == "derived.pdf"

    @patch("ai_pipeline_core.deployment._resolve._validate_url", new_callable=AsyncMock)
    @patch("ai_pipeline_core.deployment._resolve._fetch_url", new_callable=AsyncMock, return_value=(b"data", None))
    async def test_url_no_name_raises(self, mock_fetch, mock_validate):
        inputs = [_DocumentInput(url="https://example.com/", class_name="ResolveDoc")]
        with pytest.raises(ValueError, match="Cannot derive document name"):
            await resolve_document_inputs(inputs, [ResolveDoc])
