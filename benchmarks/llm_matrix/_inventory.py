"""Parse the production LiteLLM inventory for benchmark parametrization."""

import hashlib
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ._models import BENCHMARK_MODEL_SET, BenchmarkDeployment, BenchmarkInventory

PRODUCTION_LITELLM_CONFIG = Path(".tmp/production-server-v2/services/litellm/litellm.yaml")


def load_litellm_inventory(path: Path = PRODUCTION_LITELLM_CONFIG) -> BenchmarkInventory:
    """Load benchmark deployment inventory from a LiteLLM YAML file."""
    config_path = path.resolve()
    payload = config_path.read_bytes()
    yaml = YAML(typ="safe")
    data = yaml.load(payload)
    if not isinstance(data, dict):
        raise ValueError(f"LiteLLM config {config_path} did not parse to a mapping.")
    raw_model_list = data.get("model_list")
    if not isinstance(raw_model_list, list):
        raise ValueError(f"LiteLLM config {config_path} is missing a model_list sequence.")

    deployments: list[BenchmarkDeployment] = []
    for index, item in enumerate(raw_model_list, start=1):
        deployment = _parse_deployment(item, index=index)
        if deployment.model_name in BENCHMARK_MODEL_SET:
            deployments.append(deployment)
    return BenchmarkInventory(
        config_path=str(config_path),
        config_sha256=hashlib.sha256(payload).hexdigest(),
        deployments=tuple(deployments),
    )


def _parse_deployment(item: Any, *, index: int) -> BenchmarkDeployment:
    if not isinstance(item, dict):
        raise ValueError(f"model_list entry {index} must be a mapping.")
    model_name = _required_str(item, "model_name", index=index)
    litellm_params = _mapping(item.get("litellm_params"))
    model_info = _mapping(item.get("model_info"))
    aipl = _mapping(model_info.get("aipl"))
    deployment_id = _required_str(model_info, "id", index=index)
    extra_body = _mapping(litellm_params.get("extra_body"))
    provider = _mapping(extra_body.get("provider"))
    provider_pin = _provider_pin(provider.get("only"))
    order = litellm_params.get("order", 0)
    if not isinstance(order, int):
        order = 0
    supports_json_schema = aipl.get("supports_json_schema", True)
    cost_optimized = aipl.get("cost_optimized", False)
    return BenchmarkDeployment(
        model_name=model_name,
        deployment_id=deployment_id,
        litellm_model=_required_str(litellm_params, "model", index=index),
        order=order,
        provider_pin=provider_pin,
        mode=model_info.get("mode") if isinstance(model_info.get("mode"), str) else None,
        cache_kind=aipl.get("cache_kind") if isinstance(aipl.get("cache_kind"), str) else None,
        supports_json_schema=bool(supports_json_schema),
        cost_optimized=bool(cost_optimized),
        is_wildcard="*" in model_name,
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _required_str(mapping: dict[str, Any], key: str, *, index: int) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"model_list entry {index} is missing required string field {key!r}.")
    return value


def _provider_pin(value: Any) -> str | None:
    if isinstance(value, list):
        pins = [item for item in value if isinstance(item, str)]
        return ",".join(pins) if pins else None
    if isinstance(value, str):
        return value
    return None
