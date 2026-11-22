"""Boundary proof: Unified Capture V1 never touches B1/B2/B3/Trader DNA.

Phase 3 hardening for issue #42. The new capture surface
(``trading_os/unified_capture.py``, ``trading_os/evidence_store.py``,
``source_method``/``outcome_known_at_recording``/``confirm_backtest_entry``
on ``LifecycleStore``) is a bounded operational-capture enhancement layered
on the existing lifecycle. It must never regenerate, rewrite, or otherwise
mutate the immutable method-validation (B1/B1-v3/B2/B3) or Trader DNA
artifacts, and must never auto-create strategy registry entries.
"""

from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

from trading_os.evidence_store import EvidenceStore
from trading_os.operational_lifecycle import LifecycleStore
from trading_os.unified_capture import capture_backtest

REPO_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_IMPORTS = {
    "trading_os.method_validation",
    "trading_os.episode_identity",
    "trading_os.rule_freeze",
    "trading_os.performance_validation",
    "trading_os.trader_dna",
}


def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(f"trading_os.{node.module}" if node.level and node.module else node.module)
    return names


def test_new_capture_modules_do_not_import_method_validation_or_trader_dna() -> None:
    for relative in ("trading_os/unified_capture.py", "trading_os/evidence_store.py"):
        imported = _imported_module_names(REPO_ROOT / relative)
        overlap = imported & FORBIDDEN_IMPORTS
        assert not overlap, f"{relative} must not import {overlap}"


def _copy_operational_inputs(destination: Path) -> None:
    shutil.copytree(REPO_ROOT / "registry", destination / "registry")
    (destination / "canonical" / "sequences").mkdir(parents=True)
    (destination / "canonical" / "records" / "trades").mkdir(parents=True)
    (destination / "canonical" / "adjudications").mkdir(parents=True)
    (destination / "canonical" / "sequences" / "numbers.json").write_text(
        json.dumps({"live_next": 1, "backtest_next": 1, "allocated": [], "tombstones": []}) + "\n",
        encoding="utf-8",
    )
    (destination / "canonical" / "sequences" / "finalizations.json").write_text(
        json.dumps({"schema_version": "finalization-index.v1", "records": {}}) + "\n",
        encoding="utf-8",
    )


def _snapshot(paths: list[Path]) -> dict[str, list[str]]:
    return {
        str(path): sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file())
        for path in paths
        if path.exists()
    }


def test_capture_backtest_leaves_method_validation_and_trader_dna_artifacts_byte_identical(
    tmp_path: Path,
) -> None:
    watched_dirs = [
        REPO_ROOT / "exports" / "method-validation",
        REPO_ROOT / "exports" / "trader-dna" / "v1",
        REPO_ROOT / "registry",
    ]
    assert any(path.exists() for path in watched_dirs), "expected at least one watched artifact dir to exist"
    before = _snapshot(watched_dirs)

    _copy_operational_inputs(tmp_path)
    store = LifecycleStore(tmp_path)
    evidence_store = EvidenceStore(tmp_path)

    fixture_file = tmp_path / "chart.png"
    fixture_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"boundary-test-bytes")
    ingested = evidence_store.ingest(fixture_file)
    entry_evidence = {
        "source_ref": ingested["locator"],
        "sha256": ingested["sha256"],
        "timing": "execution",
        "type": "chart_snapshot",
        "category": "entry",
        "captured_at": "2026-06-01T14:00:00Z",
    }

    result = capture_backtest(
        store,
        strategy_id="stg-vegas-tunnel",
        rule_version="stg-vegas-tunnel:v1",
        symbol="BTCUSDT",
        direction="long",
        intended_entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        planned_rr=2.0,
        timeframe_context="1h",
        timeframe_trigger="15m",
        market_context="boundary-test fixture",
        setup_tags=["vegas_tunnel"],
        confirmation_tags=[],
        thesis="boundary-test thesis",
        chart_refs=["fixture://chart"],
        created_at="2026-06-01T13:55:00Z",
        source_method="tradingview_replay",
        entry_evidence=entry_evidence,
        actual_entry=100.0,
        execution_at="2026-06-01T14:00:00Z",
        run_id="boundary-run-1",
        parameters_ref="fixture://params",
        dataset_ref="fixture://dataset",
        data_period={"from": "2026-01-01", "to": "2026-06-01"},
    )
    assert result["trade_id"]

    after = _snapshot(watched_dirs)
    assert before == after, "capture_backtest must not add/remove files under watched B1/B2/B3/Trader DNA dirs"


def test_new_backtest_draft_never_auto_creates_a_registry_entry(tmp_path: Path) -> None:
    _copy_operational_inputs(tmp_path)
    registry_path = tmp_path / "registry" / "strategy-registry.yaml"
    before_text = registry_path.read_text(encoding="utf-8")
    store = LifecycleStore(tmp_path)
    evidence_store = EvidenceStore(tmp_path)

    fixture_file = tmp_path / "chart2.png"
    fixture_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"registry-boundary-bytes")
    ingested = evidence_store.ingest(fixture_file)
    entry_evidence = {
        "source_ref": ingested["locator"],
        "sha256": ingested["sha256"],
        "timing": "execution",
        "type": "chart_snapshot",
        "category": "entry",
        "captured_at": "2026-06-02T14:00:00Z",
    }
    capture_backtest(
        store,
        strategy_id="stg-vegas-tunnel",
        rule_version="stg-vegas-tunnel:v1",
        symbol="ETHUSDT",
        direction="short",
        intended_entry=200.0,
        stop_loss=210.0,
        take_profit=180.0,
        planned_rr=2.0,
        timeframe_context="1h",
        timeframe_trigger="15m",
        market_context="registry boundary fixture",
        setup_tags=["vegas_tunnel"],
        confirmation_tags=[],
        thesis="registry boundary thesis",
        chart_refs=["fixture://chart2"],
        created_at="2026-06-02T13:55:00Z",
        source_method="manual_backtest",
        entry_evidence=entry_evidence,
        actual_entry=200.0,
        execution_at="2026-06-02T14:00:00Z",
        run_id="boundary-run-2",
        parameters_ref="fixture://params2",
        dataset_ref="fixture://dataset2",
        data_period={"from": "2026-01-01", "to": "2026-06-02"},
    )
    after_text = registry_path.read_text(encoding="utf-8")
    assert before_text == after_text, "registry must not be mutated by a capture flow"
