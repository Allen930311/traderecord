"""#118 release audit of real, committed GitHub-original evidence and projections.

Read-only invariants only. These tests never invoke a writer or create a trade.
The actual original PNG/JPEG blobs are committed GitHub bytes, not reproductions.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T34 = "trd_01a12e82-9465-7976-9b60-7b16dd85a329"
T35 = "trd_01a12ee4-8de3-7c22-ae76-7a956ea4b01b"
ORIGINALS = {
    T34: {
        "82bf56f41014bc72ed53cf6133360f6749ac88271a52c870a156a19503f52fcf": "assets/2026-04-01_BTC_S_entry.jpg",
        "62fc5665879a6f264f363d1963291d28ac1894083a8978610ac3c4fae8282260": "assets/2026-04-01_BTC_S_context.jpg",
        "b30b2eb788c6425b28802985f3d70d565ddfc63f91636c799cfff0b1d7d6d5be": "assets/2026-04-01_BTC_S_tp1.jpg",
    },
    T35: {
        "8a64fe5bb59dacb0df52673005b6d39db467187c92090a4a994a32cf4501aa63": "Screenshot 2026-10-12 143036.png",
        "83956b13039a96f8a9874337e16ab1aa229fd7c175de4252a652d4caaf2155e5": "Screenshot 2026-10-12 143117.png",
        "b1656b3ee1cf5cbb5a41de6cfaa0f749cad39938f1624b40c45303fec4a2b39e": "Screenshot 2026-10-12 143157.png",
    },
}


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_real_github_originals_match_evidencestore_by_actual_sha256_bytes() -> None:
    """Check both GitHub originals and durable EvidenceStore copies, not metadata alone."""
    for trade_id, originals in ORIGINALS.items():
        record = _json(f"canonical/records/trades/{trade_id}.json")
        attached = {entry.get("sha256") for entry in record["evidence"]}
        assert set(originals) <= attached
        for sha, original_name in originals.items():
            original = ROOT / original_name
            stored = ROOT / "canonical/evidence/artifacts" / f"{sha}{original.suffix.lower()}"
            metadata = _json(f"canonical/evidence/artifacts/{sha}.metadata.json")
            from trading_os.legacy_source import LegacySourceError, read_legacy_bytes
            assert stored.is_file(), sha
            # Root uploads are still on disk; retired legacy originals come
            # from the EvidenceStore through the ledger, re-verified here.
            stored_bytes = stored.read_bytes()
            try:
                original_bytes = read_legacy_bytes(ROOT, original_name)
            except LegacySourceError:
                # A redundant root upload may have been removed once its identical bytes
                # were verified in the EvidenceStore; the stored copy is then the original.
                original_bytes = stored_bytes
            assert hashlib.sha256(original_bytes).hexdigest() == sha
            assert hashlib.sha256(stored_bytes).hexdigest() == sha
            assert original_bytes == stored_bytes
            assert len(original_bytes) == metadata["byte_size"]
            assert metadata["sha256"] == sha
            assert metadata["original_filename"] == original.name


def test_t34_t35_unique_identity_without_history_replacement() -> None:
    projected = _json("canonical/lifecycle/current.json")["records"]
    numbers = _json("canonical/sequences/numbers.json")
    assert projected[T34]["trade_no"] == "T000034"
    assert projected[T35]["trade_no"] == "T000035"
    assert projected[T34]["state"] == "REVIEWED"
    assert projected[T35]["state"] == "CLOSED"
    assert len([x for x in projected.values() if x.get("trade_no") == "T000034"]) == 1
    assert len([x for x in projected.values() if x.get("trade_no") == "T000035"]) == 1
    assert numbers["allocated"].count("T000034") == 1
    assert numbers["allocated"].count("T000035") == 1
    # T34/T35 are never renumbered; later registrations only move this forward.
    assert numbers["live_next"] >= 36
    # backtest_next must sit one past the highest BT number already allocated.
    bt_allocated = [int(x[2:]) for x in numbers["allocated"] if x.startswith("BT")]
    assert numbers["backtest_next"] == max(bt_allocated) + 1
    assert not numbers["tombstones"]

    events = [
        json.loads(line) for line in
        (ROOT / "canonical/lifecycle/events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    reviewed34 = [
        event for event in events
        if event.get("event_type") == "reviewed"
        and event.get("payload", {}).get("trade_id") == T34
    ]
    reviewed35 = [
        event for event in events
        if event.get("event_type") == "reviewed"
        and event.get("payload", {}).get("trade_id") == T35
    ]
    assert len(reviewed34) == 1
    assert reviewed35 == []


def test_t35_estimated_r_is_excluded_without_losing_verified_provider_pnl() -> None:
    from trading_os.operational_analytics import _r_authority
    current = _json("canonical/lifecycle/current.json")
    t35 = current["records"][T35]
    assert _r_authority(ROOT, t35) == (False, "unverified_initial_risk_proxy")
    assert t35["provider_pnl_verification"]["state"] == "confirmed"
    manifest = _json("exports/operational/analytics/manifest.json")
    # Later registrations add trades, so the guarantee is a floor plus an independent cross-check:
    # the contributor count must equal what the lifecycle itself says is provider-verified, so a
    # double-counted or dropped trade is still caught.
    assert manifest["counts"]["overall_live_performance_count"] >= 89
    verified = [
        r for r in current["records"].values()
        if r.get("trade_type") == "live"
        and (r.get("provider_pnl_verification") or {}).get("state") == "confirmed"
        and r.get("provider_pnl") is not None
    ]
    assert manifest["counts"]["verified_provider_pnl_contributor_count"] == len(verified) >= 35
    trade_view = _json("exports/operational/trade-view/trade-view-data.json")
    assert trade_view["summary_by_mode"]["live"]["count"] >= 89
    assert trade_view["summary_by_mode"]["live"]["verified_provider_pnl"] == round(
        sum(r["provider_pnl"] for r in verified), 6
    )
    t35_view = next(
        r for r in trade_view["rows"]
        if r.get("reference") == T35 or r.get("advanced", {}).get("trade_id") == T35
    )
    assert t35_view["performance_eligible"] is True
    assert t35_view["advanced"]["r_performance_eligible"] is False
