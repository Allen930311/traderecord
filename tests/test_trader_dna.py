from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_os.trader_dna import (
    B1_DATASET_SHA256,
    B1_EPISODE_RESOLUTION_SHA256,
    B1_MANIFEST_DIGEST,
    B2_MANIFEST_DIGEST,
    B2_PERFORMANCE_FILE_SHA256,
    B3_MANIFEST_DIGEST,
    TRADER_DNA_SCHEMA_VERSION,
    TraderDNAError,
    _chronology_quality,
    _date_key,
    run_trader_dna,
    validate_trader_dna,
)


ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA = "9adc9342e8ae721077a73e4b8c2f882d890fec7d"


def _generated(root: Path, tmp_path: Path) -> dict:
    return run_trader_dna(
        root,
        baseline_sha=BASELINE_SHA,
        analysis_date="2026-09-11",
        output_dir=tmp_path / "trader-dna" / "v1",
    )


def test_trader_dna_has_exact_provenance_pins_and_coverage(tmp_path):
    manifest = _generated(ROOT, tmp_path)
    payload = json.loads((tmp_path / "trader-dna" / "v1" / "trader-dna.json").read_text(encoding="utf-8"))
    pins = payload["identity"]["source_version_pins"]
    assert payload["schema_version"] == TRADER_DNA_SCHEMA_VERSION
    assert pins["trading_record"]["fresh_baseline_sha"] == BASELINE_SHA
    assert pins["b1_v3"]["dataset_sha256"] == B1_DATASET_SHA256
    assert pins["b1_v3"]["manifest_digest"] == B1_MANIFEST_DIGEST
    assert pins["b1_v3"]["episode_resolution_sha256"] == B1_EPISODE_RESOLUTION_SHA256
    assert pins["b3_v1"]["manifest_digest"] == B3_MANIFEST_DIGEST
    assert pins["b2_v1"]["manifest_digest"] == B2_MANIFEST_DIGEST
    assert pins["b2_v1"]["performance_artifact_sha256"] == B2_PERFORMANCE_FILE_SHA256
    assert manifest["evidence_coverage"]["analyzed_episodes"] == 133
    assert manifest["optimization_performed"] is False


def test_trader_dna_dimensions_preserve_claim_classes_and_evidence(tmp_path):
    _generated(ROOT, tmp_path)
    payload = json.loads((tmp_path / "trader-dna" / "v1" / "trader-dna.json").read_text(encoding="utf-8"))
    assert validate_trader_dna(payload) == []
    assert {item["evidence_class"] for item in payload["dimensions"]} <= {"fact", "descriptive_pattern", "hypothesis"}
    supported = [item for item in payload["dimensions"] if item["status"] == "supported"]
    assert supported
    assert all(item["supporting_artifacts"] for item in supported)
    assert all(item["optimization_implication"] is None for item in payload["dimensions"])


def test_chronology_quality_downgrades_historical_path_claim(tmp_path):
    _generated(ROOT, tmp_path)
    payload = json.loads((tmp_path / "trader-dna" / "v1" / "trader-dna.json").read_text(encoding="utf-8"))
    quality = payload["chronology_quality"]
    assert quality == payload["evidence_coverage"]["chronology_quality"]
    assert quality["event_date_complete"] is True
    assert quality["time_entry_known"] == 16
    assert quality["time_entry_missing"] == 117
    assert quality["intraday_order_recoverable"] is False
    finding = next(item for item in payload["dimensions"] if item["dimension_id"] == "historical_drawdown_and_streak")
    assert finding["status"] == "mixed"
    assert finding["evidence_class"] == "descriptive_pattern"
    assert finding["observability"] == "partially_observable"
    assert "not an exact execution or intraday recovery path" in finding["claim"]
    assert any("Same-day order-sensitive" in limitation for limitation in finding["limitations"])


def test_same_day_missing_time_uses_deterministic_tiebreak_without_claiming_chronology():
    rows = [
        {"event_date": "2026-01-01", "time_entry": None, "source_path": "b.md", "step0_record_id": "2"},
        {"event_date": "2026-01-01", "time_entry": None, "source_path": "a.md", "step0_record_id": "1"},
        {"event_date": "2026-01-01", "time_entry": "09:00", "source_path": "c.md", "step0_record_id": "3"},
    ]
    quality = _chronology_quality(rows)
    ordered_ids = [row["step0_record_id"] for row in sorted(rows, key=_date_key)]
    assert ordered_ids == ["3", "1", "2"]
    assert quality["time_entry_known"] == 1
    assert quality["time_entry_missing"] == 2
    assert quality["intraday_order_recoverable"] is False
    assert quality["ordering_contract"] == "event_date_then_known_time_then_stable_tiebreak"


def test_small_samples_are_insufficient_and_not_a_comparative_order(tmp_path):
    _generated(ROOT, tmp_path)
    payload = json.loads((tmp_path / "trader-dna" / "v1" / "trader-dna.json").read_text(encoding="utf-8"))
    maturity = next(item for item in payload["dimensions"] if item["dimension_id"] == "strategy_evidence_maturity")
    values = maturity["supporting_metrics"][0]["value"]
    assert values["stg-po3"]["n"] == 8
    assert values["stg-po3"]["b2_confidence"] == "insufficient"
    assert values["stg-triple-tap"]["n"] == 2
    assert values["stg-triple-tap"]["b2_confidence"] == "insufficient"
    assert payload["guardrails"]["comparative_ordering_performed"] is False
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True).lower()
    assert "best_strategy" not in serialized
    assert "top_strategy" not in serialized
    assert "winner" not in serialized


def test_unsupported_dimensions_are_not_observable(tmp_path):
    _generated(ROOT, tmp_path)
    payload = json.loads((tmp_path / "trader-dna" / "v1" / "trader-dna.json").read_text(encoding="utf-8"))
    dimensions = {item["dimension_id"]: item for item in payload["dimensions"]}
    for dimension_id in (
        "setup_confirmation_observability",
        "risk_sizing_observability",
        "holding_duration_and_session",
        "psychological_state_and_discipline",
        "method_vs_execution_loss_attribution",
    ):
        assert dimensions[dimension_id]["status"] == "not_observable"
        assert dimensions[dimension_id]["observability"] == "not_observable"


def test_deterministic_artifacts_are_byte_identical(tmp_path):
    first = tmp_path / "first" / "v1"
    second = tmp_path / "second" / "v1"
    run_trader_dna(ROOT, baseline_sha=BASELINE_SHA, analysis_date="2026-09-11", output_dir=first)
    run_trader_dna(ROOT, baseline_sha=BASELINE_SHA, analysis_date="2026-09-11", output_dir=second)
    names = ("README.md", "manifest.json", "trader-dna.json", "trader-dna.md", "improvement-hypotheses.md")
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_invalid_baseline_fails_closed(tmp_path):
    with pytest.raises(TraderDNAError, match="baseline_sha"):
        run_trader_dna(ROOT, baseline_sha="not-a-sha", analysis_date="2026-09-11", output_dir=tmp_path / "v1")


def test_human_report_is_self_contained_for_strategy_cohorts(tmp_path):
    _generated(ROOT, tmp_path)
    report = (tmp_path / "trader-dna" / "v1" / "trader-dna.md").read_text(encoding="utf-8")
    assert "see B2" not in report
    assert "| Vegas Tunnel | 82 | 1.507 | 1.397 |" in report
    assert "| PO3 / Power of 3 | 8 | 3.417 | 1.856 |" in report
