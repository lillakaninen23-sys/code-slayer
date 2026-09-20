"""WorkerRuntimeConfig role/execution field validation (H.4.1).

Bounds for output_token_budget/tool_choice_enforcement/
planner_policy_version/planner_timeout_seconds were previously enforced
only inside WorkerRuntimeConfig.from_mapping() (the TOML-parsing path) --
a direct WorkerRuntimeConfig(...) construction (e.g. from
api.admin.AdminFacade.register_worker()) bypassed all of it. These tests
prove __post_init__ itself is now the single, authoritative gate,
regardless of construction path.
"""

from __future__ import annotations

import pytest

from code_slayer.config.schema import ConfigError, WorkerRuntimeConfig


def _worker(**overrides) -> WorkerRuntimeConfig:
    kwargs = dict(
        worker_id="w1",
        kind="openai_compatible",
        network_class="local",
        ollama_server_id="local",
        model_tag="demo:1",
        approved_model_digest=None,
        approved_runtime_version=None,
        effective_context_tokens=4096,
        temperature=0.0,
        normalizer_id=None,
        normalizer_version=None,
    )
    kwargs.update(overrides)
    return WorkerRuntimeConfig(**kwargs)


def test_planner_timeout_seconds_default_is_30():
    assert _worker().planner_timeout_seconds == 30.0


def test_planner_timeout_seconds_explicit_accepted():
    assert _worker(planner_timeout_seconds=60.0).planner_timeout_seconds == 60.0


def test_planner_timeout_seconds_accepts_int():
    assert _worker(planner_timeout_seconds=60).planner_timeout_seconds == 60


def test_planner_timeout_seconds_boundaries_accepted():
    assert _worker(planner_timeout_seconds=1.0).planner_timeout_seconds == 1.0
    assert _worker(planner_timeout_seconds=1800.0).planner_timeout_seconds == 1800.0


@pytest.mark.parametrize("bad", [0.0, -1.0, 0.999, 1800.001, 5000.0])
def test_planner_timeout_seconds_out_of_range_rejected(bad):
    with pytest.raises(ConfigError, match="planner_timeout_seconds"):
        _worker(planner_timeout_seconds=bad)


def test_planner_timeout_seconds_bool_rejected():
    with pytest.raises(ConfigError, match="planner_timeout_seconds"):
        _worker(planner_timeout_seconds=True)


def test_planner_timeout_seconds_string_rejected():
    with pytest.raises(ConfigError, match="planner_timeout_seconds"):
        _worker(planner_timeout_seconds="60.0")


def test_output_token_budget_out_of_range_rejected():
    with pytest.raises(ConfigError, match="output_token_budget"):
        _worker(output_token_budget=0)
    with pytest.raises(ConfigError, match="output_token_budget"):
        _worker(output_token_budget=2_000_000)


def test_output_token_budget_bool_rejected():
    with pytest.raises(ConfigError, match="output_token_budget"):
        _worker(output_token_budget=True)


def test_output_token_budget_boundaries_accepted():
    assert _worker(output_token_budget=1).output_token_budget == 1
    assert _worker(output_token_budget=1_000_000).output_token_budget == 1_000_000


def test_tool_choice_enforcement_empty_rejected():
    with pytest.raises(ConfigError, match="tool_choice_enforcement"):
        _worker(tool_choice_enforcement="")


def test_tool_choice_enforcement_whitespace_only_rejected():
    with pytest.raises(ConfigError, match="tool_choice_enforcement"):
        _worker(tool_choice_enforcement="   ")


def test_planner_policy_version_empty_rejected():
    with pytest.raises(ConfigError, match="planner_policy_version"):
        _worker(planner_policy_version="")
