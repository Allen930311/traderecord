import json
from pathlib import Path

import pytest

from trading_os.contracts import validate_adjudication, validate_record, validate_strategy


def base_record(**overrides):
    record = {
        "schema_version": "trade.v1", "record_id": "rec_00000000000000000000", "record_kind": "trade_candidate",
        "trade_id": None, "observation_id": None, "trade_no": None, "classification_status": "pending", "type": None,
        "execution_environment": "unknown", "recording_timing": "unknown", "legacy_source": {}, "review": {},
        "identity": {}, "strategy": {}, "plan": {}, "actual": {"execution_status": "planned", "position_status": "planned"},
        "result": {"outcome": "unknown"}, "evidence": [], "dedupe": {}, "analytics": {}, "raw_legacy": {}, "provenance": {},
    }
    record.update(overrides)
    return record


def test_pending_candidate_allows_unknown_type():
    assert validate_record(base_record()) == []


def test_record_identity_rejects_manifest_namespace():
    errors = validate_record(base_record(record_id="src_abc"))
    assert any(e.path == "record_id" for e in errors)


def test_observation_cannot_have_type_or_number():
    errors = validate_record(base_record(record_kind="observation", type="live"))
    assert any(e.path == "type" for e in errors)


def test_resolved_trade_requires_entry_and_id():
    errors = validate_record(base_record(record_kind="trade", classification_status="resolved", type="live"))
    assert {e.path for e in errors} >= {"trade_id", "actual.execution_status"}


def test_strategy_requires_machine_rules():
    errors = validate_strategy({"strategy_id": "stg-example-b", "classification": "independent_system", "status": "proposed", "rule_versions": [{}]})
    assert errors


def test_strategy_rejects_unstable_numeric_id():
    errors = validate_strategy({"strategy_id": "1", "classification": "component", "status": "proposed", "rule_versions": []})
    assert any(e.path == "strategy_id" for e in errors)


def test_adjudication_rejects_unsupported_decision_type():
    decision = {
        "adjudication_version": "adjudication.v1",
        "adjudication_id": "adj_test",
        "subject_refs": ["src_test"],
        "decision_type": "signal_occurrence",
        "before": {},
        "after": {},
        "decision": "confirm",
        "reason": "test",
        "evidence_refs": [],
        "reviewer": "tester",
        "decided_at": "2026-09-01T00:00:00+08:00",
        "supersedes": None,
    }
    assert any(e.path == "decision_type" for e in validate_adjudication(decision))


def test_all_v1_durable_adjudication_json_files_validate():
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "canonical" / "adjudications").glob("adj_*.json"))
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        errors = validate_adjudication(payload)
        assert errors == [], f"{path.name}: {errors}"
