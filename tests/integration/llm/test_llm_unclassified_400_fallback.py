"""Provider-400 fallback advancement integration test.

Production W1-T009 sank a sub-deployment when a real upstream returned a 400
that the AIPL proxy did not stamp with ``x-aipl-failure-class``; the framework
raised ``TerminalError`` and the configured ``AIModel.fallback`` was never
tried. The fix at ``_llm_core/client.py:_execute_attempt`` advances the chain
on any 400 when a fallback is configured, regardless of whether the proxy
classified it.

This integration test sends ``temperature=99.0`` — out of every OpenAI-compat
provider's accepted range — to a real primary with a real fallback. The
upstream 400 phrasings ("Expected temperature to be at most 2, received 99" /
"Unable to submit request because it has a temperature value of 99 …") do
not match any pattern in ``aipl_filter._refine_failure_class``, so the proxy
emits ``x-aipl-failure-class: null`` and the 400 reaches ``_execute_attempt``
unclassified. Both primary and fallback reject the request, so the chain
exhausts with ``TerminalError`` — but the test proves that **the fallback was
attempted at all**, which is what the bug suppressed. The dedicated unit
test in ``tests/unit/_llm_core/test_fallback_advancement.py`` pins the
deterministic contract; this test ensures the wire path between
``_execute_attempt`` and a real AIPL-classified-or-not 400 still produces
fallback advancement.
"""

import pytest

from ai_pipeline_core._llm_core.client import generate
from ai_pipeline_core._llm_core.request import GenerationSpec, LLMRequest
from ai_pipeline_core._llm_core.types import CoreMessage, Role
from ai_pipeline_core.exceptions import TerminalError
from ai_pipeline_core.settings import settings
from tests.support.model_catalog import DEFAULT_TEST_MODEL, WEAK_SCHEMA_TEST_MODEL

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]

# OpenAI-compat ``temperature`` is capped at 2.0 across every upstream we
# route to; sending 99.0 reliably produces a 400 whose wording does not
# match any AIPL proxy classification pattern.
OUT_OF_RANGE_TEMPERATURE = 99.0


@pytest.mark.asyncio
async def test_unclassified_400_engages_fallback_chain(nonce: str) -> None:
    """An unclassified 400 from the primary must engage the fallback before raising.

    Without the fix the framework would raise ``TerminalError`` referencing
    the primary model on the first 400. With the fix, the framework advances
    to the fallback; when the fallback also 400s the chain exhausts with a
    ``TerminalError`` referencing the **fallback** model, which is the
    evidence that fallback advancement happened end to end.

    No ``cost_budget`` interaction: both attempts fail, so neither produces
    a billable success cost.
    """
    primary = WEAK_SCHEMA_TEST_MODEL.model_copy(update={"fallback": DEFAULT_TEST_MODEL})
    request = LLMRequest(
        model=primary,
        messages=(
            CoreMessage(
                role=Role.USER,
                content=f"Reply with the marker UC400-{nonce} and nothing else.",
            ),
        ),
        generation=GenerationSpec(temperature=OUT_OF_RANGE_TEMPERATURE),
        purpose=f"unclassified-400-fallback-integration-{nonce}",
    )

    with pytest.raises(TerminalError) as exc_info:
        await generate(request)

    message = str(exc_info.value)
    assert DEFAULT_TEST_MODEL.name in message, (
        f"chain exhaustion must surface the fallback model name {DEFAULT_TEST_MODEL.name!r} — "
        f"its presence proves the framework advanced past the primary on an "
        f"unclassified 400 rather than raising on the first failure. "
        f"Got message: {message!r}"
    )
