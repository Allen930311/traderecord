"""Facade-level tests for the LIVE capture provider/chart evidence split.

Correction pass #3 on PR #43: ``capture live`` / ``capture retrospective-live``
used to accept a single ``--evidence-file``/``--evidence-type`` (CLI) or
``entry_evidence`` (facade) input, defaulted to ``broker_fill`` regardless of
what the file actually was. A plain owner-supplied chart screenshot could
therefore sail through as PROVIDER EXECUTION AUTHORITY. These tests exercise
the fix directly at the ``trading_os.unified_capture.capture_live`` facade
layer (CLI-layer coverage is in tests/test_unified_capture_cli.py).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from resolver_helpers import matching_observations, no_match_observations
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.unified_capture import capture_live, capture_retrospective_live


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


_LIVE_KWARGS = dict(
    strategy_observations=matching_observations("stg-vegas-tunnel"),
    symbol="BTCUSDT",
    direction="long",
    intended_entry=100.0,
    stop_loss=95.0,
    take_profit=110.0,
    planned_rr=2.0,
    timeframe_context="1h",
    timeframe_trigger="15m",
    market_context="facade fixture",
    setup_tags=["vegas_tunnel"],
    confirmation_tags=[],
    thesis="facade thesis",
    chart_refs=["fixture://chart"],
    created_at="2026-09-12T00:10:00Z",
    provider="propw",
    provider_lifecycle_id="live-open-1",
    provider_execution_timestamp="2026-09-12T00:10:30Z",
    candidate_source="fixture://live-candidate-match",
    actual_entry=100.0,
)


def test_capture_live_rejects_missing_provider_evidence(tmp_path: Path) -> None:
    """A chart-only capture (no provider evidence at all) must fail closed
    with a clear LifecycleError -- it must never fall back to treating the
    chart as execution authority."""

    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    chart_evidence = {
        "source_ref": "fixture://chart-only",
        "timing": "pre_trade",
        "type": "chart_snapshot",
        "category": "pre_trade_context",
        "sha256": "c" * 64,
    }

    with pytest.raises(LifecycleError, match="provider_evidence"):
        capture_live(store, chart_evidence=chart_evidence, provider_evidence=None, **_LIVE_KWARGS)

    assert not list((tmp_path / "canonical" / "records" / "trades").glob("*.json"))


def test_capture_live_chart_evidence_never_becomes_execution_authority(tmp_path: Path) -> None:
    """A real provider evidence ref plus an optional chart both attach to the
    draft/trade, but only the provider evidence ref may appear in
    identity_link.evidence_refs / execution_verification.entry.evidence_refs."""

    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    provider_evidence = {
        "source_ref": "fixture://live-provider-fill",
        "timing": "execution",
        "type": "broker_fill",
        "category": "entry",
        "sha256": "d" * 64,
    }
    chart_evidence = {
        "source_ref": "fixture://live-chart",
        "timing": "pre_trade",
        "type": "chart_snapshot",
        "category": "pre_trade_context",
        "sha256": "e" * 64,
    }

    result = capture_live(
        store,
        provider_evidence=provider_evidence,
        chart_evidence=chart_evidence,
        **_LIVE_KWARGS,
    )
    trade_id = result["trade_id"]
    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{trade_id}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))

    assert trade["actual"]["execution_verification"]["entry"]["state"] == "confirmed"
    evidence_refs = trade["actual"]["execution_verification"]["entry"]["evidence_refs"]
    assert evidence_refs == [provider_evidence["source_ref"]]
    assert chart_evidence["source_ref"] not in evidence_refs

    identity_link = trade["operational_identity_link"]
    assert identity_link["evidence_refs"] == [provider_evidence["source_ref"]]
    assert chart_evidence["source_ref"] not in identity_link["evidence_refs"]

    # Both pieces of evidence remain attached to the trade's overall evidence
    # list -- the chart is not discarded, just never treated as authority.
    attached_refs = {item["source_ref"] for item in trade["evidence"]}
    assert provider_evidence["source_ref"] in attached_refs
    assert chart_evidence["source_ref"] in attached_refs


def test_capture_live_succeeds_without_optional_chart_evidence(tmp_path: Path) -> None:
    """chart_evidence is optional -- provider evidence alone still finalizes,
    proving the two-slot split did not regress the plain single-evidence
    LIVE path."""

    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    provider_evidence = {
        "source_ref": "fixture://live-provider-only-fill",
        "timing": "execution",
        "type": "broker_fill",
        "category": "entry",
        "sha256": "f" * 64,
    }

    result = capture_live(store, provider_evidence=provider_evidence, **_LIVE_KWARGS)
    trade_id = result["trade_id"]
    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{trade_id}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))
    assert trade["actual"]["execution_verification"]["entry"]["state"] == "confirmed"


def test_capture_retrospective_live_chart_evidence_never_becomes_execution_authority(tmp_path: Path) -> None:
    """Same split, same invariant, for the retrospective LIVE path (a late-
    reported real execution) -- capture_retrospective_live is capture_live
    under a different recording_timing, so the same provider/chart evidence
    split and the same fail-closed behavior must hold here too."""

    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    provider_evidence = {
        "source_ref": "fixture://retro-provider-fill",
        "timing": "execution",
        "type": "broker_fill",
        "category": "entry",
        "sha256": "0" * 64,
        "captured_at": "2026-06-01T14:00:00Z",
    }
    chart_evidence = {
        "source_ref": "fixture://retro-chart",
        "timing": "pre_trade",
        "type": "chart_snapshot",
        "category": "pre_trade_context",
        "sha256": "1" * 64,
    }
    kwargs = dict(_LIVE_KWARGS)
    kwargs.update(
        created_at="2026-06-01T13:55:00Z",
        provider_execution_timestamp="2026-06-01T14:00:00Z",
        recording_timing="retrospective",
    )

    result = capture_retrospective_live(
        store,
        provider_evidence=provider_evidence,
        chart_evidence=chart_evidence,
        **kwargs,
    )
    trade_id = result["trade_id"]
    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{trade_id}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))
    assert trade["recording_timing"] == "retrospective"

    evidence_refs = trade["actual"]["execution_verification"]["entry"]["evidence_refs"]
    assert evidence_refs == [provider_evidence["source_ref"]]
    assert chart_evidence["source_ref"] not in evidence_refs
    identity_link = trade["operational_identity_link"]
    assert identity_link["evidence_refs"] == [provider_evidence["source_ref"]]
    assert chart_evidence["source_ref"] not in identity_link["evidence_refs"]


def test_capture_retrospective_live_rejects_missing_provider_evidence(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    chart_evidence = {
        "source_ref": "fixture://retro-chart-only",
        "timing": "pre_trade",
        "type": "chart_snapshot",
        "category": "pre_trade_context",
        "sha256": "2" * 64,
    }
    kwargs = dict(_LIVE_KWARGS)
    kwargs.update(
        created_at="2026-06-01T13:55:00Z",
        provider_execution_timestamp="2026-06-01T14:00:00Z",
        recording_timing="retrospective",
    )

    with pytest.raises(LifecycleError, match="provider_evidence"):
        capture_retrospective_live(store, chart_evidence=chart_evidence, provider_evidence=None, **kwargs)

    assert not list((tmp_path / "canonical" / "records" / "trades").glob("*.json"))
