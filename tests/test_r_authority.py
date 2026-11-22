"""R authority regression: post-trade stop proxies must not contaminate LIVE R.

No canonical fixtures are modified and provider P&L stays independently eligible.
"""
from pathlib import Path
import json
import pytest

from trading_os.operational_analytics import _r_authority, _metrics, OperationalAnalyticsError


def test_real_t35_packet_unknown_original_stop_excludes_only_r():
    root = Path(__file__).resolve().parents[1]
    lifecycle = json.loads((root / "canonical/lifecycle/current.json").read_text(encoding="utf-8"))
    trade = lifecycle["records"]["trd_01a12ee4-8de3-7c22-ae76-7a956ea4b01b"]
    assert trade["trade_no"] == "T000035"
    assert trade["provider_pnl_verification"]["state"] == "confirmed"
    assert trade["planned"]["stop_loss_price"] == "unknown"
    assert _r_authority(root, trade) == (False, "unverified_initial_risk_proxy")


def test_unverified_r_is_not_live_r_denominator_but_provider_pnl_remains():
    data = [
        {
            "trade_no": "SYNTHETIC-RISK-PROXY", "sample_origin": "live",
            "performance_eligible": True, "r_performance_eligible": False,
            "realized_r": -1.054886, "provider_pnl": -24.10953153,
            "provider_pnl_verified": True, "event_date": "2026-10-08",
        },
        {
            "trade_no": "SYNTHETIC-VERIFIED", "sample_origin": "live",
            "performance_eligible": True, "r_performance_eligible": True,
            "realized_r": 1.0, "provider_pnl": 10.0,
            "provider_pnl_verified": True, "event_date": "2026-10-09",
        },
    ]
    result = _metrics(data, {})
    assert result["count"] == 1
    assert result["total_r"] == 1.0
    assert result["win_rate"] == 1.0
    assert result["provider_pnl_observation_count"] == 2
    assert result["verified_provider_pnl"] == -14.109532


def test_r_authority_structured_packet_only(tmp_path):
    lifecycle = {"registration_packet_sha256": "a" * 64}
    packet_path = tmp_path / "canonical/registration/packets" / ("a" * 64 + ".json")
    packet_path.parent.mkdir(parents=True)
    packet = {
        "packet_sha256": "a" * 64,
        "canonical_capture": {
            "realized_r_authority": "deterministic_execution_calculation",
            "stop_loss": "unknown",
        },
        "agent_assessment": {"realized_r_estimation": "Free text is NOT a source of R authority"},
    }
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    assert _r_authority(tmp_path, lifecycle)[0] is False
    packet["canonical_capture"]["stop_loss"] = 100.0
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    assert _r_authority(tmp_path, lifecycle)[0] is True
    packet["packet_sha256"] = "b" * 64
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    with pytest.raises(OperationalAnalyticsError, match="SHA binding"):
        _r_authority(tmp_path, lifecycle)


def test_real_t32_owner_attested_r_without_original_stop_excludes_only_r():
    """Owner decision 2026-10-12: attested 0.9R has no recorded original stop."""
    root = Path(__file__).resolve().parents[1]
    lifecycle = json.loads((root / "canonical/lifecycle/current.json").read_text(encoding="utf-8"))
    trade = lifecycle["records"]["trd_01a11cdf-d73b-7f57-a807-5b711e5ba3ff"]
    assert trade["trade_no"] == "T000032"
    assert trade["realized_r"] == 0.9
    assert trade["provider_pnl_verification"]["state"] == "confirmed"
    assert _r_authority(root, trade) == (False, "unverified_initial_risk_owner_attested")


def test_owner_attested_r_needs_recorded_original_stop(tmp_path):
    lifecycle = {"registration_packet_sha256": "c" * 64}
    packet_path = tmp_path / "canonical/registration/packets" / ("c" * 64 + ".json")
    packet_path.parent.mkdir(parents=True)
    packet = {
        "packet_sha256": "c" * 64,
        "canonical_capture": {"realized_r_authority": "owner_attestation", "stop_loss": "unknown"},
    }
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    assert _r_authority(tmp_path, lifecycle) == (False, "unverified_initial_risk_owner_attested")
    packet["canonical_capture"]["stop_loss"] = 29.4
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    assert _r_authority(tmp_path, lifecycle) == (True, "registered_r_authority_unmodified")


def test_every_canonical_trade_appears_once_in_trade_view(tmp_path):
    """An R-excluded counted trade (T000032) must stay visible in Trade View."""
    import shutil
    from trading_os.trade_view import build_trade_view
    root = Path(__file__).resolve().parents[1]
    build_trade_view(root, output_dir=tmp_path)
    view = json.loads((tmp_path / "trade-view-data.json").read_text(encoding="utf-8"))
    lifecycle = json.loads((root / "canonical/lifecycle/current.json").read_text(encoding="utf-8"))

    def refs(row):
        adv = row.get("advanced") or {}
        return {row.get("reference"), row.get("trade_no"), adv.get("trade_id"), adv.get("trade_no")} - {None}

    for trade_id, record in lifecycle["records"].items():
        hits = [row for row in view["rows"] if {trade_id, record["trade_no"]} & refs(row)]
        assert len(hits) == 1, (record["trade_no"], len(hits))
    t32 = next(row for row in view["rows"] if "T000032" in refs(row) or
               "trd_01a11cdf-d73b-7f57-a807-5b711e5ba3ff" in refs(row))
    assert t32["performance_eligible"] is True
    assert t32["advanced"]["r_performance_eligible"] is False
