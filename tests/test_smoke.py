"""Smoke tests verifying showcase examples import without errors and have valid flow chain structure."""

import subprocess
import sys


def test_showcase_imports() -> None:
    """examples/showcase.py imports without errors."""
    import examples.showcase as m

    assert hasattr(m, "ShowcasePipeline")


def test_showcase_database_imports() -> None:
    """examples/showcase_database.py imports without errors."""
    import examples.showcase_database as m

    assert hasattr(m, "DatabaseShowcasePipeline")


def test_showcase_replay_imports() -> None:
    """examples/showcase_replay.py imports without errors."""
    import examples.showcase_replay as m

    assert hasattr(m, "ReplayUppercaseTask")


def test_showcase_remote_deployment_imports() -> None:
    """examples/showcase_remote_deployment.py imports without errors."""
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import examples.showcase_remote_deployment as m; assert hasattr(m, 'CompetitiveIntelPipeline')",
        ],
        check=True,
    )


def test_showcase_builds_plan() -> None:
    """ShowcasePipeline.build_plan() returns FlowStep-wrapped PipelineFlow instances."""
    from examples.showcase import ShowcasePipeline
    from ai_pipeline_core.pipeline import PipelineFlow, FlowOptions

    deployment = ShowcasePipeline()
    plan = deployment.build_plan(FlowOptions())
    flows = [step.flow for step in plan.steps]
    assert len(flows) >= 1
    assert all(isinstance(f, PipelineFlow) for f in flows)


def test_database_showcase_builds_plan() -> None:
    """DatabaseShowcasePipeline.build_plan() returns FlowStep-wrapped PipelineFlow instances."""
    from examples.showcase_database import DatabaseShowcasePipeline
    from ai_pipeline_core.pipeline import PipelineFlow, FlowOptions

    deployment = DatabaseShowcasePipeline()
    plan = deployment.build_plan(FlowOptions())
    flows = [step.flow for step in plan.steps]
    assert len(flows) >= 1
    assert all(isinstance(f, PipelineFlow) for f in flows)
