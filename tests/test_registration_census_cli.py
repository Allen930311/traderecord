from __future__ import annotations

import json
import os
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


def _project(root: Path) -> Path:
    ledger_dir = root / "provider-ledgers" / "propw" / "normalized"
    ledger_dir.mkdir(parents=True)
    rows = [
        {"open_id": "open", "lifecycle": {"instrument": "BTC", "direction": "1", "entry_time": "2026-01-01T00:00:00Z", "entry_price": "100", "open_size": "1", "normalization_status": "open_only", "provider_lifecycle_phase": "OPEN", "is_closed": 0}},
        {"open_id": "closed", "lifecycle": {"instrument": "BTC", "direction": "1", "entry_time": "2026-02-01T00:00:00Z", "entry_price": "100", "exit_time": "2026-02-01T01:00:00Z", "weighted_exit_price": "101", "normalization_status": "ok", "provider_lifecycle_phase": "CLOSED", "open_size": "1", "closed_size": "1", "is_closed": 1, "is_partial": 0}},
    ]
    (ledger_dir / "trade-lifecycles.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    (root / "canonical" / "sequences").mkdir(parents=True)
    (root / "canonical" / "sequences" / "finalizations.json").write_text('{"records":{}}', encoding="utf-8")
    return root


def test_next_keeps_closed_default_and_explicit_phase_and_candidates_work(tmp_path: Path):
    project = _project(tmp_path / "project")
    project.mkdir(exist_ok=True)

    default = _run(project, "census", "--next")
    assert default.returncode == 0, default.stderr
    assert json.loads(default.stdout)["open_id"] == "closed"

    opened = _run(project, "census", "--next", "--phase", "open")
    assert opened.returncode == 0, opened.stderr
    assert json.loads(opened.stdout)["open_id"] == "open"

    candidates = _run(project, "census", "--candidates", "--phase", "all")
    assert candidates.returncode == 0, candidates.stderr
    assert [row["open_id"] for row in json.loads(candidates.stdout)] == ["open", "closed"]
