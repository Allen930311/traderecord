from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflows" / "strategy-change.yaml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_strategy_change_is_internal_explicit_and_goes_through_a_pull_request():
    workflow = _workflow()
    assert workflow["owner_facing"] is False
    assert workflow["effect"] == "REPO_CHANGE"
    entry = workflow["entry"]
    assert "owner_explicitly" in entry["starts_only_when"]
    assert {"strategy_health_check", "registration", "review"} <= set(entry["never_started_by"])
    step_ids = [step["id"] for step in workflow["steps"]]
    assert step_ids.index("pull_request") < step_ids.index("after_merge")
    assert "never merges without the Owner" in " ".join(
        step["action"].replace("\n", " ") for step in workflow["steps"] if step["id"] == "pull_request"
    )


def test_owner_decisions_are_recorded_exactly():
    decisions = _workflow()["owner_decisions"]
    assert decisions["a_published_rule_version_is_never_edited"] is True
    assert decisions["frozen_b1_b3_strategies_may_get_new_versions"] is True
    assert decisions["a_new_version_does_not_retire_the_previous_one"] is True
    assert decisions["only_the_owner_stops_a_version"] is True


def test_the_change_types_never_edit_a_version_file():
    change_types = _workflow()["change_types"]
    assert set(change_types) == {"new_strategy", "new_version", "stop_a_version", "retire_a_strategy"}
    assert "editing any existing version file" in change_types["new_version"]["never"]
    assert "retired_rule_versions" in change_types["stop_a_version"]["agent_writes"]


def test_the_health_check_names_this_workflow_and_compares_versions_separately():
    health = yaml.safe_load((ROOT / "workflows" / "strategy-health-check.yaml").read_text(encoding="utf-8"))
    assert health["research_hypothesis_contract"]["adoption_path_workflow"] == "workflows/strategy-change.yaml"
    assert health["research_hypothesis_contract"]["registry_mutation"] == "forbidden"
    comparison = health["version_comparison"]
    assert comparison["when_a_strategy_has_more_than_one_rule_version"]["never_pool_versions_into_one_win_rate"] is True
    assert "rule_version" in health["analysis_dimensions"]


def test_the_entry_documents_point_at_the_workflow():
    assert "workflows/strategy-change.yaml" in (ROOT / "AGENT_CONTEXT.md").read_text(encoding="utf-8")
    assert "workflows/strategy-change.yaml" in (ROOT / "registry" / "README.md").read_text(encoding="utf-8")
