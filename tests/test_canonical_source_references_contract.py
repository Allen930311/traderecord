"""Automated contract checks for canonical-source pointers and runtime alignment."""
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from trading_os.evidence_time import EvidenceTimeError, normalize_evidence_time_metadata
from trading_os.live_chart_plan import PROVENANCE, R_BASIS, derive_live_chart_plan
from trading_os.operational_lifecycle import (
    REVIEW_SCHEMA,
    RULE_COMPLIANCE_VALUES,
    derive_rule_violation,
)
from trading_os.registration_executor import (
    COMPACT_EXECUTOR_INPUT_SCHEMA,
    EXECUTOR_INPUT_SCHEMA,
    RegistrationExecutorError,
    _CAPTURE_REQUIRED,
    _SUPPLEMENT_SCHEMAS,
    _packet_capture,
)
from trading_os.registration_packet import (
    ALLOWED_RESOLVER_STATUSES,
    LEGACY_PACKET_SCHEMA_VERSION,
    PACKET_SCHEMA_VERSION,
    REQUIRED_SECTIONS,
    TRANSPORT_MODES,
    OwnerInputRequired,
    _validate_authority_pins,
    prepare_confirmation_preview,
    require_owner_original_sl_before_preview,
)
from trading_os.review_preview import (
    ANALYSIS_SCHEMA,
    CHECKLIST_STATES,
    EXECUTION_QUALITY,
    PREVIEW_SCHEMA,
    REVIEW_FIELDS,
    SETUP_QUALITY,
)
from trading_os.review_service import ReviewAuthorizationError, _preflight_writer_inputs

ROOT = Path(__file__).resolve().parents[1]

YAML_POINTER_KEYS = frozenset({
    "canonical_source",
    "canonical_policy_source",
    "canonical_chart_reading_source",
    "canonical_evidence_policy",
    "canonical_timing_contract",
    "review_policy",
    "retrospective_attribution_authority",
    "strategy_authority",
    "upstream_workflow",
    "canonical_record_workflow",
    "provider_lookup_workflow",
    "execution_workflow",
    "adoption_path_workflow",
    "only_invoked_by",
    "invoked_by",
    "receiver_entry_workflow",
})

YAML_PYTHON_REF_KEYS = frozenset({
    "helper",
    "implementation",
    "lifecycle_authority",
    "evidence_store",
    "executor_module",
    "packet_builder",
    "compact_intake_adapter",
    "legacy_pending_reader",
    "census_projection",
    "writer",
    "method",
    "capture_facade",
    "module",
    "runtime_module",
    "baseline",
    "owner",
})


def _read_text(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def _load_yaml(rel_path: str) -> Any:
    return yaml.safe_load(_read_text(rel_path))


def _walk_yaml(node: Any, prefix: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, str(key), value
            yield from _walk_yaml(value, path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_yaml(item, f"{prefix}[{index}]")


def _resolve_yaml_section(data: Any, section: str) -> Any:
    cursor = data
    parts = section.split(".")
    if isinstance(cursor, dict) and parts[0] in cursor:
        for part in parts:
            assert isinstance(cursor, dict) and part in cursor, (
                f"YAML section {section!r} missing key {part!r}"
            )
            cursor = cursor[part]
        return cursor
    if len(parts) == 1 and isinstance(data, dict) and isinstance(data.get("workflow"), list):
        for step in data["workflow"]:
            if isinstance(step, dict) and step.get("id") == section:
                return step
    raise AssertionError(f"YAML section {section!r} does not resolve")


def _assert_review_rules_section_exists(section_num: str) -> None:
    review_rules = _read_text("REVIEW_RULES.md")
    pattern = rf"^##\s+{re.escape(section_num)}\.\s+"
    assert re.search(pattern, review_rules, flags=re.MULTILINE), (
        f"REVIEW_RULES.md is missing heading for §{section_num}"
    )


def _resolve_reference(ref: str, *, origin: str) -> Any:
    assert isinstance(ref, str) and ref.strip(), f"{origin}: pointer must be a non-empty string"
    review_sec = re.fullmatch(r"REVIEW_RULES\.md\s+§(\d+)", ref.strip())
    if review_sec:
        assert (ROOT / "REVIEW_RULES.md").is_file()
        _assert_review_rules_section_exists(review_sec.group(1))
        return review_sec.group(1)

    if "#" in ref:
        target_file, _, section = ref.partition("#")
        assert target_file, f"{origin}: reference {ref!r} must include an explicit target file before '#'"
        assert section, f"{origin}: reference {ref!r} has empty section after '#'"
        target = ROOT / target_file
        assert target.is_file(), f"{origin}: target file {target_file!r} for {ref!r} does not exist"
        if target.suffix in {".yaml", ".yml"}:
            return _resolve_yaml_section(_load_yaml(target_file), section)
        heading_text = target.read_text(encoding="utf-8")
        assert section in heading_text, f"{origin}: section {section!r} not found in {target_file!r}"
        return section

    target = ROOT / ref
    assert target.is_file(), (
        f"{origin}: expected file reference {ref!r} to resolve on disk "
        f"(internal YAML pointers must use '<file>#<section>')"
    )
    return target


def _resolve_python_symbol(dotted: str) -> Any:
    parts = dotted.split(".")
    for split in range(len(parts), 0, -1):
        mod_name = ".".join(parts[:split])
        try:
            obj: Any = importlib.import_module(mod_name)
        except ImportError:
            continue
        for attr in parts[split:]:
            obj = getattr(obj, attr)
        return obj
    raise AssertionError(f"Could not resolve Python symbol {dotted!r}")


# ---------------------------------------------------------------------------
# Check 1 — All YAML Canonical-Source References Resolve
# ---------------------------------------------------------------------------


def test_all_yaml_canonical_source_and_workflow_references_resolve() -> None:
    workflow_files = sorted((ROOT / "workflows").glob("*.yaml"))
    assert workflow_files, "expected workflows/*.yaml files"

    resolved_pointer_count = 0
    resolved_python_count = 0

    for wf_path in workflow_files:
        rel = wf_path.relative_to(ROOT).as_posix()
        data = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        for yaml_path, key, value in _walk_yaml(data):
            if key in YAML_POINTER_KEYS or key.endswith("_source"):
                assert isinstance(value, str), f"{rel}:{yaml_path} expected string pointer, got {type(value).__name__}"
                _resolve_reference(value, origin=f"{rel}:{yaml_path}")
                resolved_pointer_count += 1
            elif key in YAML_PYTHON_REF_KEYS and isinstance(value, str) and value.startswith("trading_os."):
                _resolve_python_symbol(value)
                resolved_python_count += 1

    assert resolved_pointer_count >= 35
    assert resolved_python_count >= 15

    trade_reg = _load_yaml("workflows/trade-registration.yaml")
    assert (
        trade_reg["registration_packet"]["authority_pins"]["canonical_source"]
        == "workflows/trade-registration.yaml#registration_packet.confirmation.required_metadata"
    )
    resolved_meta = _resolve_reference(
        trade_reg["registration_packet"]["authority_pins"]["canonical_source"],
        origin="workflows/trade-registration.yaml:registration_packet.authority_pins.canonical_source",
    )
    assert resolved_meta == trade_reg["registration_packet"]["confirmation"]["required_metadata"]


# ---------------------------------------------------------------------------
# Check 2 — All Markdown Cross-File Links & Section Pointers Resolve
# ---------------------------------------------------------------------------


def test_markdown_cross_references_and_section_pointers_resolve() -> None:
    doc_names = ("AGENT_CONTEXT.md", "AUTHORITY.md", "SKILL.md")
    md_link_re = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    wf_hash_re = re.compile(r"(workflows/[a-zA-Z0-9_-]+\.yaml)#([a-zA-Z0-9_.]+)")

    for doc_name in doc_names:
        text = _read_text(doc_name)

        # 1. Verify every Markdown [label](target) link resolves to an existing file (and fragment if any).
        links = md_link_re.findall(text)
        assert links, f"{doc_name} should contain Markdown cross-file links"
        for target in links:
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            _resolve_reference(target, origin=f"{doc_name} link")

        # 2. Verify every inline workflow.yaml#section reference resolves inside that YAML.
        for wf_file, section in wf_hash_re.findall(text):
            _resolve_reference(f"{wf_file}#{section}", origin=f"{doc_name} inline hash pointer")

        # 3. Verify every REVIEW_RULES.md §N (or (§2, §4) / （§2、§4）) citation matches an actual ## N. heading.
        for match in re.finditer(r"REVIEW_RULES\.md[^\n]{0,40}", text):
            for num in re.findall(r"§\s*(\d+)", match.group(0)):
                _assert_review_rules_section_exists(num)

    # 4. Verify explicit section keys & step IDs cited by PR #195 docs exist in their target YAMLs.
    agent_context = _read_text("AGENT_CONTEXT.md")
    authority = _read_text("AUTHORITY.md")
    skill = _read_text("SKILL.md")

    for required_token in (
        "AUTHORITY.md",
        "REVIEW_RULES.md",
        "§2",
        "§4",
        "workflows/trade-registration.yaml",
        "evidence_timing_contract",
        "live_chart_plan_r",
        "ask_owner_for_missing_original_sl",
        "workflows/registration-execution.yaml",
        "workflows/provider-sync.yaml",
        "credential_bootstrap_from_har",
    ):
        assert required_token in agent_context, f"AGENT_CONTEXT.md missing cited ref {required_token!r}"

    for required_token in (
        "REVIEW_RULES.md",
        "§2",
        "§4",
        "workflows/trade-registration.yaml#evidence_timing_contract",
        "registration_packet",
        "live_chart_plan_r",
        "workflows/registration-execution.yaml",
        "AGENT_CONTEXT.md",
        "SKILL.md",
    ):
        assert required_token in authority, f"AUTHORITY.md missing cited ref {required_token!r}"

    for required_token in (
        "AGENT_CONTEXT.md",
        "AUTHORITY.md",
        "REVIEW_RULES.md",
        "§2",
        "§4",
        "workflows/trade-registration.yaml",
        "evidence_timing_contract",
        "live_chart_plan_r",
        "ask_owner_for_missing_original_sl",
        "workflows/registration-execution.yaml",
        "workflows/provider-sync.yaml",
        "credential_bootstrap_from_har",
    ):
        assert required_token in skill, f"SKILL.md missing cited ref {required_token!r}"

    for section_key in ("evidence_timing_contract", "live_chart_plan_r", "registration_packet"):
        _resolve_reference(
            f"workflows/trade-registration.yaml#{section_key}",
            origin="explicit Markdown section citation check",
        )
    _resolve_reference(
        "workflows/trade-registration.yaml#ask_owner_for_missing_original_sl",
        origin="explicit Markdown step citation check",
    )
    _resolve_reference(
        "workflows/provider-sync.yaml#credential_bootstrap_from_har",
        origin="explicit Markdown step citation check",
    )


def test_markdown_preserves_missing_sl_blocked_and_replay_equality_semantics() -> None:
    agent_context = _read_text("AGENT_CONTEXT.md")
    authority = _read_text("AUTHORITY.md")
    skill = _read_text("SKILL.md")

    # AGENT_CONTEXT.md preserves missing original SL -> ask first -> unavailable flag -> CLOSED BLOCKED semantics.
    assert "owner_judgment.original_sl_unavailable_confirmed=true" in agent_context
    assert "leave `stop_loss`" in agent_context and "unknown" in agent_context
    assert "OPEN LIVE trades" in agent_context
    assert "CLOSED lifecycle" in agent_context
    assert "Owner-attested" in agent_context and "governed manual adjudication" in agent_context
    assert "return BLOCKED without" in agent_context and "inventing a T-ID or calculated R" in agent_context

    # AGENT_CONTEXT.md, SKILL.md, and AUTHORITY.md preserve Replay plan-evidence equality + truthful provenance.
    assert "Contemporaneous and Replay charts are equally admissible" in agent_context
    assert "first-class plan evidence" in agent_context
    assert "truthful provenance metadata" in agent_context

    assert "所有 Owner 提供的圖（含 Replay）都直接當作計畫與圖面證據" in skill
    for field in ("timing", "provenance.capture_mode", "captured_at", "represented_market_time"):
        assert field in skill

    assert "truthful `review/post_trade`" in authority
    assert "directly" in authority and "admissible as the Owner's plan and chart evidence" in authority


# ---------------------------------------------------------------------------
# Check 3 — Runtime Contract Alignment
# ---------------------------------------------------------------------------


def test_runtime_live_chart_plan_contract_alignment() -> None:
    trade_reg = _load_yaml("workflows/trade-registration.yaml")
    plan_cfg = trade_reg["live_chart_plan_r"]
    review_rules = _read_text("REVIEW_RULES.md")

    assert _resolve_reference(plan_cfg["r_calculation"]["canonical_source"], origin="live_chart_plan_r") == "4"
    assert _resolve_reference(plan_cfg["r_calculation"]["chart_evidence_source"], origin="live_chart_plan_r") == "2"
    assert plan_cfg["r_calculation"]["implementation"] == "trading_os.live_chart_plan.derive_live_chart_plan"
    assert plan_cfg["helper"] == "trading_os.live_chart_plan.derive_live_chart_plan"
    assert _resolve_python_symbol(plan_cfg["helper"]) is derive_live_chart_plan

    assert R_BASIS == "actual_entry_to_chart_stop"
    assert PROVENANCE == {"chart_annotation", "chart_estimate"}

    # Check REVIEW_RULES.md §2 & §4 include matching provenance, formulas, and helper link.
    sec2 = review_rules[review_rules.index("## 2."):review_rules.index("## 3.")]
    sec4 = review_rules[review_rules.index("## 4."):review_rules.index("## 5.")]
    for token in PROVENANCE:
        assert token in sec2
    assert "trading_os/live_chart_plan.py" in sec2
    assert plan_cfg["realized_r_authority"] == "deterministic_execution_calculation"
    assert plan_cfg["realized_r_authority"] in sec2
    for formula in (
        "(planned_tp - actual_entry) / (actual_entry - planned_sl)",
        "(actual_exit - actual_entry) / (actual_entry - planned_sl)",
        "(actual_entry - planned_tp) / (planned_sl - actual_entry)",
        "(actual_entry - actual_exit) / (planned_sl - actual_entry)",
    ):
        assert formula in sec4

    # Verify YAML field schema matches derive_live_chart_plan signature and runtime behavior.
    assert plan_cfg["price_fields"] == ["entry", "stop", "target"]
    assert plan_cfg["each_price"] == ["value", "provenance", "evidence_ref"]
    sig_params = set(inspect.signature(derive_live_chart_plan).parameters)
    assert {"chart", "direction", "provider_entry_price", "provider_exit_price", "attached_refs"} <= sig_params

    sample_chart = {
        field: {"value": val, "provenance": "chart_annotation", "evidence_ref": "artifact:" + "a" * 64 + ".png"}
        for field, val in zip(plan_cfg["price_fields"], (100, 98, 106))
    }
    open_plan = derive_live_chart_plan(sample_chart, direction="long", provider_entry_price=100)
    assert open_plan["r_basis"] == R_BASIS
    assert open_plan["planned_rr"] == 3.0
    assert "realized_r" not in open_plan
    assert set(open_plan["chart_price_evidence"]) == set(plan_cfg["price_fields"])

    closed_plan = derive_live_chart_plan(
        sample_chart, direction="long", provider_entry_price=100, provider_exit_price=104
    )
    assert closed_plan["realized_r"] == 2.0
    assert closed_plan["realized_r_authority"] == plan_cfg["realized_r_authority"] == "deterministic_execution_calculation"


def test_runtime_registration_packet_contract_alignment() -> None:
    reg_exec = _load_yaml("workflows/registration-execution.yaml")
    req_contract = reg_exec["required_packet_contract"]

    canonical_packet = _resolve_reference(
        req_contract["canonical_source"],
        origin="workflows/registration-execution.yaml#required_packet_contract.canonical_source",
    )
    transport_contract = _resolve_reference(
        req_contract["transport_contract"]["canonical_source"],
        origin="workflows/registration-execution.yaml#required_packet_contract.transport_contract.canonical_source",
    )
    resolved_auth_meta = _resolve_reference(
        canonical_packet["authority_pins"]["canonical_source"],
        origin="workflows/trade-registration.yaml#registration_packet.authority_pins.canonical_source",
    )

    assert PACKET_SCHEMA_VERSION == "registration-packet.v2"
    assert canonical_packet["confirmation"]["schema_version"] == PACKET_SCHEMA_VERSION
    assert reg_exec["runtime_surface"]["packet_schema_version"] == PACKET_SCHEMA_VERSION
    assert LEGACY_PACKET_SCHEMA_VERSION == "registration-packet.v1"
    assert LEGACY_PACKET_SCHEMA_VERSION in reg_exec["runtime_surface"]["legacy_readable_packet_schema_versions"]

    assert list(REQUIRED_SECTIONS) == canonical_packet["required_sections"] == req_contract["required_sections"]
    assert TRANSPORT_MODES == {"same_agent", "external_handoff"} == set(transport_contract["allowed"])
    assert (
        ALLOWED_RESOLVER_STATUSES
        == set(reg_exec["strategy_execution_contract"]["allowed_confirmed_statuses_for_trade_registration"])
        == {"MATCHED", "NO_MATCH", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE"}
    )

    assert resolved_auth_meta == canonical_packet["confirmation"]["required_metadata"]
    assert resolved_auth_meta == req_contract["required_confirmation_metadata"]

    resolver_val = reg_exec["authority_pin_validation"]["resolver_validation"]
    _resolve_reference(
        resolver_val["canonical_evidence_policy"],
        origin="workflows/registration-execution.yaml#authority_pin_validation.resolver_validation.canonical_evidence_policy",
    )
    timing_contract = _resolve_reference(
        resolver_val["canonical_timing_contract"],
        origin="workflows/registration-execution.yaml#authority_pin_validation.resolver_validation.canonical_timing_contract",
    )
    replay_shape = timing_contract["artifact_provenance"]["retrospective_replay"]
    normalized_replay = normalize_evidence_time_metadata(
        {
            "category": replay_shape["category"],
            "timing": replay_shape["timing"],
            "type": replay_shape["type"],
            "role": replay_shape["role"],
            "provenance": {"capture_mode": replay_shape["provenance_capture_mode"]},
        }
    )
    assert normalized_replay["timing_relative_to_trade"] == "post_trade"
    with pytest.raises(EvidenceTimeError, match="review/post_trade"):
        normalize_evidence_time_metadata(
            {
                "category": "pre_trade_context",
                "timing": "pre_trade",
                "type": replay_shape["type"],
                "role": replay_shape["role"],
                "provenance": {"capture_mode": replay_shape["provenance_capture_mode"]},
            }
        )

    # Confirm runtime pin builders/validators reference every canonical authority pin name.
    pin_source = inspect.getsource(_validate_authority_pins) + "\n" + inspect.getsource(prepare_confirmation_preview)
    for pin_name in resolved_auth_meta["authority_pins"]:
        assert pin_name in pin_source, f"runtime authority pin handling missing {pin_name!r}"
    for pin_name in resolved_auth_meta["conditional_authority_pins"]["owner_attestation_present"]:
        assert pin_name in pin_source, f"runtime conditional authority pin handling missing {pin_name!r}"

    # Verify missing original SL guard matches ask_owner_for_missing_original_sl & unreadable_level.
    with pytest.raises(OwnerInputRequired) as exc_info:
        require_owner_original_sl_before_preview({"canonical_capture": {"stop_loss": "unknown"}, "owner_judgment": {}})
    assert "原始停損（SL）" in exc_info.value.question

    # Preserving explicit Owner non-recall exception passes the pre-preview guard.
    require_owner_original_sl_before_preview(
        {
            "canonical_capture": {"stop_loss": "unknown"},
            "owner_judgment": {"original_sl_unavailable_confirmed": True},
        }
    )


def test_runtime_registration_executor_contract_alignment() -> None:
    reg_exec = _load_yaml("workflows/registration-execution.yaml")
    fields_ref = reg_exec["runtime_surface"]["executor_behavior"]["required_capture_fields_ref"]
    exec_input = _resolve_reference(
        fields_ref["canonical_source"],
        origin="workflows/registration-execution.yaml#runtime_surface.executor_behavior.required_capture_fields_ref",
    )

    assert EXECUTOR_INPUT_SCHEMA == "registration-executor-input.v1" == exec_input["schema"]
    assert (
        COMPACT_EXECUTOR_INPUT_SCHEMA
        == "registration-executor-input.v2"
        == reg_exec["runtime_surface"]["executor_behavior"]["compact_intake_input"]["executor_schema"]
    )
    assert [EXECUTOR_INPUT_SCHEMA, COMPACT_EXECUTOR_INPUT_SCHEMA] == reg_exec["runtime_surface"]["executor_input_schemas"]

    assert len(_CAPTURE_REQUIRED) == 10
    assert list(_CAPTURE_REQUIRED) == exec_input["required_capture_fields"]
    assert exec_input["closed_trade_fields"] == ["realized_r", "realized_r_authority"]
    assert "10" in fields_ref["summary"] and "realized_r, realized_r_authority" in fields_ref["summary"]

    # Verify _packet_capture runtime enforcement matches closed_trade_fields & 10 required fields.
    base_capture = {
        "intended_entry": 100.0,
        "stop_loss": "unknown",
        "take_profit": "unknown",
        "planned_rr": "unknown",
        "timeframe_context": "15m",
        "timeframe_trigger": "5m",
        "market_context": "trend",
        "thesis": "synthetic thesis",
        "execution_environment": "live",
        "outcome_known_at_recording": "true",
    }
    open_packet = {"canonical_capture": dict(base_capture)}
    assert _packet_capture(open_packet, is_closed=False)["stop_loss"] == "unknown"

    with pytest.raises(RegistrationExecutorError, match="realized_r"):
        _packet_capture({"canonical_capture": dict(base_capture)}, is_closed=True)

    with pytest.raises(RegistrationExecutorError, match="realized_r_authority"):
        _packet_capture({"canonical_capture": {**base_capture, "realized_r": -1.0}}, is_closed=True)

    closed_packet = {
        "canonical_capture": {
            **base_capture,
            "realized_r": -1.0,
            "realized_r_authority": "owner_attestation",
        }
    }
    assert _packet_capture(closed_packet, is_closed=True)["realized_r_authority"] == "owner_attestation"

    # Supplement schemas align with workflows/registration-execution.yaml#supplements.
    supplements = reg_exec["supplements"]
    assert set(_SUPPLEMENT_SCHEMAS) == {
        "chart-roles-confirmation.v1",
        "chart-roles-confirmation.v2",
        "owner-words-unrecoverable.v1",
        "plan-r-correction.v1",
    }
    for action_name in ("record_chart_roles", "record_plan_r_correction", "record_owner_words_unrecoverable"):
        assert action_name in supplements


def test_runtime_review_preview_and_operational_lifecycle_contract_alignment() -> None:
    review_rules = _read_text("REVIEW_RULES.md")
    agent_context = _read_text("AGENT_CONTEXT.md")
    authority = _read_text("AUTHORITY.md")
    skill = _read_text("SKILL.md")
    single_review = _load_yaml("workflows/single-trade-review.yaml")
    trade_reg = _load_yaml("workflows/trade-registration.yaml")

    assert _resolve_reference(
        single_review["architecture"]["review_policy"],
        origin="workflows/single-trade-review.yaml#architecture.review_policy",
    )
    assert single_review["canonical_boundary"]["canonical_mutation"] == (
        "allowed_only_through_capture_review_and_LifecycleStore.review"
    )

    assert RULE_COMPLIANCE_VALUES == {"compliant", "violation", "unknown", "not_observable"}
    assert "compliant | violation | unknown | not_observable" in review_rules
    for doc_name, text in (("AUTHORITY.md", authority), ("SKILL.md", skill)):
        for value in RULE_COMPLIANCE_VALUES:
            assert value in text, f"{doc_name} missing rule_compliance value {value!r}"
    assert "compliant, violation, unknown, or not_observable" in "\n".join(trade_reg["invariants"])
    assert derive_rule_violation({"rule_compliance": "compliant"}) is False
    assert derive_rule_violation({"rule_compliance": "violation"}) is True
    assert derive_rule_violation({"rule_compliance": "unknown"}) is None
    assert derive_rule_violation({"rule_compliance": "not_observable"}) is None

    assert ANALYSIS_SCHEMA == "strategy-discovery-review.v1"
    assert PREVIEW_SCHEMA == "canonical-trade-review-preview.v1"
    assert REVIEW_SCHEMA == "operational-trade-review.v2"
    assert ANALYSIS_SCHEMA in review_rules
    assert PREVIEW_SCHEMA in review_rules
    assert "capture review-preview" in yaml.safe_dump(single_review, allow_unicode=True)

    assert CHECKLIST_STATES == frozenset({"YES", "NO", "UNKNOWN", "N/A"})
    assert "YES/NO/UNKNOWN/N/A" in agent_context
    for state in CHECKLIST_STATES:
        assert f"`{state}`" in review_rules

    assert SETUP_QUALITY == frozenset({"valid", "invalid", "unknown", "not_observable", "not_applicable"})
    assert EXECUTION_QUALITY == frozenset({"aligned", "deviated", "unknown", "not_observable", "not_applicable"})
    assert "Setup Quality" in review_rules and "Execution Quality" in review_rules
    assert "Setup Quality" in agent_context and "Execution Quality" in agent_context
    assert "Setup Quality" in yaml.safe_dump(single_review, allow_unicode=True)

    # Canonical review projection fields align with REVIEW_FIELDS (plus review_analysis).
    allowed_projection_fields = set(trade_reg["registration_packet"]["canonical_review_projection"]["allowed_fields"])
    assert REVIEW_FIELDS - {"review_analysis"} == allowed_projection_fields

    # Diagnostic class vocabulary in REVIEW_RULES.md §11 & SKILL.md matches _preflight_writer_inputs.
    diagnostic_values = ("METHOD", "EXECUTION", "PROCESS-RISK", "INSUFFICIENT", "MIXED")
    assert " | ".join(diagnostic_values) in review_rules
    for value in diagnostic_values:
        assert f"`{value}`" in skill
        _preflight_writer_inputs(
            {"rule_compliance": "unknown", "diagnostic_class": value},
            "2026-10-11T10:30:00Z",
            None,
        )
    with pytest.raises(ReviewAuthorizationError, match="diagnostic_class"):
        _preflight_writer_inputs(
            {"rule_compliance": "unknown", "diagnostic_class": "INVALID_CLASS"},
            "2026-10-11T10:30:00Z",
            None,
        )


def test_trade_registration_workflow_contains_no_forbidden_agent_names() -> None:
    raw = _read_text("workflows/trade-registration.yaml").lower()
    dumped = yaml.safe_dump(_load_yaml("workflows/trade-registration.yaml"), allow_unicode=True).lower()
    for forbidden in ("codex", "claude", "chatgpt"):
        assert forbidden not in raw, f"forbidden agent name {forbidden!r} found in workflows/trade-registration.yaml"
        assert forbidden not in dumped
