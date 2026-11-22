import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _run(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    source_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    return subprocess.run(
        [sys.executable, "-m", "trading_os", *args],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _backtest_candidate() -> dict:
    return {
        "schema_version": "trade.v1",
        "record_id": "rec_11111111111111111111",
        "record_kind": "trade_candidate",
        "trade_id": None,
        "observation_id": None,
        "trade_no": None,
        "classification_status": "resolved",
        "type": "backtest",
        "execution_environment": "paper",
        "recording_timing": "contemporaneous",
        "legacy_source": {"path": "trades/cli.md"},
        "identity": {"direction": "long", "instrument": {"symbol": "BTCUSDT"}},
        "strategy": {"strategy_id": "stg-example-b", "rule_version": "stg-example-b:v1", "variant": "cli"},
        "plan": {"timeframe_trigger": "15m"},
        "actual": {
            "entry_price": 100,
            "execution_status": "entered",
            "position_status": "open",
            "execution_verification": {
                "schema_version": "execution-verification.v1",
                "entry": {
                    "state": "confirmed",
                    "basis": "simulator_run",
                    "attestation_support": None,
                    "evidence_refs": ["datasets/cli.csv"],
                    "adjudication_revision_ref": None,
                },
                "exit": {
                    "state": "not_applicable",
                    "basis": "none",
                    "attestation_support": None,
                    "evidence_refs": [],
                    "adjudication_revision_ref": None,
                },
            },
            "execution_evidence": {
                "entry": {
                    "simulator_run": {
                        "evidence_kind": "simulator_run",
                        "run_id": "run-cli-1",
                        "source_ref": "datasets/cli.csv",
                        "instrument": "BTCUSDT",
                        "side": "long",
                        "parameters_ref": "params/cli.json",
                        "dataset_ref": "datasets/cli.csv",
                        "simulated_execution": True,
                        "simulated_at": "2026-09-02T00:00:00Z",
                        "simulated_price": 100,
                        "data_period": {"from": "2026-01-01", "to": "2026-09-01"},
                    }
                }
            },
        },
        "result": {"outcome": "unknown"},
        "evidence": [{
            "source_ref": "trades/cli.md",
            "method": "manual_note",
            "timing": "pre_trade",
            "locator": "body:1",
            "sha256": "abc",
            "field": "plan.timeframe_trigger",
            "assertion": "timeframe_gte",
            "assertion_value": "15m",
        }],
        "review": {},
        "dedupe": {"status": "canonical"},
        "analytics": {},
        "raw_legacy": {},
        "provenance": {},
    }


def test_public_adjudicate_finalize_and_audit_cli(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "canonical" / "drafts").mkdir(parents=True)
    (project / "canonical" / "records" / "trades").mkdir(parents=True)
    (project / "canonical" / "sequences").mkdir(parents=True)
    (project / "canonical" / "adjudications").mkdir(parents=True)
    source_root = Path(__file__).resolve().parents[1]
    shutil.copytree(source_root / "registry", project / "registry")
    candidate = _backtest_candidate()
    (project / "canonical" / "drafts" / "rec_11111111111111111111.json").write_text(json.dumps(candidate), encoding="utf-8")

    decision = {
        "adjudication_version": "adjudication.v1",
        "adjudication_id": "adj_cli_type",
        "subject_refs": ["rec_11111111111111111111"],
        "decision_type": "type",
        "before": {"type": None},
        "after": {"type": "backtest"},
        "decision": "confirm",
        "reason": "test",
        "evidence_refs": [],
        "reviewer": "tester",
        "decided_at": "2026-09-02T00:00:00Z",
        "supersedes": None,
    }
    decision_path = project / "decision.json"
    decision_path.write_text(json.dumps(decision), encoding="utf-8")
    applied = _run(project, "adjudicate", "apply", str(decision_path))
    assert applied.returncode == 0, applied.stderr
    assert applied.stdout.strip() == "adj_cli_type"

    finalized = _run(project, "finalize", "rec_11111111111111111111", "--reviewer", "tester")
    retried = _run(project, "finalize", "rec_11111111111111111111", "--reviewer", "tester")
    assert finalized.returncode == 0, finalized.stderr
    assert retried.returncode == 0, retried.stderr
    assert finalized.stdout.strip().startswith("trd_")
    assert retried.stdout.strip() == finalized.stdout.strip()
    assert len(list((project / "canonical" / "records" / "trades").glob("*.json"))) == 1

    audited = _run(
        project,
        "audit",
        finalized.stdout.strip(),
        "--strategy",
        "stg-example-b",
        "--version",
        "stg-example-b:v1",
    )
    assert audited.returncode == 0, audited.stderr
    report = json.loads(audited.stdout)
    assert report["required_rule_compliance"]["passed"] == 1
    assert report["required_rule_compliance"]["unknown"] == 0
