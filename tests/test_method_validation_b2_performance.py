from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from trading_os.performance_validation import (
    ACTIVE_STRATEGY_IDS,
    B1_DATASET_PATH,
    B1_DATASET_SHA256,
    B1_EPISODE_RESOLUTION_PATH,
    B1_EPISODE_RESOLUTION_SHA256,
    B1_MANIFEST_DIGEST,
    B1_MANIFEST_FILE_SHA256,
    B1_V2_ANCESTOR_DATASET_SHA256,
    B1_V2_ANCESTOR_MANIFEST_DIGEST,
    B2Error,
    B3_MANIFEST_DIGEST,
    PERFORMANCE_SCHEMA_VERSION,
    _curve_and_drawdown,
    _metric_summary,
    _select_rows,
    run_b2_validation,
    validate_b2_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
B2_DIR = ROOT / "exports" / "method-validation" / "b2"
BASELINE_SHA = "93353b368fed06e457f9b15dfeef4a4ada6d5168"
ANALYSIS_DATE = "2026-09-11"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_row(
    trade_ref: str,
    realized_r: float,
    outcome: str,
    event_date: str,
    *,
    instrument: str = "EURUSD",
    time_entry: str | None = "09:00",
) -> dict[str, object]:
    return {
        "trade_ref": trade_ref,
        "canonical_realized_r": realized_r,
        "outcome": outcome,
        "event_date": event_date,
        "time_entry": time_entry,
        "instrument": instrument,
        "sample_origin": "backtest",
        "source_path": f"source/{trade_ref}.md",
        "step0_record_id": f"step0-{trade_ref}",
        "analysis_eligibility": "eligible",
        "strategy_id": "stg-vegas-tunnel",
        "strategy_status": "active",
        "canonical_episode_id": f"episode-{trade_ref}",
        "canonical_representative_trade_ref": trade_ref,
        "episode_role": "primary",
        "episode_resolution": "distinct",
        "performance_counting_eligibility": True,
        "performance_exclusion_reason": None,
    }


def test_committed_b2_artifacts_are_pinned_valid_and_deterministic(tmp_path: Path):
    manifest = json.loads((B2_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert validate_b2_manifest(manifest, project_root=ROOT, output_dir=B2_DIR) == []
    regenerated = run_b2_validation(
        ROOT,
        output_dir=tmp_path / "b2-regenerated",
        baseline_sha=BASELINE_SHA,
        analysis_date=ANALYSIS_DATE,
    )
    assert regenerated == manifest
    assert manifest["baseline_sha"] == BASELINE_SHA
    assert manifest["b1_pin"] == {
        "dataset_version": "method-validation-b1.v3",
        "dataset_sha256": B1_DATASET_SHA256,
        "manifest_digest": B1_MANIFEST_DIGEST,
        "episode_resolution_path": B1_EPISODE_RESOLUTION_PATH,
        "episode_resolution_sha256": B1_EPISODE_RESOLUTION_SHA256,
    }
    assert manifest["b3_pin"]["manifest_digest"] == B3_MANIFEST_DIGEST
    assert manifest["active_strategy_ids"] == list(ACTIVE_STRATEGY_IDS)
    assert manifest["inputs"]["b1"]["manifest_file_sha256"] == B1_MANIFEST_FILE_SHA256
    assert manifest["inputs"]["b1"]["b1_v2_ancestor_pin"]["dataset_sha256"] == B1_V2_ANCESTOR_DATASET_SHA256
    assert manifest["inputs"]["b1"]["b1_v2_ancestor_pin"]["manifest_digest"] == B1_V2_ANCESTOR_MANIFEST_DIGEST
    assert manifest["universe"]["b2_analyzed_rows"] == 133
    assert manifest["universe"]["b2_analyzed_by_strategy"] == {
        "stg-vegas-tunnel": 82,
        "stg-po3": 8,
        "stg-ict-liquidity": 41,
        "stg-triple-tap": 2,
    }
    assert manifest["universe"]["b2_analyzed_unique_episodes"] == manifest["universe"]["b2_analyzed_rows"]
    assert manifest["observation_identity"]["b1_v3_countable_unique_episodes"] == 181
    assert manifest["observation_identity"]["unresolved_rows_excluded"] == 17
    assert manifest["negative_guarantees"]["optimization_performed"] is False


def test_b2_cli_smoke_is_machine_readable(tmp_path: Path):
    output_dir = tmp_path / "b2"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trading_os",
            "b2-validate",
            "--baseline-sha",
            BASELINE_SHA,
            "--analysis-date",
            ANALYSIS_DATE,
            "--output-dir",
            str(output_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["schema_version"] == "method-validation-b2.v1"
    assert output["analyzed_rows"] == 133
    assert output["manifest_digest"] == json.loads(
        (output_dir / "manifest.json").read_text(encoding="utf-8")
    )["manifest_digest"]


def test_metric_formulas_cover_distribution_profit_factor_streaks_and_drawdown():
    rows = [
        _fixture_row("a", 1.0, "win", "2026-01-01", instrument="EURUSD"),
        _fixture_row("b", -0.5, "loss", "2026-01-02", instrument="EURUSD"),
        _fixture_row("c", 0.0, "breakeven", "2026-01-03", instrument="GBPUSD"),
        _fixture_row("d", 2.0, "win", "2026-01-04", instrument="USDJPY"),
        _fixture_row("e", -1.0, "loss", "2026-01-05", instrument="USDJPY"),
    ]
    summary = _metric_summary(rows)
    assert summary["n"] == 5
    assert summary["win_rate"] == 0.4
    assert summary["loss_rate"] == 0.4
    assert summary["breakeven_rate"] == 0.2
    assert summary["total_r"] == 1.5
    assert summary["mean_r"] == 0.3
    assert summary["median_r"] == 0.0
    assert summary["q25_r"] == -0.5
    assert summary["q75_r"] == 1.0
    assert summary["gross_positive_r"] == 3.0
    assert summary["gross_negative_r"] == -1.5
    assert summary["profit_factor_r"] == 2.0
    assert summary["profit_factor_status"] == "finite"

    curve, drawdown = _curve_and_drawdown(rows)
    assert [item["cumulative_r"] for item in curve] == [1.0, 0.5, 0.5, 2.5, 1.5]
    assert drawdown == {
        "final_cumulative_r": 1.5,
        "peak_cumulative_r": 2.5,
        "max_cumulative_r_drawdown": 1.0,
    }


def test_profit_factor_is_null_when_no_negative_r_and_no_nan_is_emitted():
    summary = _metric_summary(
        [
            _fixture_row("a", 1.0, "win", "2026-01-01"),
            _fixture_row("b", 0.0, "breakeven", "2026-01-02"),
        ]
    )
    assert summary["gross_negative_r"] == 0.0
    assert summary["profit_factor_r"] is None
    assert summary["profit_factor_status"] == "no_negative_r"


def test_missing_and_invalid_canonical_r_fail_closed_at_selection_boundary():
    row = _fixture_row("missing-r", 0.0, "win", "2026-01-01")
    row.pop("canonical_realized_r")
    with pytest.raises(B2Error, match="canonical_realized_r"):
        _select_rows([row])


def test_duplicate_countable_episode_ids_fail_closed_at_selection_boundary():
    first = _fixture_row("first", 1.0, "win", "2026-01-01")
    second = _fixture_row("second", -1.0, "loss", "2026-01-02")
    second["canonical_episode_id"] = first["canonical_episode_id"]
    with pytest.raises(B2Error, match="duplicate performance-countable"):
        _select_rows([first, second])


def test_missing_time_is_retained_but_sorted_after_valid_time():
    rows = [
        _fixture_row("missing", 1.0, "win", "2026-01-01", time_entry=None),
        _fixture_row("valid", -1.0, "loss", "2026-01-01", time_entry="08:00"),
    ]
    curve, _ = _curve_and_drawdown(rows)
    assert [item["trade_ref"] for item in curve] == ["valid", "missing"]


def test_b2_generation_preserves_all_immutable_inputs(tmp_path: Path):
    protected = {
        relative: _sha256(ROOT / relative)
        for relative in (
            B1_DATASET_PATH,
            "exports/method-validation/b1/canonical-trade-dataset.jsonl",
            "exports/method-validation/b1/manifest.json",
            "exports/method-validation/b1/README.md",
            "exports/method-validation/b3/manifest.json",
            "exports/strategy-discovery/step-0/corpus-manifest.json",
            "exports/strategy-discovery/step-0/step-0-results.jsonl",
            "registry/strategy-registry.yaml",
            "registry/versions/stg-vegas-tunnel-v1.yaml",
            "registry/versions/stg-po3-v1.yaml",
            "registry/versions/stg-ict-liquidity-v1.yaml",
            "registry/versions/stg-triple-tap-v1.yaml",
        )
    }
    run_b2_validation(
        ROOT,
        output_dir=tmp_path / "b2",
        baseline_sha=BASELINE_SHA,
        analysis_date=ANALYSIS_DATE,
    )
    assert {relative: _sha256(ROOT / relative) for relative in protected} == protected


def test_manifest_rejects_tampered_optimization_claim(tmp_path: Path):
    manifest = run_b2_validation(
        ROOT,
        output_dir=tmp_path / "b2",
        baseline_sha=BASELINE_SHA,
        analysis_date=ANALYSIS_DATE,
    )
    tampered = deepcopy(manifest)
    tampered["optimization_performed"] = True
    tampered["negative_guarantees"]["optimization_performed"] = True
    errors = validate_b2_manifest(tampered)
    assert "manifest_digest" in errors
    assert "negative_guarantees" in errors
    assert "optimization_performed" in errors


def test_performance_artifact_contains_all_required_strategy_metrics_and_observability():
    performance = json.loads(
        (B2_DIR / "strategy-performance.json").read_text(encoding="utf-8")
    )
    assert performance["schema_version"] == PERFORMANCE_SCHEMA_VERSION
    assert [item["strategy_id"] for item in performance["strategies"]] == list(ACTIVE_STRATEGY_IDS)
    for strategy in performance["strategies"]:
        assert set(strategy) >= {
            "sample",
            "segments",
            "live_backtest_delta",
            "missingness",
            "temporal",
            "market_concentration",
            "positive_r_concentration",
            "rule_observability",
            "confidence",
            "assessment",
            "chronological_cumulative_r",
        }
        assert "rank" not in strategy
        assert all(
            item["status"] in {"machine_observable", "NOT_MACHINE_OBSERVABLE"}
            for item in strategy["rule_observability"]
        )
    assert performance["b1_pin"]["dataset_version"] == "method-validation-b1.v3"
    assert performance["b1_pin"]["episode_resolution_sha256"] == B1_EPISODE_RESOLUTION_SHA256
    assert performance["observation_identity"]["b2_analyzed_unique_episodes"] == 133
    by_id = {item["strategy_id"]: item for item in performance["strategies"]}
    assert by_id["stg-vegas-tunnel"]["confidence"]["classification"] == "moderate"
    assert by_id["stg-po3"]["confidence"]["classification"] == "insufficient"
    assert by_id["stg-triple-tap"]["segments"]["overall"]["profit_factor_r"] is None
