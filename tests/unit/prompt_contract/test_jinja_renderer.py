"""Tests for Jinja prompt rendering and ``.j2`` template discovery.

Coverage:

- ``render_prompt`` wires the canonical context names into Jinja.
- ``StrictUndefined`` turns unknown variables into render-time errors.
- ``to_json`` filter renders Pydantic models and plain dicts as indented JSON.
- Discovery loads the paired ``.j2`` body next to the defining module.
- An end-to-end Jinja-template-backed contract renders the prompt through
  ``PromptContract.execute``.
"""

from __future__ import annotations

import sys
import textwrap
import types
from pathlib import Path
from typing import Any, ClassVar

import jinja2
import pytest
from pydantic import BaseModel

from ai_pipeline_core import FrozenBaseModel
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.prompt_contract import Methodology, PromptContract
from ai_pipeline_core.prompt_contract.body_file import load_body_file
from ai_pipeline_core.prompt_contract.render import (
    _to_json_filter,
    build_prompt_render_context,
    render_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures: tiny output type + helper to stage classes inside tmp_path
# ---------------------------------------------------------------------------


class JinjaOutput(FrozenBaseModel):
    """Tiny output used by Jinja renderer tests.

    Two fields keep the contract in structured (not single-str prose) mode.
    """

    answer: str
    score: int = 0


def _install_module(monkeypatch: pytest.MonkeyPatch, name: str, file: Path) -> str:
    """Create a stub module backed by ``file`` and register it in ``sys.modules``."""
    module_name = f"_test_jinja_stub_{name}"
    module = types.ModuleType(module_name)
    module.__file__ = str(file)
    monkeypatch.setitem(sys.modules, module_name, module)
    return module_name


def _context(cls: type, **kwargs: object) -> Any:
    kwargs.setdefault("dynamic_fields", {})
    kwargs.setdefault("ordered_documents", ())
    return build_prompt_render_context(cls, **kwargs)  # type: ignore[arg-type]  # kwargs widen keyword-only signature


# ---------------------------------------------------------------------------
# to_json filter
# ---------------------------------------------------------------------------


class TestToJsonFilter:
    def test_basemodel_round_trips_via_model_dump_json(self) -> None:
        class M(BaseModel):
            x: int
            y: str

        rendered = _to_json_filter(M(x=1, y="hi"))
        assert '"x": 1' in rendered
        assert '"y": "hi"' in rendered
        assert "\n" in rendered

    def test_plain_dict_uses_json_dumps_with_indent(self) -> None:
        rendered = _to_json_filter({"a": 1, "b": [2, 3]})
        assert '"a": 1' in rendered
        assert "\n" in rendered

    def test_unencodable_object_falls_back_to_str(self) -> None:
        rendered = _to_json_filter(Path("/tmp/example"))
        assert "tmp/example" in rendered


# ---------------------------------------------------------------------------
# render_prompt: rendering, strict undefined, custom filter exposure
# ---------------------------------------------------------------------------


class _RenderTargetContract(PromptContract[JinjaOutput]):
    """Lightweight contract used to seed a render context for renderer tests."""

    purpose: ClassVar[str] = "render"
    returns: ClassVar[str] = "JinjaOutput"
    success_criteria: ClassVar[str] = "any"

    topic: str


class TestJinjaRendering:
    def test_render_exposes_canonical_top_level_names(self) -> None:
        context = _context(_RenderTargetContract, dynamic_fields={"topic": "ml"})
        template = textwrap.dedent("""
            contract={{ contract.purpose }}
            inputs.topic.text={{ inputs.topic.text }}
            input_order={{ input_order | length }}
            documents={{ documents | length }}
            methodologies={{ methodologies | length }}
            output={{ output.class_name }}
            tools={{ tools | length }}
            citations_enabled={{ citations.enabled }}
            notation_active={{ notation.active }}
        """).strip()
        rendered = render_prompt(context, template)
        assert "contract=render" in rendered
        assert "inputs.topic.text=ml" in rendered
        assert "output=JinjaOutput" in rendered
        assert "notation_active=False" in rendered

    def test_strict_undefined_raises_on_unknown_variable(self) -> None:
        context = _context(_RenderTargetContract, dynamic_fields={"topic": "ml"})
        with pytest.raises(jinja2.UndefinedError):
            render_prompt(context, "{{ does_not_exist }}")

    def test_to_json_filter_available_for_pydantic_model(self) -> None:
        context = _context(_RenderTargetContract, dynamic_fields={"topic": "ml"})
        rendered = render_prompt(context, "{{ contract | to_json }}")
        assert '"purpose": "render"' in rendered

    def test_to_json_filter_available_for_plain_dict(self) -> None:
        context = _context(_RenderTargetContract, dynamic_fields={"topic": "ml"})
        rendered = render_prompt(context, "{{ inputs | to_json }}")
        # inputs is a dict[str, InputView] — to_json falls back to json.dumps with default=str.
        assert "topic" in rendered


# ---------------------------------------------------------------------------
# Discovery: paired .j2 body next to the defining module
# ---------------------------------------------------------------------------


class TestTemplateDiscovery:
    def test_jinja_template_discovered(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        py_file = tmp_path / "definer.py"
        py_file.write_text("# stub")
        (tmp_path / "my_analysis.j2").write_text("## H2\n\nhello {{ contract.purpose }}\n")
        cls = type("MyAnalysisContract", (), {})
        cls.__module__ = _install_module(monkeypatch, "jinja_only", py_file)
        body = load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)
        assert "{{ contract.purpose }}" in body.source

    def test_exempt_with_no_file_returns_empty_source(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        py_file = tmp_path / "definer.py"
        py_file.write_text("# stub")
        cls = type("MyAnalysisContract", (), {})
        cls.__module__ = _install_module(monkeypatch, "exempt_none", py_file)
        body = load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=True)
        assert body.source == ""


# ---------------------------------------------------------------------------
# End-to-end: PromptContract.execute() renders the paired .j2 template
# next to the contract's module.
# ---------------------------------------------------------------------------


def _patch_generate(monkeypatch: pytest.MonkeyPatch, response: ModelResponse[Any]) -> list[Any]:
    captured: list[Any] = []

    async def fake_generate(request: Any, *, response_type: Any = None) -> ModelResponse[Any]:
        _ = response_type
        captured.append(request)
        return response

    from ai_pipeline_core.llm import _engine

    monkeypatch.setattr(_engine, "generate", fake_generate)
    return captured


def _make_response(parsed: JinjaOutput) -> ModelResponse[Any]:
    return ModelResponse[Any](
        content=parsed.model_dump_json(),
        parsed=parsed,
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        cost=0.0,
        model="test-model",
        response_id="r",
    )


def _build_jinja_template_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[type[PromptContract[JinjaOutput]], type[Methodology]]:
    """Construct a PromptContract subclass whose paired ``.j2`` lives in ``tmp_path``."""
    py_file = tmp_path / "_jinja_e2e_module.py"
    py_file.write_text("# stub for e2e jinja tests")
    module_name = _install_module(monkeypatch, "jinja_e2e", py_file)

    # Paired Jinja template for the methodology.
    (tmp_path / "render_e2e.j2").write_text(
        textwrap.dedent("""
            ## Methodology body

            Purpose: {{ methodology.purpose }}
            Fields: {% for f in methodology.fields %}{{ f.name }}={{ f.text }} {% endfor %}
        """).strip()
        + "\n"
    )

    # Paired Jinja template for the contract.
    (tmp_path / "render_e2e_jinja.j2").write_text(
        textwrap.dedent("""
            ## Header

            Contract purpose: {{ contract.purpose }}
            Topic: {{ inputs.topic.text }}
            Output: {{ output.class_name }}
            Methodology count: {{ methodologies | length }}
            {% if methodologies %}First-methodology-body:
            {{ methodologies[0].body }}{% endif %}
        """).strip()
        + "\n"
    )

    methodology_cls: type[Methodology] = types.new_class(
        "RenderE2eMethodology",
        (Methodology,),
        {},
        lambda ns: ns.update({
            "__doc__": "Methodology for the Jinja e2e renderer test.",
            "__module__": module_name,
            "purpose": "guide e2e rendering",
            "rubric": "single-rubric for e2e test",
        }),
    )

    contract_cls: type[PromptContract[JinjaOutput]] = types.new_class(
        "RenderE2eJinjaContract",
        (PromptContract[JinjaOutput],),
        {},
        lambda ns: ns.update({
            "__doc__": "Contract for the Jinja end-to-end renderer test.",
            "__module__": module_name,
            "__annotations__": {"topic": str},
            "purpose": "use jinja template",
            "returns": "JinjaOutput",
            "success_criteria": "answer non-empty",
            "methodologies": (methodology_cls,),
        }),
    )
    return contract_cls, methodology_cls


class TestEndToEndJinjaRendering:
    @pytest.mark.asyncio
    async def test_execute_uses_jinja_template(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from tests.support.model_catalog import DEFAULT_TEST_MODEL

        contract_cls, _methodology_cls = _build_jinja_template_contract(monkeypatch, tmp_path)
        assert "{{ contract.purpose }}" in contract_cls._body

        captured = _patch_generate(monkeypatch, _make_response(JinjaOutput(answer="ok")))
        await contract_cls(topic="ml").execute(DEFAULT_TEST_MODEL)

        assert len(captured) == 1
        user_text = "\n".join(
            message.content if isinstance(message.content, str) else ""
            for message in captured[0].messages
            if message.role.value == "user"
        )
        # Substrings emitted only when the Jinja body template is rendered.
        assert "Contract purpose: use jinja template" in user_text
        assert "Topic: ml" in user_text
        assert "Output: JinjaOutput" in user_text
        # Methodology body was rendered with the methodology view's `purpose` field.
        assert "Purpose: guide e2e rendering" in user_text
        assert "rubric=single-rubric for e2e test" in user_text

    def test_methodology_body_rendered_when_jinja(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The methodology's ``.j2`` template renders during view build."""
        contract_cls, methodology_cls = _build_jinja_template_contract(monkeypatch, tmp_path)
        assert "{{ methodology.purpose }}" in methodology_cls._body

        # Build a contract-render context directly to inspect the methodology body.
        context = _context(contract_cls, dynamic_fields={"topic": "x"})
        assert len(context.methodologies) == 1
        body = context.methodologies[0].body
        assert "Purpose: guide e2e rendering" in body
        assert "rubric=single-rubric for e2e test" in body
