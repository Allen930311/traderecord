from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.vocabularies import SOURCE_METHODS


def _copy_registry(source: Path, destination: Path) -> None:
    shutil.copytree(source / "registry", destination / "registry")
    (destination / "canonical" / "sequences").mkdir(parents=True)
    (destination / "canonical" / "records" / "trades").mkdir(parents=True)
    (destination / "canonical" / "adjudications").mkdir(parents=True)
    import json
    (destination / "canonical" / "sequences" / "numbers.json").write_text(
        json.dumps({"live_next": 1, "backtest_next": 1, "allocated": [], "tombstones": []}) + "\n",
        encoding="utf-8",
    )
    (destination / "canonical" / "sequences" / "finalizations.json").write_text(
        json.dumps({"schema_version": "finalization-index.v1", "records": {}}) + "\n",
        encoding="utf-8",
    )


def _add_experimental_strategy(destination: Path) -> None:
    path = destination / "registry" / "strategy-registry.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["strategies"].append(
        {
            "strategy_id": "stg-experimental-test",
            "display_name": "Experimental Test",
            "classification": "experimental",
            "status": "experimental",
            "rule_versions": [],
        }
    )
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _draft_kwargs(**overrides):
    base = dict(
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
        market_context="fixture market context",
        setup_tags=["vegas_tunnel"],
        confirmation_tags=[],
        thesis="fixture thesis",
        chart_refs=["fixture://chart"],
        created_at="2026-09-11T00:00:00Z",
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize("source_method", sorted(SOURCE_METHODS))
def test_backtest_draft_accepts_each_source_method_and_round_trips(tmp_path: Path, source_method: str) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    result = store.create_draft(**_draft_kwargs(trade_type="backtest", source_method=source_method))
    draft = store._draft(result["draft_id"])
    assert draft["operational_capture"]["source_method"] == source_method
    assert draft["type"] == "backtest"


def test_backtest_draft_rejects_unknown_source_method(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    with pytest.raises(LifecycleError, match="source_method"):
        store.create_draft(**_draft_kwargs(trade_type="backtest", source_method="not_a_real_method"))


def test_backtest_draft_requires_a_source_method(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    with pytest.raises(LifecycleError, match="source_method"):
        store.create_draft(**_draft_kwargs(trade_type="backtest"))


def test_live_draft_rejects_source_method(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    with pytest.raises(LifecycleError, match="source_method"):
        store.create_draft(**_draft_kwargs(trade_type="live", source_method="manual_backtest"))


@pytest.mark.parametrize("value", [True, False, "unknown"])
def test_outcome_known_at_recording_round_trips(tmp_path: Path, value) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    result = store.create_draft(
        **_draft_kwargs(trade_type="backtest", source_method="tradingview_replay", outcome_known_at_recording=value)
    )
    draft = store._draft(result["draft_id"])
    assert draft["operational_capture"]["outcome_known_at_recording"] == value


def test_outcome_known_at_recording_defaults_to_unknown(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    result = store.create_draft(**_draft_kwargs(trade_type="backtest", source_method="historical_chart_review"))
    draft = store._draft(result["draft_id"])
    assert draft["operational_capture"]["outcome_known_at_recording"] == "unknown"


def test_outcome_known_at_recording_rejects_invalid_value(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    with pytest.raises(LifecycleError, match="outcome_known_at_recording"):
        store.create_draft(
            **_draft_kwargs(trade_type="backtest", source_method="manual_backtest", outcome_known_at_recording="maybe")
        )


def test_missed_setup_backtest_on_active_strategy_finalizes_with_bt_number(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    store = LifecycleStore(tmp_path)
    result = store.create_draft(
        **_draft_kwargs(
            trade_type="backtest",
            source_method="live_market_unexecuted",
            outcome_known_at_recording=True,
        )
    )
    draft_id = result["draft_id"]
    store.attach_evidence(
        draft_id,
        {
            "source_ref": "fixture://replay-chart",
            "timing": "pre_trade",
            "type": "chart_snapshot",
            "category": "entry",
            "sha256": "e" * 64,
        },
    )
    store.confirm_backtest_entry(
        draft_id,
        actual_entry=100.0,
        execution_at="2026-09-11T00:01:00Z",
        run_id="run-1",
        parameters_ref="fixture://params/run-1",
        dataset_ref="fixture://dataset/btcusdt-1h",
        data_period={"from": "2026-01-01", "to": "2026-09-01"},
        source_ref="fixture://replay-chart",
    )
    projection = store.current()
    record = projection["records"][draft_id]
    assert record["state"] == "OPEN"
    finalized = store.finalize(draft_id, occurred_at="2026-09-11T00:02:00Z")
    trade_id = finalized["trade_id"]
    assert finalized["trade_no"].startswith("BT")

    import json
    trade_path = tmp_path / "canonical" / "records" / "trades" / f"{trade_id}.json"
    trade = json.loads(trade_path.read_text(encoding="utf-8"))
    assert trade["type"] == "backtest"
    assert trade["operational_capture"]["source_method"] == "live_market_unexecuted"
    assert trade["operational_capture"]["outcome_known_at_recording"] is True
    assert trade["actual"]["execution_verification"]["entry"]["basis"] == "simulator_run"
    # No PropW/provider fields were ever populated for this sample.
    assert "operational_identity_link" not in trade


def test_experimental_backtest_requires_allow_experimental(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    _add_experimental_strategy(tmp_path)
    store = LifecycleStore(tmp_path)
    with pytest.raises(LifecycleError, match="allow_experimental"):
        store.create_draft(
            **_draft_kwargs(
                strategy_id="stg-experimental-test",
                rule_version="unfrozen:experimental",
                trade_type="backtest",
                source_method="external_simulator",
            )
        )


def test_experimental_backtest_finalizes_with_allow_experimental(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    _add_experimental_strategy(tmp_path)
    store = LifecycleStore(tmp_path)
    result = store.create_draft(
        **_draft_kwargs(
            strategy_id="stg-experimental-test",
            rule_version="unfrozen:experimental",
            trade_type="backtest",
            source_method="external_simulator",
            allow_experimental=True,
        )
    )
    draft_id = result["draft_id"]
    store.attach_evidence(
        draft_id,
        {
            "source_ref": "fixture://experimental-replay",
            "timing": "pre_trade",
            "type": "chart_snapshot",
            "category": "entry",
            "sha256": "f" * 64,
        },
    )
    store.confirm_backtest_entry(
        draft_id,
        actual_entry=100.0,
        execution_at="2026-09-11T00:01:00Z",
        run_id="run-experimental-1",
        parameters_ref="fixture://params/run-experimental-1",
        dataset_ref="fixture://dataset/btcusdt-1h",
        data_period="2026-01-01/2026-09-01",
        source_ref="fixture://experimental-replay",
    )
    finalized = store.finalize(draft_id, occurred_at="2026-09-11T00:02:00Z")
    assert finalized["trade_no"].startswith("BT")


def _review_only_replay_draft(tmp_path: Path, *, draft_source: str = "tradingview_replay", chart_kind: str = "replay_chart"):
    """An experimental BACKTEST draft whose only chart is review/post_trade evidence (a Replay chart)."""
    source = Path(__file__).resolve().parents[1]
    _copy_registry(source, tmp_path)
    _add_experimental_strategy(tmp_path)
    store = LifecycleStore(tmp_path)
    draft_id = store.create_draft(
        **_draft_kwargs(
            strategy_id="stg-experimental-test",
            rule_version="unfrozen:experimental",
            trade_type="backtest",
            source_method=draft_source,
            allow_experimental=True,
        )
    )["draft_id"]
    store.attach_evidence(draft_id, {
        "source_ref": "fixture://replay-chart", "timing": "post_trade", "type": chart_kind,
        "category": "review", "method": chart_kind, "sha256": "e" * 64,
    })
    return store, draft_id


REPLAY_META = {"source_method": "tradingview_replay", "time_quality": "known", "price_provenance": "chart_annotation"}


def test_replay_chart_entry_with_a_known_time_is_accepted_and_stays_unverified(tmp_path: Path) -> None:
    """#175: a known candle time must not demand execution-time `entry` evidence for a Replay chart."""
    store, draft_id = _review_only_replay_draft(tmp_path)
    store.confirm_backtest_entry(
        draft_id, actual_entry=100.0, execution_at="2026-10-12T04:11:42Z",
        source_ref="fixture://replay-chart", execution_metadata=REPLAY_META,
    )
    draft = store._draft(draft_id)
    evidence = draft["actual"]["execution_evidence"]["entry"]["manual_declared_execution"]
    assert draft["actual"]["entry_at"] == "2026-10-12T04:11:42Z"
    assert evidence["actual_fill_verified"] is False and evidence["time_quality"] == "known"
    assert evidence["chart_source_method"] == "tradingview_replay"


@pytest.mark.parametrize("metadata", [
    None,
    {"source_method": "manual_backtest", "time_quality": "known", "price_provenance": "chart_annotation"},
])
def test_review_evidence_alone_still_cannot_confirm_a_non_replay_entry(tmp_path: Path, metadata) -> None:
    """The relaxation is scoped to TradingView Replay: other sources still need entry-category evidence."""
    store, draft_id = _review_only_replay_draft(tmp_path)
    with pytest.raises(LifecycleError, match="not attached"):
        store.confirm_backtest_entry(
            draft_id, actual_entry=100.0, execution_at="2026-10-12T04:11:42Z",
            source_ref="fixture://replay-chart", execution_metadata=metadata,
        )


def test_replay_chart_with_unknown_time_keeps_its_not_observable_record(tmp_path: Path) -> None:
    store, draft_id = _review_only_replay_draft(tmp_path)
    store.confirm_backtest_entry(
        draft_id, actual_entry=100.0, execution_at=None, source_ref="fixture://replay-chart",
        execution_metadata={"source_method": "tradingview_replay", "time_quality": "not_observable",
                            "price_provenance": "chart_annotation"},
    )
    evidence = store._draft(draft_id)["actual"]["execution_evidence"]["entry"]["manual_declared_execution"]
    assert evidence["time_quality"] == "not_observable" and evidence["simulated_at"] is None


@pytest.mark.parametrize("draft_source", ["manual_backtest", "live_market_unexecuted", "historical_chart_review"])
def test_forged_replay_metadata_cannot_relax_a_non_replay_draft(tmp_path: Path, draft_source: str) -> None:
    """Codex review of #175: the relaxation rests on the draft's recorded source, not caller metadata."""
    store, draft_id = _review_only_replay_draft(tmp_path, draft_source=draft_source)
    with pytest.raises(LifecycleError, match="not attached"):
        store.confirm_backtest_entry(
            draft_id, actual_entry=100.0, execution_at="2026-10-12T04:11:42Z",
            source_ref="fixture://replay-chart", execution_metadata=REPLAY_META,
        )
    with pytest.raises(LifecycleError):  # the unknown-time form is closed the same way
        store.confirm_backtest_entry(
            draft_id, actual_entry=100.0, execution_at=None, source_ref="fixture://replay-chart",
            execution_metadata={**REPLAY_META, "time_quality": "not_observable"},
        )


def test_review_evidence_that_is_not_a_replay_chart_is_refused_even_on_a_replay_draft(tmp_path: Path) -> None:
    store, draft_id = _review_only_replay_draft(tmp_path, chart_kind="chart_snapshot")
    with pytest.raises(LifecycleError, match="replay chart"):
        store.confirm_backtest_entry(
            draft_id, actual_entry=100.0, execution_at="2026-10-12T04:11:42Z",
            source_ref="fixture://replay-chart", execution_metadata=REPLAY_META,
        )
