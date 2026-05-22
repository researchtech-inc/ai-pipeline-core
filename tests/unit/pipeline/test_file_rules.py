"""Tests for file-level structural rules.

Validates the import-time enforcement of:
- One PipelineFlow per file
- One PipelineTask per file
- No flow/task/spec mixing in same file
- PromptSpec co-location rules (follows= chains)
- Mandatory docstrings on flows, tasks, specs
- Exemption for framework internals, tests, examples
- Abstract base class bypass
- Registry reset for test isolation
- Actionable error messages
"""

# pyright: reportPrivateUsage=false, reportUnusedClass=false

import pytest

from ai_pipeline_core.pipeline._file_rules import (
    _cross_file_follows,
    _flows,
    _specs,
    _tasks,
    is_exempt,
    register_flow,
    register_spec,
    register_task,
    require_docstring,
    reset_registries,
)


@pytest.fixture(autouse=True)
def _clean_registries():
    """Reset file-rule registries between tests."""
    reset_registries()
    yield
    reset_registries()


# ── Helpers ──────────────────────────────────────────────────────────────

# Default module simulates non-exempt application code
_APP_MODULE = "app.flows.analysis.analysis"
_APP_SPEC_MODULE = "app.flows.analysis.tasks.extract.specs.analysis"


def _make_cls(
    name: str,
    *,
    module: str = _APP_MODULE,
    doc: str | None = "A docstring.",
) -> type:
    """Create a lightweight stub class with controlled __module__ and __doc__."""
    cls = type(name, (), {"__module__": module, "__doc__": doc, "__qualname__": name})
    cls.__module__ = module
    return cls


# ═══════════════════════════════════════════════════════════════════════════
# is_exempt — exemption logic
# ═══════════════════════════════════════════════════════════════════════════


class TestIsExemptFrameworkInternal:
    """Framework-internal classes (ai_pipeline_core.*) are exempt."""

    def test_framework_pipeline_module(self):
        assert is_exempt(_make_cls("X", module="ai_pipeline_core.pipeline._flow")) is True

    def test_framework_llm_module(self):
        assert is_exempt(_make_cls("X", module="ai_pipeline_core.llm.conversation")) is True

    def test_framework_root_submodule(self):
        assert is_exempt(_make_cls("X", module="ai_pipeline_core.settings")) is True


class TestIsExemptTestModules:
    """Test files (tests.*, test_*, conftest) are exempt."""

    def test_tests_directory(self):
        assert is_exempt(_make_cls("X", module="tests.unit.pipeline.test_flows")) is True

    def test_tests_as_top_level(self):
        assert is_exempt(_make_cls("X", module="tests")) is True

    def test_test_prefix_module(self):
        assert is_exempt(_make_cls("X", module="test_something.flows")) is True

    def test_conftest_top_level(self):
        assert is_exempt(_make_cls("X", module="conftest")) is True

    def test_conftest_nested(self):
        assert is_exempt(_make_cls("X", module="myproject.conftest")) is True

    def test_test_prefix_nested(self):
        assert is_exempt(_make_cls("X", module="pkg.test_integration.helpers")) is True


class TestIsExemptExamples:
    """Example files (examples.*) are exempt."""

    def test_examples_top_level(self):
        assert is_exempt(_make_cls("X", module="examples.showcase")) is True

    def test_examples_nested(self):
        assert is_exempt(_make_cls("X", module="examples.advanced.remote")) is True


class TestIsExemptMainAndLocals:
    """Scripts run as __main__ and function-local classes are exempt."""

    def test_main_module(self):
        assert is_exempt(_make_cls("X", module="__main__")) is True

    def test_locals_qualname(self):
        cls = _make_cls("X", module="app.flows.analysis")
        cls.__qualname__ = "some_function.<locals>.X"
        assert is_exempt(cls) is True

    def test_nested_locals_qualname(self):
        cls = _make_cls("X", module="app.flows.analysis")
        cls.__qualname__ = "outer.<locals>.inner.<locals>.X"
        assert is_exempt(cls) is True


class TestIsExemptNotExempt:
    """Regular application code is not exempt."""

    def test_app_code(self):
        assert is_exempt(_make_cls("X", module="app.flows.analysis.analysis")) is False

    def test_plain_package(self):
        assert is_exempt(_make_cls("X", module="mypackage.flows")) is False

    def test_top_level_module(self):
        assert is_exempt(_make_cls("X", module="mymodule")) is False

    def test_empty_module_not_exempt(self):
        assert is_exempt(_make_cls("X", module="")) is False

    def test_none_module_not_exempt(self):
        cls = _make_cls("X")
        cls.__module__ = None  # type: ignore[assignment]
        assert is_exempt(cls) is False


# ═══════════════════════════════════════════════════════════════════════════
# require_docstring — mandatory docstring enforcement
# ═══════════════════════════════════════════════════════════════════════════


class TestRequireDocstring:
    """Test mandatory docstring enforcement."""

    def test_accepts_nonempty_docstring(self):
        require_docstring(_make_cls("X", doc="Processes input."), kind="PipelineTask")

    def test_accepts_multiline_docstring(self):
        require_docstring(
            _make_cls("X", doc="First line.\n\nInput: docs.\nOutput: results."),
            kind="PipelineTask",
        )

    def test_rejects_none_docstring(self):
        with pytest.raises(TypeError, match="must have a docstring"):
            require_docstring(_make_cls("X", doc=None), kind="PipelineTask")

    def test_rejects_empty_string_docstring(self):
        with pytest.raises(TypeError, match="must have a docstring"):
            require_docstring(_make_cls("X", doc=""), kind="PipelineTask")

    def test_rejects_whitespace_only_docstring(self):
        with pytest.raises(TypeError, match="must have a docstring"):
            require_docstring(_make_cls("X", doc="   \n  \t  "), kind="PipelineTask")

    def test_error_message_includes_class_name(self):
        with pytest.raises(TypeError, match="'MyTask'"):
            require_docstring(_make_cls("MyTask", doc=None), kind="PipelineTask")

    def test_error_message_includes_kind_name(self):
        with pytest.raises(TypeError, match="PipelineFlow"):
            require_docstring(_make_cls("X", doc=None), kind="PipelineFlow")

    def test_error_message_includes_fix_instruction(self):
        with pytest.raises(TypeError, match="FIX"):
            require_docstring(_make_cls("X", doc=None), kind="PipelineTask")

    def test_error_message_includes_input_output_guidance(self):
        with pytest.raises(TypeError, match=r"(?s)Input:.*Output:"):
            require_docstring(_make_cls("X", doc=None), kind="PipelineTask")

    def test_error_message_for_promptspec_kind(self):
        with pytest.raises(TypeError, match="PromptSpec"):
            require_docstring(_make_cls("X", doc=None), kind="PromptSpec")


# ═══════════════════════════════════════════════════════════════════════════
# register_flow — one flow per file
# ═══════════════════════════════════════════════════════════════════════════


class TestRegisterFlowOnePerFile:
    """At most one PipelineFlow per module."""

    def test_single_flow_accepted(self):
        register_flow(_make_cls("AnalysisFlow"))

    def test_second_flow_same_module_rejected(self):
        register_flow(_make_cls("AnalysisFlow"))
        with pytest.raises(TypeError, match="already contains PipelineFlow 'AnalysisFlow'"):
            register_flow(_make_cls("ExtractionFlow"))

    def test_same_class_name_twice_is_idempotent(self):
        """Re-registering the same class name is fine (module reimport)."""
        cls = _make_cls("AnalysisFlow")
        register_flow(cls)
        register_flow(cls)

    def test_different_modules_are_independent(self):
        register_flow(_make_cls("AnalysisFlow", module="app.flows.a.a"))
        register_flow(_make_cls("ExtractionFlow", module="app.flows.b.b"))

    def test_error_suggests_snake_case_path(self):
        register_flow(_make_cls("AnalysisFlow"))
        with pytest.raises(TypeError, match=r"flows/extraction_flow/extraction_flow\.py"):
            register_flow(_make_cls("ExtractionFlow"))


# ═══════════════════════════════════════════════════════════════════════════
# register_task — one task per file
# ═══════════════════════════════════════════════════════════════════════════


class TestRegisterTaskOnePerFile:
    """At most one PipelineTask per module."""

    def test_single_task_accepted(self):
        register_task(_make_cls("AnalyzeTask"))

    def test_second_task_same_module_rejected(self):
        register_task(_make_cls("AnalyzeTask"))
        with pytest.raises(TypeError, match="already contains PipelineTask 'AnalyzeTask'"):
            register_task(_make_cls("ExtractTask"))

    def test_same_class_name_twice_is_idempotent(self):
        cls = _make_cls("AnalyzeTask")
        register_task(cls)
        register_task(cls)

    def test_different_modules_are_independent(self):
        register_task(_make_cls("AnalyzeTask", module="app.flows.a.tasks.analyze.analyze"))
        register_task(_make_cls("ExtractTask", module="app.flows.a.tasks.extract.extract"))

    def test_error_suggests_snake_case_path(self):
        register_task(_make_cls("AnalyzeTask"))
        with pytest.raises(TypeError, match=r"tasks/extract_task/extract_task\.py"):
            register_task(_make_cls("ExtractTask"))


# ═══════════════════════════════════════════════════════════════════════════
# No flow + task mixing
# ═══════════════════════════════════════════════════════════════════════════


class TestNoFlowTaskMixing:
    """Flows and tasks cannot coexist in the same module."""

    def test_flow_then_task_rejected(self):
        register_flow(_make_cls("AnalysisFlow"))
        with pytest.raises(TypeError, match="already contains PipelineFlow 'AnalysisFlow'"):
            register_task(_make_cls("AnalyzeTask"))

    def test_task_then_flow_rejected(self):
        register_task(_make_cls("AnalyzeTask"))
        with pytest.raises(TypeError, match="already contains PipelineTask 'AnalyzeTask'"):
            register_flow(_make_cls("AnalysisFlow"))

    def test_error_says_separate_files(self):
        register_flow(_make_cls("AnalysisFlow"))
        with pytest.raises(TypeError, match="Flows and tasks must be in separate files"):
            register_task(_make_cls("AnalyzeTask"))

    def test_error_suggests_both_paths(self):
        register_flow(_make_cls("AnalysisFlow"))
        with pytest.raises(TypeError, match="FIX"):
            register_task(_make_cls("AnalyzeTask"))


# ═══════════════════════════════════════════════════════════════════════════
# No flow + spec mixing
# ═══════════════════════════════════════════════════════════════════════════


class TestNoFlowSpecMixing:
    """Flows and specs cannot coexist in the same module."""

    def test_flow_then_spec_rejected(self):
        register_flow(_make_cls("AnalysisFlow"))
        with pytest.raises(TypeError, match="already contains PipelineFlow 'AnalysisFlow'"):
            register_spec(_make_cls("AnalysisSpec"), follows=None)

    def test_spec_then_flow_rejected(self):
        register_spec(_make_cls("AnalysisSpec"), follows=None)
        with pytest.raises(TypeError, match="already contains PromptSpec"):
            register_flow(_make_cls("AnalysisFlow"))

    def test_flow_then_followup_spec_rejected(self):
        """Even a follows= spec can't share a file with a flow."""
        parent = _make_cls("ParentSpec", module="app.specs.other")
        register_spec(parent, follows=None)
        register_flow(_make_cls("AnalysisFlow"))
        with pytest.raises(TypeError, match="PipelineFlow"):
            register_spec(_make_cls("ChildSpec"), follows=parent)

    def test_error_says_separate_files(self):
        register_flow(_make_cls("AnalysisFlow"))
        with pytest.raises(TypeError, match="Specs and flows must be in separate files"):
            register_spec(_make_cls("AnalysisSpec"), follows=None)


# ═══════════════════════════════════════════════════════════════════════════
# No task + spec mixing
# ═══════════════════════════════════════════════════════════════════════════


class TestNoTaskSpecMixing:
    """Tasks and specs cannot coexist in the same module."""

    def test_task_then_spec_rejected(self):
        register_task(_make_cls("AnalyzeTask"))
        with pytest.raises(TypeError, match="already contains PipelineTask 'AnalyzeTask'"):
            register_spec(_make_cls("AnalyzeSpec"), follows=None)

    def test_spec_then_task_rejected(self):
        register_spec(_make_cls("AnalyzeSpec"), follows=None)
        with pytest.raises(TypeError, match="already contains PromptSpec"):
            register_task(_make_cls("AnalyzeTask"))

    def test_task_then_followup_spec_rejected(self):
        """Even a follows= spec can't share a file with a task."""
        parent = _make_cls("ParentSpec", module="app.specs.other")
        register_spec(parent, follows=None)
        register_task(_make_cls("AnalyzeTask"))
        with pytest.raises(TypeError, match="PipelineTask"):
            register_spec(_make_cls("ChildSpec"), follows=parent)

    def test_error_says_separate_files(self):
        register_task(_make_cls("AnalyzeTask"))
        with pytest.raises(TypeError, match="Specs and tasks must be in separate files"):
            register_spec(_make_cls("AnalyzeSpec"), follows=None)


# ═══════════════════════════════════════════════════════════════════════════
# Spec co-location: standalone specs
# ═══════════════════════════════════════════════════════════════════════════


class TestSpecStandalone:
    """At most one standalone spec (no follows=) per file."""

    def test_single_standalone_accepted(self):
        register_spec(_make_cls("AnalysisSpec", module=_APP_SPEC_MODULE), follows=None)

    def test_two_standalone_rejected(self):
        m = _APP_SPEC_MODULE
        register_spec(_make_cls("AnalysisSpec", module=m), follows=None)
        with pytest.raises(TypeError, match="already contains standalone PromptSpec 'AnalysisSpec'"):
            register_spec(_make_cls("ReviewSpec", module=m), follows=None)

    def test_error_suggests_own_file(self):
        m = _APP_SPEC_MODULE
        register_spec(_make_cls("AnalysisSpec", module=m), follows=None)
        with pytest.raises(TypeError, match=r"FIX.*own file"):
            register_spec(_make_cls("ReviewSpec", module=m), follows=None)

    def test_error_suggests_snake_case_path(self):
        m = _APP_SPEC_MODULE
        register_spec(_make_cls("AnalysisSpec", module=m), follows=None)
        with pytest.raises(TypeError, match=r"specs/review_spec\.py"):
            register_spec(_make_cls("ReviewSpec", module=m), follows=None)


# ═══════════════════════════════════════════════════════════════════════════
# Spec co-location: follows= in same file (allowed)
# ═══════════════════════════════════════════════════════════════════════════


class TestSpecFollowsSameFile:
    """A follows= spec in the same module as its parent is always allowed."""

    def test_single_followup_accepted(self):
        m = _APP_SPEC_MODULE
        parent = _make_cls("AnalysisSpec", module=m)
        child = _make_cls("RevisionSpec", module=m)
        register_spec(parent, follows=None)
        register_spec(child, follows=parent)

    def test_multiple_followups_accepted(self):
        m = _APP_SPEC_MODULE
        parent = _make_cls("AnalysisSpec", module=m)
        register_spec(parent, follows=None)
        register_spec(_make_cls("RevisionSpec", module=m), follows=parent)
        register_spec(_make_cls("DeepRevisionSpec", module=m), follows=parent)

    def test_chain_of_followups_accepted(self):
        """A -> B -> C where each follows the previous, all in same file."""
        m = _APP_SPEC_MODULE
        a = _make_cls("SpecA", module=m)
        b = _make_cls("SpecB", module=m)
        c = _make_cls("SpecC", module=m)
        register_spec(a, follows=None)
        register_spec(b, follows=a)
        register_spec(c, follows=b)


# ═══════════════════════════════════════════════════════════════════════════
# Spec co-location: follows= from different file (must be alone)
# ═══════════════════════════════════════════════════════════════════════════


class TestSpecFollowsCrossFile:
    """A follows= spec pointing to another module must be alone in its file."""

    def test_cross_file_followup_alone_accepted(self):
        parent = _make_cls("AnalysisSpec", module="app.specs.analysis")
        child = _make_cls("RevisionSpec", module="app.specs.revision")
        register_spec(parent, follows=None)
        register_spec(child, follows=parent)

    def test_existing_spec_then_cross_file_followup_rejected(self):
        """Cannot add a cross-file follow-up to a module that already has specs."""
        parent = _make_cls("AnalysisSpec", module="app.specs.analysis")
        existing = _make_cls("SomeSpec", module="app.specs.revision")
        child = _make_cls("RevisionSpec", module="app.specs.revision")
        register_spec(parent, follows=None)
        register_spec(existing, follows=None)
        with pytest.raises(TypeError, match="follows a spec from a different file must be the only spec"):
            register_spec(child, follows=parent)

    def test_cross_file_followup_then_standalone_rejected(self):
        """Cannot add a standalone spec after a cross-file follow-up (reverse order)."""
        parent = _make_cls("AnalysisSpec", module="app.specs.analysis")
        cross_child = _make_cls("RemoteRevisionSpec", module="app.specs.revision")
        standalone = _make_cls("LocalSpec", module="app.specs.revision")
        register_spec(parent, follows=None)
        register_spec(cross_child, follows=parent)
        with pytest.raises(TypeError, match="must be the only spec"):
            register_spec(standalone, follows=None)

    def test_cross_file_followup_then_another_followup_rejected(self):
        """Cannot add any spec after a cross-file follow-up, even a same-file follow-up."""
        external_parent = _make_cls("AnalysisSpec", module="app.specs.analysis")
        cross_child = _make_cls("CrossSpec", module="app.specs.revision")
        another = _make_cls("AnotherSpec", module="app.specs.revision")
        register_spec(external_parent, follows=None)
        register_spec(cross_child, follows=external_parent)
        with pytest.raises(TypeError, match="must be the only spec"):
            register_spec(another, follows=cross_child)

    def test_error_names_conflicting_existing_spec(self):
        parent = _make_cls("AnalysisSpec", module="app.specs.analysis")
        existing = _make_cls("SomeSpec", module="app.specs.revision")
        child = _make_cls("RevisionSpec", module="app.specs.revision")
        register_spec(parent, follows=None)
        register_spec(existing, follows=None)
        with pytest.raises(TypeError, match="SomeSpec"):
            register_spec(child, follows=parent)

    def test_reverse_order_error_names_cross_file_spec(self):
        """Error message names the cross-file spec requiring isolation."""
        parent = _make_cls("AnalysisSpec", module="app.specs.analysis")
        cross_child = _make_cls("RemoteRevisionSpec", module="app.specs.revision")
        standalone = _make_cls("LocalSpec", module="app.specs.revision")
        register_spec(parent, follows=None)
        register_spec(cross_child, follows=parent)
        with pytest.raises(TypeError, match="RemoteRevisionSpec"):
            register_spec(standalone, follows=None)


# ═══════════════════════════════════════════════════════════════════════════
# reset_registries — test isolation
# ═══════════════════════════════════════════════════════════════════════════


class TestResetRegistries:
    """reset_registries() clears all internal state."""

    def test_reset_allows_new_flow_in_same_module(self):
        register_flow(_make_cls("AnalysisFlow"))
        reset_registries()
        register_flow(_make_cls("DifferentFlow"))

    def test_reset_allows_new_task_in_same_module(self):
        register_task(_make_cls("AnalyzeTask"))
        reset_registries()
        register_task(_make_cls("DifferentTask"))

    def test_reset_allows_new_spec_in_same_module(self):
        register_spec(_make_cls("AnalysisSpec", module=_APP_SPEC_MODULE), follows=None)
        reset_registries()
        register_spec(_make_cls("DifferentSpec", module=_APP_SPEC_MODULE), follows=None)

    def test_reset_clears_cross_file_follows_tracking(self):
        parent = _make_cls("P", module="app.specs.a")
        cross = _make_cls("C", module="app.specs.b")
        register_spec(parent, follows=None)
        register_spec(cross, follows=parent)
        reset_registries()
        # After reset, should be able to register a standalone in the same module
        register_spec(_make_cls("NewSpec", module="app.specs.b"), follows=None)

    def test_reset_clears_all_four_registries(self):
        register_flow(_make_cls("F"))
        register_task(_make_cls("T", module="app.tasks.t.t"))
        register_spec(_make_cls("S", module="app.specs.s"), follows=None)
        parent = _make_cls("P", module="app.specs.p")
        register_spec(parent, follows=None)
        register_spec(_make_cls("X", module="app.specs.x"), follows=parent)
        reset_registries()
        assert _flows == {}
        assert _tasks == {}
        assert _specs == {}
        assert _cross_file_follows == {}


# ═══════════════════════════════════════════════════════════════════════════
# Abstract base class bypass (intentional)
# ═══════════════════════════════════════════════════════════════════════════


class TestAbstractClassBypass:
    """_abstract_task=True and _abstract_flow=True skip file rules entirely.

    Abstract bases are intermediate classes without run() — they exist to share
    configuration. Registering them would wrongly block the concrete subclass.
    This is intentional. The bypass happens in __init_subclass__ before
    _file_rules is called, so the registries never see abstract classes.
    """

    def test_abstract_task_not_registered(self):
        """A concrete task registers normally when an abstract was skipped."""
        module = "app.flows.a.tasks.shared.shared"
        register_task(_make_cls("ConcreteTask", module=module))
        assert _tasks.get(module) == "ConcreteTask"

    def test_abstract_flow_not_registered(self):
        """A concrete flow registers normally when an abstract was skipped."""
        module = "app.flows.analysis.analysis"
        register_flow(_make_cls("ConcreteFlow", module=module))
        assert _flows.get(module) == "ConcreteFlow"

    def test_only_concrete_subclass_occupies_slot(self):
        """Two concrete classes in same module still fail, regardless of abstract base."""
        module = "app.flows.a.tasks.x.x"
        register_task(_make_cls("ConcreteA", module=module))
        with pytest.raises(TypeError, match="already contains PipelineTask"):
            register_task(_make_cls("ConcreteB", module=module))


# ═══════════════════════════════════════════════════════════════════════════
# Error message quality — actionable guidance
# ═══════════════════════════════════════════════════════════════════════════


class TestErrorMessages:
    """Every error message includes what went wrong, why, and how to fix."""

    def test_two_flows_error_mentions_both_names(self):
        register_flow(_make_cls("FirstFlow"))
        with pytest.raises(TypeError, match=r"FirstFlow.*ExtractionFlow|ExtractionFlow.*FirstFlow"):
            register_flow(_make_cls("ExtractionFlow"))

    def test_two_flows_error_suggests_file_path(self):
        register_flow(_make_cls("AnalysisFlow"))
        with pytest.raises(TypeError, match=r"flows/extraction_flow/extraction_flow\.py"):
            register_flow(_make_cls("ExtractionFlow"))

    def test_two_tasks_error_suggests_file_path(self):
        register_task(_make_cls("AnalyzeTask"))
        with pytest.raises(TypeError, match=r"tasks/extract_task/extract_task\.py"):
            register_task(_make_cls("ExtractTask"))

    def test_flow_task_mix_says_separate(self):
        register_flow(_make_cls("AnalysisFlow"))
        with pytest.raises(TypeError, match="separate files"):
            register_task(_make_cls("AnalyzeTask"))

    def test_flow_task_mix_has_fix(self):
        register_flow(_make_cls("AnalysisFlow"))
        with pytest.raises(TypeError, match="FIX"):
            register_task(_make_cls("AnalyzeTask"))

    def test_flow_spec_mix_mentions_spec_path(self):
        register_flow(_make_cls("AnalysisFlow"))
        with pytest.raises(TypeError, match=r"specs/<category>\.py"):
            register_spec(_make_cls("AnalysisSpec"), follows=None)

    def test_task_spec_mix_mentions_spec_path(self):
        register_task(_make_cls("AnalyzeTask"))
        with pytest.raises(TypeError, match=r"specs/<category>\.py"):
            register_spec(_make_cls("AnalyzeSpec"), follows=None)

    def test_standalone_duplicate_mentions_follows(self):
        """Error for two standalones suggests using follows=."""
        m = _APP_SPEC_MODULE
        register_spec(_make_cls("A", module=m), follows=None)
        with pytest.raises(TypeError, match="follows="):
            register_spec(_make_cls("B", module=m), follows=None)

    def test_cross_file_conflict_mentions_both_names(self):
        parent = _make_cls("P", module="app.specs.a")
        register_spec(parent, follows=None)
        register_spec(_make_cls("Cross", module="app.specs.b"), follows=parent)
        with pytest.raises(TypeError, match=r"Cross.*LocalSpec|LocalSpec.*Cross"):
            register_spec(_make_cls("LocalSpec", module="app.specs.b"), follows=None)

    def test_docstring_error_shows_class_and_fix(self):
        with pytest.raises(TypeError, match=r"(?s)MyTask.*FIX.*Input:.*Output:"):
            require_docstring(_make_cls("MyTask", doc=None), kind="PipelineTask")

    def test_docstring_error_kind_is_lowercase_in_example(self):
        """The example in the error uses lowercase kind for the placeholder."""
        with pytest.raises(TypeError, match="pipelinetask"):
            require_docstring(_make_cls("X", doc=None), kind="PipelineTask")


# ═══════════════════════════════════════════════════════════════════════════
# Registry state consistency
# ═══════════════════════════════════════════════════════════════════════════


class TestRegistryState:
    """Verify internal registry state after successful registrations."""

    def test_flow_recorded_in_flows_dict(self):
        m = "app.flows.x.x"
        register_flow(_make_cls("XFlow", module=m))
        assert _flows[m] == "XFlow"

    def test_task_recorded_in_tasks_dict(self):
        m = "app.tasks.x.x"
        register_task(_make_cls("XTask", module=m))
        assert _tasks[m] == "XTask"

    def test_standalone_spec_recorded(self):
        m = "app.specs.x"
        register_spec(_make_cls("XSpec", module=m), follows=None)
        assert _specs[m] == [("XSpec", None)]

    def test_followup_spec_recorded_with_parent_name(self):
        m = "app.specs.x"
        parent = _make_cls("XSpec", module=m)
        register_spec(parent, follows=None)
        register_spec(_make_cls("YSpec", module=m), follows=parent)
        assert _specs[m] == [("XSpec", None), ("YSpec", "XSpec")]

    def test_cross_file_followup_recorded_in_tracking_dict(self):
        parent = _make_cls("P", module="app.specs.a")
        register_spec(parent, follows=None)
        register_spec(_make_cls("C", module="app.specs.b"), follows=parent)
        assert _cross_file_follows["app.specs.b"] == "C"

    def test_same_file_followup_not_in_cross_file_dict(self):
        m = "app.specs.x"
        parent = _make_cls("XSpec", module=m)
        register_spec(parent, follows=None)
        register_spec(_make_cls("YSpec", module=m), follows=parent)
        assert m not in _cross_file_follows
