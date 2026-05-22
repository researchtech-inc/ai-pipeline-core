"""Tests for benchmark inventory parsing."""

from ._inventory import PRODUCTION_LITELLM_CONFIG, load_litellm_inventory
from ._models import BENCHMARK_MODEL_SET


def test_inventory_finds_expected_deployment_counts() -> None:
    """The benchmark inventory must reflect the production proxy YAML."""
    inventory = load_litellm_inventory(PRODUCTION_LITELLM_CONFIG)

    assert inventory.wildcard_deployments == ()
    assert inventory.model_names == BENCHMARK_MODEL_SET
    assert all(deployment.model_name in BENCHMARK_MODEL_SET for deployment in inventory.concrete_deployments)
    assert all(deployment.deployment_id for deployment in inventory.concrete_deployments)
