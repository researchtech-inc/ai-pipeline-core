"""Tests for build_auto_run_id determinism, sanitization, and validation."""

# pyright: reportPrivateUsage=false

from ai_pipeline_core import Document, FlowOptions
from ai_pipeline_core.deployment._helpers import build_auto_run_id, validate_run_id


class _AutoIdDoc(Document):
    """Input document for auto run_id tests."""


def _make_doc(content: str = "test") -> _AutoIdDoc:
    return _AutoIdDoc.create_root(name="in.txt", content=content, reason="auto-id-test")


class TestBuildAutoRunId:
    def test_deterministic_same_inputs(self) -> None:
        doc = _make_doc()
        opts = FlowOptions()
        id1 = build_auto_run_id(output_dir_name="mydir", documents=[doc], options=opts)
        id2 = build_auto_run_id(output_dir_name="mydir", documents=[doc], options=opts)
        assert id1 == id2

    def test_different_dir_different_id(self) -> None:
        doc = _make_doc()
        opts = FlowOptions()
        id1 = build_auto_run_id(output_dir_name="dir-a", documents=[doc], options=opts)
        id2 = build_auto_run_id(output_dir_name="dir-b", documents=[doc], options=opts)
        assert id1 != id2

    def test_different_content_different_id(self) -> None:
        opts = FlowOptions()
        id1 = build_auto_run_id(output_dir_name="d", documents=[_make_doc("aaa")], options=opts)
        id2 = build_auto_run_id(output_dir_name="d", documents=[_make_doc("bbb")], options=opts)
        assert id1 != id2

    def test_passes_validate_run_id(self) -> None:
        doc = _make_doc()
        run_id = build_auto_run_id(output_dir_name="my-project", documents=[doc], options=FlowOptions())
        validate_run_id(run_id)

    def test_sanitizes_special_characters(self) -> None:
        doc = _make_doc()
        run_id = build_auto_run_id(output_dir_name="my dir/with:special@chars!", documents=[doc], options=FlowOptions())
        validate_run_id(run_id)
        assert "/" not in run_id
        assert " " not in run_id
        assert ":" not in run_id

    def test_empty_dir_name_uses_fallback(self) -> None:
        doc = _make_doc()
        run_id = build_auto_run_id(output_dir_name="", documents=[doc], options=FlowOptions())
        assert run_id.startswith("run-")
        validate_run_id(run_id)

    def test_all_special_chars_dir_uses_fallback(self) -> None:
        doc = _make_doc()
        run_id = build_auto_run_id(output_dir_name="///", documents=[doc], options=FlowOptions())
        assert run_id.startswith("run-")

    def test_long_dir_name_truncated(self) -> None:
        doc = _make_doc()
        long_name = "a" * 200
        run_id = build_auto_run_id(output_dir_name=long_name, documents=[doc], options=FlowOptions())
        validate_run_id(run_id)
        # dir part is max 40 + 1 dash + 6 date + 1 dash + 8 hash = 56 max
        assert len(run_id) <= 60

    def test_empty_documents(self) -> None:
        run_id = build_auto_run_id(output_dir_name="d", documents=[], options=FlowOptions())
        validate_run_id(run_id)

    def test_uses_utc_not_local(self) -> None:
        """Verify the function uses UTC by inspecting the source code for datetime.now(UTC)."""
        import inspect

        source = inspect.getsource(build_auto_run_id)
        assert "UTC" in source, "build_auto_run_id must use UTC for deterministic cross-timezone run IDs"

    def test_format_pattern(self) -> None:
        doc = _make_doc()
        run_id = build_auto_run_id(output_dir_name="project", documents=[doc], options=FlowOptions())
        parts = run_id.split("-")
        # project-DDMMYY-hash8
        assert parts[0] == "project"
        assert len(parts[-1]) == 8  # hash part
