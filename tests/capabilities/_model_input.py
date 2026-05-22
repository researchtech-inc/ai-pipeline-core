"""Parse ``CAPABILITIES_MODELS`` into runtime ``AIModel`` inputs.

The probe suite is model-agnostic by design: there is no hardcoded inventory.
The dev-cli wrapper sets ``CAPABILITIES_MODELS`` (CSV) before invoking pytest
so this module turns those names into ``AIModel(name=...)`` instances.
"""

import os

from ai_pipeline_core.llm import AIModel

ENV_KEY = "CAPABILITIES_MODELS"


def parse_models() -> tuple[AIModel, ...]:
    """Return runtime AIModel inputs from the env var; loud error when empty.

    Refusing to default protects users from silent runs that probe a wrong
    model and bill the wrong account.
    """
    raw = os.environ.get(ENV_KEY, "").strip()
    if not raw:
        raise RuntimeError(
            f"Capability suite requires {ENV_KEY} (CSV of model names). "
            "Run via 'dev capabilities --models <names>' which sets this for you, "
            "or export the variable manually before invoking pytest."
        )
    names = tuple(name.strip() for name in raw.split(",") if name.strip())
    if not names:
        raise RuntimeError(f"{ENV_KEY} contained no usable model names after parsing.")
    return tuple(AIModel(name=name) for name in names)
