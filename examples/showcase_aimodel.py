#!/usr/bin/env python3
"""AIModel capability showcase.

This example is safe to run without an LLM proxy. It constructs and prints
AIModel configurations, and only sends a real request when explicitly enabled.

Usage::

    python examples/showcase_aimodel.py
    RUN_AIMODEL_SHOWCASE_SEND=1 python examples/showcase_aimodel.py
"""

import asyncio
import json
import os

from ai_pipeline_core import AIModel, Conversation
from ai_pipeline_core.settings import settings


def _model_summary(model: AIModel) -> dict[str, object]:
    """Return the JSON-serializable fields worth inspecting in this example."""
    return model.model_dump(mode="json", exclude_none=True)


def _fallback_names(model: AIModel) -> list[str]:
    names = [model.name]
    current = model.fallback
    while current is not None:
        names.append(current.name)
        current = current.fallback
    return names


def build_models() -> dict[str, AIModel]:
    """Create representative AIModel configurations."""
    fallback_chain = AIModel(
        name="gemini-3-flash",
        fallback=AIModel(
            name="gpt-5.4-mini",
        ),
    )
    cached_fast = AIModel(
        name="gemini-3-flash",
        cache_ttl=1,
    )
    direct_model = AIModel(
        name="gpt-5.4-mini",
        skip_cost_optimized=True,
    )
    watchdog_tuned = AIModel(
        name="gemini-3-flash",
        timeout_s=120.0,
    )
    return {
        "fallback_chain": fallback_chain,
        "cached_fast": cached_fast,
        "direct_model": direct_model,
        "watchdog_tuned": watchdog_tuned,
    }


async def maybe_send(model: AIModel) -> str | None:
    """Optionally make a live request through the configured AIPL proxy."""
    if not settings.openai_base_url:
        print("Skipping live send: OPENAI_BASE_URL is not configured.")
        return None
    if os.environ.get("RUN_AIMODEL_SHOWCASE_SEND") != "1":
        print("Skipping live send: set RUN_AIMODEL_SHOWCASE_SEND=1 to use the configured AIPL proxy.")
        return None

    conv = Conversation(model=model)
    conv = await conv.send(
        "Reply with one short sentence explaining AIModel fallback chains.", purpose="aimodel-showcase"
    )
    print("\nLive response:")
    print(conv.content)
    return conv.content


async def main() -> None:
    models = build_models()
    _print_model_summaries(models)
    await maybe_send(models["fallback_chain"])


def smoke() -> None:
    """Construct AIModel examples without making any live calls."""
    _print_model_summaries(build_models())


def _print_model_summaries(models: dict[str, AIModel]) -> None:
    """Print model summaries for the showcase."""
    print("AIModel configurations:")
    for label, model in models.items():
        print(f"\n{label}:")
        print(json.dumps(_model_summary(model), indent=2, sort_keys=True))
    print("\nFallback order:", " -> ".join(_fallback_names(models["fallback_chain"])))


if __name__ == "__main__":
    asyncio.run(main())
