"""The 2026-09-22 SOL incident is a fixture for generic retrospective attribution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_historical_unresolved_live import _copy_operational_inputs
from trading_os.evidence_store import EvidenceStore
from trading_os.operational_lifecycle import LifecycleStore
from trading_os.strategy_resolver import resolve_strategy
from trading_os.unified_capture import capture_retrospective_live


ROOT = Path(__file__).resolve().parents[1]
SOL_TRADES = (
    ("T000025", "2609221733349255", "trd_01a1173e-0acf-74da-8812-2e3642b1c09f"),
    ("T000026", "2609221734119259", "trd_01a1173e-1201-78f3-9898-37bd37c6e46e"),
)


def _packet_for_open_id(open_id: str) -> dict:
    for path in (ROOT / "canonical/registration/packets").glob("*.json"):
        packet = json.loads(path.read_text(encoding="utf-8"))
        facts = packet.get("provider_facts", {})
        if str(facts.get("provider_lifecycle_id")) == open_id:
            return packet
    raise AssertionError(f"registration packet missing for SOL fixture {open_id}")


@pytest.mark.parametrize(("source_trade_no", "open_id", "source_trade_id"), SOL_TRADES)
def test_sol_incident_owner_attestation_resolves_without_relabeling_replay_or_mutating_source(
    tmp_path: Path,
    source_trade_no: str,
    open_id: str,
    source_trade_id: str,
):
    source_packets = {candidate_open_id: _packet_for_open_id(candidate_open_id) for _, candidate_open_id, _ in SOL_TRADES}
    source_packet = source_packets[open_id]
    source_paths = {
        ROOT / "canonical/records/trades" / f"{candidate_trade_id}.json"
        for _, _, candidate_trade_id in SOL_TRADES
    }
    source_paths.update(
        path for path in (
            ROOT / "canonical/lifecycle/current.json",
            ROOT / "canonical/lifecycle/events.jsonl",
            ROOT / "canonical/lifecycle/identity-links.jsonl",
            ROOT / "canonical/sequences/numbers.json",
            ROOT / "canonical/sequences/finalizations.json",
        ) if path.exists()
    )
    source_paths.update(
        ROOT / "canonical/registration/packets" / f"{packet['packet_sha256']}.json"
        for packet in source_packets.values()
    )
    for packet in source_packets.values():
        for image in packet.get("image_evidence", []):
            digest = image.get("sha256")
            if isinstance(digest, str):
                artifact_root = ROOT / "canonical/evidence/artifacts"
                source_paths.update(
                    path for path in (
                        artifact_root / f"{digest}.png",
                        artifact_root / f"{digest}.jpg",
                        artifact_root / f"{digest}.jpeg",
                        artifact_root / f"{digest}.webp",
                        artifact_root / f"{digest}.metadata.json",
                    ) if path.exists()
                )
    source_bytes = {path: path.read_bytes() for path in source_paths}
    facts = source_packet["provider_facts"]
    images = source_packet["image_evidence"]
    assert source_packet["owner_confirmation"]["status"] == "confirmed"
    assert len(images) == 3
    assert all(item["category"] == "review" and item["timing"] == "post_trade" for item in images)

    # Build an isolated copy from the exact provider facts. This intentionally
    # does not consume or rewrite the incident's frozen resolver_input, whose
    # historical fake-pre_trade observations are the defect under repair.
    _copy_operational_inputs(ROOT, tmp_path)
    store = LifecycleStore(tmp_path)
    provider_timestamp = facts["entry_time"]
    entry_price = float(facts["entry_price"])
    sha_char = "a" if open_id.endswith("55") else "b"
    kwargs = {
        "strategy_observations": {},
        "symbol": "SOLUSDT",
        "direction": "long",
        "intended_entry": "unknown",
        "stop_loss": 114.983,
        "take_profit": 120.230,
        "planned_rr": "not_observable",
        "timeframe_context": "unknown",
        "timeframe_trigger": "15m",
        "market_context": "MSS above Vegas Tunnel with a pullback to support OB",
        "setup_tags": [],
        "confirmation_tags": [],
        "thesis": "Owner retrospective attribution fixture",
        "chart_refs": [],
        "created_at": provider_timestamp,
        "provider_evidence": {
            "source_ref": f"fixture://propw-entry-{open_id}",
            "timing": "execution",
            "type": "broker_fill",
            "category": "entry",
            "sha256": sha_char * 64,
            "captured_at": provider_timestamp,
        },
        "provider": facts["provider"],
        "provider_lifecycle_id": open_id,
        "provider_execution_timestamp": provider_timestamp,
        "candidate_source": "fixture://provider-ledger-match",
        "actual_entry": entry_price,
        "outcome_known_at_recording": True,
    }
    captured = capture_retrospective_live(store, **kwargs)
    trade_id, draft_id, trade_no = captured["trade_id"], captured["draft_id"], captured["trade_no"]
    assert trade_no.startswith("T")

    source_lifecycle = json.loads((ROOT / "canonical/lifecycle/current.json").read_text(encoding="utf-8"))["records"][source_trade_id]
    store.final_close(
        trade_id,
        exit_price=source_lifecycle["close"]["exit_price"],
        closed_at=source_lifecycle["close"]["closed_at"],
        realized_r=source_lifecycle["realized_r"],
        provider_pnl=source_lifecycle["provider_pnl"],
        source_ref=f"fixture://provider-exit-{open_id}",
    )

    replay_refs = []
    evidence_store = EvidenceStore(tmp_path)
    for item in images:
        image_path = ROOT / "canonical/evidence/artifacts" / f"{item['sha256']}.png"
        ingested = evidence_store.ingest(image_path)
        assert ingested["sha256"] == item["sha256"]
        assert ingested["locator"] == item["locator"]
        replay_refs.append(item["locator"])
        store.attach_evidence(
            draft_id,
            {
                **ingested,
                "source_ref": item["locator"],
                "category": "review",
                "timing": "post_trade",
                "type": "replay_chart",
                "provenance": {"capture_mode": "retrospective_replay"},
                "role": "historical_market_reconstruction",
            },
        )

    resolver_input = {
        "owner_attestation": {
            "timing": "retrospective",
            "setup_status": "HAS_SETUP",
            "primary_strategy_id": "stg-ict-liquidity",
            "entry_expression": "ob-mss",
            "entry_triggers": ["MSS", "OB_RETEST"],
            "confluences": ["VEGAS_TUNNEL"],
            "rationale": "MSS 出現在 Vegas Tunnel 上方，而且回踩支撐 OB。",
            "plan_facts": {"stop_loss_price": 114.983, "target_price": 120.230},
            "evidence_refs": replay_refs,
        },
        "retrospective_observations": [
            {"claim_id": "entry_trigger:MSS", "finding": "not_observable", "timing": "post_trade", "evidence_refs": replay_refs},
            {"claim_id": "entry_trigger:OB_RETEST", "finding": "not_observable", "timing": "post_trade", "evidence_refs": replay_refs},
            {"claim_id": "confluence:vegas_tunnel", "finding": "corroborates", "timing": "post_trade", "evidence_refs": replay_refs},
        ],
        "plan": {"stop_loss_price": 114.983, "target_price": 120.230},
    }
    result = resolve_strategy(
        ROOT,
        resolver_input,
        provider_entry_at=provider_timestamp,
        provider_execution_verified=True,
    )
    assert result["status"] == "MATCHED"
    assert result["strategy_id"] == "stg-ict-liquidity"
    assert result["resolution_authority"] == "owner_retrospective_strategy_attribution"
    assert all(item["timing"] == "post_trade" for item in result["retrospective_observations"])
    assert result["confluences"] == ["vegas_tunnel"]
    assert result["confluence_types"] == {"vegas_tunnel": "context"}
    assert result["confidence"] == "medium"
    assert {"provider_verified", "owner_attested", "retrospective_reconstructed", "not_observable"} <= set(result["evidence_quality"])

    store.resolve_strategy_attribution(
        trade_id,
        resolver_input=resolver_input,
        amendment_id=f"owner-retrospective-{open_id}",
        resolved_at="2026-10-08T00:00:00Z",
    )
    current = store.current()["records"][trade_id]
    assert current["state"] == "CLOSED"
    assert current["trade_no"] == trade_no
    assert current["strategy_id"] == "stg-ict-liquidity"
    assert current["strategy_resolution"]["status"] == "replay_reconstructed"
    assert current["strategy_resolution"]["entry_expression"] == "ob-mss"
    assert current["strategy_resolution"]["entry_triggers"] == ["MSS", "OB_RETEST"]
    assert current["strategy_resolution"]["confluences"] == ["vegas_tunnel"]
    assert current["strategy_resolution"]["confluence_types"] == {"vegas_tunnel": "context"}
    assert current["strategy_resolution"]["owner_attestation"]["timing"] == "retrospective"
    replay_evidence = [evidence for evidence in current["evidence"] if evidence.get("source_ref") in replay_refs]
    assert all(
        evidence["category"] == "review" and evidence["timing"] == "post_trade"
        and evidence["provenance"]["capture_mode"] == "retrospective_replay"
        and evidence["provenance"]["capture_time_status"] == "not_observable"
        and evidence["role"] == "historical_market_reconstruction"
        for evidence in replay_evidence
    )
    assert len(replay_evidence) == 3
    assert all(path.read_bytes() == data for path, data in source_bytes.items())
    assert source_trade_no in json.loads((ROOT / "canonical/sequences/numbers.json").read_text(encoding="utf-8"))["allocated"]
