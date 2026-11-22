"""A registration packet pins the strategy rule version it used, not the whole registry.

Adding an unrelated strategy to the registry must not invalidate registered packets
or break an idempotent ALREADY_REGISTERED retry.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from test_registration_packet import _draft, _preview_capture, _root
from test_strategy_versions import _add_vegas_v2
from trading_os.registration_executor import _draft_matches_frozen_packet, _recover_registered_draft
from trading_os.strategy_resolver import resolve_strategy
from test_trade_registration_workflow import (
    _capture_kwargs,
    _copy_operational_inputs,
    _frozen_no_setup_packet,
)
from trading_os.evidence_store import EvidenceStore
from trading_os.operational_lifecycle import LifecycleStore
from trading_os.registration_census import registration_state
from trading_os.registration_packet import (
    OwnerReconfirmationRequired,
    RegistrationPacketError,
    _validate_authority_pins,
    build_registration_intake,
    preflight_packet_draft,
    prepare_confirmation_preview,
    confirmation_snapshot_digest,
    packet_sha256,
    _validate_strategy_version_pin,
    freeze_packet,
    persist_frozen_packet,
    strategy_resolutions_equivalent,
    strategy_version_pin,
    validate_frozen_packet,
)
from trading_os.registry import load_registry, registry_as_of_b3, registry_digest
from trading_os.unified_capture import capture_retrospective_live

ROOT = Path(__file__).resolve().parents[1]
VEGAS = {"strategy_id": "stg-vegas-tunnel", "rule_version": "stg-vegas-tunnel:v1"}


def _edit_registry(root: Path, edit) -> None:
    path = root / "registry" / "strategy-registry.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _add_unrelated_strategy(root: Path) -> None:
    _edit_registry(
        root,
        lambda data: data["strategies"].append(
            {
                "strategy_id": "stg-added-later",
                "display_name": "Added later",
                "classification": "independent_system",
                "status": "active",
                "legacy_aliases": [],
                "rule_versions": [],
            }
        ),
    )


def _drop_aa(root: Path) -> None:
    """The registry as it was before AA joined, so AA later enters the resolver matrix."""
    _edit_registry(root, lambda data: data["strategies"].remove(
        next(item for item in data["strategies"] if item["strategy_id"] == "stg-example-a")
    ))


def _restore_registry(root: Path) -> None:
    (root / "registry" / "strategy-registry.yaml").write_bytes((ROOT / "registry" / "strategy-registry.yaml").read_bytes())


def _matrix_ids(resolution: dict) -> set[str]:
    return {entry["strategy_id"] for entry in resolution["matrix"]}


def test_new_packets_pin_the_used_rule_version_or_nothing(tmp_path):
    root = _root(tmp_path)
    frozen = freeze_packet(root, _draft(root, {}))
    assert frozen["authority_pins"]["strategy_version_pin"] is None  # unresolved attribution
    pin = strategy_version_pin(root, VEGAS)
    assert set(pin) == {"strategy_id", "rule_version", "version_digest"}
    assert strategy_version_pin(root, {"status": "no_match", "strategy_id": None, "rule_version": None}) is None


def test_registered_packet_stays_valid_after_an_unrelated_strategy_is_added(tmp_path):
    root = _root(tmp_path)
    frozen = freeze_packet(root, _draft(root, {}))
    _add_unrelated_strategy(root)
    assert registry_digest(load_registry(root)) != frozen["authority_pins"]["strategy_registry_digest"]

    assert validate_frozen_packet(root, frozen)["valid"] is True


def test_registered_packet_survives_a_strategy_with_rules_joining_the_resolver_matrix(tmp_path):
    root = _root(tmp_path)
    _drop_aa(root)
    frozen = freeze_packet(root, _draft(root, {}))
    assert "stg-example-a" not in _matrix_ids(frozen["strategy_resolution"])

    _restore_registry(root)  # AA is added after the registration
    assert "stg-example-a" in _matrix_ids(_draft(root, {})["strategy_resolution"])
    assert validate_frozen_packet(root, frozen)["valid"] is True


def test_pinned_version_survives_registry_additions_but_not_its_own_change(tmp_path):
    root = _root(tmp_path)
    packet = {"strategy_resolution": dict(VEGAS)}
    pin = strategy_version_pin(root, VEGAS)
    _validate_strategy_version_pin(root, packet, pin)

    _add_unrelated_strategy(root)
    _validate_strategy_version_pin(root, packet, pin)

    version_file = root / "registry" / "versions" / "stg-vegas-tunnel-v1.yaml"
    data = yaml.safe_load(version_file.read_text(encoding="utf-8"))
    data["freeze_date"] = "2099-01-01"
    version_file.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(OwnerReconfirmationRequired, match="rule version changed"):
        _validate_strategy_version_pin(root, packet, pin)


def test_version_pin_must_match_the_frozen_resolution(tmp_path):
    root = _root(tmp_path)
    pin = strategy_version_pin(root, VEGAS)

    with pytest.raises(OwnerReconfirmationRequired, match="does not match"):
        _validate_strategy_version_pin(
            root, {"strategy_resolution": {"strategy_id": "stg-po3", "rule_version": "stg-po3:v1"}}, pin
        )
    with pytest.raises(OwnerReconfirmationRequired, match="names a strategy"):
        _validate_strategy_version_pin(root, {"strategy_resolution": dict(VEGAS)}, None)
    with pytest.raises(OwnerReconfirmationRequired, match="no longer available"):
        _validate_strategy_version_pin(
            root,
            {"strategy_resolution": {"strategy_id": "stg-ghost", "rule_version": "stg-ghost:v1"}},
            {"strategy_id": "stg-ghost", "rule_version": "stg-ghost:v1", "version_digest": "0" * 64},
        )
    with pytest.raises(RegistrationPacketError, match="strategy_version_pin"):
        _validate_strategy_version_pin(root, {"strategy_resolution": dict(VEGAS)}, {"strategy_id": "stg-vegas-tunnel"})
    _validate_strategy_version_pin(root, {"strategy_resolution": {}}, None)


def test_legacy_packets_pinned_to_the_whole_registry_stay_valid_for_approved_additions_only(tmp_path):
    root = _root(tmp_path)
    frozen = freeze_packet(root, _draft(root, {}))
    legacy = copy.deepcopy(frozen)
    legacy["authority_pins"].pop("strategy_version_pin")

    # Pinned to today's registry, and to the registry before the approved post-B3 addition.
    _validate_authority_pins(root, legacy)
    historical = registry_digest(registry_as_of_b3(load_registry(root)))
    legacy["authority_pins"]["strategy_registry_digest"] = historical
    _validate_authority_pins(root, legacy)

    # Strategies added after B3 never matter to a legacy packet ...
    _add_unrelated_strategy(root)
    _validate_authority_pins(root, legacy)
    # ... but a change to what B3 froze is drift.
    _edit_registry(root, lambda data: next(
        item for item in data["strategies"] if item["strategy_id"] == "stg-ict-liquidity"
    )["legacy_aliases"].append("tampered"))
    with pytest.raises(OwnerReconfirmationRequired, match="strategy registry"):
        _validate_authority_pins(root, legacy)
    legacy["authority_pins"]["strategy_registry_digest"] = "0" * 64
    with pytest.raises(OwnerReconfirmationRequired, match="strategy registry"):
        _validate_authority_pins(root, legacy)


def test_a_legacy_packet_still_finds_the_b3_frozen_version_files_unchanged(tmp_path):
    root = _root(tmp_path)
    (root / "exports" / "method-validation" / "b3").mkdir(parents=True)
    (root / "exports" / "method-validation" / "b3" / "manifest.json").write_bytes(
        (ROOT / "exports" / "method-validation" / "b3" / "manifest.json").read_bytes()
    )
    _drop_aa(root)  # a genuine legacy packet predates AA: its matrix lists only B3-pinned versions
    legacy = copy.deepcopy(freeze_packet(root, _draft(root, {})))
    legacy["authority_pins"].pop("strategy_version_pin")
    _restore_registry(root)
    _validate_authority_pins(root, legacy)

    version = root / "registry" / "versions" / "stg-po3-v1.yaml"  # the registry digest does not cover this
    original = version.read_bytes()
    lf, crlf = bytes((10,)), bytes((13, 10))
    version.write_bytes(original + b"# tampered" + lf)
    with pytest.raises(OwnerReconfirmationRequired, match="frozen B3 rule version file changed"):
        _validate_authority_pins(root, legacy)

    # A checkout that converted the same file to CRLF is not a change.
    version.write_bytes(original.replace(lf, crlf))
    _validate_authority_pins(root, legacy)

    # A version the B3 manifest does not pin cannot be authenticated for a legacy packet.
    legacy["strategy_resolution"]["matrix"].append(
        {"strategy_id": "stg-example-a", "rule_version": "stg-example-a:v2", "result": "INSUFFICIENT", "rules": []}
    )
    with pytest.raises(OwnerReconfirmationRequired, match="does not pin"):
        _validate_authority_pins(root, legacy)


def _resolution(matrix):
    return {"status": "MATCHED", "strategy_id": "stg-vegas-tunnel", "matrix": matrix}


def test_resolution_replay_tolerates_newer_matrix_entries_only(tmp_path):
    old = {"strategy_id": "stg-po3", "rule_version": "stg-po3:v1", "result": "INSUFFICIENT"}
    new = {"strategy_id": "stg-added-later", "rule_version": "stg-added-later:v1", "result": "INSUFFICIENT"}
    frozen = _resolution([old])

    assert strategy_resolutions_equivalent(_resolution([old]), frozen) is True
    assert strategy_resolutions_equivalent(_resolution([old, new]), frozen) is True
    # A frozen entry that changed, or vanished, is drift.
    assert strategy_resolutions_equivalent(_resolution([{**old, "result": "PASS"}, new]), frozen) is False
    assert strategy_resolutions_equivalent(_resolution([new]), frozen) is False
    # A newer strategy that changed the decision itself is drift.
    assert strategy_resolutions_equivalent({**_resolution([old, new]), "status": "AMBIGUOUS"}, frozen) is False
    assert strategy_resolutions_equivalent({**_resolution([old]), "reason": "different"}, frozen) is False


def test_already_registered_retry_survives_an_added_strategy(tmp_path):
    _copy_operational_inputs(tmp_path)
    _drop_aa(tmp_path)
    packet = _frozen_no_setup_packet(tmp_path)
    store = LifecycleStore(tmp_path)
    persist_frozen_packet(store, packet)
    kwargs = _capture_kwargs()
    kwargs.update({"strategy_observations": None, "frozen_registration_packet": packet})
    first = capture_retrospective_live(store, **kwargs)
    assert first["registered"] is True
    events = tmp_path / "canonical" / "lifecycle" / "events.jsonl"
    events_before = events.read_bytes() if events.exists() else b""

    _restore_registry(tmp_path)  # AA is added after the registration
    _add_unrelated_strategy(tmp_path)

    assert validate_frozen_packet(tmp_path, packet)["valid"] is True
    assert persist_frozen_packet(store, packet)["status"] == "ALREADY_REGISTERED"
    again = capture_retrospective_live(store, **kwargs)
    assert again["already_registered"] is True
    assert again["trade_id"] == first["trade_id"]
    assert registration_state(tmp_path, "propw", "2506100706543892")["status"] == "REGISTERED"
    assert (events.read_bytes() if events.exists() else b"") == events_before


def test_repeated_executor_input_matches_its_frozen_packet_despite_newer_matrix_entries(tmp_path):
    root = _root(tmp_path)
    frozen = freeze_packet(root, _draft(root, {}))
    repeated = copy.deepcopy(frozen)
    for key in ("schema_version", "semantic_state", "packet_sha256"):
        repeated.pop(key)
    # A caller's input carries no builder-owned pins; the executor derives them.
    repeated["authority_pins"] = {"source_main_sha": frozen["authority_pins"]["source_main_sha"]}
    assert _draft_matches_frozen_packet(repeated, frozen) is True

    # The live resolver now lists a strategy added after the registration.
    repeated["strategy_resolution"]["matrix"].append(
        {"strategy_id": "stg-added-later", "rule_version": "stg-added-later:v1", "result": "INSUFFICIENT", "rules": []}
    )
    assert _draft_matches_frozen_packet(repeated, frozen) is True

    # But a changed decision, or a changed frozen matrix entry, is a different input.
    changed = copy.deepcopy(repeated)
    changed["strategy_resolution"]["status"] = "MATCHED"
    assert _draft_matches_frozen_packet(changed, frozen) is False
    changed = copy.deepcopy(repeated)
    changed["strategy_resolution"]["matrix"][0]["result"] = "PASS"
    assert _draft_matches_frozen_packet(changed, frozen) is False


TRIPLE_TAP = {"strategy_id": "stg-triple-tap", "rule_version": "stg-triple-tap:v1"}


def _touch_version_file(root: Path, version_id: str) -> None:
    path = root / "registry" / "versions" / (version_id.replace(":", "-") + ".yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["freeze_date"] = "2099-01-01"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_pin_of_an_inheriting_version_changes_with_its_inherited_parent(tmp_path):
    root = _root(tmp_path)
    packet = {"strategy_resolution": dict(TRIPLE_TAP)}
    pin = strategy_version_pin(root, TRIPLE_TAP)
    _validate_strategy_version_pin(root, packet, pin)

    _touch_version_file(root, "stg-ict-liquidity:v1")  # the parent of stg-triple-tap:v1
    assert strategy_version_pin(root, TRIPLE_TAP)["version_digest"] != pin["version_digest"]
    with pytest.raises(OwnerReconfirmationRequired, match="rule version changed"):
        _validate_strategy_version_pin(root, packet, pin)


def test_confirmation_snapshot_binds_the_version_pin_and_keeps_legacy_digests(tmp_path):
    root = _root(tmp_path)
    pin_a = strategy_version_pin(root, VEGAS)
    pin_b = {**pin_a, "version_digest": "0" * 64}
    body = {"provider_facts": {"provider": "propw"}, "strategy_resolution": dict(VEGAS)}

    with_a = confirmation_snapshot_digest({**body, "authority_pins": {"strategy_version_pin": pin_a}})
    with_b = confirmation_snapshot_digest({**body, "authority_pins": {"strategy_version_pin": pin_b}})
    with_null = confirmation_snapshot_digest({**body, "authority_pins": {"strategy_version_pin": None}})
    legacy = confirmation_snapshot_digest({**body, "authority_pins": {"source_main_sha": "1" * 40}})

    assert len({with_a, with_b, with_null, legacy}) == 4
    # A packet frozen before the pin existed keeps exactly the digest it had.
    assert legacy == confirmation_snapshot_digest(body)


def _matched_draft(root: Path) -> dict:
    source_image = ROOT / "canonical/evidence/artifacts/dfcafe619d4d64fe7d0fcdb47bba9fce3feeb214f86eea33470e65b49a736e46.png"
    ingested = EvidenceStore(root).ingest(source_image)
    ref = ingested["locator"]
    resolver_input = {
        "owner_attestation": {
            "timing": "retrospective",
            "setup_status": "HAS_SETUP",
            "primary_strategy_id": "stg-ict-liquidity",
            "entry_expression": "ob-mss",
            "entry_triggers": ["MSS", "OB_RETEST"],
            "confluences": [{"kind": "context", "id": "VEGAS_TUNNEL"}],
            "rationale": "MSS above Vegas Tunnel and pullback to support OB.",
            "evidence_refs": [ref],
        },
        "retrospective_observations": [
            {"claim_id": "entry_trigger:MSS", "finding": "not_observable", "timing": "post_trade", "evidence_refs": [ref]},
            {"claim_id": "confluence:vegas_tunnel", "finding": "corroborates", "timing": "post_trade", "evidence_refs": [ref]},
        ],
    }
    draft = _draft(root, resolver_input)
    draft["image_evidence"] = [
        {
            "sha256": ingested["sha256"],
            "locator": ingested["locator"],
            "original_filename": ingested["original_filename"],
            "category": "review",
            "timing": "post_trade",
            "type": "replay_chart",
            "captured_at": "2026-10-08T04:00:00Z",
            "provenance": {"capture_mode": "retrospective_replay", "captured_at": "2026-10-08T04:00:00Z"},
            "represented_market_time": {"start": "2025-06-10T12:00:00Z", "end": "2025-06-10T12:15:00Z"},
            "role": "historical_market_reconstruction",
        }
    ]
    draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(draft)
    return draft


def test_matched_packet_pins_its_version_and_a_change_after_preview_needs_a_new_preview(tmp_path):
    root = _root(tmp_path)
    draft = _matched_draft(root)
    assert draft["strategy_resolution"]["strategy_id"] == "stg-ict-liquidity"

    frozen = freeze_packet(root, copy.deepcopy(draft))
    pin = frozen["authority_pins"]["strategy_version_pin"]
    assert pin["strategy_id"] == "stg-ict-liquidity" and pin["rule_version"] == "stg-ict-liquidity:v1"
    assert validate_frozen_packet(root, frozen)["valid"] is True

    # The confirmed rule version changes after the Owner's preview, resolver output unchanged.
    _touch_version_file(root, "stg-ict-liquidity:v1")
    with pytest.raises(OwnerReconfirmationRequired):
        freeze_packet(root, copy.deepcopy(draft))
    # And the already frozen packet no longer validates either.
    with pytest.raises(OwnerReconfirmationRequired, match="rule version changed"):
        validate_frozen_packet(root, frozen)


def test_a_new_registration_without_the_pin_is_refused_at_persist(tmp_path):
    _copy_operational_inputs(tmp_path)
    _drop_aa(tmp_path)  # keep the matrix to B3-pinned versions so the pin check, not the version check, refuses
    packet = _frozen_no_setup_packet(tmp_path)
    store = LifecycleStore(tmp_path)

    stripped = copy.deepcopy(packet)
    stripped["authority_pins"].pop("strategy_version_pin")
    # A self-consistent forgery: the preview digest and the packet hash are recomputed.
    stripped["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(stripped)
    stripped["packet_sha256"] = packet_sha256(stripped)

    with pytest.raises(OwnerReconfirmationRequired, match="must pin authority_pins.strategy_version_pin"):
        persist_frozen_packet(store, stripped)
    assert persist_frozen_packet(store, packet)["status"] != "ALREADY_REGISTERED"


def test_compact_intake_preview_digest_survives_the_commit_preflight(tmp_path):
    """The real compact-intake flow: preview computes the pin from the resolved strategy, and the
    commit preflight starts from an empty strategy_resolution and derives it. Both must agree."""
    root = _root(tmp_path)
    image = tmp_path / "owner-chart.png"
    image.write_bytes(
        (ROOT / "canonical/evidence/artifacts/dfcafe619d4d64fe7d0fcdb47bba9fce3feeb214f86eea33470e65b49a736e46.png").read_bytes()
    )
    intake = {
        "schema": "registration-intake.v1",
        "provider": "propw",
        "provider_lifecycle_id": "2506100706543892",
        "canonical_capture": _preview_capture(),
        "owner_recollection": {"verbatim_text": "MSS above Vegas Tunnel and pullback to support OB."},
        "owner_judgment": {},
        "agent_observation": {"summary": "Replay chart.", "evidence_refs": ["image:setup"]},
        "agent_assessment": {},
        "agreed_facts": [],
        "unresolved_facts": [],
        "resolver_input": {
            "owner_attestation": {
                "timing": "retrospective",
                "setup_status": "HAS_SETUP",
                "primary_strategy_id": "stg-ict-liquidity",
                "entry_expression": "ob-mss",
                "entry_triggers": ["MSS", "OB_RETEST"],
                "confluences": [{"kind": "context", "id": "VEGAS_TUNNEL"}],
                "rationale": "MSS above Vegas Tunnel and pullback to support OB.",
                "evidence_refs": ["image:setup"],
            },
            "retrospective_observations": [
                {"claim_id": "entry_trigger:MSS", "finding": "not_observable", "timing": "post_trade", "evidence_refs": ["image:setup"]},
                {"claim_id": "confluence:vegas_tunnel", "finding": "corroborates", "timing": "post_trade", "evidence_refs": ["image:setup"]},
            ],
        },
        "images": [
            {
                "alias": "setup",
                "path": str(image),
                "category": "review",
                "timing": "post_trade",
                "type": "replay_chart",
                "captured_at": "2026-10-08T04:00:00Z",
                "provenance": {"capture_mode": "retrospective_replay", "captured_at": "2026-10-08T04:00:00Z"},
                "represented_market_time": {"start": "2025-06-10T12:00:00Z", "end": "2025-06-10T12:15:00Z"},
                "role": "historical_market_reconstruction",
            }
        ],
    }
    draft, image_files = build_registration_intake(root, intake)
    preview = prepare_confirmation_preview(root, draft, source_main_sha="a" * 40, image_files=image_files)
    assert preview["strategy_resolution"]["strategy_id"] == "stg-ict-liquidity"
    preview_pin = preview["authority_pins"]["strategy_version_pin"]
    assert preview_pin["rule_version"] == "stg-ict-liquidity:v1"

    confirmed, _ = build_registration_intake(root, intake)
    confirmed["owner_confirmation"] = {
        "status": "confirmed",
        "confirmed_at": "2026-10-13T00:00:00Z",
        "preview_digest": preview["preview_digest"],
    }
    confirmed.setdefault("authority_pins", {})["source_main_sha"] = "a" * 40
    prepared = preflight_packet_draft(root, confirmed, derive_strategy_resolution=True)
    assert prepared["strategy_resolution"]["strategy_id"] == "stg-ict-liquidity"

    # The executor then ingests the image, hands the derived resolution to the official freeze,
    # and the frozen packet must carry the very pin the Owner confirmed.
    ingested = EvidenceStore(root).ingest(image)
    confirmed["image_evidence"][0]["locator"] = ingested["locator"]
    confirmed["strategy_resolution"] = prepared["strategy_resolution"]
    frozen = freeze_packet(root, confirmed)
    assert frozen["authority_pins"]["strategy_version_pin"] == preview_pin
    assert validate_frozen_packet(root, frozen)["valid"] is True


def test_a_later_resolver_may_add_descriptive_rule_fields_but_not_change_or_drop_frozen_ones():
    rule = {"rule_id": "ict_liquidity.identity", "state": "PASS", "code": "observed", "evidence_refs": ["a"]}
    entry = {"strategy_id": "stg-ict-liquidity", "rule_version": "stg-ict-liquidity:v1", "result": "PASS", "rules": [rule]}
    frozen = _resolution([entry])

    added = {**rule, "supports_fact_timing": "pre_trade", "basis": ["contemporaneous_capture"]}
    assert strategy_resolutions_equivalent(_resolution([{**entry, "rules": [added]}]), frozen) is True
    # A frozen value that changed, a frozen key that vanished, or a changed rule count is drift.
    assert strategy_resolutions_equivalent(_resolution([{**entry, "rules": [{**added, "state": "FAIL"}]}]), frozen) is False
    assert strategy_resolutions_equivalent(
        _resolution([{**entry, "rules": [{k: v for k, v in added.items() if k != "code"}]}]), frozen
    ) is False
    assert strategy_resolutions_equivalent(_resolution([{**entry, "rules": [added, added]}]), frozen) is False
    assert strategy_resolutions_equivalent(_resolution([{**entry, "rules": []}]), frozen) is False
    # The top-level decision stays exact: an added top-level field is still drift.
    assert strategy_resolutions_equivalent({**_resolution([entry]), "new_field": 1}, frozen) is False


def test_only_the_known_descriptive_keys_may_be_added_and_only_to_rule_results():
    rule = {"rule_id": "ict_liquidity.identity", "state": "PASS", "code": "observed", "evidence_refs": ["a"]}
    entry = {"strategy_id": "stg-ict-liquidity", "rule_version": "stg-ict-liquidity:v1", "result": "PASS", "rules": [rule]}
    frozen = _resolution([entry])

    for key in ("blocking", "overridden", "superseded_by"):
        added_to_rule = {**entry, "rules": [{**rule, key: True}]}
        assert strategy_resolutions_equivalent(_resolution([added_to_rule]), frozen) is False, key
        assert strategy_resolutions_equivalent(_resolution([{**entry, key: True}]), frozen) is False, key
    # A nested value inside a rule result must still be exact, and a known key cannot replace a frozen one.
    assert strategy_resolutions_equivalent(
        _resolution([{**entry, "rules": [{**rule, "evidence_refs": ["a", "b"]}]}]), frozen
    ) is False
    assert strategy_resolutions_equivalent(
        _resolution([{**entry, "rules": [{**rule, "basis": ["x"]}]}]),
        _resolution([{**entry, "rules": [{**rule, "basis": ["y"]}]}]),
    ) is False


def test_registered_legacy_packets_with_older_rule_result_fields_still_validate():
    """T000025 and T000026 were frozen before rule results carried supports_fact_timing/basis."""
    for packet_sha in (
        "aa876a20b813b7f34b2399d2f34f7296443fa4c5889f2bde8fa2d6eedb2666b2",
        "d91b50f83bb3ab8cd9a8e764cb9fae06ae22f6e5ce40a288609116adebfbe5ab",
    ):
        packet = json.loads((ROOT / "canonical" / "registration" / "packets" / f"{packet_sha}.json").read_text(encoding="utf-8"))
        assert validate_frozen_packet(ROOT, packet)["valid"] is True


def test_same_resolver_input_is_the_same_registration_even_if_the_registry_moved_on(tmp_path):
    root = _root(tmp_path)
    frozen = freeze_packet(root, _draft(root, {}))
    repeated = copy.deepcopy(frozen)
    for key in ("schema_version", "semantic_state", "packet_sha256"):
        repeated.pop(key)
    repeated["authority_pins"] = {"source_main_sha": frozen["authority_pins"]["source_main_sha"]}
    repeated["strategy_resolution"] = {}  # a compact intake: the resolver derives it later

    assert _draft_matches_frozen_packet(repeated, frozen) is True  # resolver_input is identical
    claimed = copy.deepcopy(repeated)
    claimed["strategy_resolution"] = {"status": "MATCHED", "strategy_id": "stg-other", "matrix": []}
    assert _draft_matches_frozen_packet(claimed, frozen) is False  # a caller-asserted decision is not derived
    repeated["resolver_input"] = {"owner_setup_attribution": {"setup_status": "NO_SETUP", "reason": "x", "evidence_refs": ["a"]}}
    assert _draft_matches_frozen_packet(repeated, frozen) is False  # a different input


def test_a_registered_compact_retry_is_recovered_after_a_strategy_version_is_added_and_stopped(tmp_path):
    _copy_operational_inputs(tmp_path)
    packet = _frozen_no_setup_packet(tmp_path)
    store = LifecycleStore(tmp_path)
    persist_frozen_packet(store, packet)
    kwargs = _capture_kwargs()
    kwargs.update({"strategy_observations": None, "frozen_registration_packet": packet})
    capture_retrospective_live(store, **kwargs)

    _add_vegas_v2(tmp_path, retired=("stg-vegas-tunnel:v1",))
    live = resolve_strategy(tmp_path, packet["resolver_input"], provider_entry_at=packet["provider_facts"]["entry_time"], provider_execution_verified=True)
    assert live["matrix"] != packet["strategy_resolution"]["matrix"]  # the live derivation really changed

    retry = copy.deepcopy(packet)
    for key in ("schema_version", "semantic_state", "packet_sha256"):
        retry.pop(key)
    retry["authority_pins"] = {}
    retry["strategy_resolution"] = {}  # a compact intake arrives with the resolution still empty
    assert _recover_registered_draft(tmp_path, retry, ignore_source_main_pin=True)["packet_sha256"] == packet["packet_sha256"]
    assert validate_frozen_packet(tmp_path, packet)["valid"] is True
