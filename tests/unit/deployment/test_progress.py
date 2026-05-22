"""Span-era deployment helper tests."""

# pyright: reportPrivateUsage=false

from uuid import uuid4

from ai_pipeline_core.deployment.base import _safe_uuid


class TestSafeUuid:
    def test_valid_uuid_string(self) -> None:
        original = uuid4()
        assert _safe_uuid(str(original)) == original

    def test_invalid_string_returns_none(self) -> None:
        assert _safe_uuid("not-a-uuid") is None
