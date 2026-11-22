from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from trading_os.paper_ingestion import PaperIngestionError, ingest_paper_execution_evidence
from trading_os.contracts import validate_record
from trading_os.operational_analytics import _cumulative_r_by_mode, _record_mode, _summary_by_mode
from trading_os.operational_lifecycle import LifecycleStore
from trading_os.sequence import NumberAllocator


def _fixture_root(source: Path, destination: Path) -> None:
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


def _evidence(**overrides):
    payload = {
        "schema_version": "paper-execution-evidence.v1",
        "execution_id": "pex_fixture_001",
        "strategy_id": "stg-vegas-tunnel",
        "rule_version": "stg-vegas-tunnel:v1",
        "execution_mode": "PAPER",
        "provider": "bingx-swap",
        "provider_environment": "public-read",
        "instrument": "BTC-USDT",
        "side": "long",
        "requested_quantity": 0.001,
        "filled_quantity": 0.001,
        "requested_price": 100.0,
        "fill_price": 100.1,
        "fees": {"amount": 0.0005, "currency": "USDT", "rate_bps": 5.0},
        "slippage": {"bps": 10.0, "amount": 0.1},
        "execution_timestamp": "2026-10-01T00:00:00Z",
        "signal_reference": "crypto://signal/fixture-001",
        "market_data_reference": {
            "provider_symbol": "BTC-USDT",
            "timeframe": "1m",
            "candle_timestamp_utc": "2026-10-01T00:00:00Z",
            "batch_digest": "b" * 64,
            "start_utc": "2026-10-01T00:00:00Z",
            "end_utc": "2026-10-01T00:02:00Z",
        },
        "crypto_run_id": "crypto-fixture-run-001",
        "crypto_commit_sha": "a" * 40,
        "producer": "crypto",
        "raw_evidence_reference": "crypto://paper-execution/pex_fixture_001",
        "status": "FILLED",
    }
    payload.update(overrides)
    return payload


def test_paper_evidence_ingests_into_canonical_backtest_record_and_is_idempotent(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _fixture_root(source, tmp_path)
    evidence_path = tmp_path / "paper-evidence.json"
    evidence_path.write_text(json.dumps(_evidence(), ensure_ascii=False) + "\n", encoding="utf-8")

    first = ingest_paper_execution_evidence(tmp_path, evidence_path)
    second = ingest_paper_execution_evidence(tmp_path, evidence_path)

    assert first["validation"] == "PASS"
    assert first["duplicate_ingest"] is False
    assert second["duplicate_ingest"] is True
    assert second["trade_id"] == first["trade_id"]

    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{first['trade_id']}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))
    assert trade["type"] == "backtest"
    assert trade["execution_mode"] == "PAPER"
    assert trade["execution_environment"] == "paper"
    assert trade["operational_capture"]["source_method"] == "paper_trade"
    assert trade["actual"]["paper_execution"]["execution_id"] == "pex_fixture_001"
    assert _record_mode(trade) == "paper"
    assert validate_record(trade) == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("execution_mode", "LIVE", "execution_mode must be PAPER"),
        ("crypto_commit_sha", "short", "full 40-character SHA"),
        ("market_data_reference", {}, "market_data_reference"),
        ("strategy_id", "not-registered", "strategy is not registered"),
    ],
)
def test_paper_evidence_fails_closed(tmp_path: Path, field: str, value, message: str) -> None:
    source = Path(__file__).resolve().parents[1]
    _fixture_root(source, tmp_path)
    evidence_path = tmp_path / "invalid-paper-evidence.json"
    evidence_path.write_text(json.dumps(_evidence(**{field: value})) + "\n", encoding="utf-8")

    with pytest.raises(PaperIngestionError, match=message):
        ingest_paper_execution_evidence(tmp_path, evidence_path)


def test_conflicting_duplicate_execution_id_fails_closed(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _fixture_root(source, tmp_path)
    evidence_path = tmp_path / "paper-evidence.json"
    evidence_path.write_text(json.dumps(_evidence()) + "\n", encoding="utf-8")
    first = ingest_paper_execution_evidence(tmp_path, evidence_path)

    evidence_path.write_text(json.dumps(_evidence(fill_price=100.2)) + "\n", encoding="utf-8")
    with pytest.raises(PaperIngestionError, match="conflicting paper evidence for existing execution_id"):
        ingest_paper_execution_evidence(tmp_path, evidence_path)
    assert first["trade_no"] == "BT000001"
    assert len(list((tmp_path / "canonical" / "records" / "trades").glob("*.json"))) == 1


def _read_events(root: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (root / "canonical" / "lifecycle" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_interrupted_after_entry_confirmation_resumes_finalization(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _fixture_root(source, tmp_path)
    evidence_path = tmp_path / "paper-evidence.json"
    evidence_path.write_text(json.dumps(_evidence()) + "\n", encoding="utf-8")

    with patch.object(LifecycleStore, "finalize", side_effect=RuntimeError("simulated crash before finalize")):
        with pytest.raises(RuntimeError, match="simulated crash before finalize"):
            ingest_paper_execution_evidence(tmp_path, evidence_path)

    events = _read_events(tmp_path)
    assert any(event["event_type"] == "backtest_entry_confirmed" for event in events)
    assert not any(event["event_type"] == "canonical_finalized" for event in events)

    resumed = ingest_paper_execution_evidence(tmp_path, evidence_path)

    assert resumed["validation"] == "PASS"
    assert resumed["duplicate_ingest"] is False
    assert resumed["trade_id"]
    assert resumed["trade_no"] == "BT000001"
    assert len(list((tmp_path / "canonical" / "records" / "trades").glob("*.json"))) == 1
    assert len(json.loads((tmp_path / "canonical" / "sequences" / "numbers.json").read_text(encoding="utf-8"))["allocated"]) == 1
    assert sum(event["event_type"] == "canonical_finalized" for event in _read_events(tmp_path)) == 1


def test_writer_finalized_before_lifecycle_event_recovers_without_reallocation(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _fixture_root(source, tmp_path)
    evidence_path = tmp_path / "paper-evidence.json"
    evidence_path.write_text(json.dumps(_evidence()) + "\n", encoding="utf-8")

    def finalize_writer_then_crash(store: LifecycleStore, draft_id: str, reviewer: str = "owner", **_kwargs):
        trade_id = store.writer.finalize_trade(draft_id, reviewer, NumberAllocator(store.root))
        raise RuntimeError(f"simulated crash after writer finalization: {trade_id}")

    with patch.object(LifecycleStore, "finalize", finalize_writer_then_crash):
        with pytest.raises(RuntimeError, match="simulated crash after writer finalization"):
            ingest_paper_execution_evidence(tmp_path, evidence_path)

    draft_id = "rec_" + hashlib.sha256(b"pex_fixture_001").hexdigest()[:24]
    finalizations = json.loads((tmp_path / "canonical" / "sequences" / "finalizations.json").read_text(encoding="utf-8"))
    indexed = finalizations["records"][draft_id]
    assert indexed["trade_id"]
    assert indexed["trade_no"] == "BT000001"
    assert not any(event["event_type"] == "canonical_finalized" for event in _read_events(tmp_path))

    recovered = ingest_paper_execution_evidence(tmp_path, evidence_path)

    assert recovered["validation"] == "PASS"
    assert recovered["duplicate_ingest"] is False
    assert recovered["trade_id"] == indexed["trade_id"]
    assert recovered["trade_no"] == indexed["trade_no"]
    assert len(list((tmp_path / "canonical" / "records" / "trades").glob("*.json"))) == 1
    finalizations_after = json.loads((tmp_path / "canonical" / "sequences" / "finalizations.json").read_text(encoding="utf-8"))
    assert finalizations_after["records"][draft_id] == indexed
    assert sum(event["event_type"] == "canonical_finalized" for event in _read_events(tmp_path)) == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("requested_quantity", 0, "requested_quantity must be a positive finite number"),
        ("fill_price", 0, "fill_price must be a positive finite number"),
        ("execution_timestamp", "not-a-timestamp", "execution_timestamp must be an ISO timestamp"),
        ("raw_evidence_reference", "crypto://paper-execution/other", "raw_evidence_reference must exactly match execution_id"),
    ],
)
def test_paper_provenance_and_numeric_fields_fail_closed(tmp_path: Path, field: str, value, message: str) -> None:
    source = Path(__file__).resolve().parents[1]
    _fixture_root(source, tmp_path)
    evidence_path = tmp_path / "invalid-paper-evidence.json"
    evidence_path.write_text(json.dumps(_evidence(**{field: value})) + "\n", encoding="utf-8")
    with pytest.raises(PaperIngestionError, match=message):
        ingest_paper_execution_evidence(tmp_path, evidence_path)


def test_market_data_timestamps_and_provider_identity_fail_closed(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _fixture_root(source, tmp_path)
    cases = [
        ({"candle_timestamp_utc": "bad"}, "market_data_reference.candle_timestamp_utc must be an ISO timestamp"),
        ({"start_utc": "2026-10-02T00:00:00Z", "end_utc": "2026-10-01T00:00:00Z"}, "start_utc must be <= end_utc"),
        ({"provider": "pionex"}, "market_data_reference.provider conflicts with provider"),
    ]
    for update, message in cases:
        payload = _evidence()
        payload["market_data_reference"].update(update)
        evidence_path = tmp_path / "invalid-market-reference.json"
        evidence_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with pytest.raises(PaperIngestionError, match=message):
            ingest_paper_execution_evidence(tmp_path, evidence_path)


def test_paper_read_model_stays_separate_from_live_and_backtest() -> None:
    rows = [
        {"counted": True, "realized_r": 1.0, "sample_origin": "live", "trade_id": "live-1"},
        {"counted": True, "realized_r": 2.0, "sample_origin": "paper", "trade_id": "paper-1"},
        {"counted": True, "realized_r": 3.0, "sample_origin": "backtest", "trade_id": "backtest-1"},
    ]
    summary = _summary_by_mode(rows, {})
    assert summary["live"]["count"] == 1
    assert summary["paper"]["count"] == 1
    assert summary["backtest"]["count"] == 1
    series = _cumulative_r_by_mode(rows)
    assert [point["reference"] for point in series["paper"]] == ["paper-1"]
