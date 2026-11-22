"""Setup Observation: an unexecuted setup is kept as obs_*, never as a trade or performance."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from resolver_helpers import matching_observations, no_match_observations
from test_historical_unresolved_live import _copy_operational_inputs
from test_registration_resolver_gate import _kwargs
from test_trade_view_v2 import _build
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.setup_observation import ObservationError, record_setup_observation
from trading_os.trade_review import review_registered_trade
from trading_os.unified_capture import capture_retrospective_live

ROOT = Path(__file__).resolve().parents[1]
OBSERVED_AT = "2026-10-05T10:00:00+08:00"


def _png(tmp_path, name="chart.png", tag=b"x"):
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + tag * 40)
    return path


def _bind(observations, image, timing):
    """Point every rule observation at the saved image (by file name) with the matching timing."""

    bound = dict(observations)
    bound["observations"] = [{**o, "evidence_refs": [image.name], "timing": timing} for o in observations.get("observations", [])]
    return bound


def _record(tmp_path, observations, **overrides):
    image = overrides.pop("image", None) or _png(tmp_path)
    timing = overrides.get("evidence_timing", "pre_trade")
    values = dict(
        symbol="BTCUSDT",
        direction="long",
        observed_at=OBSERVED_AT,
        timeframe_context="1h",
        timeframe_trigger="15m",
        reason_not_taken="missed",
        evidence_files=[image],
        strategy_observations=_bind(observations, image, timing),
        setup_quality="good",
        recording_timing="contemporaneous",
        evidence_timing="pre_trade",
    )
    values.update(overrides)
    return record_setup_observation(tmp_path, **values)


def _prepared(tmp_path):
    _copy_operational_inputs(ROOT, tmp_path)
    return tmp_path


def _obs_files(tmp_path):
    return sorted((tmp_path / "canonical" / "records" / "observations").glob("obs_*.json"))


def test_all_four_resolver_outcomes_can_be_stored(tmp_path):
    root = _prepared(tmp_path)
    ambiguous = matching_observations("stg-vegas-tunnel")
    ambiguous["observations"].append({"rule_id": "po3.identity", "state": "PASS", "timing": "pre_trade", "evidence_refs": ["x"]})
    cases = {
        "MATCHED": matching_observations("stg-vegas-tunnel"),
        "NO_MATCH": no_match_observations(),
        "AMBIGUOUS": ambiguous,
        "INSUFFICIENT_EVIDENCE": {},
    }
    for i, (expected, observations) in enumerate(cases.items()):
        result = _record(root, {**observations, "plan": {"stop_loss_price": 1.0, "target_price": 2.0}}, observed_at=f"2026-10-0{i + 1}T10:00:00+08:00")
        assert result["recorded"] is True and result["strategy_result"]["status"] == expected
        record = json.loads((root / "canonical" / "records" / "observations" / f"{result['observation_id']}.json").read_text(encoding="utf-8"))
        assert record["setup_observation"]["resolver_result"]["status"] == expected
        assert ("strategy_resolution" in record) == (expected in {"MATCHED", "NO_MATCH"})  # only resolved results carry one
        assert record["strategy"]["strategy_id"] == ("stg-vegas-tunnel" if expected == "MATCHED" else None)
    assert len(_obs_files(root)) == 4


def test_observation_is_not_a_trade(tmp_path):
    root = _prepared(tmp_path)
    store = LifecycleStore(root)
    result = _record(root, matching_observations("stg-vegas-tunnel"))
    record = json.loads(_obs_files(root)[0].read_text(encoding="utf-8"))
    assert record["record_kind"] == "observation" and record["type"] is None
    assert record["trade_id"] is None and record["trade_no"] is None
    assert record["observation_id"] == result["observation_id"] and result["observation_id"].startswith("obs_")
    assert record["setup_observation"]["executed"] is False and record["actual"]["execution_status"] == "not_executed"
    # no T-number, draft, identity link or lifecycle event
    assert json.loads((root / "canonical/sequences/numbers.json").read_text(encoding="utf-8"))["allocated"] == []
    assert not (root / "canonical" / "lifecycle" / "identity-links.jsonl").exists()
    assert store._read_events() == []
    drafts = root / "canonical" / "drafts"
    assert not (drafts.exists() and list(drafts.glob("*.json")))


def test_observation_never_enters_performance(tmp_path):
    root = _prepared(tmp_path)
    _build(root)
    base = json.loads((root / "exports/operational/analytics/current.json").read_text(encoding="utf-8"))
    _record(root, matching_observations("stg-vegas-tunnel"))
    data, analytics = _build(root)
    assert analytics["summary_by_mode"] == base["summary_by_mode"]
    assert analytics["metrics"]["global"] == base["metrics"]["global"]
    assert analytics["counts"]["performance_observation_count"] == base["counts"]["performance_observation_count"]
    obs_id = json.loads(_obs_files(root)[0].read_text(encoding="utf-8"))["observation_id"]
    assert obs_id not in json.dumps(analytics) and obs_id not in json.dumps(data["rows"])


def test_review_and_registration_refuse_an_observation(tmp_path):
    root = _prepared(tmp_path)
    store = LifecycleStore(root)
    obs_id = _record(root, matching_observations("stg-vegas-tunnel"))["observation_id"]
    with pytest.raises(LifecycleError, match="already-registered"):
        review_registered_trade(store, obs_id, {"rule_compliance": "compliant"}, reviewed_at="2026-10-05T11:00:00+08:00")


def test_requires_evidence_and_valid_vocabulary_and_writes_nothing_on_failure(tmp_path):
    root = _prepared(tmp_path)
    obs = matching_observations("stg-vegas-tunnel")
    with pytest.raises(ObservationError, match="at least one evidence"):
        _record(root, obs, evidence_files=[])
    with pytest.raises(ObservationError, match="reason_not_taken"):
        _record(root, obs, reason_not_taken="forgot")
    with pytest.raises(ObservationError, match="reason_note"):
        _record(root, obs, reason_not_taken="other")
    with pytest.raises(ObservationError, match="direction"):
        _record(root, obs, direction="up")
    with pytest.raises(Exception):  # missing evidence file fails closed in EvidenceStore
        _record(root, obs, evidence_files=[root / "nope.png"])
    assert _obs_files(root) == []


def test_same_setup_is_recorded_once(tmp_path):
    root = _prepared(tmp_path)
    obs = matching_observations("stg-vegas-tunnel")
    first = _record(root, obs)
    again = _record(root, obs)
    assert again["already_recorded"] is True and again["observation_id"] == first["observation_id"]
    assert len(_obs_files(root)) == 1
    other = _record(root, obs, observed_at="2026-10-05T12:00:00+08:00")
    assert other["observation_id"] != first["observation_id"] and len(_obs_files(root)) == 2


def test_cli_observe_records_and_a_registered_trade_is_unaffected(tmp_path):
    root = _prepared(tmp_path)
    image = _png(root)
    obs_file = root / "obs.json"
    obs_file.write_text(json.dumps(_bind(matching_observations("stg-vegas-tunnel"), image, "pre_trade")), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable, "-m", "trading_os", "observe", "--symbol", "BTCUSDT", "--direction", "long", "--observed-at", OBSERVED_AT,
            "--timeframe-context", "1h", "--timeframe-trigger", "15m", "--reason-not-taken", "skipped", "--setup-quality", "good",
            "--evidence-file", str(image), "--strategy-observations-file", str(obs_file),
            "--recording-timing", "contemporaneous", "--evidence-timing", "pre_trade",
        ],
        cwd=root, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["strategy_result"]["status"] == "MATCHED"
    # a real trade registered afterwards still gets the first T-number, untouched by observations
    trade = capture_retrospective_live(LifecycleStore(root), **_kwargs(matching_observations("stg-vegas-tunnel")))
    assert trade["trade_no"] == "T000001"
    missing = subprocess.run(
        [sys.executable, "-m", "trading_os", "observe", "--symbol", "BTCUSDT", "--direction", "long", "--observed-at", OBSERVED_AT,
         "--timeframe-context", "1h", "--timeframe-trigger", "15m", "--reason-not-taken", "skipped", "--strategy-observations-file", str(obs_file),
         "--recording-timing", "contemporaneous", "--evidence-timing", "pre_trade"],
        cwd=root, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert missing.returncode == 2 and "evidence" in (missing.stdout + missing.stderr)
