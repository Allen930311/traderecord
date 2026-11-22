from __future__ import annotations

from resolver_helpers import MATCHING_OBSERVATIONS_FILE
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _copy_registry(source: Path, destination: Path) -> None:
    shutil.copytree(source / "registry", destination / "registry")
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


def _run_cli(root: Path, *args: str) -> dict:
    source = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "trading_os", *args],
        cwd=root, env=env, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def test_cli_evidence_ingest_is_standalone(tmp_path: Path) -> None:
    png = tmp_path / "chart.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    result = _run_cli(tmp_path, "evidence", "ingest", "--file", str(png))
    assert result["media_type"] == "image/png"
    assert result["deduped"] is False
    artifact = tmp_path / "canonical" / "evidence" / "artifacts" / f"{result['sha256']}.png"
    assert artifact.exists()


def test_cli_evidence_ingest_attaches_to_draft(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    draft_result = _run_cli(
        tmp_path, "capture", "draft",
        "--strategy-observations-file", MATCHING_OBSERVATIONS_FILE,
        "--symbol", "BTCUSDT", "--direction", "long", "--entry", "100", "--sl", "95",
        "--tp", "110", "--planned-rr", "2", "--timeframe-context", "1h",
        "--timeframe-trigger", "15m", "--market-context", "cli fixture",
        "--thesis", "cli fixture thesis", "--created-at", "2026-09-11T00:00:00Z",
    )
    draft_id = draft_result["draft_id"]
    png = tmp_path / "chart.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x01" * 16)
    result = _run_cli(
        tmp_path, "evidence", "ingest", "--file", str(png),
        "--draft-id", draft_id, "--category", "pre_trade_context", "--timing", "pre_trade",
        "--type", "chart_snapshot",
    )
    assert result["draft_id"] == draft_id
    assert result["evidence_event_id"]
    draft_path = tmp_path / "canonical" / "drafts" / f"{draft_id}.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    evidence = draft["evidence"][0]
    assert evidence["sha256"] == result["sha256"]
    assert evidence["media_type"] == "image/png"
    assert evidence["category"] == "pre_trade_context"
    # The durable locator returned by the evidence store must round-trip
    # into the attached evidence entry exactly -- never silently fall back
    # to LifecycleStore.attach_evidence's own "artifact:full" default.
    assert evidence["locator"] == result["locator"]
    assert evidence["locator"] != "artifact:full"

    # Re-ingesting the identical file for the same draft+category is
    # idempotent: no duplicate evidence entry is created.
    result_again = _run_cli(
        tmp_path, "evidence", "ingest", "--file", str(png),
        "--draft-id", draft_id, "--category", "pre_trade_context", "--timing", "pre_trade",
        "--type", "chart_snapshot",
    )
    assert result_again["evidence_event_id"] == result["evidence_event_id"]
    draft_again = json.loads(draft_path.read_text(encoding="utf-8"))
    assert len(draft_again["evidence"]) == 1
