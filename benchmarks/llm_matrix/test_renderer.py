"""Tests for benchmark renderer output using synthetic data."""

from datetime import UTC, datetime

from ai_pipeline_core.llm import AIModel
from tools.benchmark.render_llm_matrix import render_files

from ._models import (
    BenchmarkCase,
    BenchmarkDeployment,
    BenchmarkInventory,
    BenchmarkRun,
    BenchmarkRunManifest,
    ProbeResult,
    RENDERER_TEST_MODEL,
)


def test_renderer_writes_matrix_investigation_and_summary(tmp_path) -> None:
    """Renderer writes all expected artifacts from a synthetic benchmark run."""
    model_name = RENDERER_TEST_MODEL
    deployment_id = f"{model_name}--unit"
    deployment = BenchmarkDeployment(
        model_name=model_name,
        deployment_id=deployment_id,
        litellm_model="unit/render-model",
        order=1,
    )
    inventory = BenchmarkInventory(
        config_path="litellm.yaml",
        config_sha256="abc123",
        deployments=(deployment,),
    )
    case = BenchmarkCase(
        case_id=f"deployment:{deployment_id}:basic_generation",
        probe_key="basic_generation",
        scope="deployment",
        model=AIModel(name=model_name),
        deployment=deployment,
        expected="pass",
        nonce="nonce",
    )
    run = BenchmarkRun(
        manifest=BenchmarkRunManifest(
            run_id="synthetic",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            proxy_base_url_hash="hash",
            proxy_config_path="litellm.yaml",
            proxy_config_sha256="abc123",
            total_cost_usd=0.01,
            total_calls=1,
            git_sha="sha",
            framework_version="0.0",
        ),
        inventory=inventory,
        cases=(case,),
        results=(
            ProbeResult(
                case_id=case.case_id,
                probe_key="basic_generation",
                scope="deployment",
                model_name=model_name,
                deployment_id=deployment_id,
                expected="pass",
                actual="fail",
                elapsed_ms=10,
                error_category="llm_error",
                error_message="synthetic failure",
            ),
        ),
    )
    input_path = tmp_path / "latest.json"
    input_path.write_text(run.model_dump_json(), encoding="utf-8")

    matrix_path, investigation_path, summary_path = render_files(input_path)

    assert deployment_id in matrix_path.read_text(encoding="utf-8")
    assert "synthetic failure" in investigation_path.read_text(encoding="utf-8")
    assert '"needs_investigation"' in summary_path.read_text(encoding="utf-8")
