# MODULE: testing
# VERSION: 0.21.2
# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build

## Functions

```python
@asynccontextmanager
async def llm_test_context(
    *,
    output_dir: Path | None = None,
    run_id: str = "llm-test",
) -> AsyncGenerator[DebugSession]:
    """Async context manager for integration tests with filesystem persistence.

    Uses a temporary directory by default.

    Yields:
        DebugSession: Active session with execution context and filesystem database.
    """
    effective_dir = output_dir or Path(tempfile.mkdtemp(prefix="llm-test-"))
    async with DebugSession(output_dir=effective_dir, run_id=run_id) as session:
        yield session


def assert_contains(text: str, substring: str, *, case_sensitive: bool = False) -> None:
    """Assert text contains substring (case-insensitive by default)."""
    a, b = (text, substring) if case_sensitive else (text.lower(), substring.lower())
    assert b in a, f"Expected {substring!r} in text ({len(text)} chars)"


def assert_contains_any(text: str, candidates: Sequence[str], *, case_sensitive: bool = False) -> None:
    """Assert text contains at least one of the candidate strings."""
    check = text if case_sensitive else text.lower()
    found = [c for c in candidates if (c if case_sensitive else c.lower()) in check]
    assert found, f"Expected any of {candidates!r} in text ({len(text)} chars)"


def assert_contains_all(text: str, required: Sequence[str], *, case_sensitive: bool = False) -> None:
    """Assert text contains all required strings."""
    check = text if case_sensitive else text.lower()
    missing = [r for r in required if (r if case_sensitive else r.lower()) not in check]
    assert not missing, f"Missing {missing!r} in text ({len(text)} chars)"


def assert_output_types(documents: tuple[Document, ...] | Sequence[Document], *expected_types: type[Document]) -> None:
    """Assert the document collection contains at least one of each expected type."""
    for doc_type in expected_types:
        matches = [d for d in documents if isinstance(d, doc_type)]
        assert matches, f"Expected at least one {doc_type.__name__} in output, got: {[type(d).__name__ for d in documents]}"


def assert_valid_parsed(conversation: Any, expected_type: type[BaseModel] | None = None) -> None:
    """Assert conversation has valid parsed structured output.

    Checks that parsed is not None, optionally checks type, and verifies
    no required string fields are empty.
    """
    parsed = conversation.parsed
    assert parsed is not None, "Conversation has no parsed output"
    if expected_type is not None:
        assert isinstance(parsed, expected_type), f"Expected {expected_type.__name__}, got {type(parsed).__name__}"
    if isinstance(parsed, BaseModel):
        for field_name, field_info in parsed.model_fields.items():
            if field_info.is_required() and field_info.annotation is str:
                value = getattr(parsed, field_name)
                assert value, f"Required string field {field_name!r} is empty"


def assert_cost_under(conversation: Any, ceiling_usd: float) -> None:
    """Assert that the LLM cost is under a ceiling.

    Works with Conversation objects via the ``cost`` property (returns float | None).
    """
    cost = getattr(conversation, "cost", None)
    if cost is None:
        cost = getattr(conversation, "cost_usd", None)
    assert cost is not None, (
        f"No cost information found on {type(conversation).__name__}. Pass a Conversation object that has completed at least one send() call."
    )
    assert cost <= ceiling_usd, f"Cost ${cost:.4f} exceeds ceiling ${ceiling_usd:.4f}"
```

## Examples

No test examples available.
