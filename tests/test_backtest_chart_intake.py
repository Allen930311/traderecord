"""Agent-owned visual interpretation is separate from canonical Writer claims."""
from copy import deepcopy

import pytest

from trading_os.backtest_chart_intake import (
    ChartIntakeError,
    prepare_backtest_chart_intake,
)


def _sample():
    return {
        "schema_version": "backtest-chart-intake.v1",
        "strategy": {"strategy_id": "stg-example-a", "rule_version": "stg-example-a:v1", "display_name": "AA 策略"},
        "mode": "BACKTEST",
        "source_method": "tradingview_replay",
        "direction": "long",
        "owner_plan_variant": "A",
        "owner_verbatim": "亞盤前出現mss 且後續走高 故等待亞盤結束高低點確認後價格回到0.618進場 出場抓前高",
        "images": [
            {"role": "entry_chart", "ref": "github://entry.png"},
            {"role": "exit_chart", "ref": "github://exit.png"},
        ],
        "prices": {
            "entry": {"value": 30813.1, "provenance": "chart_annotation", "evidence_ref": "github://entry.png"},
            "stop": {"value": 30747.1, "provenance": "chart_annotation", "evidence_ref": "github://entry.png"},
            "target": {"value": 31034.1, "provenance": "chart_annotation", "evidence_ref": "github://exit.png"},
        },
        "simulated_entry_at": None,
        "outcome": "target_area_reached_on_chart_not_execution_proof",
    }


def test_aa_agent_template_uses_chart_without_making_fake_execution():
    result = prepare_backtest_chart_intake(_sample())
    assert result["planned_rr_from_chart"] == "3.348"
    assert result["price_quality"] == "chart_derived"
    assert result["simulated_entry_time_status"] == "unknown"
    assert result["actual_exit_verified"] is False
    assert result["canonical_bt_id"] is None
    assert result["performance_counted"] is False
    assert result["owner_plan_variant"] == "A"
    assert result["owner_verbatim"] == _sample()["owner_verbatim"]


def test_invalid_chart_reference_fails():
    sample = _sample()
    sample["prices"]["entry"]["evidence_ref"] = "github://unattached.png"
    with pytest.raises(ChartIntakeError, match="attached original"):
        prepare_backtest_chart_intake(sample)


def test_unknown_prices_are_preserved_not_guessed():
    sample = _sample()
    sample["prices"]["entry"] = {"value": None, "provenance": "unknown", "evidence_ref": None}
    result = prepare_backtest_chart_intake(sample)
    assert result["planned_rr_from_chart"] is None


def test_impossible_rr_rejected():
    sample = _sample()
    sample["prices"]["stop"]["value"] = 31000
    with pytest.raises(ChartIntakeError, match="contradict"):
        prepare_backtest_chart_intake(sample)


def test_missing_time_is_allowed_but_fake_iso_date_is_rejected():
    sample = _sample()
    assert prepare_backtest_chart_intake(sample)["simulated_entry_at"] is None
    sample["simulated_entry_at"] = "2026-10-09"
    with pytest.raises(ChartIntakeError, match="ISO datetime"):
        prepare_backtest_chart_intake(sample)


def test_no_live_capture():
    sample = _sample()
    sample["mode"] = "LIVE"
    with pytest.raises(ChartIntakeError, match="BACKTEST only"):
        prepare_backtest_chart_intake(sample)


def test_unknown_price_cannot_hide_non_null_fill():
    sample = _sample()
    sample["prices"]["entry"]["provenance"] = "unknown"
    with pytest.raises(ChartIntakeError, match="unknown cannot carry"):
        prepare_backtest_chart_intake(sample)


def test_aa_remains_distinct_from_avf():
    out = prepare_backtest_chart_intake(_sample())
    assert out["strategy"]["strategy_id"] == "stg-example-a"
    assert out["strategy"]["rule_version"] == "stg-example-a:v1"
