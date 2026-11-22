"""Setup Observation: resolver evidence must bind to the saved images, and timing/provenance is never auto-promoted."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from resolver_helpers import matching_observations
from test_setup_observation import OBSERVED_AT, ROOT, _bind, _obs_files, _png, _prepared, _record
from trading_os.setup_observation import ObservationError, record_setup_observation


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _call(root, image, observations, **overrides):
    values = dict(
        symbol="BTCUSDT", direction="long", observed_at=OBSERVED_AT, timeframe_context="1h", timeframe_trigger="15m",
        reason_not_taken="missed", evidence_files=[image], strategy_observations=observations,
        recording_timing="contemporaneous", evidence_timing="pre_trade",
    )
    values.update(overrides)
    return record_setup_observation(root, **values)


def test_resolver_refs_bind_to_the_saved_artifact_by_sha_locator_or_filename(tmp_path):
    root = _prepared(tmp_path)
    image = _png(root)
    sha = _sha(image)
    for i, ref in enumerate([sha, f"artifact:{sha}.png", image.name]):
        obs = matching_observations("stg-vegas-tunnel")
        obs["observations"] = [{**o, "evidence_refs": [ref], "timing": "pre_trade"} for o in obs["observations"]]
        result = _call(root, image, obs, observed_at=f"2026-10-0{i + 1}T10:00:00+08:00")
        assert result["strategy_result"]["status"] == "MATCHED"
    saved = json.loads(_obs_files(root)[0].read_text(encoding="utf-8"))
    assert saved["evidence"][0]["sha256"] == sha
    assert saved["strategy_resolution"]["status"] == "contemporaneous_confirmed"


def test_unknown_evidence_refs_fail_closed_and_nothing_is_written(tmp_path):
    root = _prepared(tmp_path)
    image = _png(root)
    obs = matching_observations("stg-vegas-tunnel")  # cites fixture://chart, which is not saved with the observation
    with pytest.raises(ObservationError, match="not saved with this observation"):
        _call(root, image, obs)
    assert _obs_files(root) == []
    artifacts = root / "canonical" / "evidence" / "artifacts"
    assert not (artifacts.exists() and any(artifacts.iterdir()))  # validated before the image was ingested


def test_rule_timing_must_match_the_saved_evidence_timing(tmp_path):
    root = _prepared(tmp_path)
    image = _png(root)
    claims_pre_trade = _bind(matching_observations("stg-vegas-tunnel"), image, "pre_trade")
    with pytest.raises(ObservationError, match="does not match the saved evidence timing"):
        _call(root, image, claims_pre_trade, evidence_timing="post_trade")
    assert _obs_files(root) == []


def test_retrospective_post_event_image_cannot_satisfy_a_pre_trade_rule(tmp_path):
    root = _prepared(tmp_path)
    result = _record(root, matching_observations("stg-vegas-tunnel"), recording_timing="retrospective", evidence_timing="post_trade")
    assert result["strategy_result"]["status"] == "INSUFFICIENT_EVIDENCE"  # Vegas v1 only accepts pre_trade chart evidence
    saved = json.loads(_obs_files(root)[0].read_text(encoding="utf-8"))
    assert saved["recording_timing"] == "retrospective" and "strategy_resolution" not in saved
    assert saved["provenance"]["observed_at"] == OBSERVED_AT and saved["provenance"]["recorded_at"] != OBSERVED_AT


def test_retrospective_pre_trade_claim_needs_an_accepted_owner_adjudication(tmp_path):
    root = _prepared(tmp_path)
    image = _png(root)
    with pytest.raises(ObservationError, match="without an accepted Owner evidence adjudication"):
        _record(root, matching_observations("stg-vegas-tunnel"), image=image, recording_timing="retrospective", evidence_timing="pre_trade")
    adj_dir = root / "canonical" / "adjudications"
    adj_dir.mkdir(parents=True, exist_ok=True)
    (adj_dir / "adj_obs.json").write_text(
        json.dumps({"adjudication_id": "adj_obs", "decision_type": "evidence", "decision": "accept", "reviewer": "example-owner", "evidence_refs": [f"artifact:{_sha(image)}.png"]}),
        encoding="utf-8",
    )
    result = _record(
        root, matching_observations("stg-vegas-tunnel"), image=image, recording_timing="retrospective", evidence_timing="pre_trade",
        pre_trade_adjudication_id="adj_obs",
    )
    assert result["strategy_result"]["status"] == "MATCHED"
    saved = json.loads(_obs_files(root)[0].read_text(encoding="utf-8"))
    assert saved["strategy_resolution"]["status"] == "replay_reconstructed"  # never contemporaneous_confirmed
    assert saved["provenance"]["pre_trade_adjudication_id"] == "adj_obs"
    other = _png(root, "other.png", tag=b"y")  # an adjudication that does not name this image is refused
    with pytest.raises(ObservationError, match="does not name every saved evidence image"):
        _record(
            root, matching_observations("stg-vegas-tunnel"), image=other, recording_timing="retrospective", evidence_timing="pre_trade",
            pre_trade_adjudication_id="adj_obs", observed_at="2026-10-09T10:00:00+08:00",
        )


def test_cli_requires_explicit_timing_provenance(tmp_path):
    root = _prepared(tmp_path)
    image = _png(root)
    obs_file = root / "obs.json"
    obs_file.write_text(json.dumps(_bind(matching_observations("stg-vegas-tunnel"), image, "pre_trade")), encoding="utf-8")
    base = [
        sys.executable, "-m", "trading_os", "observe", "--symbol", "BTCUSDT", "--direction", "long", "--observed-at", OBSERVED_AT,
        "--timeframe-context", "1h", "--timeframe-trigger", "15m", "--reason-not-taken", "skipped",
        "--evidence-file", str(image), "--strategy-observations-file", str(obs_file),
    ]
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    for extra in ([], ["--recording-timing", "contemporaneous"], ["--evidence-timing", "pre_trade"]):
        done = subprocess.run(base + extra, cwd=root, capture_output=True, text=True, env=env)
        assert done.returncode == 2 and "required" in done.stderr
