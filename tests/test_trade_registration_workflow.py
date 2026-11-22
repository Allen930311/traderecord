from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from resolver_helpers import matching_observations, no_match_observations, write_observations
from trading_os.agent_routing import resolve_daily_workflow
from trading_os.evidence_store import EvidenceStore
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.registration_census import registration_state
from trading_os.registration_packet import (
    RegistrationPacketError,
    confirmation_snapshot_digest,
    freeze_packet,
    persist_frozen_packet,
)
from trading_os.strategy_resolver import resolve_strategy
from trading_os.unified_capture import capture_retrospective_live
from review_test_support import owner_confirmed_review


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflows" / "trade-registration.yaml"
REVIEW_RULES = ROOT / "REVIEW_RULES.md"
SKILL = ROOT / "SKILL.md"
AGENT_CONTEXT = ROOT / "AGENT_CONTEXT.md"


def _load():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(step_id):
    return next(step for step in _load()["workflow"] if step["id"] == step_id)


def _copy_operational_inputs(destination: Path) -> None:
    for relative in ("registry", "exports/method-validation", "exports/trader-dna/v1"):
        shutil.copytree(ROOT / relative, destination / relative)
    for relative in (
        "canonical/adjudications",
        "canonical/sequences",
        "canonical/records/trades",
        "canonical/records/observations",
        "canonical/records/superseded",
    ):
        (destination / relative).mkdir(parents=True)
    (destination / "canonical/sequences/numbers.json").write_text(
        json.dumps({"live_next": 1, "backtest_next": 1, "allocated": [], "tombstones": []}) + "\n",
        encoding="utf-8",
    )
    (destination / "canonical/sequences/finalizations.json").write_text(
        json.dumps({"schema_version": "finalization-index.v1", "records": {}}) + "\n",
        encoding="utf-8",
    )


def _capture_kwargs():
    return {
        "strategy_observations": no_match_observations(),
        "symbol": "SUIUSDT",
        "direction": "long",
        "intended_entry": "unknown",
        "stop_loss": "unknown",
        "take_profit": "unknown",
        "planned_rr": "unknown",
        "timeframe_context": "unknown",
        "timeframe_trigger": "15m",
        "market_context": "unknown",
        "setup_tags": [],
        "confirmation_tags": [],
        "thesis": "unknown",
        "chart_refs": [],
        "created_at": "2025-06-10T07:06:54+08:00",
        "provider_evidence": {
            "source_ref": "fixture://propw-registration-entry",
            "timing": "execution",
            "type": "broker_fill",
            "category": "entry",
            "sha256": "a" * 64,
            "captured_at": "2025-06-10T07:06:54+08:00",
        },
        "provider": "propw",
        "provider_lifecycle_id": "2506100706543892",
        "provider_execution_timestamp": "2025-06-10T07:06:54+08:00",
        "candidate_source": "fixture://propw-registration-match",
        "actual_entry": 3.4354,
        "outcome_known_at_recording": True,
    }


def _write_sui_provider_lifecycle(root: Path) -> dict:
    ledger = root / "provider-ledgers" / "propw" / "normalized"
    ledger.mkdir(parents=True, exist_ok=True)
    provider_facts = {
        "provider": "propw",
        "provider_lifecycle_id": "2506100706543892",
        "instrument": "SUI",
        "direction": "long",
        "entry_time": "2025-06-10T07:06:54+08:00",
        "entry_price": 3.4354,
        "exit_time": "2025-06-12T02:05:48+08:00",
        "weighted_exit_price": 3.4354,
        "net_profit_sum": -2.40318203,
        "open_size": "1",
        "closed_size": "1",
        "is_closed": True,
        "is_partial": False,
        "provider_lifecycle_phase": "CLOSED",
    }
    # Mirror the real ledger shape: the payload identifies itself by open_id and
    # does not repeat provider / provider_lifecycle_id.
    payload = {k: v for k, v in provider_facts.items() if k not in {"provider", "provider_lifecycle_id"}}
    ledger.joinpath("trade-lifecycles.jsonl").write_text(
        json.dumps(
            {
                "open_id": provider_facts["provider_lifecycle_id"],
                "lifecycle": {
                    "open_id": provider_facts["provider_lifecycle_id"],
                    **payload,
                    "normalization_status": "ok",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return provider_facts


def _frozen_no_setup_packet(
    root: Path,
    *,
    owner_judgment: object = None,
    transport_mode: str = "same_agent",
    evidence_manifest: Path | None = None,
) -> dict:
    provider_facts = _write_sui_provider_lifecycle(root)
    resolver_input = {
        "owner_setup_attribution": {
            "setup_status": "NO_SETUP",
            "reason": "actual entry had no setup",
            "evidence_refs": ["owner:confirmed-review"],
        }
    }
    packet_draft = {
        "packet_version": 1,
        "transport_mode": transport_mode,
        "owner_confirmation": {
            "status": "confirmed",
            "confirmed_at": "2026-10-06T08:00:00Z",
        },
        "provider_facts": provider_facts,
        "image_evidence": (
            [
                {
                    "sha256": artifact["sha256"],
                    "original_filename": artifact["original_filename"],
                    "package_path": artifact.get("zip_path") or artifact.get("path"),
                    **({"byte_size": artifact["byte_size"]} if artifact.get("byte_size") is not None else {}),
                }
                for artifact in (
                    (json.loads(evidence_manifest.read_text(encoding="utf-8")).get("artifacts", []))
                    if transport_mode == "external_handoff" and evidence_manifest is not None
                    else []
                )
                if Path(str(artifact.get("original_filename") or artifact.get("path") or "")).suffix.lower()
                in {".png", ".jpg", ".jpeg", ".webp"}
            ]
        ),
        "agent_observation": {},
        "agent_assessment": {},
        "owner_recollection": {"verbatim_text": "Exact Owner wording for this registration fixture."},
        "owner_judgment": {} if owner_judgment is None else owner_judgment,
        "agreed_facts": [],
        "unresolved_facts": [],
        "resolver_input": resolver_input,
        "strategy_resolution": resolve_strategy(root, resolver_input),
        "authority_pins": {"source_main_sha": "1" * 40, "strategy_version_pin": None},
        "canonical_review_projection": {},
        "research_hypotheses": [],
    }
    packet_draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(
        packet_draft,
        evidence_manifest_sha256=(
            hashlib.sha256(evidence_manifest.read_bytes()).hexdigest()
            if transport_mode == "external_handoff" and evidence_manifest is not None
            else None
        ),
    )
    return freeze_packet(root, packet_draft, evidence_manifest=evidence_manifest)


def _close_and_verify(store: LifecycleStore, draft_id: str, trade_id: str) -> None:
    exit_ref = "fixture://propw-registration-exit"
    store.final_close(
        trade_id,
        exit_price=3.4354,
        closed_at="2025-06-12T02:05:48+08:00",
        realized_r=0.0,
        provider_pnl=-2.40318203,
        source_ref=exit_ref,
    )
    store.attach_evidence(
        draft_id,
        {
            "source_ref": exit_ref,
            "timing": "post_trade",
            "type": "broker_fill",
            "category": "exit",
            "sha256": "b" * 64,
            "captured_at": "2025-06-12T02:05:48+08:00",
        },
    )
    store.verify_exit(
        trade_id,
        authority_class="owner_attestation",
        evidence_refs=[exit_ref],
        realized_r=0.0,
    )


def test_trade_registration_is_owner_facing_orchestration_over_existing_system():
    data = _load()
    assert data["workflow_id"] == "trade_registration"
    assert data["authority"] == "trading-record"
    assert data["architecture"]["new_state_model"] is False
    assert data["architecture"]["provider_lookup_workflow"] == "workflows/trade-lookup.yaml"
    assert data["architecture"]["canonical_record_workflow"] == "workflows/record-reconciliation.yaml"
    assert data["canonical_mutation"] == {
        "mode": "delegated_only",
        "entry_workflow": "registration_execution",
        "internal_record_workflow": "record_reconciliation",
        "direct_record_reconciliation_route": "forbidden",
    }
    assert _step("load_provider_truth")["delegates_to"] == "trade_lookup"
    # Registration no longer calls record_reconciliation itself: it freezes the owner-confirmed packet and
    # hands off to the write-side registration_execution workflow, which alone invokes record_reconciliation.
    assert _step("delegate_execution")["delegates_to"] == "registration_execution"
    assert data["architecture"]["execution_workflow"] == "workflows/registration-execution.yaml"


def test_router_separates_lookup_registration_and_review_semantics():
    assert resolve_daily_workflow("查一下 PropW 這筆進出場") == "trade_lookup"
    assert resolve_daily_workflow("幫我登記這筆交易") == "trade_registration"
    assert resolve_daily_workflow("幫我記錄這筆交易，進出場去 PropW 查") == "trade_registration"
    assert resolve_daily_workflow("這筆先復盤後補登") == "single_trade_review"
    assert resolve_daily_workflow("復盤最近 20 筆交易") == "performance_analysis"
    assert resolve_daily_workflow("幫我復盤本月績效") == "performance_analysis"
    assert resolve_daily_workflow("看一下我的勝率") == "performance_analysis"
    assert resolve_daily_workflow("完成復盤") == "performance_analysis"


def test_frozen_registration_capture_does_not_run_second_strategy_resolution(tmp_path: Path, monkeypatch):
    _copy_operational_inputs(tmp_path)
    frozen = _frozen_no_setup_packet(tmp_path)
    persist_frozen_packet(LifecycleStore(tmp_path), frozen)
    assert frozen["strategy_resolution"]["status"] == "NO_MATCH"
    assert "plan" not in frozen["resolver_input"]

    import trading_os.unified_capture as unified_capture

    def _forbidden_second_resolve(*args, **kwargs):
        raise AssertionError("frozen registration path must not call resolve_strategy again")

    monkeypatch.setattr(unified_capture, "resolve_strategy", _forbidden_second_resolve)

    kwargs = _capture_kwargs()
    kwargs.update(
        {
            "strategy_observations": None,
            "stop_loss": 3.30,
            "take_profit": 3.80,
            "planned_rr": 2.0,
            "frozen_registration_packet": frozen,
        }
    )
    result = capture_retrospective_live(LifecycleStore(tmp_path), **kwargs)

    assert result["registered"] is True
    assert result["strategy_result"] == frozen["strategy_resolution"]
    assert result["strategy_result"]["status"] == "NO_MATCH"


def test_frozen_registration_capture_requires_durable_packet_persistence(tmp_path: Path):
    _copy_operational_inputs(tmp_path)
    frozen = _frozen_no_setup_packet(tmp_path)
    kwargs = _capture_kwargs()
    kwargs.update({"strategy_observations": None, "frozen_registration_packet": frozen})
    with pytest.raises(RegistrationPacketError, match="durably persisted"):
        capture_retrospective_live(LifecycleStore(tmp_path), **kwargs)


def test_frozen_registration_rejects_capture_instrument_drift(tmp_path: Path):
    _copy_operational_inputs(tmp_path)
    frozen = _frozen_no_setup_packet(tmp_path)
    persist_frozen_packet(LifecycleStore(tmp_path), frozen)
    kwargs = _capture_kwargs()
    kwargs.update(
        {
            "strategy_observations": None,
            "symbol": "BTCUSDT",
            "frozen_registration_packet": frozen,
        }
    )
    with pytest.raises(LifecycleError, match="instrument does not match"):
        capture_retrospective_live(LifecycleStore(tmp_path), **kwargs)


def test_registered_frozen_retry_requires_same_packet_identity(tmp_path: Path):
    _copy_operational_inputs(tmp_path)
    packet_a = _frozen_no_setup_packet(tmp_path)
    store = LifecycleStore(tmp_path)
    persist_frozen_packet(store, packet_a)
    kwargs = _capture_kwargs()
    kwargs.update({"strategy_observations": None, "frozen_registration_packet": packet_a})

    first = capture_retrospective_live(store, **kwargs)
    assert first["registered"] is True
    assert first["packet_sha256"] == packet_a["packet_sha256"]

    state = registration_state(tmp_path, "propw", "2506100706543892")
    assert state["status"] == "REGISTERED"
    assert state["packet_sha256"] == packet_a["packet_sha256"]
    projected = store._projected_record(first["trade_id"])
    assert projected["registration_packet_sha256"] == packet_a["packet_sha256"]
    assert projected["identity_link"]["registration_packet_sha256"] == packet_a["packet_sha256"]

    same = capture_retrospective_live(store, **kwargs)
    assert same["already_registered"] is True
    assert same["trade_id"] == first["trade_id"]
    assert same["packet_sha256"] == packet_a["packet_sha256"]

    persisted_same = persist_frozen_packet(store, packet_a)
    assert persisted_same["status"] == "ALREADY_REGISTERED"
    assert persisted_same["trade_id"] == first["trade_id"]

    packet_b = _frozen_no_setup_packet(
        tmp_path,
        owner_judgment={"changed_after_confirmation": True},
    )
    assert packet_b["packet_sha256"] != packet_a["packet_sha256"]
    changed = dict(kwargs)
    changed["frozen_registration_packet"] = packet_b
    with pytest.raises(RegistrationPacketError, match="different frozen Registration Packet"):
        persist_frozen_packet(store, packet_b)

    # Even if a caller bypasses the packet-persist command and drops packet B into the
    # canonical packet directory, the durable provider binding still rejects the retry.
    forced = tmp_path / "canonical" / "registration" / "packets" / f"{packet_b['packet_sha256']}.json"
    forced.parent.mkdir(parents=True, exist_ok=True)
    forced.write_text(json.dumps(packet_b), encoding="utf-8")
    with pytest.raises(RegistrationPacketError, match="different frozen Registration Packet"):
        capture_retrospective_live(store, **changed)


def test_in_progress_frozen_retry_requires_same_packet_identity(tmp_path: Path, monkeypatch):
    _copy_operational_inputs(tmp_path)
    packet_a = _frozen_no_setup_packet(tmp_path)
    store = LifecycleStore(tmp_path)
    persist_frozen_packet(store, packet_a)
    kwargs = _capture_kwargs()
    kwargs.update({"strategy_observations": None, "frozen_registration_packet": packet_a})

    def _stop_before_finalization(*args, **kwargs):
        raise LifecycleError("simulated finalization stop")

    monkeypatch.setattr(store, "finalize", _stop_before_finalization)
    with pytest.raises(LifecycleError, match="simulated finalization stop"):
        capture_retrospective_live(store, **kwargs)

    state = registration_state(tmp_path, "propw", "2506100706543892")
    assert state["status"] == "IN_PROGRESS"
    assert state["packet_sha256"] == packet_a["packet_sha256"]

    same = capture_retrospective_live(store, **kwargs)
    assert same["registration_status"] == "REGISTRATION_IN_PROGRESS"
    assert same["packet_sha256"] == packet_a["packet_sha256"]

    persisted_same = persist_frozen_packet(store, packet_a)
    assert persisted_same["status"] == "REGISTRATION_IN_PROGRESS"
    assert persisted_same["packet_sha256"] == packet_a["packet_sha256"]

    packet_b = _frozen_no_setup_packet(
        tmp_path,
        owner_judgment={"different_packet": True},
    )
    changed = dict(kwargs)
    changed["frozen_registration_packet"] = packet_b
    with pytest.raises(RegistrationPacketError, match="different frozen Registration Packet"):
        persist_frozen_packet(store, packet_b)

    # Even if a caller bypasses the packet-persist command and drops packet B into the
    # canonical packet directory, the durable provider binding still rejects the retry.
    forced = tmp_path / "canonical" / "registration" / "packets" / f"{packet_b['packet_sha256']}.json"
    forced.parent.mkdir(parents=True, exist_ok=True)
    forced.write_text(json.dumps(packet_b), encoding="utf-8")
    with pytest.raises(RegistrationPacketError, match="different frozen Registration Packet"):
        capture_retrospective_live(store, **changed)


def test_external_handoff_resolved_capture_requires_and_accepts_evidence_package(tmp_path: Path):
    _copy_operational_inputs(tmp_path)
    evidence_root = tmp_path / "external-handoff"
    evidence_root.mkdir()
    image = evidence_root / "image.png"
    image.write_bytes(b"exact-owner-image")
    import hashlib

    manifest = {
        "artifacts": [
            {
                "path": "image.png",
                "original_filename": "image.png",
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "byte_size": image.stat().st_size,
            }
        ]
    }
    manifest_path = evidence_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    frozen = _frozen_no_setup_packet(
        tmp_path,
        transport_mode="external_handoff",
        evidence_manifest=manifest_path,
    )
    persist_frozen_packet(
        LifecycleStore(tmp_path),
        frozen,
        evidence_manifest=manifest_path,
        evidence_root=evidence_root,
    )

    kwargs = _capture_kwargs()
    kwargs.update({"strategy_observations": None, "frozen_registration_packet": frozen})
    with pytest.raises(RegistrationPacketError, match="external_handoff packet requires"):
        capture_retrospective_live(LifecycleStore(tmp_path), **kwargs)

    kwargs.update(
        {
            "registration_evidence_manifest": manifest_path,
            "registration_evidence_root": evidence_root,
        }
    )
    result = capture_retrospective_live(LifecycleStore(tmp_path), **kwargs)
    assert result["registered"] is True
    assert result["packet_sha256"] == frozen["packet_sha256"]


def test_historical_replay_image_preserves_original_then_binds_review_post_trade(tmp_path: Path):
    _copy_operational_inputs(tmp_path)
    image_path = tmp_path / "owner-sui-replay.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"owner-original-chart-bytes")
    evidence_store = EvidenceStore(tmp_path)

    original = evidence_store.ingest(image_path)
    store = LifecycleStore(tmp_path)
    captured = capture_retrospective_live(store, **_capture_kwargs())
    draft_id = captured["draft_id"]
    trade_id = captured["trade_id"]

    rebound = evidence_store.ingest(image_path)
    assert rebound["deduped"] is True
    assert rebound["sha256"] == original["sha256"]
    assert rebound["locator"] == original["locator"]

    store.attach_evidence(
        draft_id,
        {
            "source_ref": rebound["locator"],
            "locator": rebound["locator"],
            "sha256": rebound["sha256"],
            "category": "review",
            "timing": "post_trade",
            "type": "chart_snapshot",
            "media_type": rebound["media_type"],
            "byte_size": rebound["byte_size"],
            "original_filename": rebound["original_filename"],
        },
    )
    record = store.current()["records"][trade_id]
    chart = next(item for item in record["evidence"] if item.get("sha256") == original["sha256"])
    assert chart["category"] == "review"
    assert chart["timing"] == "post_trade"
    assert chart["locator"] == original["locator"]
    assert not any(
        item.get("sha256") == original["sha256"] and item.get("category") == "pre_trade_context"
        for item in record["evidence"]
    )


def test_registration_packet_projects_only_supported_fields_into_canonical_review(tmp_path: Path):
    data = _load()
    projection = data["registration_packet"]["canonical_review_projection"]
    allowed = set(projection["allowed_fields"])
    assert allowed == {
        "what_went_well",
        "what_went_wrong",
        "rule_compliance",
        "execution_deviation",
        "thesis_validity",
        "entry_quality",
        "exit_quality",
        "evidence_completeness",
        "owner_notes",
        "review_status",
        "diagnostic_class",
    }
    assert "agent_observation" in projection["noncanonical_packet_fields"]
    assert "strategy_resolution" in projection["noncanonical_packet_fields"]
    assert "research_hypotheses" in projection["noncanonical_packet_fields"]

    _copy_operational_inputs(tmp_path)
    store = LifecycleStore(tmp_path)
    captured = capture_retrospective_live(store, **_capture_kwargs())
    draft_id = captured["draft_id"]
    trade_id = captured["trade_id"]
    _close_and_verify(store, draft_id, trade_id)

    packet_path = tmp_path / "registration-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "agent_observation": ["No recognizable setup at actual entry"],
                "owner_recollection": "unknown",
                "owner_judgment": "no meaningful setup",
                "strategy_resolution": {"status": "unresolved"},
                "research_hypotheses": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    packet_artifact = EvidenceStore(tmp_path).ingest(packet_path)
    store.attach_evidence(
        draft_id,
        {
            "source_ref": packet_artifact["locator"],
            "locator": packet_artifact["locator"],
            "sha256": packet_artifact["sha256"],
            "category": "review",
            "timing": "post_trade",
            "type": "manual_note",
            "media_type": packet_artifact["media_type"],
            "byte_size": packet_artifact["byte_size"],
            "original_filename": packet_artifact["original_filename"],
        },
    )

    review_payload = {
        "rule_compliance": "not_observable",
        "diagnostic_class": "INSUFFICIENT",
        "entry_quality": "owner_observed_no_meaningful_setup_at_actual_entry",
        "evidence_completeness": "provider_and_chart_evidence_present",
        "owner_notes": "Owner cannot identify the original strategy from replay.",
    }
    assert set(review_payload) <= allowed
    owner_confirmed_review(
        store,
        trade_id,
        review_payload,
        reviewed_at="2025-06-12T02:06:48+08:00",
    )
    current = store.current()["records"][trade_id]
    assert current["review"]["rule_compliance"] == "not_observable"
    assert current["review"]["diagnostic_class"] == "INSUFFICIENT"
    assert "agent_observation" not in current["review"]
    assert any(item.get("sha256") == packet_artifact["sha256"] for item in current["evidence"])


def test_docs_expose_registration_and_independent_atomic_review_sops():
    agent_context = AGENT_CONTEXT.read_text(encoding="utf-8")
    skill = SKILL.read_text(encoding="utf-8")
    review_rules = REVIEW_RULES.read_text(encoding="utf-8")
    assert "workflows/trade-registration.yaml" in agent_context
    assert "workflows/single-trade-review.yaml" in agent_context
    assert "workflows/performance-analysis.yaml" in agent_context
    assert "workflows/strategy-health-check.yaml" in agent_context
    assert "workflows/trade-review.yaml" in agent_context
    assert "## 登記（Trade Registration）" in skill
    assert "Owner-Facing Atomic Review and Analysis SOPs" in skill
    assert "single_trade_review" in skill
    assert "provider-side read-only" in skill
    assert "workflows/performance-analysis.yaml" in skill
    assert "workflows/strategy-health-check.yaml" in skill
    assert "governed post-trade review policy" in review_rules


def test_review_rules_use_current_rule_compliance_contract_and_forbid_psychology_inference():
    text = REVIEW_RULES.read_text(encoding="utf-8")
    section = text.split("## 11. 復盤輸出結論格式", 1)[1]
    assert "rule_compliance: compliant | violation | unknown | not_observable" in section
    assert "diagnostic_class: METHOD | EXECUTION | PROCESS-RISK | INSUFFICIENT | MIXED" in section
    assert "rule_violation: true/false" not in section
    assert "mistake_type: psychology | execution | analysis | overtrading | risk_mgmt | none" not in section
    assert "必須逐張實際檢視" in text
    assert "心理／動機標籤只有在 owner 明確陳述時才可記錄" in text


def test_skill_has_no_competing_v1_owner_routes():
    skill = SKILL.read_text(encoding="utf-8")
    rules = REVIEW_RULES.read_text(encoding="utf-8")
    context = AGENT_CONTEXT.read_text(encoding="utf-8")
    assert "## 日常入口（四個 Owner SOP）" in skill
    assert "## Unified Capture V1（owner 自然語言" not in skill
    assert "## Single Trade Review Fast Path" not in skill
    assert "### GitHub-only approved review request format" not in skill
    assert "當前 active strategy/rule" not in skill
    assert "進場前留下的 Pre-Trade 證據為準" not in rules
    assert "既有 canonical T-ID 和 provider binding 永不因新增" in context
    assert "單筆復盤（唯一入口）" in skill
    assert "Owner 只跟 Agent 說話" in skill
    assert "不得請 Owner 開 GitHub Issue" in skill
    assert "Owner talks only to the Agent" in context
    assert "No pre-confirmation" in context
    assert "Owner 確認前都先**只讀**原始 bytes" in skill
    assert "有 write capability 就直接 `EvidenceStore.ingest`" not in skill
    authority = (ROOT / "AUTHORITY.md").read_text(encoding="utf-8")
    assert "a separate retrospective visual-observation lane" not in authority
    assert "directly\n  admissible as the Owner\'s plan" in authority
