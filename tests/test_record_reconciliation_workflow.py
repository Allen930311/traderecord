from fnmatch import fnmatchcase
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflows" / "record-reconciliation.yaml"
STATUS = ROOT / "STATUS.md"


def _load():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _load_runtime_writes():
    text = STATUS.read_text(encoding="utf-8")
    block = text.split("```yaml", 1)[1].split("```", 1)[0]
    return yaml.safe_load(block)["runtime_writes"]


def _workflow_step(step_id):
    return next(step for step in _load()["workflow"] if step["id"] == step_id)


def _persistence_policy():
    return _load()["persistence_policy"]


def test_record_reconciliation_is_workflow_over_existing_system():
    data = _load()
    assert data["workflow_id"] == "record_reconciliation"
    assert data["authority"] == "trading-record"
    assert data["architecture"]["kind"] == "workflow_over_existing_system"
    assert data["architecture"]["new_state_model"] is False
    assert data["architecture"]["lifecycle_authority"].endswith("LifecycleStore")
    assert data["architecture"]["capture_facade"] == "trading_os.unified_capture"


def test_record_reconciliation_uses_existing_capture_surfaces():
    data = _load()
    modes = data["mode_resolution"]
    assert modes["live_contemporaneous"]["capture"] == "python -m trading_os capture live"
    assert "capture retrospective-live --registration-packet-file" in modes["live_retrospective"]["capture"]
    assert "Do not invoke resolve_live_strategy" in modes["live_retrospective"]["strategy_contract"]
    assert modes["backtest"]["capture"] == "python -m trading_os capture backtest"


def test_record_reconciliation_is_not_owner_routable_and_has_fail_closed_contract():
    data = _load()
    # Owner natural-language routes were removed: this workflow is internal and only reachable
    # through the write-side registration_execution workflow.
    assert "natural_language_intents" not in data
    assert data["routing_contract"]["owner_routable"] is False
    assert data["routing_contract"]["only_invoked_by"] == "workflows/registration-execution.yaml"
    assert data["architecture"]["invoked_by"] == "workflows/registration-execution.yaml"

    invariants = "\n".join(data["invariants"])
    assert "must not mirror this workflow" in invariants
    assert "Do not invent" in invariants
    assert "Read back canonical state" in invariants

    assert set(data["completion"]) == {
        "CAPTURED",
        "LIFECYCLE_RECONCILED",
        "REVIEWED",
        "COMPLETE",
        "INCOMPLETE",
        "NEEDS_OWNER_FACTS",
        "BLOCKED",
    }
    assert data["agent_execution"]["same_turn_continue"] is True
    assert data["completion_gate"]["stale_status"] == "PROJECTION_STALE"
    assert data["completion_gate"]["required"]["persistence"]["remote_main_contains_completed_state"] is True
    assert data["completion_gate"]["required"]["persistence"]["fresh_main_read_back"] is True


def test_routine_record_uses_direct_main_delivery():
    policy = _persistence_policy()
    routine = policy["lanes"]["routine_record"]
    assert policy["default_effect_class"] == "PROJECT_RUNTIME_WRITE"
    assert routine["effect_class"] == "PROJECT_RUNTIME_WRITE"
    assert routine["delivery"] == "direct_main"
    assert routine["pr_required"] is False
    assert routine["persistence_path"] == [
        "fresh origin/main",
        "record reconciliation",
        "projection refresh (returns embedded verify result)",
        "required test lane selected by registration-execution.yaml#test_policy",
        "commit",
        "pre-push fetch/check origin/main",
        "push origin/main",
        "fresh-main read-back from exact origin/main objects (no second checkout required)",
        "clean worktree",
    ]


def test_system_changes_use_pull_request_delivery():
    system = _persistence_policy()["lanes"]["system_change"]
    assert system["effect_class"] == "REPO_CHANGE"
    assert system["delivery"] == "pull_request"
    assert system["pr_required"] is True
    assert {
        "trading_os/**",
        "registry/**",
        "workflows/**",
        "tests/**",
        "schema_or_vocabulary_change",
        "lifecycle_or_state_machine_change",
        "missing_system_capability",
    } <= set(system["triggers"])


def test_unknown_effect_fails_closed_to_pull_request():
    unknown = _persistence_policy()["unknown_effect"]
    assert unknown == {
        "effect_class": "REPO_CHANGE",
        "delivery": "pull_request",
        "fail_closed": True,
    }


def test_routine_direct_main_requires_all_runtime_closure_gates():
    gates = set(_persistence_policy()["lanes"]["routine_record"]["direct_main_gates"])
    assert {
        "fresh_origin_main",
        "clean_managed_worktree_before_mutation",
        "dedupe_and_lifecycle_identity_checks_pass",
        "governed_sequence_allocator_passes",
        "lifecycle_state_valid",
        "required_evidence_linkage_valid",
        "projection_refresh_succeeds",
        "projection_verification_current",
        "target_visible_in_required_read_models",
        "required_tests_pass",
        "pre_push_fetch_origin_main",
        "pre_push_origin_main_matches_mutation_base",
        "no_system_change_surface_changed",
        "no_unresolved_merge_conflict_or_concurrency_condition",
    } <= gates


def test_routine_direct_main_rechecks_origin_before_push_and_fails_closed_on_advance():
    step = _workflow_step("persistence_and_fresh_main_read_back")
    action = step["action"]
    fail_closed = set(_load()["completion_gate"]["fail_closed"])
    assert "Immediately before push, fetch origin/main again" in action
    assert "verify it still equals the mutation base" in action
    assert "refresh (which embeds verify) and the deterministically selected required tests" in action
    assert "origin/main advanced during routine run without replay/revalidation" in fail_closed


def test_completion_delivery_preserves_remote_and_fresh_main_requirements():
    persistence = _load()["completion_gate"]["required"]["persistence"]
    assert persistence["delivery_lanes"]["routine_record"] == {
        "delivery": "direct_main",
        "remote_persistence": "push_origin_main",
        "fresh_main_read_back": True,
        "worktree_clean": True,
    }
    assert persistence["delivery_lanes"]["system_change"] == {
        "delivery": "pull_request_then_merge",
        "remote_persistence": "merge_into_origin_main",
        "fresh_main_read_back": True,
        "worktree_clean": True,
    }
    assert persistence["remote_main_contains_completed_state"] is True
    assert persistence["fresh_main_read_back"] is True


def test_routine_artifact_allowlist_is_narrow_and_excludes_system_surfaces():
    policy = _persistence_policy()
    allowlist = set(policy["routine_artifact_allowlist"])
    candidate_path = "Research/backtest-candidates/**"
    assert candidate_path in set(_load_runtime_writes())
    assert {
        "canonical/drafts/**",
        "canonical/records/trades/**",
        "canonical/sequences/numbers.json",
        "canonical/sequences/finalizations.json",
        "canonical/lifecycle/events.jsonl",
        "canonical/lifecycle/identity-links.jsonl",
        "canonical/lifecycle/current.json",
        "canonical/evidence/artifacts/**",
        "exports/operational/**",
        "PERFORMANCE.md",
        candidate_path,
    } <= allowlist
    forbidden = set(policy["forbidden_routine_surfaces"])
    assert candidate_path not in forbidden
    system_change = set(policy["lanes"]["system_change"]["triggers"])
    assert candidate_path not in system_change
    candidate_file = candidate_path.removesuffix("**") + "candidate.json"
    protected_surfaces = forbidden | system_change
    assert not any(fnmatchcase(candidate_file, surface) for surface in protected_surfaces)
    assert {
        "trading_os/**",
        "registry/**",
        "workflows/**",
        "tests/**",
        "SKILL.md",
        "STATUS.md",
        "PROJECT_CONTEXT.md",
    } <= forbidden
    assert allowlist.isdisjoint(forbidden)


def test_canonical_mutation_requires_projection_closure():
    step = _workflow_step("refresh_projections")
    assert step["command"] == "python -m trading_os projections refresh --target-trade <trade_id>"
    assert step["sequence"] == ["analytics", "health", "trade_view", "handoff", "verify"]
    assert step["owner_confirmation_required"] is False
    assert step["failure"] == "PROJECTION_STALE"


def test_projection_product_visible_acceptance_is_separate_from_canonical_acceptance():
    acceptance = _load()["acceptance"]
    assert acceptance["canonical_lifecycle_e2e"]["status"] == "passed"
    assert acceptance["projection_product_visible_closure"]["status"] == "passed"
    assert acceptance["projection_product_visible_closure"]["target_trade"] == "T000006"
    assert "does not claim projection closure" in acceptance["canonical_lifecycle_e2e"]["meaning"]


def test_runtime_write_contract_covers_unified_capture_durable_surfaces_only():
    declarations = set(_load_runtime_writes())
    assert {
        "canonical/drafts/*.json",
        "canonical/records/trades/*.json",
        "canonical/sequences/numbers.json",
        "canonical/sequences/finalizations.json",
    } <= declarations
    assert "canonical/**" not in declarations
    assert "trading_os/foo.py" not in declarations


def test_exit_reconciliation_separates_partial_and_final_close_branches():
    branches = _workflow_step("reconcile_exit")["branches"]
    partial = branches["partial_close"]
    final = branches["final_close"]

    assert partial["resulting_state"] == "PARTIALLY_CLOSED"
    assert partial["verify_exit"] == "forbidden"
    assert "verify-exit" not in partial["steps"]
    assert final["resulting_state"] == "CLOSED"
    assert "verify-exit" in final["steps"]
    assert {item["canonical_state"] for item in final["requires"] if "canonical_state" in item} == {"CLOSED"}
    assert {item["exit_verification_state"] for item in final["requires"] if "exit_verification_state" in item} == {"confirmed"}


def test_review_requires_closed_state_verified_exit_and_sufficient_facts():
    review = _workflow_step("review")
    gate = review["gate"]
    assert set(gate["required_canonical_states"]) == {"CLOSED", "REVIEW_PENDING", "REVIEWED"}
    assert gate["required_exit_verification_state"] == "confirmed"
    assert gate["required_review_facts"] == "sufficient"
    assert gate["if_not_closed"] == "do_not_review"
    assert gate["if_exit_verification_not_confirmed"] == "reconcile_exit_first"


def test_lifecycle_reconciled_requires_confirmed_final_exit_and_acceptance_is_precise():
    data = _load()
    completion = data["completion"]["LIFECYCLE_RECONCILED"]["means"]
    assert "canonical_state=CLOSED" in completion
    assert "exit_verification_state=confirmed" in completion
    assert data["acceptance"]["canonical_lifecycle_e2e"]["status"] == "passed"
    assert "canonical capture" in data["acceptance"]["canonical_lifecycle_e2e"]["meaning"]


def test_agent_entry_documents_route_registration_through_execution_workflow():
    agent_context = (ROOT / "AGENT_CONTEXT.md").read_text(encoding="utf-8")
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    status = (ROOT / "STATUS.md").read_text(encoding="utf-8")
    assert "workflows/trade-registration.yaml" in agent_context
    assert "workflows/registration-execution.yaml" in agent_context
    assert "workflows/registration-execution.yaml" in skill
    assert "internal, non-owner-routable" in status
    assert "must never route here directly" in status
