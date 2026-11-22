from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _load_yaml(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_trade_registration_requires_owner_confirmation_before_execution() -> None:
    workflow = _load_yaml("workflows/trade-registration.yaml")
    steps = [step["id"] for step in workflow["workflow"]]

    assert steps.index("resolve_strategy") < steps.index("present_for_owner_confirmation")
    assert steps.index("resolve_strategy") < steps.index("prepare_confirmation_preview")
    assert steps.index("ask_owner_for_missing_original_sl") < steps.index("prepare_confirmation_preview")
    assert steps.index("prepare_confirmation_preview") < steps.index("present_for_owner_confirmation")
    assert steps.index("present_for_owner_confirmation") < steps.index("confirm_registration_packet")
    assert steps.index("confirm_registration_packet") < steps.index("build_registration_packet")
    assert steps.index("build_registration_packet") < steps.index("route_execution")
    assert steps.index("route_execution") < steps.index("delegate_execution")

    gate = workflow["owner_confirmation_gate"]
    assert gate["required_before_canonical_trade_mutation"] is True
    assert gate["evidence_ingest_before_confirmation_allowed"] is False
    assert gate["confirmed_packet_semantics"] == "frozen"
    preview = workflow["registration_packet"]["confirmation_preview"]
    assert preview["mode"] == "one_read_only_call_per_unchanged_draft"
    assert preview["compact_intake"]["schema"] == "registration-intake.v1"
    assert any("create_a_second_writer_or_resolver" in item for item in preview["does_not"])
    assert "run_the_existing_strategy_resolver_once" in preview["performs"]
    assert "freeze_or_persist_a_registration_packet" in preview["does_not"]
    assert "create_a_trade_draft_identity_link_or_T_ID" in preview["does_not"]
    assert "prepare-confirmation" in preview["command"]
    measurement = workflow["preconfirmation_benchmark"]
    assert measurement["schema"] == "registration-preconfirmation-benchmark.v2"
    assert "target_seconds" not in measurement
    assert "actual_owner_response_wait" in measurement["excludes"]
    assert "not a measurement of LLM image reasoning" in measurement["comparison_rule"]

    owner_rule = workflow["authority_model"]["owner"]["rule"]
    assert "NO_MATCH" in owner_rule
    assert "may not replace the Owner's intent" in owner_rule
    owner_intent = workflow["authority_model"]["owner"]["resolver_input_for_historical_intent"]
    assert owner_intent["field"] == "owner_attestation"
    assert owner_intent["registry_mapping_required"] is True
    assert owner_intent["chart_provenance_relabeling"] == "forbidden"


def test_registration_execution_routing_is_capability_based_not_agent_named() -> None:
    workflow = _load_yaml("workflows/trade-registration.yaml")
    routing = workflow["execution_routing"]

    assert routing["mode"] == "capability_based"
    assert routing["named_agent_dependency"] == "forbidden"
    assert routing["branches"]["write_capable"]["handoff_required"] is False
    assert routing["branches"]["write_capable"]["execution_workflow"] == "workflows/registration-execution.yaml"
    assert routing["branches"]["not_write_capable"]["handoff_required"] is True
    assert routing["branches"]["not_write_capable"]["receiver_requirement"] == "any_authorized_write_capable_agent"
    assert routing["branches"]["not_write_capable"]["receiver_entry_workflow"] == "workflows/registration-execution.yaml"
    assert workflow["evidence_preservation_contract"]["repository_ingest"]["required_before_canonical_trade_mutation"] is True
    assert workflow["evidence_preservation_contract"]["repository_ingest"]["write_capable_agent"] == "after_owner_confirmation_via_registration_executor_only"
    assert workflow["registration_packet"]["handoff_form_when_no_write_capability"] == "frozen_packet_plus_evidence_zip"
    required_sections = workflow["registration_packet"]["required_sections"]
    assert "resolver_input" in required_sections
    assert "authority_pins" in required_sections
    metadata = workflow["registration_packet"]["confirmation"]["required_metadata"]
    assert metadata["owner_confirmation"] == ["status", "confirmed_at", "preview_digest"]
    assert metadata["packet"] == ["packet_version", "semantic_state", "packet_sha256", "transport_mode"]
    assert set(metadata["authority_pins"]) == {
        "provider_lifecycle_key",
        "source_main_sha",
        "strategy_registry_digest",
        "strategy_version_pin",
        "resolver_version",
    }
    assert metadata["conditional_authority_pins"]["owner_attestation_present"] == [
        "retrospective_attribution_registry_digest"
    ]

    assert workflow["authority_invariants"]["evidence_provenance_is_not_evidence_admissibility"] is True
    assert workflow["authority_invariants"]["retrospective_replay_policy"]["contemporaneous_screenshot_required"] is False
    assert workflow["architecture"]["retrospective_attribution_authority"] == "registry/retrospective-attribution.yaml"
    preview_step = next(step for step in workflow["workflow"] if step["id"] == "prepare_confirmation_preview")
    assert "fetches origin/main once" in preview_step["action"]
    assert "does not freeze a packet" in preview_step["action"]

    serialized = yaml.safe_dump(workflow, allow_unicode=True).lower()
    for named_agent in ("codex", "claude", "chatgpt"):
        assert named_agent not in serialized


def test_registration_execution_is_the_only_strict_write_side_entry() -> None:
    execution = _load_yaml("workflows/registration-execution.yaml")

    assert execution["workflow_id"] == "registration_execution"
    assert execution["owner_facing"] is False
    assert execution["entry_modes"]["same_agent"]["external_handoff_required"] is False
    assert execution["entry_modes"]["handoff_receiver"]["external_handoff_required"] is True

    freeze = execution["semantic_freeze_contract"]
    assert freeze["owner_confirmation_state"] == "confirmed"
    assert freeze["packet_semantic_state"] == "frozen"
    assert freeze["executor_role"] == "validate_and_execute"
    for forbidden in (
        "redo_trade_review",
        "reanalyze_images_to_replace_confirmed_meaning",
        "choose_a_different_strategy",
        "construct_new_strategy_observations",
        "replace_confirmed_strategy_resolution",
    ):
        assert forbidden in freeze["executor_must_not"]

    runtime = execution["runtime_surface"]
    assert runtime["module"] == "trading_os.registration_packet"
    assert runtime["packet_schema_version"] == "registration-packet.v2"
    assert "registration-packet freeze" in runtime["commands"]["freeze"]
    assert "registration-packet validate" in runtime["commands"]["validate"]
    assert "registration-packet persist" in runtime["commands"]["persist"]
    assert runtime["frozen_packet_store"] == "canonical/registration/packets/<packet_sha256>.json"
    assert runtime["legacy_pending_reader"] == "trading_os.registration_pending.load_pending"
    assert runtime["legacy_pending_store"] == "canonical/registration/pending-strategy.jsonl"
    assert runtime["unresolved_packet_status"] == "READY_FOR_RECORD_RECONCILIATION"

    execution_sections = execution["required_packet_contract"]["required_sections"]
    assert "resolver_input" in execution_sections
    assert "authority_pins" in execution_sections
    owner_verbatim = execution["required_packet_contract"]["owner_verbatim_contract"]
    assert owner_verbatim["packet_field"] == "owner_recollection.verbatim_text"
    assert owner_verbatim["required_for"] == "every_new_registration"
    assert owner_verbatim["preflight_before_evidence_ingest"] is True
    assert owner_verbatim["persistence"]["writer"] == "trading_os.operational_lifecycle.LifecycleStore"
    assert owner_verbatim["persistence"]["projection_retry"] == "verify_first_then_refresh_derived_only_if_stale"
    assert "remain immutable and readable" in owner_verbatim["legacy_rule"]

    resolved_binding = execution["evidence_execution_contract"]["packet_persistence"]["resolved_binding"]
    assert resolved_binding["draft_provenance_field"] == "registration_packet_sha256"
    assert resolved_binding["lifecycle_projection_field"] == "registration_packet_sha256"
    assert resolved_binding["provider_binding_field"] == "registration_packet_sha256"
    assert resolved_binding["retry_rule"] == "exact_packet_sha_and_canonical_semantics_required"

    image_binding = execution["required_packet_contract"]["image_evidence_binding"]
    assert image_binding["packet_field"] == "image_evidence"
    assert image_binding["identity"] == ["sha256", "original_filename", "location"]
    assert "exactly equal" in image_binding["external_handoff_rule"]
    assert "EvidenceStore" in image_binding["same_agent_rule"]

    external_integrity = execution["required_packet_contract"]["required_handoff_integrity_when_external"]
    assert "packet_sha256" in external_integrity
    assert "evidence_manifest_sha256" in external_integrity
    assert "exact_original_evidence_bytes" in external_integrity
    assert "per_artifact_sha256" in external_integrity

    pins = execution["authority_pin_validation"]
    assert pins["resolver_validation"]["allowed"] == "deterministic_integrity_replay_only"
    assert pins["resolver_validation"]["mismatch"] == "OWNER_RECONFIRMATION_REQUIRED"
    assert "OWNER_RECONFIRMATION_REQUIRED" in pins["strategy_registry_digest"]["rule"]
    assert "OWNER_RECONFIRMATION_REQUIRED" in pins["provider_facts"]["rule"]

    recording = execution["recording"]
    assert recording["delegates_to"] == "workflows/record-reconciliation.yaml"
    assert "--registration-packet-file" in recording["frozen_capture_commands"]["same_agent"]
    assert "--registration-manifest" not in recording["frozen_capture_commands"]["same_agent"]
    assert "--registration-manifest" in recording["frozen_capture_commands"]["external_handoff"]
    assert "--registration-evidence-root" in recording["frozen_capture_commands"]["external_handoff"]
    assert recording["real_historical_mode"] == "LIVE_retrospective_frozen_registration"
    assert recording["historical_closed_trade_required_end_state"] == {
        "canonical_state": "CLOSED",
        "exit_verification_state": "confirmed",
        "review_status": "not_started",
    }

    metadata = execution["required_packet_contract"]["required_confirmation_metadata"]
    assert metadata["owner_confirmation"] == ["status", "confirmed_at", "preview_digest"]
    assert metadata["packet"] == ["packet_version", "semantic_state", "packet_sha256", "transport_mode"]
    assert set(metadata["authority_pins"]) == {
        "provider_lifecycle_key",
        "source_main_sha",
        "strategy_registry_digest",
        "strategy_version_pin",
        "resolver_version",
    }
    assert metadata["conditional_authority_pins"]["owner_attestation_present"] == [
        "retrospective_attribution_registry_digest"
    ]

    compact_input = execution["runtime_surface"]["executor_behavior"]["compact_intake_input"]
    assert execution["runtime_surface"]["executor_input_schemas"] == [
        "registration-executor-input.v1",
        "registration-executor-input.v2",
    ]
    assert compact_input["fields"] == ["registration_intake", "owner_confirmation"]
    assert compact_input["agent_full_packet_assembly_required"] is False
    assert compact_input["owner_digest_recomputed_before_evidence_ingest"] is True
    assert compact_input["external_handoff_uses_existing_v1_packet_and_manifest_route"] is True
    assert execution["entry_modes"]["handoff_receiver"]["input"] == "frozen_registration_packet_plus_exact_evidence_package"

    step_map = {step["id"]: step for step in execution["runtime_workflow"]}
    validate_branches = step_map["validate_packet_envelope"]["branches"]
    assert "--manifest" not in validate_branches["same_agent"]["command"]
    assert "--evidence-root" not in validate_branches["same_agent"]["command"]
    assert "--manifest" in validate_branches["external_handoff"]["command"]
    assert "--evidence-root" in validate_branches["external_handoff"]["command"]
    assert "validate_handoff_integrity" not in step_map

    evidence_authority = execution["authority_pin_validation"]["retrospective_attribution_registry_digest"]
    assert evidence_authority["required_when"] == "resolver_input.owner_attestation_is_present"
    assert execution["required_packet_contract"]["image_evidence_binding"]["preserved_semantics"] == [
        "timing",
        "captured_at",
        "provenance.capture_mode",
        "represented_market_time",
        "role",
        "timing_relative_to_trade",
    ]
    assert "persist_strategy_pending" not in step_map
    reconciliation = step_map["execute_record_reconciliation"]
    assert reconciliation["condition"] == "owner_confirmed_frozen_packet_and_provider_facts_valid"
    assert "every resolver outcome" in reconciliation["action"]
    assert "Provider identity and entry verification alone gate T-number allocation" in reconciliation["action"]
    steps = [step["id"] for step in execution["runtime_workflow"]]
    assert steps.index("validate_packet_envelope") < steps.index("validate_authority_pins")
    assert steps.index("validate_authority_pins") < steps.index("ingest_exact_evidence")
    assert steps.index("persist_frozen_packet") < steps.index("execute_record_reconciliation")
    assert steps.index("execute_record_reconciliation") < steps.index("refresh_and_verify_projections")
    assert steps.index("refresh_and_verify_projections") < steps.index("select_and_run_required_tests")
    assert steps.index("select_and_run_required_tests") < steps.index("persist_remote_main")

    agent_workflow = execution["workflow"]
    assert len(agent_workflow) == 1
    assert agent_workflow[0]["id"] == "execute_registration"
    assert agent_workflow[0]["command"] == execution["runtime_surface"]["executor_command"]
    benchmark = execution["test_policy"]["end_to_end_benchmark"]
    assert benchmark["schema"] == "registration-executor-telemetry.v1"
    assert set(benchmark["scenarios"]) == {
        "compact_intake_composition", "clean_checkout", "dirty_source_checkout", "external_handoff", "already_registered_retry"
    }
    assert benchmark["acceptance_target_seconds"] == 180
    assert benchmark["protected_trade_ids"] == ["T000027", "T000028"]
    assert benchmark["synthetic_coverage"] == {
        "original_appshots": 2,
        "replay_provenance": "review_post_trade_retrospective_replay",
        "owner_historical_attribution": True,
        "expected_strategy_status": "MATCHED",
        "confirmed_compact_intake_composition": {
            "path": ["raw_images", "compact_intake_preview", "owner_confirmation_digest", "one_executor_call", "canonical_projection_remote_readback"],
            "exact_preview_digest_reaches_frozen_packet": True,
            "plan_thesis_and_evidence_drift_rejected_before_ingest": True,
            "dirty_source_checkout_uses_safe_worktree": True,
        },
        "retry_inputs": [
            "unchanged_registration_executor_input_without_source_main_sha",
            "frozen_packet",
            "changed_registered_input_blocked_before_ingest",
        ],
        "external_handoff": {
            "source_manifest_sha256_and_exact_original_bytes": True,
            "receiver_validates_before_ingest": True,
            "tampered_package_blocked_without_mutation": True,
            "fresh_main_remote_readback_required": True,
        },
    }
    assert "external_handoff" in benchmark["scenarios"]


def test_record_reconciliation_consumes_frozen_packet_without_re_review() -> None:
    workflow = _load_yaml("workflows/record-reconciliation.yaml")
    contract = workflow["invocation_contract"]

    assert workflow["architecture"]["invoked_by"] == "workflows/registration-execution.yaml"
    routing = workflow["routing_contract"]
    assert routing["owner_routable"] is False
    assert routing["only_invoked_by"] == "workflows/registration-execution.yaml"
    assert contract["registration_packet_required_when_invoked_by_registration_execution"] is True
    assert contract["required_confirmation_state"] == "confirmed"
    assert contract["required_semantic_state"] == "frozen"
    assert "reinterpret_confirmed_strategy_resolution" in contract["executor_must_not"]
    assert "redo_trade_review" in contract["executor_must_not"]

    steps = {step["id"]: step for step in workflow["workflow"]}
    assert "validate_registration_packet" in steps
    assert "resolve_strategy" not in steps
    assert "never from registration_execution" in steps["review"]["condition"]
    retrospective = workflow["canonical_amendment_path"]["evidence"]["retrospective_owner_attestation"]
    time_model = workflow["canonical_amendment_path"]["evidence"]["time_model"]
    assert retrospective["visual_evidence_admissibility"] == "post_trade_replay_is_admissible_for_reconstruction"
    assert retrospective["visual_provenance"] == "preserve_actual_category_and_timing"
    assert retrospective["primary_strategy"] == "required_and_validated_against_active_frozen_registry"
    assert retrospective["entry_expression"] == "optional_and_validated_when_defined_by_the_selected_strategy"
    assert retrospective["strategies_without_expression_dimension"] == "resolve_without_fake_entry_expression"
    assert retrospective["chronology"]["unknown_timing"] == "compatible_at_reduced_confidence"
    assert retrospective["chronology"]["trigger_after_provider_entry"].startswith("material_conflict")
    assert time_model["timing_and_admissibility_are_distinct"] is True
    assert retrospective["chronology"]["represented_interval_must_cover_provider_entry"] is False
    assert retrospective["contradiction_contract"]["observed_fact"] == "required_exact_owner_facing_fact"
    amendment_authority = workflow["canonical_amendment_path"]["evidence"]["later_retrospective_attribution_amendment"]
    assert amendment_authority["event"] == "strategy_attribution_resolved"
    assert amendment_authority["authority_snapshot_location"] == "payload.amendment.authority"
    assert amendment_authority["append_only_snapshot"] is True
    assert amendment_authority["current_registry_mutation_rewrites_prior_snapshot"] is False


def test_review_policy_belongs_to_single_trade_review_not_registration() -> None:
    registration = _load_yaml("workflows/trade-registration.yaml")
    review = _load_yaml("workflows/single-trade-review.yaml")
    router = _load_yaml("workflows/trade-review.yaml")
    rules = (ROOT / "REVIEW_RULES.md").read_text(encoding="utf-8")

    assert "review_authority" not in registration["architecture"]
    assert review["architecture"]["review_policy"] == "REVIEW_RULES.md"
    assert router["architecture"]["kind"] == "router_orchestrator"
    assert "workflows/single-trade-review.yaml" in rules
    flat = rules.replace("\n> ", "")
    assert "它**不是**`trade_registration` 的執行步驟" in flat
    assert "保持 `review_status: not_started`" in flat


def test_resolver_uncertainty_missing_chart_and_partial_plan_do_not_block_live_registration() -> None:
    registration = _load_yaml("workflows/trade-registration.yaml")
    serialized = yaml.safe_dump(registration, allow_unicode=True)
    gates = registration["visual_analysis_contract"]["gates"]
    assert "unresolved" in gates["strategy_evidence_insufficient_or_ambiguous"]
    assert "not a registration gate" in gates["strategy_evidence_insufficient_or_ambiguous"]
    assert "allocate the canonical T-number" in gates["provider_verified_entry"]
    assert "replay evidence is retrospective" in gates["provider_verified_entry"]
    assert "registration stays pending" not in serialized


def test_registration_uses_first_class_retrospective_authority_and_derived_rule_projection() -> None:
    registration = _load_yaml("workflows/trade-registration.yaml")
    contract = registration["evidence_timing_contract"]
    replay = contract["artifact_provenance"]["retrospective_replay"]
    assert replay == {
        "category": "review",
        "timing": "post_trade",
        "type": "replay_chart",
        "provenance_capture_mode": "retrospective_replay",
        "role": "historical_market_reconstruction",
    }
    assert contract["retrospective_authority"]["schema"] == "first_class_owner_attestation_plus_retrospective_observations"
    projection = contract["retrospective_rule_fact_projection"]
    assert projection["schema"] == "retrospective-rule-facts.v1"
    assert "registry_digest" in projection["authority_pins"][-1]
    assert projection["owner_supplies_second_rule_attestation"] is False
    assert "relabel_retrospective_replay_as_pre_trade" in contract["forbidden"]
    invariants = "\n".join(registration["invariants"])
    assert "actual entry had no setup" in invariants
    assert "not be downgraded to INSUFFICIENT_EVIDENCE" in invariants
    assert "Missing chart evidence does not itself block a T-ID" in invariants
    assert "valid numeric realized R" in invariants
    assert "return BLOCKED before ingest or canonical mutation" in invariants
    assert "REGISTERED_INCOMPLETE" in invariants


def test_status_and_skill_do_not_describe_review_as_registration_step() -> None:
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    status = (ROOT / "STATUS.md").read_text(encoding="utf-8")

    assert "但這是登記流程的一個持久化步驟" not in skill
    assert "remains part of canonical registration" not in status
    assert "capability-based" in status
    assert "frozen Registration Packet" in skill
    assert "workflows/registration-execution.yaml" in skill
    assert "workflows/registration-execution.yaml" in status
    assert "canonical/registration/**" in status


def test_every_workflow_invariant_is_plain_text() -> None:
    """A ': ' inside an unquoted YAML list item silently turns the invariant into a one-key mapping."""

    for path in sorted((ROOT / "workflows").glob("*.yaml")):
        invariants = yaml.safe_load(path.read_text(encoding="utf-8")).get("invariants")
        if isinstance(invariants, list):
            bad = [i for i, item in enumerate(invariants) if not isinstance(item, str)]
            assert not bad, f"{path.name}: invariants {bad} parsed as mappings (unquoted ': ' in the text)"

def test_preserve_owner_images_never_ingests_before_chat_confirmation() -> None:
    workflow = _load_yaml("workflows/trade-registration.yaml")
    steps = {step["id"]: step for step in workflow["workflow"]}
    before = steps["preserve_owner_images"]["action"]
    assert "do not ingest to EvidenceStore" in before
    assert "After explicit in-chat confirmation" in before
    assert workflow["owner_confirmation_gate"]["evidence_ingest_before_confirmation_allowed"] is False
    assert "pending until the authorized Executor runs" in workflow["completion"]["REGISTRATION_PACKET_READY"]
    assert "post-confirmation Executor ingest" in workflow["evidence_preservation_contract"]["identity_rule"]


def test_missing_original_sl_routes_to_owner_chat_not_immediate_block() -> None:
    workflow = _load_yaml("workflows/trade-registration.yaml")
    steps = {step["id"]: step for step in workflow["workflow"]}
    ask = steps["ask_owner_for_missing_original_sl"]["action"]
    assert "原始停損（SL）" in ask
    assert "not a new Owner" not in ask
    assert "original_sl_unavailable_confirmed" in ask
    assert "Only" in workflow["live_chart_plan_r"]["unreadable_level"]


def test_early_visibility_check_batches_missing_entry_sl_tp_and_recollection_once() -> None:
    workflow = _load_yaml("workflows/trade-registration.yaml")
    order = [step["id"] for step in workflow["workflow"]]
    steps = {step["id"]: step for step in workflow["workflow"]}

    assert order.index("analyze_owner_images") < order.index("early_sl_visibility_check")
    assert order.index("early_sl_visibility_check") < order.index("collect_owner_recollection")
    assert order.index("early_sl_visibility_check") < order.index("ask_owner_for_missing_original_sl")
    assert order.index("collect_owner_recollection") < order.index("resolve_strategy")
    assert order.index("ask_owner_for_missing_original_sl") < order.index("resolve_strategy")
    assert order.index("resolve_strategy") < order.index("prepare_confirmation_preview")

    early = steps["early_sl_visibility_check"]
    assert early["condition"] == "owner_supplied_images_or_trade_plan_pending"
    early_action = early["action"]
    for token in (
        "analyze_owner_images",
        "backtest_image_first_intake",
        "Entry",
        "Original SL",
        "TP",
        "owner_recollection.verbatim_text",
        "1R",
        "planned_rr",
        "realized_r",
        "target_capture",
        "intended_entry",
        "backtest-chart-intake.v1",
        "planned_rr_from_chart",
        "ONE single combined Owner chat prompt",
        "resolve_strategy",
        "prepare_confirmation_preview",
        "backtest-registration preview",
        '"unknown"',
        "original_sl_unavailable_confirmed",
    ):
        assert token in early_action

    recollection = steps["collect_owner_recollection"]["action"]
    for token in (
        "owner_recollection.verbatim_text",
        "Entry",
        "Original SL",
        "TP",
        "ONE single combined Owner chat prompt",
        "resolve_strategy",
        "prepare_confirmation_preview",
        "backtest-registration preview",
        '"unknown"',
        "original_sl_unavailable_confirmed",
        "JSON",
        "CLI",
        "digest",
    ):
        assert token in recollection

    ask = steps["ask_owner_for_missing_original_sl"]["action"]
    for token in (
        "Entry",
        "Original SL",
        "TP",
        "owner_recollection.verbatim_text",
        "ONE single combined Owner chat prompt",
        "resolve_strategy",
        "prepare_confirmation_preview",
        "backtest-registration preview",
        "intended_entry",
        "planned_rr",
        "target_capture",
        "realized_r",
        "backtest-chart-intake.v1",
        "planned_rr_from_chart",
        '"unknown"',
        "original_sl_unavailable_confirmed",
        "JSON",
        "CLI",
        "digest",
        "moved, protective, or provider final stop",
        "Owner-attested numeric R",
    ):
        assert token in ask

    unreadable = workflow["live_chart_plan_r"]["unreadable_level"]
    for token in (
        "Entry",
        "Original SL",
        "TP",
        "owner_recollection",
        "verbatim_text",
        "1R",
        "planned_rr",
        "realized_r",
        "target_capture",
        "intended_entry",
        "ONE single combined Owner chat prompt",
        "resolve_strategy",
        "prepare_confirmation_preview",
        '"unknown"',
        "Only",
        "original_sl_unavailable_confirmed",
        "moved/protective/final stop",
        "JSON",
        "CLI",
        "digest",
    ):
        assert token in unreadable
