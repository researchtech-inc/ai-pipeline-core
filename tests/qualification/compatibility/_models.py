"""Model set for compatibility-lane breadth tests."""

from tests.support.model_catalog import TEST_MODEL_SET

ModelNames = tuple[str, ...]

COMPATIBILITY_MODELS: ModelNames = tuple(m.name for m in TEST_MODEL_SET)
