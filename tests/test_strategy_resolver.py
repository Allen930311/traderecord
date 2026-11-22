from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from trading_os.operational_lifecycle import LifecycleError, _validate_strategy_resolution
from trading_os.strategy_resolver import ResolverError, resolve_strategy, to_lifecycle_resolution

ROOT = Path(__file__).resolve().parents[1]
PLAN = {"stop_loss_price": 1.0, "target_price": 2.0}


def obs(rule_id, state="PASS", timing="pre_trade", refs=("chart:1",)):
    return {"rule_id": rule_id, "state": state, "timing": timing, "evidence_refs": list(refs)}


def vegas_pass():
    return [obs("vegas.tunnel.touch")]


ALL = ["stg-vegas-tunnel", "stg-po3", "stg-ict-liquidity", "stg-triple-tap", "stg-example-a"]


def test_vegas_match():
    r = resolve_strategy(ROOT, {"observations": vegas_pass(), "plan": PLAN})
    assert r["status"] == "MATCHED" and r["strategy_id"] == "stg-vegas-tunnel" and r["rule_version"] == "stg-vegas-tunnel:v1"
    assert r["resolved"] is True
    lifecycle = to_lifecycle_resolution(r, contemporaneous=True)
    trace = lifecycle["rule_observations"]
    assert trace["schema_version"] == "resolved-rule-observations.v2"
    assert trace["strategy_id"] == "stg-vegas-tunnel"
    assert trace["rule_version"] == "stg-vegas-tunnel:v1"
    assert trace["rules"] == [{
        "rule_id": "vegas.tunnel.touch",
        "state": "PASS",
        "code": "observed",
        "evidence_refs": ["chart:1"],
        "supports_fact_timing": "pre_trade",
        "basis": ["contemporaneous_capture"],
    }]
    _validate_strategy_resolution(lifecycle)


def test_per_rule_simple_owner_attestation_lane_is_rejected():
    observation = {
        "rule_id": "vegas.tunnel.touch",
        "state": "PASS",
        "supports_fact_timing": "pre_trade",
        "basis": ["retrospective_replay", "owner_attestation"],
        "owner_attestation": {
            "status": "confirmed",
            "statement": "I remember the Vegas setup was present before entry.",
        },
        "evidence_refs": ["chart:replay"],
    }
    with pytest.raises(ResolverError, match="per-rule owner_attestation is not an authority schema"):
        resolve_strategy(ROOT, {"observations": [observation], "plan": PLAN})

    observation.pop("owner_attestation")
    with pytest.raises(ResolverError, match="basis must contain unique supported values"):
        resolve_strategy(ROOT, {"observations": [observation], "plan": PLAN})


def test_nothing_observed_is_insufficient_not_no_match():
    r = resolve_strategy(ROOT, {})
    assert r["status"] == "INSUFFICIENT_EVIDENCE" and r["resolved"] is False
    lifecycle = to_lifecycle_resolution(r, contemporaneous=True)
    assert lifecycle["status"] == "unresolved"
    assert lifecycle["resolver_status"] == "INSUFFICIENT_EVIDENCE"
    assert lifecycle["strategy_id"] is None and lifecycle["rule_version"] is None
    assert lifecycle["strategy_stats_eligible"] is False
    _validate_strategy_resolution(lifecycle)


def test_all_fail_is_no_match():
    fails = [obs(rid, "FAIL") for rid in ("vegas.tunnel.touch", "po3.identity", "ict_liquidity.identity", "triple_tap.identity")]
    fails.extend(obs(rule_id, "FAIL") for rule_id in (
        "example_a.direction_locked",
        "example_a.range_frozen",
        "example_a.first_entry",
        "example_a.initial_stop",
        "example_a.conditional_retry",
        "example_a.first_target",
        "example_a.second_target",
        "example_a.expiry",
    ))
    r = resolve_strategy(ROOT, {"observations": fails, "plan": PLAN})
    assert r["status"] == "NO_MATCH"
    res = to_lifecycle_resolution(r, contemporaneous=False)
    assert res["status"] == "no_match" and res["strategy_id"] is None and res["strategy_stats_eligible"] is False
    _validate_strategy_resolution(res)  # accepted by the lifecycle validator


def test_owner_no_setup_attribution_overrides_visual_strategy_match():
    owner = {
        "setup_status": "NO_SETUP",
        "reason": "I did not enter this trade because of any setup",
        "evidence_refs": ["owner:trade-review"],
    }
    r = resolve_strategy(
        ROOT,
        {"observations": vegas_pass(), "plan": PLAN, "owner_setup_attribution": owner},
    )
    assert any(m["strategy_id"] == "stg-vegas-tunnel" and m["result"] == "MATCH" for m in r["matrix"])
    assert r["status"] == "NO_MATCH"
    assert r["resolved"] is True
    assert r["strategy_id"] is None and r["rule_version"] is None
    assert r["resolution_authority"] == "owner_setup_attribution"
    lifecycle = to_lifecycle_resolution(r, contemporaneous=False)
    assert lifecycle["status"] == "no_match"
    assert lifecycle["strategy_stats_eligible"] is False


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"setup_status": "HAS_SETUP", "reason": "x", "evidence_refs": ["owner:x"]},
        {"setup_status": "NO_SETUP", "reason": " ", "evidence_refs": ["owner:x"]},
        {"setup_status": "NO_SETUP", "reason": "x", "evidence_refs": []},
    ],
)
def test_owner_no_setup_attribution_fails_closed_when_malformed(bad):
    with pytest.raises(ResolverError):
        resolve_strategy(ROOT, {"owner_setup_attribution": bad})


def test_owner_no_match_requires_full_coverage_reason_and_refs():
    good = {"reviewed_strategy_ids": ALL, "reason": "no setup visible", "evidence_refs": ["img:1"]}
    assert resolve_strategy(ROOT, {"owner_no_match": good})["status"] == "NO_MATCH"
    for bad in (
        {**good, "reviewed_strategy_ids": ALL[:3]},
        {**good, "reason": " "},
        {**good, "evidence_refs": []},
    ):
        assert resolve_strategy(ROOT, {"owner_no_match": bad})["status"] == "INSUFFICIENT_EVIDENCE"


def test_conflicting_owner_no_match_when_identity_passes_is_not_no_match():
    r = resolve_strategy(ROOT, {"observations": [obs("po3.identity")], "owner_no_match": {"reviewed_strategy_ids": ALL, "reason": "x", "evidence_refs": ["i"]}})
    assert r["status"] == "INSUFFICIENT_EVIDENCE"


def test_two_unrelated_matches_are_ambiguous():
    po3 = [obs(rid) for rid in ("po3.identity", "po3.phase_sequence", "po3.directional_confirmation", "po3.psar_confirmation")]
    r = resolve_strategy(ROOT, {"observations": vegas_pass() + po3, "plan": PLAN})
    assert r["status"] == "AMBIGUOUS"


def test_match_with_other_identity_passing_but_undecided_is_ambiguous():
    r = resolve_strategy(ROOT, {"observations": vegas_pass() + [obs("po3.identity")], "plan": PLAN})
    assert r["status"] == "AMBIGUOUS"


def test_disallowed_timing_is_undecided():
    r = resolve_strategy(ROOT, {"observations": [obs("vegas.tunnel.touch", timing="post_trade")], "plan": PLAN})
    assert r["status"] != "MATCHED"


def test_child_beats_parent_when_both_match():
    ict = ["ict_liquidity.identity", "ict_liquidity.entry_expression", "triple_tap.identity", "triple_tap.liquidity_touch_count", "triple_tap.rsi_divergence", "triple_tap.mss_confirmation"]
    r = resolve_strategy(ROOT, {"observations": [obs(x) for x in ict], "plan": PLAN})
    assert r["status"] == "MATCHED" and r["strategy_id"] == "stg-triple-tap"


def _owner_attestation(**overrides):
    value = {
        "timing": "retrospective",
        "setup_status": "HAS_SETUP",
        "primary_strategy_id": "stg-ict-liquidity",
        "entry_expression": "ob-mss",
        "entry_triggers": ["MSS", "OB_RETEST"],
        "confluences": ["stg-vegas-tunnel"],
        "rationale": "MSS appeared above Vegas Tunnel and price retested supporting OB.",
        "plan_facts": {"stop_loss_price": 114.983, "target_price": 120.23},
        "evidence_refs": ["artifact:replay-1", "artifact:replay-2", "artifact:replay-3"],
    }
    value.update(overrides)
    return value


def test_retrospective_owner_attestation_accepts_post_trade_replay_without_pretrade_claims():
    replay_refs = ["artifact:replay-1", "artifact:replay-2", "artifact:replay-3"]
    resolver_input = {
        "owner_attestation": _owner_attestation(),
        "retrospective_observations": [
            {"claim_id": "entry_trigger:MSS", "finding": "not_observable", "timing": "post_trade", "evidence_refs": replay_refs},
            {"claim_id": "entry_trigger:OB_RETEST", "finding": "not_observable", "timing": "post_trade", "evidence_refs": replay_refs},
            {"claim_id": "confluence:stg-vegas-tunnel", "finding": "corroborates", "timing": "post_trade", "evidence_refs": replay_refs},
        ],
        "plan": {"stop_loss_price": 114.983, "target_price": 120.23},
    }
    result = resolve_strategy(
        ROOT,
        resolver_input,
        provider_entry_at="2026-09-22T09:33:35Z",
        provider_execution_verified=True,
    )
    assert "observations" not in resolver_input  # replay evidence is not fabricated into rule observations
    assert result["status"] == "MATCHED"
    assert result["strategy_id"] == "stg-ict-liquidity"
    assert result["rule_version"] == "stg-ict-liquidity:v1"
    assert result["resolution_authority"] == "owner_retrospective_strategy_attribution"
    assert result["confidence"] == "medium"
    ict = next(item for item in result["matrix"] if item["strategy_id"] == "stg-ict-liquidity")
    setup_rules = [rule for rule in ict["rules"] if rule["rule_id"] in {"ict_liquidity.identity", "ict_liquidity.entry_expression"}]
    assert all(rule["state"] == "UNKNOWN" for rule in setup_rules)
    assert all("artifact:replay-" not in " ".join(rule["evidence_refs"]) for rule in ict["rules"])

    lifecycle = to_lifecycle_resolution(result, contemporaneous=False)
    assert lifecycle["status"] == "replay_reconstructed"
    assert lifecycle["entry_expression"] == "ob-mss"
    assert lifecycle["entry_triggers"] == ["MSS", "OB_RETEST"]
    assert lifecycle["confluences"] == ["stg-vegas-tunnel"]
    assert lifecycle["owner_attestation"]["timing"] == "retrospective"
    assert all(item["timing"] == "post_trade" for item in lifecycle["retrospective_observations"])


def test_retrospective_owner_attestation_does_not_require_any_screenshot():
    result = resolve_strategy(
        ROOT,
        {"owner_attestation": _owner_attestation(evidence_refs=[])},
        provider_entry_at="2026-09-22T09:33:35Z",
    )
    assert result["status"] == "MATCHED"
    assert result["strategy_id"] == "stg-ict-liquidity"
    assert result["confidence"] == "medium"


@pytest.mark.parametrize(
    ("strategy_id", "rule_version"),
    [
        ("stg-vegas-tunnel", "stg-vegas-tunnel:v1"),
        ("stg-po3", "stg-po3:v1"),
        ("stg-example-a", "stg-example-a:v2"),
    ],
)
def test_positive_owner_attribution_does_not_require_undefined_entry_expression(strategy_id, rule_version):
    attestation = _owner_attestation(
        primary_strategy_id=strategy_id,
        entry_expression=None,
        entry_triggers=[],
        confluences=[],
        rationale=f"Owner confirms this entry used {strategy_id}.",
    )
    if strategy_id == "stg-example-a":
        attestation["rationale"] = "亞盤前出現 mss 所以看多進廠在亞盤結束時 於 0.618進場 這是 AA策略"
        attestation["evidence_refs"] = ["image:entry", "image:exit"]
    result = resolve_strategy(ROOT, {"owner_attestation": attestation})

    assert result["status"] == "MATCHED"
    assert result["strategy_id"] == strategy_id
    assert result["rule_version"] == rule_version
    assert result["resolution_authority"] == "owner_retrospective_strategy_attribution"
    assert "entry_expression" not in result["owner_attestation"]
    assert result["owner_attestation"]["entry_triggers"] == []
    if strategy_id == "stg-example-a":
        assert result["owner_attestation"]["rationale"] == "亞盤前出現 mss 所以看多進廠在亞盤結束時 於 0.618進場 這是 AA策略"
        assert result["owner_attestation"]["evidence_refs"] == ["image:entry", "image:exit"]
    lifecycle = to_lifecycle_resolution(result, contemporaneous=False)
    assert lifecycle["strategy_id"] == strategy_id
    assert "entry_expression" not in lifecycle


def test_positive_owner_attribution_keeps_expression_optional_for_strategy_that_defines_it():
    attestation = _owner_attestation(entry_expression=None, entry_triggers=[])
    result = resolve_strategy(ROOT, {"owner_attestation": attestation})

    assert result["status"] == "MATCHED"
    assert result["strategy_id"] == "stg-ict-liquidity"
    assert "entry_expression" not in result["owner_attestation"]


def test_material_post_trade_timeline_contradiction_keeps_owner_strategy_unresolved():
    result = resolve_strategy(
        ROOT,
        {
            "owner_attestation": _owner_attestation(),
            "retrospective_observations": [
                {
                    "claim_id": "entry_trigger:MSS",
                    "finding": "corroborates",
                    "timing": "post_trade",
                    "observed_at": "2026-09-22T09:53:35Z",
                    "evidence_refs": ["artifact:replay-1"],
                }
            ],
            "plan": {"stop_loss_price": 114.983, "target_price": 120.23},
        },
        provider_entry_at="2026-09-22T09:33:35Z",
        provider_execution_verified=True,
    )
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["resolved"] is False
    assert result["strategy_id"] is None
    assert result["material_conflicts"][0]["claim_id"] == "entry_trigger:MSS"
    lifecycle = to_lifecycle_resolution(result, contemporaneous=False)
    assert lifecycle["status"] == "unresolved"
    assert lifecycle["strategy_id"] is None
    assert lifecycle["material_conflicts"]


def test_retrospective_owner_plan_is_checked_against_persisted_plan_context():
    result = resolve_strategy(
        ROOT,
        {
            "owner_attestation": _owner_attestation(
                plan_facts={"stop_loss_price": 114.983, "target_price": 120.23}
            ),
            # A caller-provided resolver file cannot replace the persisted plan.
            "plan": {"stop_loss_price": 114.983, "target_price": 120.23},
        },
        plan_context={"stop_loss_price": 115.0, "target_price": 120.23},
    )
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["strategy_id"] is None
    assert result["material_conflicts"][0]["claim_id"] == "plan:stop_loss_price"
    assert result["material_conflicts"][0]["recorded_value"] == 115.0


def test_retrospective_owner_attestation_rejects_non_registry_identity_or_expression():
    with pytest.raises(ResolverError, match="active strategy"):
        resolve_strategy(ROOT, {"owner_attestation": _owner_attestation(primary_strategy_id="made-up")})
    with pytest.raises(ResolverError, match="active frozen strategy rules"):
        resolve_strategy(ROOT, {"owner_attestation": _owner_attestation(entry_expression="made-up")})
    with pytest.raises(ResolverError, match="does not define that dimension"):
        resolve_strategy(
            ROOT,
            {"owner_attestation": _owner_attestation(primary_strategy_id="stg-vegas-tunnel", entry_expression="fake-expression")},
        )
    with pytest.raises(ResolverError, match="entry_triggers conflict"):
        resolve_strategy(ROOT, {"owner_attestation": _owner_attestation(entry_triggers=["MSS"])})


def test_vegas_tunnel_context_confluence_keeps_ict_as_primary_strategy():
    result = resolve_strategy(
        ROOT,
        {
            "owner_attestation": _owner_attestation(confluences=["VEGAS_TUNNEL"]),
        },
    )
    assert result["status"] == "MATCHED"
    assert result["strategy_id"] == "stg-ict-liquidity"
    assert result["confluences"] == ["vegas_tunnel"]
    assert result["confluence_types"] == {"vegas_tunnel": "context"}


def test_entry_expression_contract_is_registry_driven_without_resolver_edits(tmp_path: Path):
    shutil.copytree(ROOT / "registry", tmp_path / "registry")
    version_path = tmp_path / "registry/versions/stg-ict-liquidity-v1.yaml"
    version = yaml.safe_load(version_path.read_text(encoding="utf-8"))
    rule = next(item for item in version["rules"] if item["rule_id"] == "ict_liquidity.entry_expression")
    rule["operands"]["values"].append("custom-pullback")
    version_path.write_text(yaml.safe_dump(version, sort_keys=False), encoding="utf-8")

    authority_path = tmp_path / "registry/retrospective-attribution.yaml"
    authority = yaml.safe_load(authority_path.read_text(encoding="utf-8"))
    authority["entry_expressions"].append(
        {
            "strategy_id": "stg-ict-liquidity",
            "rule_version": "stg-ict-liquidity:v1",
            "expression_id": "custom-pullback",
            "required_components": ["MSS", "CUSTOM_RETEST"],
        }
    )
    authority["confluence_contexts"].append(
        {
            "context_id": "custom_context",
            "status": "active",
            "display_name": "Custom context",
            "aliases": ["custom-context"],
        }
    )
    authority_path.write_text(yaml.safe_dump(authority, sort_keys=False), encoding="utf-8")

    result = resolve_strategy(
        tmp_path,
        {
            "owner_attestation": _owner_attestation(
                entry_expression="custom-pullback",
                entry_triggers=["MSS", "CUSTOM_RETEST"],
                confluences=[{"kind": "context", "id": "custom-context"}],
            )
        },
    )
    assert result["status"] == "MATCHED"
    assert result["strategy_id"] == "stg-ict-liquidity"
    assert result["entry_expression"] == "custom-pullback"
    assert result["confluences"] == ["custom_context"]
    assert result["confluence_types"] == {"custom_context": "context"}


def test_material_contradiction_requires_inspectable_fact_and_verified_provider_time():
    observation = {
        "claim_id": "entry_trigger:MSS",
        "finding": "contradicts",
        "timing": "post_trade",
        "contradiction_code": "trigger_after_provider_entry",
        "observed_fact": "MSS was first visible at 09:53:35Z, after the verified 09:33:35Z entry.",
        "observed_at": "2026-09-22T09:53:35Z",
        "evidence_refs": ["artifact:replay-1"],
    }
    with pytest.raises(ResolverError, match="verified provider execution"):
        resolve_strategy(
            ROOT,
            {"owner_attestation": _owner_attestation(), "retrospective_observations": [observation]},
            provider_entry_at="2026-09-22T09:33:35Z",
        )
    result = resolve_strategy(
        ROOT,
        {"owner_attestation": _owner_attestation(), "retrospective_observations": [observation]},
        provider_entry_at="2026-09-22T09:33:35Z",
        provider_execution_verified=True,
    )
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["material_conflicts"][0]["observed_fact"] == observation["observed_fact"]
    assert observation["observed_fact"] in to_lifecycle_resolution(result, contemporaneous=False)["reason"]

    with pytest.raises(ResolverError, match="concrete observed_fact"):
        resolve_strategy(
            ROOT,
            {
                "owner_attestation": _owner_attestation(),
                "retrospective_observations": [{
                    "claim_id": "entry_trigger:MSS",
                    "finding": "contradicts",
                    "timing": "post_trade",
                    "contradiction_code": "visual_timeline_conflict",
                    "evidence_refs": ["artifact:replay-1"],
                }],
            },
            provider_entry_at="2026-09-22T09:33:35Z",
            provider_execution_verified=True,
        )


def test_retrospective_visual_observation_cannot_use_owner_attestation_as_visual_evidence():
    with pytest.raises(ResolverError, match="must cite attached evidence"):
        resolve_strategy(
            ROOT,
            {
                "owner_attestation": _owner_attestation(evidence_refs=["owner:remembered-note"]),
                "retrospective_observations": [
                    {
                        "claim_id": "entry_trigger:MSS",
                        "finding": "corroborates",
                        "timing": "post_trade",
                        "evidence_refs": ["owner:remembered-note"],
                    }
                ],
            },
        )


def test_retrospective_owner_no_setup_uses_the_same_first_class_attestation():
    result = resolve_strategy(
        ROOT,
        {"owner_attestation": {"timing": "retrospective", "setup_status": "NO_SETUP", "rationale": "No setup motivated this entry."}},
    )
    assert result["status"] == "NO_MATCH"
    assert result["strategy_id"] is None
    assert result["resolution_authority"] == "owner_retrospective_strategy_attribution"


@pytest.mark.parametrize(
    "bad",
    [
        [{"rule_id": "nope", "state": "PASS", "evidence_refs": ["x"]}],
        [{"rule_id": "vegas.tunnel.touch", "state": "MAYBE", "evidence_refs": ["x"]}],
        [{"rule_id": "vegas.tunnel.touch", "state": "PASS", "evidence_refs": []}],
    ],
)
def test_malformed_observations_rejected(bad):
    with pytest.raises(ResolverError):
        resolve_strategy(ROOT, {"observations": bad})


def test_conflicting_observations_are_undecided():
    r = resolve_strategy(ROOT, {"observations": [obs("vegas.tunnel.touch"), obs("vegas.tunnel.touch", "FAIL")], "plan": PLAN})
    assert r["status"] != "MATCHED"


def _adjudicated_root(tmp_path, *, decision="accept", decision_type="evidence", refs=("assets/readable.png",)):
    import json
    import shutil

    shutil.copytree(ROOT / "registry", tmp_path / "registry")
    adj_dir = tmp_path / "canonical" / "adjudications"
    adj_dir.mkdir(parents=True)
    (adj_dir / "adj_test.json").write_text(
        json.dumps({"adjudication_id": "adj_test", "decision_type": decision_type, "decision": decision, "reviewer": "example-owner", "evidence_refs": list(refs)}),
        encoding="utf-8",
    )
    return tmp_path


def _cited(adj_id="adj_test", refs=("assets/readable.png", "adjudication:adj_test")):
    return {
        "observations": [{"rule_id": "vegas.tunnel.touch", "state": "PASS", "timing": "pre_trade", "evidence_refs": list(refs), "adjudication_id": adj_id}],
        "plan": PLAN,
    }


def test_observation_may_cite_an_accepted_owner_adjudication(tmp_path):
    root = _adjudicated_root(tmp_path)
    assert resolve_strategy(root, _cited())["status"] == "MATCHED"


@pytest.mark.parametrize(
    "kwargs, observation",
    [
        ({}, _cited(adj_id="adj_missing")),
        ({"decision": "reject"}, _cited()),
        ({"decision_type": "strategy"}, _cited()),
        ({}, _cited(refs=("assets/other.png", "adjudication:adj_test"))),  # evidence not covered by the adjudication
    ],
)
def test_adjudication_citations_fail_closed(tmp_path, kwargs, observation):
    root = _adjudicated_root(tmp_path, **kwargs)
    with pytest.raises(ResolverError):
        resolve_strategy(root, observation)
