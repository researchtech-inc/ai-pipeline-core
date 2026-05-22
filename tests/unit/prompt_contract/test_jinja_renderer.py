"""Tests for ``JinjaPromptRenderer`` and ``.md.j2`` template discovery (Stage B PR 3).

Coverage:

- ``JinjaPromptRenderer`` wires the canonical context names into Jinja.
- ``StrictUndefined`` turns unknown variables into render-time errors.
- ``to_json`` filter renders Pydantic models and plain dicts as indented JSON.
- Discovery prefers ``.md.j2`` over ``.md`` and rejects ambiguity
  (TypeError when both files exist for one class).
- Discovery falls back to ``.md`` static body when only ``.md`` exists.
- Discovery falls back to the default renderer when neither file exists
  (exempt path; non-exempt path is covered by the existing missing-file
  test in ``test_body_file.py``).
- Import-time Jinja syntax errors and H1-outside-fence errors surface as
  ``TypeError`` at class definition time.
- An end-to-end Jinja-template-backed contract renders the prompt via
  the new renderer through ``PromptContract.execute``.
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
from ai_pipeline_core.prompt_contract._body_file import load_body_file
from ai_pipeline_core.prompt_contract._render import (
    DefaultPromptRenderer,
    JinjaPromptRenderer,
    _to_json_filter,
    build_prompt_render_context,
    select_renderer_for_contract,
)


# ---------------------------------------------------------------------------
# Fixtures: tiny output type + helper to stage classes inside tmp_path
# ---------------------------------------------------------------------------


class JinjaOutput(FrozenBaseModel):
    """Tiny output used by Jinja renderer tests."""

    answer: str


def _install_module(monkeypatch: pytest.MonkeyPatch, name: str, file: Path) -> str:
    """Create a stub module backed by ``file`` and register it in ``sys.modules``."""
    module_name = f"_test_jinja_stub_{name}"
    module = types.ModuleType(module_name)
    module.__file__ = str(file)
    monkeypatch.setitem(sys.modules, module_name, module)
    return module_name


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
# JinjaPromptRenderer: rendering, strict undefined, custom filter exposure
# ---------------------------------------------------------------------------


class _RenderTargetContract(PromptContract[JinjaOutput]):
    """Lightweight contract used to seed a render context for renderer tests."""

    purpose: ClassVar[str] = "render"
    returns: ClassVar[str] = "JinjaOutput"
    success_criteria: ClassVar[str] = "any"

    topic: str


class TestJinjaPromptRenderer:
    def test_render_exposes_canonical_top_level_names(self) -> None:
        context = build_prompt_render_context(_RenderTargetContract, dynamic_fields={"topic": "ml"})
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
        rendered = JinjaPromptRenderer(template).render(context)
        assert "contract=render" in rendered
        assert "inputs.topic.text=ml" in rendered
        assert "output=JinjaOutput" in rendered
        assert "notation_active=False" in rendered

    def test_strict_undefined_raises_on_unknown_variable(self) -> None:
        context = build_prompt_render_context(_RenderTargetContract, dynamic_fields={"topic": "ml"})
        template = "{{ does_not_exist }}"
        with pytest.raises(jinja2.UndefinedError):
            JinjaPromptRenderer(template).render(context)

    def test_to_json_filter_available_for_pydantic_model(self) -> None:
        context = build_prompt_render_context(_RenderTargetContract, dynamic_fields={"topic": "ml"})
        rendered = JinjaPromptRenderer("{{ contract | to_json }}").render(context)
        assert '"purpose": "render"' in rendered

    def test_to_json_filter_available_for_plain_dict(self) -> None:
        context = build_prompt_render_context(_RenderTargetContract, dynamic_fields={"topic": "ml"})
        rendered = JinjaPromptRenderer("{{ inputs | to_json }}").render(context)
        # inputs is a dict[str, InputView] — to_json falls back to json.dumps with default=str.
        assert "topic" in rendered

    def test_select_renderer_default_for_static_body(self) -> None:
        renderer = select_renderer_for_contract(_RenderTargetContract)
        assert isinstance(renderer, DefaultPromptRenderer)


# ---------------------------------------------------------------------------
# Discovery: .md.j2 vs .md vs both vs neither
# ---------------------------------------------------------------------------


class TestTemplateDiscovery:
    def test_jinja_template_discovered(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        py_file = tmp_path / "definer.py"
        py_file.write_text("# stub")
        (tmp_path / "my_analysis.md.j2").write_text("## H2\n\nhello {{ contract.purpose }}\n")
        cls = type("MyAnalysisContract", (), {})
        cls.__module__ = _install_module(monkeypatch, "jinja_only", py_file)
        body = load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)
        assert body.format == "jinja"
        assert "{{ contract.purpose }}" in body.source

    def test_static_md_used_when_jinja_absent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        py_file = tmp_path / "definer.py"
        py_file.write_text("# stub")
        (tmp_path / "my_analysis.md").write_text("## H2\n\nstatic body\n")
        cls = type("MyAnalysisContract", (), {})
        cls.__module__ = _install_module(monkeypatch, "static_only", py_file)
        body = load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)
        assert body.format == "static"
        assert "static body" in body.source

    def test_both_files_present_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        py_file = tmp_path / "definer.py"
        py_file.write_text("# stub")
        (tmp_path / "my_analysis.md").write_text("## H2\n\nstatic\n")
        (tmp_path / "my_analysis.md.j2").write_text("## H2\n\njinja {{ contract.purpose }}\n")
        cls = type("MyAnalysisContract", (), {})
        cls.__module__ = _install_module(monkeypatch, "both", py_file)
        with pytest.raises(TypeError, match="has both a Jinja template and a static markdown body"):
            load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)

    def test_exempt_with_neither_returns_none_format(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        py_file = tmp_path / "definer.py"
        py_file.write_text("# stub")
        cls = type("MyAnalysisContract", (), {})
        cls.__module__ = _install_module(monkeypatch, "exempt_none", py_file)
        body = load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=True)
        assert body.format == "none"
        assert body.source == ""


# ---------------------------------------------------------------------------
# Import-time validation: Jinja syntax + H1 inside .md.j2
# ---------------------------------------------------------------------------


class TestJinjaImportTimeValidation:
    def test_invalid_jinja_syntax_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        py_file = tmp_path / "definer.py"
        py_file.write_text("# stub")
        # Unterminated {% block opens a tag jinja2 cannot parse.
        (tmp_path / "my_analysis.md.j2").write_text("## H2\n\n{% if x %}no endif\n")
        cls = type("MyAnalysisContract", (), {})
        cls.__module__ = _install_module(monkeypatch, "bad_jinja", py_file)
        with pytest.raises(TypeError, match="invalid Jinja syntax"):
            load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)

    def test_h1_outside_fence_in_jinja_template_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        py_file = tmp_path / "definer.py"
        py_file.write_text("# stub")
        (tmp_path / "my_analysis.md.j2").write_text("## H2 ok\n\n# Bad H1 in template\n")
        cls = type("MyAnalysisContract", (), {})
        cls.__module__ = _install_module(monkeypatch, "h1_jinja", py_file)
        with pytest.raises(TypeError, match="uses '# ' \\(H1\\)"):
            load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)

    def test_empty_jinja_template_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        py_file = tmp_path / "definer.py"
        py_file.write_text("# stub")
        (tmp_path / "my_analysis.md.j2").write_text("   \n   \n")
        cls = type("MyAnalysisContract", (), {})
        cls.__module__ = _install_module(monkeypatch, "empty_jinja", py_file)
        with pytest.raises(TypeError, match="body file is empty"):
            load_body_file(cls, suffix="Contract", kind="PromptContract", exempt=False)


# ---------------------------------------------------------------------------
# End-to-end: PromptContract.execute() picks JinjaPromptRenderer when
# a paired .md.j2 template is discovered next to the contract's module.
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


def _build_jinja_template_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[type[PromptContract[JinjaOutput]], type[Methodology]]:
    """Construct a PromptContract subclass whose paired ``.md.j2`` lives in ``tmp_path``."""
    py_file = tmp_path / "_jinja_e2e_module.py"
    py_file.write_text("# stub for e2e jinja tests")
    module_name = _install_module(monkeypatch, "jinja_e2e", py_file)

    # Paired Jinja template for the methodology.
    (tmp_path / "render_e2e.md.j2").write_text(
        textwrap.dedent("""
            ## Methodology body

            Purpose: {{ methodology.purpose }}
            Fields: {% for f in methodology.fields %}{{ f.name }}={{ f.text }} {% endfor %}
        """).strip()
        + "\n"
    )

    # Paired Jinja template for the contract.
    (tmp_path / "render_e2e_jinja.md.j2").write_text(
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
        assert contract_cls._body_format == "jinja"
        assert select_renderer_for_contract(contract_cls).__class__ is JinjaPromptRenderer

        captured = _patch_generate(monkeypatch, _make_response(JinjaOutput(answer="ok")))
        await contract_cls(topic="ml").execute(DEFAULT_TEST_MODEL)

        assert len(captured) == 1
        user_text = "\n".join(message.content if isinstance(message.content, str) else "" for message in captured[0].messages if message.role.value == "user")
        # Substrings emitted only when the new Jinja renderer is wired up.
        assert "Contract purpose: use jinja template" in user_text
        assert "Topic: ml" in user_text
        assert "Output: JinjaOutput" in user_text
        # Methodology body was rendered with the methodology view's `purpose` field.
        assert "Purpose: guide e2e rendering" in user_text
        assert "rubric=single-rubric for e2e test" in user_text

    def test_methodology_body_rendered_when_jinja(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The methodology's ``.md.j2`` template renders during view build."""
        contract_cls, methodology_cls = _build_jinja_template_contract(monkeypatch, tmp_path)
        assert methodology_cls._body_format == "jinja"

        # Build a contract-render context directly to inspect the methodology body.
        context = build_prompt_render_context(contract_cls, dynamic_fields={"topic": "x"})
        assert len(context.methodologies) == 1
        body = context.methodologies[0].body
        assert "Purpose: guide e2e rendering" in body
        assert "rubric=single-rubric for e2e test" in body
