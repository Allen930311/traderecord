from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from test_operational_closeout import FIXED_GENERATED_AT, FIXED_SOURCE_COMMIT, _make_fixture, _run_cli
from resolver_helpers import live_resolution
from trading_os import projections
from trading_os.backtest_handoff import emit_handoff
from trading_os.operational_lifecycle import LifecycleStore
from trading_os.registration_executor import _verify_or_refresh_owner_statement_projections
from trading_os.strategy_health import refresh_health
from trading_os.trade_view import build_trade_view
from trading_os.registration_census import write_census


ROOT = Path(__file__).resolve().parents[1]


def _refresh(root: Path, trade_id: str) -> dict:
    return projections.refresh_projections(
        root,
        generated_at=FIXED_GENERATED_AT,
        source_commit=FIXED_SOURCE_COMMIT,
        target_trade=trade_id,
    )


def _write_open_propw_lifecycle(root: Path, open_id: str) -> None:
    ledger = root / "provider-ledgers/propw/normalized/trade-lifecycles.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({
            "open_id": open_id,
            "lifecycle": {
                "instrument": "BTCUSDT",
                "direction": "long",
                "entry_time": "2026-10-09T12:00:00Z",
                "entry_price": "100",
                "open_size": "1",
                "closed_size": "0",
                "is_closed": 0,
                "is_partial": 0,
                "normalization_status": "open_only",
                "provider_lifecycle_phase": "OPEN",
            },
        }) + "\n",
        encoding="utf-8",
    )


def _register_open_propw_trade(root: Path, open_id: str) -> str:
    store = LifecycleStore(root)
    draft_id = "rec_22222222222222222222"
    store.create_draft(
        strategy_id="stg-vegas-tunnel",
        strategy_resolution=live_resolution("stg-vegas-tunnel", evidence_ref="fixture://registration-plan"),
        rule_version="stg-vegas-tunnel:v1",
        symbol="BTCUSDT",
        direction="long",
        intended_entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        planned_rr=2.0,
        timeframe_context="1h",
        timeframe_trigger="15m",
        market_context="synthetic registration test",
        setup_tags=["vegas_tunnel"],
        confirmation_tags=[],
        thesis="synthetic PropW registration",
        chart_refs=["fixture://registration-plan"],
        created_at="2026-10-09T12:00:00Z",
        draft_id=draft_id,
    )
    store.attach_evidence(draft_id, {
        "source_ref": "fixture://registration-plan",
        "timing": "pre_trade",
        "type": "chart_snapshot",
        "category": "pre_trade_context",
        "sha256": "a" * 64,
        "captured_at": "2026-10-09T11:59:00Z",
    })
    store.mark_entry_pending(draft_id, provider={"provider": "propw"}, occurred_at="2026-10-09T12:00:05Z")
    store.attach_evidence(draft_id, {
        "source_ref": "fixture://propw-entry",
        "timing": "execution",
        "type": "broker_fill",
        "category": "entry",
        "sha256": "b" * 64,
        "captured_at": "2026-10-09T12:00:00Z",
    })
    store.create_identity_link(
        draft_id,
        provider="propw",
        provider_lifecycle_id=open_id,
        instrument="BTCUSDT",
        direction="long",
        provider_execution_timestamp="2026-10-09T12:00:00Z",
        candidate_source="fixture://registration-census",
        match_disposition="MATCHED",
        evidence_refs=["fixture://propw-entry"],
        created_at="2026-10-09T12:00:04Z",
        authority="fixture_deterministic_match",
    )
    store.verify_entry(
        draft_id,
        instrument="BTCUSDT",
        direction="long",
        actual_entry=100.0,
        execution_at="2026-10-09T12:00:00Z",
        source_ref="fixture://propw-entry",
        provider_lifecycle_id=open_id,
        provider="propw",
    )
    return store.finalize(draft_id, occurred_at="2026-10-09T12:00:06Z")["trade_id"]


def test_projection_orchestration_order_is_deterministic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        projections,
        "refresh_analytics",
        lambda *_args, **_kwargs: calls.append("analytics") or {"generated_at": FIXED_GENERATED_AT, "source_commit": FIXED_SOURCE_COMMIT},
    )
    monkeypatch.setattr(
        projections,
        "refresh_health",
        lambda *_args, **_kwargs: calls.append("health") or {"generated_at": FIXED_GENERATED_AT},
    )
    monkeypatch.setattr(projections, "build_trade_view", lambda *_args, **_kwargs: calls.append("trade_view") or {})
    monkeypatch.setattr(projections, "emit_handoff", lambda *_args, **_kwargs: calls.append("handoff") or {})
    monkeypatch.setattr(projections, "verify_projections", lambda *_args, **_kwargs: calls.append("verify") or {"status": "CURRENT"})

    result = projections.refresh_projections(tmp_path, target_trade="T000006")

    assert calls == ["analytics", "health", "trade_view", "handoff", "verify"]
    assert result["refresh_order"] == ["analytics", "health", "trade_view", "handoff"]


def test_projection_closure_verifies_target_and_digest_chain(tmp_path: Path) -> None:
    store, _draft_id, trade_id = _make_fixture(ROOT, tmp_path)

    result = _refresh(tmp_path, trade_id)

    assert result["status"] == "CURRENT"
    assert result["duration_seconds"] >= 0
    visibility = result["verification"]["target_visibility"]
    assert visibility["canonical_present"] is True
    assert visibility["lifecycle_present"] is True
    assert visibility["analytics_present"] is True
    assert visibility["trade_view_present"] is True
    lifecycle = json.loads((tmp_path / "canonical/lifecycle/current.json").read_text(encoding="utf-8"))
    analytics = json.loads((tmp_path / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    health = json.loads((tmp_path / "exports/operational/health/current.json").read_text(encoding="utf-8"))
    view = json.loads((tmp_path / "exports/operational/trade-view/trade-view-data.json").read_text(encoding="utf-8"))
    handoff = json.loads((tmp_path / "exports/operational/handoff/current.json").read_text(encoding="utf-8"))
    assert analytics["canonical_lifecycle_events_sha256"] == lifecycle["events_sha256"]
    assert health["analytics_artifact_digest"] == analytics["artifact_digest"]
    assert view["sources"]["analytics_artifact_digest"] == analytics["artifact_digest"]
    assert view["sources"]["health_artifact_digest"] == health["artifact_digest"]
    assert handoff["source_artifact_digests"] == {"analytics": analytics["artifact_digest"], "health": health["artifact_digest"]}
    assert store.current()["records"][trade_id]["state"] == "REVIEWED"


def test_interrupted_owner_statement_retry_refreshes_only_derived_projections(tmp_path: Path) -> None:
    store, _draft_id, trade_id = _make_fixture(ROOT, tmp_path)
    _refresh(tmp_path, trade_id)
    source_id = "registration:interrupted-owner-statement"
    exact_text = "原文保留中文、換行，\n以及標點。"

    appended = store.record_owner_statement(
        trade_id,
        verbatim_text=exact_text,
        source_id=source_id,
    )
    event_count_before_retry = len(store._read_events())
    events_before_retry = (tmp_path / "canonical/lifecycle/events.jsonl").read_bytes()
    canonical_before_retry = {
        path.relative_to(tmp_path / "canonical").as_posix(): path.read_bytes()
        for path in (tmp_path / "canonical").rglob("*")
        if path.is_file()
    }
    with pytest.raises(projections.ProjectionStaleError):
        projections.verify_projections(tmp_path, target_trade=trade_id)

    retried = store.record_owner_statement(
        trade_id,
        verbatim_text=exact_text,
        source_id=source_id,
    )
    recovered = _verify_or_refresh_owner_statement_projections(tmp_path, trade_id)

    canonical_after_retry = {
        path.relative_to(tmp_path / "canonical").as_posix(): path.read_bytes()
        for path in (tmp_path / "canonical").rglob("*")
        if path.is_file()
    }
    assert retried["event_id"] == appended["event_id"]
    assert recovered["status"] == "CURRENT"
    assert canonical_after_retry == canonical_before_retry
    assert (tmp_path / "canonical/lifecycle/events.jsonl").read_bytes() == events_before_retry
    assert len(store._read_events()) == event_count_before_retry
    statements = [
        item
        for item in store.current()["records"][trade_id]["owner_statements"]
        if item.get("source_id") == source_id
    ]
    assert len(statements) == 1
    assert statements[0]["event_id"] == appended["event_id"]
    assert statements[0]["verbatim_text"] == exact_text


def test_trade_view_exposes_only_fresh_unregistered_provider_candidates(tmp_path: Path) -> None:
    _store, _draft_id, trade_id = _make_fixture(ROOT, tmp_path)
    ledger = tmp_path / "provider-ledgers/propw/normalized/trade-lifecycles.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({
            "open_id": "open-123",
            "lifecycle": {
                "instrument": "BTCUSDT", "direction": "long", "entry_time": "2026-10-09T12:00:00Z",
                "entry_price": "100", "open_size": "1", "closed_size": "0", "is_closed": 0,
                "is_partial": 0, "normalization_status": "open_only", "provider_lifecycle_phase": "OPEN",
            },
        }) + "\n",
        encoding="utf-8",
    )
    write_census(tmp_path)

    _refresh(tmp_path, trade_id)

    view = json.loads((tmp_path / "exports/operational/trade-view/trade-view-data.json").read_text(encoding="utf-8"))
    census = view["registration_census"]
    assert census["freshness"]["status"] == "fresh"
    assert census["candidate_count"] == 1
    candidate = census["candidates"][0]
    assert candidate["provider_lifecycle_key"] == "propw:open-123"
    assert candidate["provider_phase"] == "OPEN"
    assert "trade_id" not in candidate and "trade_no" not in candidate
    verified = projections.verify_projections(tmp_path, target_trade=trade_id, require_census_fresh=True)
    assert verified["registration_census_freshness"] == "fresh"
    assert verified["registration_census_candidate_count"] == 1

    finalizations_path = tmp_path / "canonical/sequences/finalizations.json"
    finalizations = json.loads(finalizations_path.read_text(encoding="utf-8"))
    finalizations.setdefault("records", {})["new-finalization"] = {"trade_no": "T999999"}
    finalizations_path.write_text(json.dumps(finalizations, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(projections.ProjectionStaleError, match="registration_census"):
        projections.verify_projections(tmp_path, target_trade=trade_id, require_census_fresh=True)


def test_normal_registration_refreshes_census_for_main_readback(tmp_path: Path) -> None:
    _make_fixture(ROOT, tmp_path)
    open_id = "propw-registration-e2e"
    _write_open_propw_lifecycle(tmp_path, open_id)
    initial = write_census(tmp_path)
    initial_census = json.loads(initial.read_text(encoding="utf-8"))
    assert initial_census["freshness"]["status"] == "fresh"
    assert initial_census["counts"]["UNREGISTERED"] == 1

    trade_id = _register_open_propw_trade(tmp_path, open_id)
    stale = projections.registration_census_read_model(tmp_path)
    assert stale["freshness"]["status"] == "stale"

    refreshed = _refresh(tmp_path, trade_id)
    assert "registration_census" in refreshed["refresh_order"]
    assert refreshed["registration_census"] == "provider-ledgers/propw/registration-census.json"
    readback = _run_cli(tmp_path, "projections", "verify", "--require-census-fresh")

    assert readback["status"] == "CURRENT"
    assert readback["registration_census_freshness"] == "fresh"
    assert readback["registration_census_candidate_count"] == 0
    assert readback["target_visibility"] is None


@pytest.mark.parametrize("tamper", ["counts", "lifecycles"])
def test_persisted_census_body_tampering_is_stale_even_when_source_digests_match(tmp_path: Path, tamper: str) -> None:
    _store, _draft_id, trade_id = _make_fixture(ROOT, tmp_path)
    _write_open_propw_lifecycle(tmp_path, "census-body-integrity")
    write_census(tmp_path)
    _refresh(tmp_path, trade_id)

    census_path = tmp_path / "provider-ledgers/propw/registration-census.json"
    persisted = json.loads(census_path.read_text(encoding="utf-8"))
    prior_digests = dict(persisted["source_digests"])
    if tamper == "counts":
        persisted["counts"]["UNREGISTERED"] = 0
    else:
        persisted["lifecycles"][0]["instrument"] = "TAMPERED"
    census_path.write_text(json.dumps(persisted, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    calculated = projections.registration_census_read_model(tmp_path)
    assert persisted["source_digests"] == prior_digests
    assert calculated["freshness"] == {"status": "stale", "detail": "PERSISTED_CENSUS_CONTENT_MISMATCH"}
    with pytest.raises(projections.ProjectionStaleError, match="registration_census"):
        projections.verify_projections(tmp_path, target_trade=trade_id, require_census_fresh=True)


@pytest.mark.parametrize("artifact", ["analytics", "health", "trade_view", "trade_view_app"])
def test_stale_projection_is_fail_closed(tmp_path: Path, artifact: str) -> None:
    _store, _draft_id, trade_id = _make_fixture(ROOT, tmp_path)
    _refresh(tmp_path, trade_id)
    paths = {
        "analytics": tmp_path / "exports/operational/analytics/current.json",
        "health": tmp_path / "exports/operational/health/current.json",
        "trade_view": tmp_path / "exports/operational/trade-view/trade-view-data.json",
        "trade_view_app": tmp_path / "exports/operational/trade-view/app.js",
    }
    payload = json.loads(paths[artifact].read_text(encoding="utf-8")) if artifact != "trade_view_app" else None
    if artifact == "analytics":
        payload["canonical_lifecycle_events_sha256"] = "0" * 64
    elif artifact == "health":
        payload["analytics_artifact_digest"] = "0" * 64
    elif artifact == "trade_view_app":
        paths[artifact].write_text(paths[artifact].read_text(encoding="utf-8") + "\n// stale asset\n", encoding="utf-8")
    else:
        payload["sources"]["health_artifact_digest"] = "0" * 64
    if artifact != "trade_view_app":
        paths[artifact].write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(projections.ProjectionStaleError, match="PROJECTION_STALE"):
        projections.verify_projections(tmp_path, target_trade=trade_id)


def test_missing_target_trade_view_row_is_fail_closed(tmp_path: Path) -> None:
    _store, _draft_id, trade_id = _make_fixture(ROOT, tmp_path)
    _refresh(tmp_path, trade_id)
    path = tmp_path / "exports/operational/trade-view/trade-view-data.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rows"] = [row for row in payload["rows"] if row.get("reference") != trade_id]
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(projections.ProjectionStaleError, match="target_trade.trade_view_present"):
        projections.verify_projections(tmp_path, target_trade=trade_id)


def test_non_countable_target_remains_visible(tmp_path: Path) -> None:
    _store, _draft_id, trade_id = _make_fixture(ROOT, tmp_path)
    _refresh(tmp_path, trade_id)
    analytics_path = tmp_path / "exports/operational/analytics/current.json"
    analytics = json.loads(analytics_path.read_text(encoding="utf-8"))
    canonical_row = next(row for row in analytics["canonical_read_model"] if row.get("trade_id") == trade_id)
    canonical_row["counted"] = False
    analytics["observations"] = [row for row in analytics["observations"] if row.get("trade_id") != trade_id]
    analytics["artifact_digest"] = projections._digest({key: value for key, value in analytics.items() if key != "artifact_digest"})
    analytics_path.write_text(json.dumps(analytics, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    analytics_manifest_path = tmp_path / "exports/operational/analytics/manifest.json"
    analytics_manifest = json.loads(analytics_manifest_path.read_text(encoding="utf-8"))
    analytics_manifest["artifact"]["sha256"] = hashlib.sha256(analytics_path.read_bytes()).hexdigest()
    analytics_manifest["artifact"]["artifact_digest"] = analytics["artifact_digest"]
    analytics_manifest["manifest_digest"] = projections._digest({key: value for key, value in analytics_manifest.items() if key != "manifest_digest"})
    analytics_manifest_path.write_text(json.dumps(analytics_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    refresh_health(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    build_trade_view(tmp_path)
    emit_handoff(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)

    result = projections.verify_projections(tmp_path, target_trade=trade_id)

    assert result["status"] == "CURRENT"
    assert result["target_visibility"]["countability"] == ["False"]


def test_cli_projection_refresh_and_verify(tmp_path: Path) -> None:
    _store, _draft_id, trade_id = _make_fixture(ROOT, tmp_path)

    refreshed = _run_cli(tmp_path, "projections", "refresh", "--generated-at", FIXED_GENERATED_AT, "--source-commit", FIXED_SOURCE_COMMIT, "--target-trade", trade_id)
    verified = _run_cli(tmp_path, "projections", "verify", "--target-trade", trade_id)

    assert refreshed["status"] == "CURRENT"
    assert verified["status"] == "CURRENT"
