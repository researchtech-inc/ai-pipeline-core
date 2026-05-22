"""Tests for ``ai_pipeline_core._pydantic_base.FrozenBaseModel``."""

import pytest
from pydantic import ValidationError

from ai_pipeline_core import FrozenBaseModel


class Sample(FrozenBaseModel):
    name: str
    value: int = 0


def test_subclass_is_frozen() -> None:
    sample = Sample(name="x", value=1)
    with pytest.raises(ValidationError):
        sample.value = 2  # type: ignore[misc]  # frozen model mutation negative test


def test_subclass_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        Sample(name="x", value=1, extra="nope")  # type: ignore[call-arg]  # negative test: wrong call signature


def test_subclass_json_round_trip() -> None:
    sample = Sample(name="x", value=42)
    restored = Sample.model_validate_json(sample.model_dump_json())
    assert restored == sample


def test_re_exported_at_top_level() -> None:
    import ai_pipeline_core

    assert ai_pipeline_core.FrozenBaseModel is FrozenBaseModel
