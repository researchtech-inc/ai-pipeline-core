# AI Docs Generator

Auto-generates `.ai-docs/` API reference guides from source code. Each module produces one self-contained markdown guide designed to be fed to AI coding agents as context.

The goal: an AI agent can correctly use any module's public API by reading only its guide — no source code browsing needed.

## Why auto-generated?

- **Single source of truth** — docs are a computed view of the code, not a second source that drifts
- **CI-enforced freshness** — `make docs-ai-check` catches missing symbols and phantom imports immediately
- **Scale** — full method source, flattened inheritance, scored test examples across 11+ modules; impossible to maintain by hand
- **Verifiable correctness** — the doc-verify subagents try to use the framework from docs alone; when they fail, it's a documentation bug with a traceable fix

## How it works

### Architecture

Three layers:

1. **`extractor.py`** — AST-based symbol extraction. Parses `.py` files into `ClassInfo`, `FunctionInfo`, `MethodInfo`, `ValueInfo`. Builds a `SymbolTable` mapping every symbol to its module. Handles inheritance resolution, field description extraction, and private-to-public symbol remapping.

2. **`guide_builder.py`** — Per-module guide assembly. Collects public symbols, flattens inheritance chains, discovers and scores test examples, selects top examples within budget, and renders to markdown.

3. **`cli.py`** — CLI with `generate` and `check` subcommands. Orchestrates the pipeline, renders `README.md` with per-module API summaries, and runs validation.

### Guide structure

Each generated guide contains these sections in order:

```
# MODULE: <name>
# CLASSES: ...
# DEPENDS: ...
# PURPOSE: ...
# VERSION: ...

## Imports                    ← from ai_pipeline_core import ... / from ai_pipeline_core.<module> import ...
## Types & Constants          ← NewType, type aliases, UPPER_CASE constants
## Internal Types             ← Private _CapitalizedName classes referenced in public API signatures
## Public API                 ← Full source code of public classes with flattened inheritance
## Functions                  ← Full source code of public module-level functions
## Examples                   ← Scored test functions demonstrating usage
## Error Examples             ← Tests using pytest.raises showing error conditions
```

## Visibility rules

A single mechanism controls what appears in docs: the **`_` prefix convention**.

### What gets scanned

| Pattern | Scanned? | Example |
|---------|----------|---------|
| `_`-prefixed directory | No — entire package invisible | `_llm_core/` |
| `_`-prefixed file | No — skipped during initial parse | `_helpers.py`, `_types.py` |
| `__init__.py` | Always — regardless of parent | `database/__init__.py` |
| Public file (no `_` prefix) | Yes | `document.py`, `conversation.py` |

### What symbols appear in guides

| Pattern | Included? | Example |
|---------|-----------|---------|
| No `_` prefix | Yes — public | `class Document`, `def render_text` |
| Single `_` prefix | No — private | `_helper()`, `_InternalClass` |
| Dunder methods | Yes — always public | `__init__`, `__eq__`, `__init_subclass__` |
| `UPPER_CASE` constants | Yes | `MAX_TOOL_ROUNDS_DEFAULT` |

### The `__init__.py` + `__all__` role

`__all__` in `__init__.py` serves two purposes:

**1. Import paths in guide headers.** The generator traces each symbol in the top-level `__all__` back to its source module:

```python
# ai_pipeline_core/__init__.py
__all__ = ["Conversation", "Document", ...]

# Produces in llm.md:
# from ai_pipeline_core import Conversation
```

Sub-package `__init__.py` `__all__` produces the second-tier imports:

```python
# ai_pipeline_core/llm/__init__.py
__all__ = ["ModelOptions", ...]

# Produces in llm.md:
# from ai_pipeline_core.llm import ModelOptions
```

**2. Rescuing symbols from private files.** A class defined in `_types.py` (private, skipped initially) but listed in `__init__.py`'s `__all__` gets discovered during the `_remap_private_symbols()` pass. It rescans the private files, finds the symbol, and assigns it to the public module's guide.

Example:
```
database/
  __init__.py       # __all__ = ["SpanRecord", "MemoryDatabase", ...]
  _types.py         # defines SpanRecord (private file, skipped initially)
  _memory.py        # defines MemoryDatabase (private file, skipped initially)
```
Result: `database.md` includes `SpanRecord` and `MemoryDatabase` with full source.

**`__all__` is NOT required for basic visibility.** A public class in a public file appears automatically. `__all__` only matters for import path generation and rescuing symbols from private files.

### Internal types

Private classes matching `_CapitalizedName` that appear in public API signatures or type annotations are automatically included in an `## Internal Types` section. This ensures guides are self-contained — an AI agent doesn't need to hunt for type definitions.

## Test examples

Tests are the canonical usage examples (the project forbids `Example:` blocks in docstrings). The generator discovers, scores, and selects the most relevant tests.

### Discovery

Convention-based mapping: `ai_pipeline_core/<module>/` → `tests/<module>/test_*.py`. Root-level `tests/test_<module>*.py` files are also included.

### Scoring

Each test is scored against the module's public symbol names:

| Factor | Score |
|--------|-------|
| Exact subject match in test name | +5 |
| Partial symbol match in test name | +3 |
| Symbol occurrences in body | +min(count, 2) |
| Error example (`pytest.raises`) | +2 |
| Short test (< 10 lines) | +2 |
| Short test (< 20 lines) | +1 |
| Pattern bonus (`creation`, `basic`, `simple`) | +1 |
| Heavy mocking (3+ mock patterns) | -5 |
| Light mocking (1-2 mock patterns) | -2 |
| Private API calls (`._method()`) | -3 |

### Selection

- Budget: 8-16 examples per module (scales with number of classes)
- `@pytest.mark.ai_docs` marked tests get priority slots regardless of score
- Error examples capped at half the remaining budget
- Marked tests that have no symbol overlap produce a warning

### Marking tests for inclusion

Use `@pytest.mark.ai_docs` to force a test into a guide:

```python
@pytest.mark.ai_docs
@pytest.mark.asyncio
async def test_warmup_then_parallel_forks(monkeypatch):
    """Warmup populates cache, forks share the warmed prefix."""
    ...
```

The marker line itself is stripped from the rendered example.

## How to write code for this system

### Making a symbol appear in docs

1. **Define it in a public file** (no `_` prefix) — it appears automatically
2. **Or** define it in a private file and re-export via `__init__.py`'s `__all__` — the generator rescues it

### Making a symbol NOT appear in docs

1. Put it in a `_`-prefixed file (`_helpers.py`) and don't re-export it
2. Or prefix the symbol name with `_` (`def _internal_helper()`)

### Adding a new module

1. Create `ai_pipeline_core/<module_name>/` with `__init__.py`
2. Add a docstring to `__init__.py` — the first line becomes the module's purpose in README
3. Define `__all__` listing public symbols
4. Create corresponding `tests/<module_name>/test_*.py`
5. Run `make docs-ai-build` — a new `<module_name>.md` guide appears

### Writing good docstrings for docs

- First line of class/function docstring becomes the summary in README
- Full docstring is preserved in the guide's Public API section
- For `__init_subclass__` with private validation helpers, enumerate all constraints as rule lines in the class docstring (the helpers aren't visible in docs)
- Use `Field(description="...")` on Pydantic model fields — descriptions appear as inline comments in the guide
- Inline `# comments` on class fields are also extracted

### Writing good test examples

- Keep tests short and focused on public API usage
- Avoid heavy mocking — prefer real (or lightweight fake) implementations
- Don't call private APIs (`._method()`) in tests meant as examples
- Use `@pytest.mark.ai_docs` for tests that best demonstrate a pattern
- Use `pytest.raises` for error condition examples — they appear in a separate section

## Usage

```bash
# Generate .ai-docs/ from source
ai-docs generate
# or via make:
make docs-ai-build

# Validate completeness and size
ai-docs check
# or via make:
make docs-ai-check
```

### CLI options

```bash
ai-docs --source-dir <path> --tests-dir <path> --output-dir <path> generate
ai-docs --source-dir <path> --output-dir <path> check
```

All paths auto-detect from the repo root if omitted.

### Validation checks

The `check` command runs three validations:

1. **Completeness** — every public symbol (by naming convention) must appear in at least one guide as `class Name`, `def name`, or `Name =`
2. **Size** — warns if any guide exceeds 40KB. README.md has a 45KB hard limit
3. **Private reexports** — detects symbols in `__all__` imported from `_`-prefixed modules that won't have definitions in the guide (phantom imports)

## Size management

| Target | Warn | Error |
|--------|------|-------|
| Module guides | 40KB | — (warning only) |
| README.md | 40KB | 47KB |

If a guide is too large:
- Move private helpers to `_`-prefixed functions
- Split large classes into separate modules
- Reduce public API surface
