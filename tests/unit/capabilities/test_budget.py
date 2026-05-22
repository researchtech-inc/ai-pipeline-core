"""Unit tests for CapabilityCostBudget mid-run enforcement."""

import pytest

from tests.capabilities._budget import CapabilityCostBudget


def test_add_accumulates_within_limit() -> None:
    budget = CapabilityCostBudget(limit_usd=1.0)
    budget.add(0.30)
    budget.add(0.40)
    assert budget.total_usd == pytest.approx(0.70)


def test_add_aborts_immediately_when_exceeded() -> None:
    budget = CapabilityCostBudget(limit_usd=1.0)
    budget.add(0.60)
    with pytest.raises(RuntimeError) as exc_info:
        budget.add(0.50)
    message = str(exc_info.value)
    assert "$1.10" in message
    assert "$1.00" in message
    assert "--cost-limit" in message
    assert budget.total_usd == pytest.approx(1.10)


def test_add_does_not_continue_silently_after_first_overrun() -> None:
    """Every subsequent ``add`` past the limit must also raise; no silent accumulation."""
    budget = CapabilityCostBudget(limit_usd=0.10)
    with pytest.raises(RuntimeError):
        budget.add(0.50)
    with pytest.raises(RuntimeError):
        budget.add(0.05)
    assert budget.total_usd == pytest.approx(0.55)


def test_add_tolerates_missing_or_negative_cost() -> None:
    budget = CapabilityCostBudget(limit_usd=1.0)
    budget.add(None)
    budget.add(-0.10)
    assert budget.total_usd == 0.0


def test_add_at_exact_limit_does_not_abort() -> None:
    """Strict ``>`` semantics: hitting the limit exactly is allowed."""
    budget = CapabilityCostBudget(limit_usd=1.0)
    budget.add(1.0)
    assert budget.total_usd == pytest.approx(1.0)
