import json
from pathlib import Path

import pytest

from trading_os.adjudication import (
    apply_decision_file,
    reduce_decisions,
    validate_adjudication_authority,
)


def _decision(after: dict, decision_type: str = "strategy", **extra):
    return {
        "adjudication_version": "adjudication.v1",
        "adjudication_id": extra.pop("adjudication_id", "adj_test"),
        "subject_refs": ["src_test"],
        "decision_type": decision_type,
        "before": {},
        "after": after,
        "decision": "confirm",
        "reason": "test",
        "evidence_refs": [],
        "reviewer": "tester",
        "decided_at": extra.pop("decided_at", "2026-09-02T00:00:00Z"),
        "supersedes": extra.pop("supersedes", None),
        **extra,
    }


def test_strategy_adjudication_cannot_write_execution_or_outcome():
    decision = _decision({
        "strategy_id": "stg-example-b",
        "execution_status": "exited",
        "position_status": "closed",
        "outcome": "win",
    })
    paths = {error.path for error in validate_adjudication_authority(decision)}
    assert paths == {"after.execution_status", "after.position_status", "after.outcome"}


def test_reducer_filters_legacy_contamination_and_honours_supersedes():
    contaminated = _decision(
        {"strategy_id": "stg-vegas-tunnel", "outcome": "win", "type": "backtest"},
        adjudication_id="old",
        decided_at="2026-09-01T00:00:00Z",
    )
    correction = _decision(
        {"strategy_id": "stg-vegas-tunnel", "setup_tags": ["vegas_tunnel"]},
        adjudication_id="new",
        decided_at="2026-09-02T00:00:00Z",
        supersedes="old",
    )
    state = reduce_decisions([contaminated, correction])
    assert state["src_test"]["strategy_id"] == "stg-vegas-tunnel"
    assert state["src_test"]["setup_tags"] == ["vegas_tunnel"]
    assert "outcome" not in state["src_test"]
    assert "type" not in state["src_test"]
    assert state["src_test"]["last_adjudication_id"] == "new"


def test_apply_decision_file_rejects_cross_domain_patch(tmp_path: Path):
    source = tmp_path / "decision.json"
    source.write_text(json.dumps(_decision({"strategy_id": "stg-example-b", "outcome": "loss"})), encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        apply_decision_file(tmp_path, source)
