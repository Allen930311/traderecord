"""Regression: the Owner-facing provider refresh is not a lookup/registration."""
from pathlib import Path

import yaml

from trading_os.agent_routing import resolve_daily_workflow

ROOT = Path(__file__).resolve().parents[1]


def test_exchange_sync_is_an_atomic_owner_sop():
    data = yaml.safe_load((ROOT / "workflows" / "provider-sync.yaml").read_text(encoding="utf-8"))
    assert data["workflow_id"] == "provider_sync"
    assert data["external_provider_access"] == {"effect": "READ_ONLY", "methods": ["GET"]}
    assert data["execution"]["command"] == "python -m trading_os provider-sync --fetch"
    assert data["invariants"]["never_allocate_t_id"] is True
    assert data["invariants"]["never_start_review"] is True
    assert data["current_limits"]["automatic_main_publishing"] == "routine_sync_may_fast_forward_main_with_publish_main_flag;_data_pr_remains_available"


def test_explicit_refresh_does_not_fall_through_to_lookup():
    for intent in ("刷新 PropW 帳本", "刷新帳本", "同步交易所數據",
                   "更新帳本", "refresh PropW", "sync PropW", "PropW 同步"):
        assert resolve_daily_workflow(intent) == "provider_sync", intent


def test_registration_takes_priority_over_embedded_sync_and_lookup():
    assert resolve_daily_workflow("幫我登記一筆 BTC 交易，先同步 PropW 帳本") == "trade_registration"
    for intent in ("查 PropW 進出場", "PropW 查進出場", "找 openId"):
        assert resolve_daily_workflow(intent) == "trade_lookup", intent


def test_review_and_analysis_are_never_triggered_by_sync():
    assert resolve_daily_workflow("復盤 T000025") == "single_trade_review"
    assert resolve_daily_workflow("Vegas 策略健康") == "strategy_health_check"
    assert resolve_daily_workflow("我的勝率") == "performance_analysis"


def test_entry_contract_declares_four_operations_and_current_rollout():
    text = " ".join((ROOT / "AGENT_CONTEXT.md").read_text(encoding="utf-8").split())
    for term in ("Sync", "Register", "Review", "Analyze",
                 "workflows/provider-sync.yaml",
                 "A digest or timestamp by itself is never approval",
                 "image-intake", "agent-request-runner.yml", "legacy"):
        assert term.lower() in text.lower()
    # One write route per SOP: no Issue-triggered Writer remains documented.
    assert "issue event" not in text.lower()
    assert "optional separate transport" not in text.lower()
    assert ("existing v1 writer and registration census still use" in text.lower()
            or ("registered means complete" in text.lower()
                and "registered_incomplete" in text.lower()))
    assert "not a second writable ledger" in text
    assert "workflows/setup-observation.yaml" in text
    assert "not an automatic review or a fifth Owner-facing SOP" in text

def test_missed_unexecuted_setup_stays_analyze_subcapability():
    for text in (
        "記一下這個沒進的 VT",
        "這裡有個很好的 setup 但我沒下",
        "幫我記錄這個沒有下單的 setup",
        "保存我錯過的setup",
        "record missedsetup",
    ):
        assert resolve_daily_workflow(text) == "setup_observation", text


def test_simple_provider_refresh_is_not_misrouted_by_no_trade_description():
    assert resolve_daily_workflow("今天沒有下單，刷新 PropW 帳本") == "provider_sync"
    assert resolve_daily_workflow("我要登記剛剛的 BTC 真實交易") == "trade_registration"

def test_unexecuted_setup_analysis_is_not_accidentally_a_recording():
    # Merely naming a missed setup must never silently select the observation writer.
    assert resolve_daily_workflow("分析我沒有下單的 setup 策略健康") == "strategy_health_check"
    assert resolve_daily_workflow("查 PropW 沒下單的原因") == "trade_lookup"

def test_sync_contract_exposes_r2_p2a_lifecycle_and_freshness_contract():
    data = yaml.safe_load((ROOT / "workflows" / "provider-sync.yaml").read_text(encoding="utf-8"))
    limits = data["current_limits"]
    assert data["status"] == "r2_p2b_code_implemented_owner_data_refresh_pending"
    assert limits["open_only_normalization"] == "valid_open_provider_phase_when_open_facts_are_complete"
    assert "next_defaults_to_closed" in limits["partial_close_selection"]
    assert "source_digests" in limits["census_freshness_fields"]
    assert limits["stored_census_may_lag_canonical"] == "detectable_as_stale_until_rebuilt"
    assert limits["legacy_ok_without_explicit_provider_phase"] == "unknown_until_ledger_refresh"
    entry = (ROOT / "AGENT_CONTEXT.md").read_text(encoding="utf-8")
    assert "PARTIALLY_CLOSED" in entry
    assert "`census --candidates`" in entry
    assert "census --next" in entry
    assert "--create-pr" in entry
    assert "--publish-main" in entry


def test_main_readback_is_read_only_and_requires_fresh_census():
    ci = (ROOT / ".github/workflows/propw-main-readback.yml").read_text(encoding="utf-8")
    assert "branches: [main]" in ci
    assert "ref: ${{ github.sha }}" in ci
    assert "EXPECTED_SHA" in ci
    assert "EXPECTED_REF" in ci
    assert "refs/heads/main" in ci
    assert "projections verify --require-census-fresh" in ci
    assert "canonical/registration/pending-strategy.jsonl" in ci
    assert "contents: read" in ci
    assert "contents: write" not in ci


def test_pr_ci_checks_actual_commits_not_empty_checkout_diff():
    ci = (ROOT / ".github/workflows/trading-record-v1.yml").read_text(encoding="utf-8")
    assert "fetch-depth: 0" in ci
    assert "Check PR patch whitespace" in ci
    assert "pull_request.base.sha" in ci
    assert "pull_request.head.sha" in ci

def test_negative_recording_is_never_a_writer_route():
    cases = (
        "請不要記錄我沒有下單的 setup，先分析原因",
        "不要記錄沒下單的 Setup，先分析原因",
        "先不要保存這個未下單 setup",
        "不用登記這筆交易，先分析",
        "don't record my missed setup",
        "請不要記錄我沒有下單的 setup，先分析策略健康",
    )
    for intent in cases:
        route = resolve_daily_workflow(intent)
        assert route not in {"setup_observation", "trade_registration", "provider_sync"}, (intent, route)
    assert resolve_daily_workflow("請不要記錄我沒有下單的 setup，先分析原因") is None
    assert resolve_daily_workflow("請不要記錄我沒有下單的 setup，先分析策略健康") == "strategy_health_check"


def test_explicit_backtest_trade_registration_wins_over_missed_setup():
    for intent in (
        "幫我記錄一筆回測交易，當時沒有實際下單",
        "記錄一筆沒有實際下單的回測交易",
        "請登記這筆沒下單的回測單",
        "register backtest trade that never had a live order",
    ):
        assert resolve_daily_workflow(intent) == "trade_registration", intent
    # Simply discussing or capturing a missed setup must never mint a BACKTEST trade.
    assert resolve_daily_workflow("記一下這個沒進的 VT") == "setup_observation"
    assert resolve_daily_workflow("保存我錯過的setup") == "setup_observation"


def test_mixed_sync_review_intents_respect_explicit_negative_writes():
    assert resolve_daily_workflow(
        "不要記錄沒下單的 setup，刷新 PropW 帳本"
    ) == "provider_sync"
    assert resolve_daily_workflow(
        "不要記錄沒下單的 setup，復盤 T000025"
    ) == "single_trade_review"
    assert resolve_daily_workflow(
        "不要刷新帳本，只復盤 T000025"
    ) == "single_trade_review"
    assert resolve_daily_workflow(
        "不要保存沒下單的 setup，請查 PropW 進出場"
    ) == "trade_lookup"
    assert resolve_daily_workflow(
        "幫我登記 BTC 交易，先刷新 PropW 帳本"
    ) == "trade_registration"


def test_explicit_backtest_negative_save_does_not_write():
    assert resolve_daily_workflow(
        "不要記錄一筆沒有下單的回測交易，先分析策略健康"
    ) == "strategy_health_check"

def test_negation_does_not_escape_into_subsequent_positive_command():
    assert resolve_daily_workflow(
        "不要同步帳本，幫我登記 BTC 交易"
    ) == "trade_registration"
    assert resolve_daily_workflow(
        "不要記錄這個 setup，先刷新 PropW 帳本"
    ) == "provider_sync"
    assert resolve_daily_workflow(
        "不要同步帳本，復盤 T000025"
    ) == "single_trade_review"
    assert resolve_daily_workflow(
        "先不要幫我記錄那個錯過的 setup"
    ) is None

def test_negation_scope_allows_a_later_independent_writer():
    assert resolve_daily_workflow(
        "不要記錄沒下單的 Setup，幫我登記 BTC 交易"
    ) == "trade_registration"
    assert resolve_daily_workflow(
        "不要登記這筆交易，記一下這個沒進的 VT"
    ) == "setup_observation"
    assert resolve_daily_workflow(
        "請不要復盤 T000025"
    ) is None


def test_same_operation_conflicts_fail_closed():
    for prompt in (
        "不要登記這筆交易，幫我登記這筆交易",
        "不要記錄沒下單的 Setup，記一下這個沒進的 VT",
        "請不要復盤 T000025，幫我復盤 T000025",
        "不要保存交易，請幫我登記 BTC 交易",
        "不要記錄，幫我登記 BTC 交易",
        "幫我登記 BTC 交易，記一下這個沒進的 VT",
    ):
        assert resolve_daily_workflow(prompt) is None, prompt


def test_review_negation_does_not_block_independent_lookup_or_sync():
    assert resolve_daily_workflow(
        "請不要復盤 T000025，查 PropW 進出場"
    ) == "trade_lookup"
    assert resolve_daily_workflow(
        "請不要復盤 T000025，刷新 PropW 帳本"
    ) == "provider_sync"


def test_positive_registration_with_sync_retains_priority():
    assert resolve_daily_workflow(
        "幫我登記一筆 BTC 交易，先同步 PropW 帳本"
    ) == "trade_registration"


def test_backtest_request_after_negated_untraded_observation_remains_registration():
    assert resolve_daily_workflow(
        "不要記錄這個沒下單的 setup，幫我登記一筆回測交易"
    ) == "trade_registration"


def test_image_review_is_a_review_not_a_registration():
    for text in ("人工復盤這張圖", "歷史交易人工復盤", "幫我復盤這張圖", "復盤這幾張圖"):
        assert resolve_daily_workflow(text) == "single_trade_review", text


def test_register_next_routes_to_registration():
    for text in ("登記下一筆", "登記下一個", "幫我登記下一筆", "登記待登記的交易", "register next trade"):
        assert resolve_daily_workflow(text) == "trade_registration", text
    assert resolve_daily_workflow("不要登記下一筆") is None


def test_review_first_then_register_later_starts_with_review():
    for text in ("這筆先復盤後補登", "先復盤這筆，之後再補登"):
        assert resolve_daily_workflow(text) == "single_trade_review", text
    assert resolve_daily_workflow("補登這筆") == "trade_registration"
