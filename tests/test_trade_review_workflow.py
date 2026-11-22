from pathlib import Path

import yaml

from trading_os.agent_routing import (
    resolve_daily_workflow,
    resolve_performance_population,
    resolve_trade_review_mode,
)


ROOT = Path(__file__).resolve().parents[1]
SINGLE = ROOT / "workflows" / "single-trade-review.yaml"
PERFORMANCE = ROOT / "workflows" / "performance-analysis.yaml"
HEALTH = ROOT / "workflows" / "strategy-health-check.yaml"
ROUTER = ROOT / "workflows" / "trade-review.yaml"


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_single_trade_review_is_an_independent_atomic_sop_with_one_writer():
    data = _load(SINGLE)
    assert data["workflow_id"] == "single_trade_review"
    assert data["architecture"]["kind"] == "atomic_workflow_over_existing_system"
    assert data["entry"]["registered_trades_only"] is True
    assert data["prerequisites"] == [
        "canonical_trade_state_closed",
        "exit_verification_state_confirmed",
    ]
    boundary = data["canonical_boundary"]
    assert boundary["canonical_mutation"] == "allowed_only_through_capture_review_and_LifecycleStore.review"
    assert boundary["second_review_writer"] == "forbidden"
    assert boundary["provider_trade_discovery"] == "forbidden"
    assert boundary["strategy_registry_mutation"] == "forbidden"


def test_performance_analysis_is_descriptive_read_only_and_defaults_to_live():
    data = _load(PERFORMANCE)
    assert data["workflow_id"] == "performance_analysis"
    assert data["architecture"]["kind"] == "atomic_workflow_over_existing_system"
    assert data["canonical_boundary"]["canonical_mutation"] == "forbidden"
    assert data["canonical_boundary"]["strategy_diagnosis"] == "forbidden"
    defaults = data["performance_population_defaults"]
    assert defaults["overall_utterances"]["population"] == "LIVE_overall"
    assert defaults["overall_utterances"]["source"] == "summary_by_mode.live"
    assert defaults["named_strategy_utterance"]["population"] == "LIVE_named_strategy_eligible"
    assert defaults["named_strategy_utterance"]["source"] == "named_strategy_summary_by_mode.<strategy_id>.live"
    assert "ALL_combined_descriptive" in defaults["explicit_only_populations"]
    assert defaults["combine_without_explicit_owner_request"] == "forbidden"
    assert defaults["review_completion_required_for_performance_eligibility"] is False
    primary = set(data["metrics"]["primary"])
    assert {"trade_count", "win_rate", "average_realized_r", "expectancy_r"} <= primary
    assert {"strategy_id", "setup_tag", "direction", "review_status"} <= set(data["metrics"]["descriptive_breakdowns"])


def test_strategy_synthesis_is_hypothesis_only_and_research_stays_in_trading_record():
    data = _load(HEALTH)
    assert data["workflow_id"] == "strategy_health_check"
    populations = data["evidence_populations"]
    assert "reviewed_live_trades" in populations["keep_separate"]
    assert "backtest_trades" in populations["keep_separate"]
    assert populations["single_combined_win_rate"] == "forbidden"
    assert data["diagnostic_population"]["performance_eligibility_equals_review_completion"] is False
    assert data["architecture"]["research_destination"] == "Trading Record Research"
    contract = data["research_hypothesis_contract"]
    assert contract["handoff_destination"] == "Trading Record Research"
    executable_handoff = contract["executable_strategy_tool_development_handoff"]
    assert executable_handoff["destination"] == "example-owner/crypto"
    assert executable_handoff["requires_explicit_owner_request"] is True
    assert executable_handoff["allowed_scope"] == "executable_strategy_or_tool_development_only"
    adoption_path = contract["adoption_path"]
    assert adoption_path == [
        "Trading_Record_Strategy_Health_research_hypothesis",
        "Optional_Trading_Record_Setup_Observation_and_manual_Replay_or_Backtest_research",
        "Owner_explicit_adoption",
        "Trading_Record_immutable_next_frozen_rule_version",
        "Trading_Record_registry_and_pin_refresh",
        "Optional_handoff_to_example-owner/crypto_for_executable_strategy_or_tool_development_only_after_explicit_Owner_request",
    ]
    assert all("Crypto" not in step for step in adoption_path[:-1])
    assert executable_handoff["destination"] in adoption_path[-1]
    assert executable_handoff["allowed_scope"] in adoption_path[-1]
    assert "explicit_Owner_request" in adoption_path[-1]
    assert contract["registry_mutation"] == "forbidden"
    assert contract["hidden_backtest"] == "forbidden"
    assert contract["future_edge_claim"] == "forbidden"
    assert "research_hypothesis" in contract["allowed_output"]


def test_setup_observation_keeps_research_local_and_requires_explicit_crypto_handoff():
    data = _load(ROOT / "workflows" / "setup-observation.yaml")
    authority = data["research_authority"]
    assert authority["destination"] == "Trading Record Research"
    handoff = authority["external_executable_development_handoff"]
    assert handoff["destination"] == "example-owner/crypto"
    assert handoff["requires_explicit_owner_request"] is True
    assert handoff["allowed_scope"] == "executable_strategy_or_tool_development_only"
    assert "manual replay/backtest research stay in Trading Record" in data["invariants"][-1]


def test_trade_review_only_routes_to_the_atomic_contracts():
    data = _load(ROUTER)
    assert data["workflow_id"] == "trade_review"
    assert data["architecture"]["kind"] == "router_orchestrator"
    assert data["architecture"]["atomic_contracts"] == {
        "single_trade_review": "workflows/single-trade-review.yaml",
        "performance_analysis": "workflows/performance-analysis.yaml",
        "strategy_health_check": "workflows/strategy-health-check.yaml",
    }
    assert "canonical_boundary" not in data
    assert "metrics" not in data
    assert "review_policy" not in data


def test_daily_router_sends_requests_directly_to_five_atomic_owner_sops():
    cases = [
        ("T000025 幫我復盤", "single_trade_review"),
        ("復盤這筆", "single_trade_review"),
        ("看一下我的勝率", "performance_analysis"),
        ("最近績效", "performance_analysis"),
        ("Vegas 的勝率", "performance_analysis"),
        ("幫我體檢 Vegas", "strategy_health_check"),
        ("分析 ICT 整體哪裡要調整", "strategy_health_check"),
        ("T000014 幫我復盤", "single_trade_review"),
        ("復盤這筆", "single_trade_review"),
        ("幫我登記這筆交易", "trade_registration"),
        ("PropW 查進出場", "trade_lookup"),
    ]
    for utterance, workflow in cases:
        assert resolve_daily_workflow(utterance) == workflow
    assert resolve_trade_review_mode("T000014 幫我復盤") == "single_trade_review"
    assert resolve_trade_review_mode("復盤這筆") == "single_trade_review"
    assert resolve_trade_review_mode("最近績效") == "performance_analysis"
    assert resolve_trade_review_mode("幫我體檢 Vegas") == "strategy_health_check"
    owner_sops = {
        "trade_lookup": ROOT / "workflows" / "trade-lookup.yaml",
        "trade_registration": ROOT / "workflows" / "trade-registration.yaml",
        "single_trade_review": SINGLE,
        "performance_analysis": PERFORMANCE,
        "strategy_health_check": HEALTH,
    }
    assert set(owner_sops) == {workflow for _, workflow in cases}
    for workflow_id, path in owner_sops.items():
        assert _load(path)["workflow_id"] == workflow_id


def test_fresh_agent_contract_docs_share_atomic_review_and_live_population_authority():
    texts = [
        (ROOT / name).read_text(encoding="utf-8")
        for name in ("AGENT_CONTEXT.md", "AUTHORITY.md", "STATUS.md", "SKILL.md", "REVIEW_RULES.md")
    ]
    assert "single-trade-review.yaml" in texts[0]
    assert "performance-analysis.yaml" in texts[0]
    assert "strategy-health-check.yaml" in texts[0]
    assert "router" in texts[0].lower()
    assert all("LifecycleStore.review()" in text or "capture review" in text for text in texts[1:])
    assert "governed post-trade review policy" in texts[-1]
    assert "portfolio diagnosis" not in texts[-1].lower()


def test_performance_population_regression_unresolved_live_is_overall_only_and_paper_is_not_live():
    from trading_os.operational_analytics import _chronology_quality, _summary_by_mode

    overall = [
        {
            "sample_origin": "live",
            "performance_eligible": True,
            "counted": False,
            "strategy_resolution_status": "unresolved",
            "realized_r": 1.0,
            "outcome": "win",
            "event_date": "2026-10-01",
            "trade_id": "trd_unresolved",
        },
        {
            "sample_origin": "paper",
            "performance_eligible": True,
            "counted": True,
            "realized_r": -1.0,
            "outcome": "loss",
            "event_date": "2026-10-02",
            "trade_id": "trd_paper",
        },
    ]
    named = [row for row in overall if row["sample_origin"] == "live" and row.get("counted") is True]
    chronology = _chronology_quality(overall)
    overall_live = _summary_by_mode(overall, chronology)["live"]
    named_live = _summary_by_mode(named, chronology)["live"]
    assert overall_live["count"] == 1 and overall_live["win_rate"] == 1.0
    assert named_live["count"] == 0
    assert overall_live["paper_count"] == 0


def test_explicit_performance_population_requests_keep_existing_scope():
    cases = [
        ("看一下我的勝率", "live_overall"),
        ("Vegas 的勝率", "live_named_strategy"),
        ("包含回測一起看", "all_combined"),
        ("看我的 PAPER 勝率", "paper"),
    ]
    for utterance, population in cases:
        assert resolve_performance_population(utterance) == population
