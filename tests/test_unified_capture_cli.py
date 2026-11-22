"""CLI-layer tests for the Unified Capture v1 owner-facing subcommands.

These exercise the real ``python -m trading_os capture backtest / live /
retrospective-live`` argparse entrypoints (subprocess), not the Python
facade functions directly -- see trading_os/unified_capture.py for those
(already covered by tests/test_correction_pass_blockers.py).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from resolver_helpers import MATCHING_OBSERVATIONS_FILE
from pathlib import Path


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


def _run_cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    source = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "trading_os", *args],
        cwd=root, env=env, capture_output=True, text=True, timeout=20,
    )


def _png_bytes(tag: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + tag * 8


def _webp_bytes(tag: bytes) -> bytes:
    # Minimal RIFF/WEBP header the evidence store's magic sniffer recognizes
    # (see EvidenceStore._detect_media_type / _MAGIC_SNIFFERS in
    # trading_os/evidence_store.py): "RIFF" .... "WEBP".
    return b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"VP8 " + tag * 8


_BASE_BACKTEST_ARGS = [
    "capture", "backtest",
    "--strategy-id", "stg-vegas-tunnel", "--rule-version", "stg-vegas-tunnel:v1",
    "--symbol", "BTCUSDT", "--direction", "long",
    "--entry", "100", "--sl", "95", "--tp", "110", "--planned-rr", "2",
    "--timeframe-context", "1h", "--timeframe-trigger", "15m",
    "--market-context", "cli fixture", "--thesis", "cli fixture thesis",
    "--created-at", "2026-09-12T00:10:00Z", "--execution-at", "2026-09-12T00:10:30Z",
    "--source-method", "live_market_unexecuted",
]


def test_capture_backtest_cli_with_png_evidence_succeeds(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    png = tmp_path / "chart.png"
    png.write_bytes(_png_bytes(b"\x01"))

    result = _run_cli(tmp_path, *_BASE_BACKTEST_ARGS, "--evidence-file", str(png))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["trade_id"].startswith("trd_")
    assert payload["trade_no"].startswith("BT")

    trade_files = list((tmp_path / "canonical" / "records" / "trades").glob("*.json"))
    assert len(trade_files) == 1
    trade = json.loads(trade_files[0].read_text(encoding="utf-8"))
    entry_evidence_entry = next(e for e in trade["evidence"] if e["category"] == "entry")
    assert entry_evidence_entry["locator"] != "artifact:full"
    assert entry_evidence_entry["locator"].startswith("artifact:")
    assert entry_evidence_entry["media_type"] == "image/png"


def test_capture_backtest_cli_with_webp_evidence_succeeds(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    webp = tmp_path / "chart.webp"
    webp.write_bytes(_webp_bytes(b"\x02"))

    result = _run_cli(
        tmp_path, *_BASE_BACKTEST_ARGS,
        "--draft-id", "rec_" + "2" * 20,
        "--evidence-file", str(webp),
    )
    assert result.returncode == 0, result.stderr
    trade_files = list((tmp_path / "canonical" / "records" / "trades").glob("*.json"))
    assert len(trade_files) == 1
    trade = json.loads(trade_files[0].read_text(encoding="utf-8"))
    entry_evidence_entry = next(e for e in trade["evidence"] if e["category"] == "entry")
    assert entry_evidence_entry["media_type"] == "image/webp"
    assert entry_evidence_entry["locator"] != "artifact:full"


def test_capture_retrospective_live_cli_accepts_unknown_plan_fields(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    png = tmp_path / "fill.png"
    png.write_bytes(_png_bytes(b"\x03"))

    result = _run_cli(
        tmp_path, "capture", "retrospective-live",
        "--strategy-observations-file", MATCHING_OBSERVATIONS_FILE,
        "--symbol", "BTCUSDT", "--direction", "long",
        "--entry", "100", "--sl", "95",
        "--tp", "unknown", "--planned-rr", "not_observable",
        "--timeframe-context", "1h", "--timeframe-trigger", "15m",
        "--market-context", "late-reported real trade", "--thesis", "not_observable",
        "--created-at", "2026-06-01T13:55:00Z",
        "--provider-evidence-file", str(png),
        "--provider", "propw", "--provider-lifecycle-id", "late-open-1",
        "--provider-execution-at", "2026-06-01T14:00:00Z",
        "--candidate-source", "fixture://late-candidate-match",
        "--actual-entry", "100",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    trade_id = payload["trade_id"]
    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{trade_id}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))
    assert trade["recording_timing"] == "retrospective"
    assert trade["plan"]["take_profit_price"] == "unknown"
    assert trade["plan"]["rr_pretrade"] == "not_observable"
    assert trade["plan"]["thesis"] == "not_observable"
    assert trade["actual"]["entry_price"] == 100.0


def test_retrospective_replay_cli_preserves_capture_provenance_and_market_interval(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    fill = tmp_path / "replay-fill.png"
    fill.write_bytes(_png_bytes(b"\x03"))
    chart = tmp_path / "historical-replay.png"
    chart.write_bytes(_png_bytes(b"\x08"))
    result = _run_cli(
        tmp_path,
        "capture", "retrospective-live",
        "--strategy-observations-file", MATCHING_OBSERVATIONS_FILE,
        "--symbol", "BTCUSDT", "--direction", "long",
        "--entry", "100", "--sl", "95", "--tp", "110", "--planned-rr", "2",
        "--timeframe-context", "1h", "--timeframe-trigger", "15m",
        "--market-context", "historical replay fixture", "--thesis", "historical replay fixture",
        "--created-at", "2026-06-01T13:55:00Z",
        "--provider-evidence-file", str(fill),
        "--provider", "propw", "--provider-lifecycle-id", "replay-open-1",
        "--provider-execution-at", "2026-06-01T14:00:00Z",
        "--candidate-source", "fixture://replay-candidate-match",
        "--actual-entry", "100",
        "--chart-file", str(chart),
        "--chart-capture-mode", "retrospective_replay",
        "--chart-captured-at", "2026-10-08T04:00:00Z",
        "--chart-represented-market-start", "2026-06-01T13:30:00Z",
        "--chart-represented-market-end", "2026-06-01T14:30:00Z",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{payload['trade_id']}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))
    replay = next(item for item in trade["evidence"] if item.get("type") == "replay_chart")
    assert replay["category"] == "review"
    assert replay["timing"] == "post_trade"
    assert replay["provenance"]["capture_mode"] == "retrospective_replay"
    assert replay["captured_at"] == "2026-10-08T04:00:00Z"
    assert replay["provenance"]["captured_at"] == replay["captured_at"]
    assert replay["provenance"]["capture_time_status"] == "known"
    assert replay["represented_market_time"]["start"] == "2026-06-01T13:30:00Z"
    assert replay["represented_market_time"]["end"] == "2026-06-01T14:30:00Z"
    assert replay["role"] == "historical_market_reconstruction"
    assert replay["timing_relative_to_trade"] == "post_trade"

_BASE_LIVE_ARGS = [
    "capture", "live",
    "--strategy-observations-file", MATCHING_OBSERVATIONS_FILE,
    "--symbol", "BTCUSDT", "--direction", "long",
    "--entry", "100", "--sl", "95", "--tp", "110", "--planned-rr", "2",
    "--timeframe-context", "1h", "--timeframe-trigger", "15m",
    "--market-context", "cli live fixture", "--thesis", "cli live thesis",
    "--created-at", "2026-09-12T00:10:00Z",
    "--provider", "propw", "--provider-lifecycle-id", "live-open-1",
    "--provider-execution-at", "2026-09-12T00:10:30Z",
    "--candidate-source", "fixture://live-candidate-match",
    "--actual-entry", "100",
]


def test_capture_live_cli_full_regression(tmp_path: Path) -> None:
    """A normal contemporaneous LIVE capture through the new CLI command
    still finalizes correctly -- proving the corrected two-slot orchestration
    (provider evidence + optional chart evidence) does not regress the
    existing lifecycle path, using REAL provider execution evidence (not a
    chart mislabeled as one -- see
    test_capture_live_cli_rejects_chart_only_capture below for the fixed
    exploit)."""

    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    fill = tmp_path / "live-fill.png"
    fill.write_bytes(_png_bytes(b"\x04"))
    chart = tmp_path / "live-chart.png"
    chart.write_bytes(_png_bytes(b"\x05"))

    result = _run_cli(
        tmp_path, *_BASE_LIVE_ARGS,
        "--provider-evidence-file", str(fill),
        "--chart-file", str(chart),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    trade_id = payload["trade_id"]
    assert trade_id.startswith("trd_")
    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{trade_id}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))
    assert trade["recording_timing"] == "contemporaneous"
    assert trade["actual"]["execution_status"] == "entered"
    assert trade["actual"]["execution_verification"]["entry"]["state"] == "confirmed"

    # The chart evidence is attached to the trade's overall evidence list...
    chart_entries = [e for e in trade["evidence"] if e.get("category") == "pre_trade_context" and e.get("type") == "chart_snapshot"]
    provider_entries = [e for e in trade["evidence"] if e.get("category") == "entry" and e.get("type") == "broker_fill"]
    assert len(chart_entries) == 1
    assert len(provider_entries) == 1
    chart_ref = chart_entries[0]["source_ref"]
    provider_ref = provider_entries[0]["source_ref"]
    assert chart_ref != provider_ref

    # ...but it must NEVER appear as identity-link/entry-verification
    # execution authority -- only the provider evidence ref may.
    execution_verification = trade["actual"]["execution_verification"]
    assert execution_verification["entry"]["evidence_refs"] == [provider_ref]
    assert chart_ref not in execution_verification["entry"]["evidence_refs"]
    identity_link = trade["operational_identity_link"]
    assert identity_link["evidence_refs"] == [provider_ref]
    assert chart_ref not in identity_link["evidence_refs"]


def test_capture_live_cli_rejects_chart_only_capture(tmp_path: Path) -> None:
    """The bug this pass fixes: a plain owner-supplied chart screenshot must
    never be silently accepted as PROVIDER EXECUTION AUTHORITY just because
    ``--evidence-type`` used to default to ``broker_fill``. There is no
    longer any single ``--evidence-file``/``--evidence-type`` pair to
    mislabel -- ``--provider-evidence-file`` is a required, separate flag,
    so omitting it (chart-only capture) fails closed at the CLI/argparse
    layer before any lifecycle state is touched."""

    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    chart = tmp_path / "chart-only.png"
    chart.write_bytes(_png_bytes(b"\x06"))

    result = _run_cli(tmp_path, *_BASE_LIVE_ARGS, "--chart-file", str(chart))
    assert result.returncode != 0
    assert "--provider-evidence-file" in result.stderr
    assert not list((tmp_path / "canonical" / "records" / "trades").glob("*.json"))


def test_capture_backtest_cli_rejects_non_numeric_planned_rr() -> None:
    """Regression: contemporaneous BACKTEST/LIVE capture at the CLI layer must
    NOT accept the retrospective-only unknown/not_observable sentinel -- the
    sentinel-accepting parser is wired only to `capture retrospective-live`."""

    args = list(_BASE_BACKTEST_ARGS)
    tp_index = args.index("--planned-rr") + 1
    args[tp_index] = "unknown"
    result = _run_cli(Path.cwd(), *args, "--evidence-file", "does-not-matter.png")
    assert result.returncode != 0
    assert "invalid float value" in result.stderr or "invalid _parse_plan_field value" in result.stderr


def test_capture_live_cli_accepts_partial_plan(tmp_path: Path) -> None:
    """A provider-verified LIVE trade can register with an unknown plan field."""

    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    fill = tmp_path / "live-fill.png"
    fill.write_bytes(_png_bytes(b"\x07"))
    args = [
        "capture", "live",
        "--strategy-observations-file", MATCHING_OBSERVATIONS_FILE,
        "--symbol", "BTCUSDT", "--direction", "long",
        "--entry", "100", "--sl", "95", "--tp", "110", "--planned-rr", "unknown",
        "--timeframe-context", "1h", "--timeframe-trigger", "15m",
        "--market-context", "cli live fixture", "--thesis", "cli live thesis",
        "--created-at", "2026-09-12T00:10:00Z",
        "--provider-evidence-file", str(fill),
        "--provider", "propw", "--provider-lifecycle-id", "live-open-1",
        "--provider-execution-at", "2026-09-12T00:10:30Z",
        "--candidate-source", "fixture://live-candidate-match",
        "--actual-entry", "100",
    ]
    result = _run_cli(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{payload['trade_id']}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))
    assert trade["plan"]["rr_pretrade"] == "unknown"
    assert trade["plan"]["completeness"] == "partial"
    assert payload["trade_no"].startswith("T")
