from __future__ import annotations

import hashlib
import json
import shutil
import copy
from pathlib import Path

import pytest

from trading_os.evidence_store import EvidenceStore
from trading_os.operational_lifecycle import LifecycleStore, normalize_strategy_resolution
from trading_os.registration_census import build_census
from trading_os.registration_packet import (
    OwnerReconfirmationRequired,
    OwnerInputRequired,
    RegistrationPacketError,
    build_registration_intake,
    confirmation_snapshot_digest,
    freeze_packet,
    packet_sha256,
    preflight_packet_draft,
    persist_frozen_packet,
    prepare_confirmation_preview,
    strategy_version_pin,
    validate_existing_registration_binding,
    validate_frozen_packet,
)
from trading_os.strategy_resolver import resolve_strategy, to_lifecycle_resolution

ROOT = Path(__file__).resolve().parents[1]


def _root(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "registry", tmp_path / "registry")
    ledger = tmp_path / "provider-ledgers" / "propw" / "normalized"
    ledger.mkdir(parents=True)
    lifecycle = {
        "open_id": "2506100706543892",
        "instrument": "SUI",
        "direction": "long",
        "entry_time": "2025-06-09T23:06:54Z",
        "entry_price": "3.4354",
        "exit_time": "2025-06-11T18:05:48Z",
        "weighted_exit_price": "3.4354",
        "open_size": "1",
        "closed_size": "1",
        "net_profit_sum": "-2.40318203",
        "is_closed": 1,
        "is_partial": 0,
        "normalization_status": "ok",
        "provider_lifecycle_phase": "CLOSED",
    }
    (ledger / "trade-lifecycles.jsonl").write_text(
        json.dumps({"open_id": lifecycle["open_id"], "lifecycle": lifecycle}) + "\n",
        encoding="utf-8",
    )
    return tmp_path


def _external_image_evidence(manifest: dict) -> list[dict]:
    items = []
    for artifact in manifest.get("artifacts", []):
        path = artifact.get("zip_path") or artifact.get("path")
        name = artifact.get("original_filename") or ""
        if Path(name or path or "").suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        items.append(
            {
                "sha256": artifact["sha256"],
                "original_filename": artifact["original_filename"],
                "package_path": path,
                **({"byte_size": artifact["byte_size"]} if artifact.get("byte_size") is not None else {}),
            }
        )
    return items


def _draft(root: Path, resolver_input: dict) -> dict:
    resolution = resolve_strategy(
        root,
        resolver_input,
        provider_entry_at="2025-06-09T23:06:54Z",
        provider_execution_verified=True,
    )
    draft = {
        "packet_version": 1,
        "transport_mode": "same_agent",
        "owner_confirmation": {"status": "confirmed", "confirmed_at": "2026-10-06T08:00:00Z"},
        "provider_facts": {
            "provider": "propw",
            "provider_lifecycle_id": "2506100706543892",
            "instrument": "SUI",
            "direction": "long",
            "entry_time": "2025-06-09T23:06:54Z",
            "entry_price": "3.4354",
            "exit_time": "2025-06-11T18:05:48Z",
            "weighted_exit_price": "3.4354",
            "net_profit_sum": "-2.40318203",
            "is_closed": True,
        },
        "image_evidence": [],
        "agent_observation": {},
        "agent_assessment": {},
        "owner_recollection": {"verbatim_text": "Owner's exact original wording."},
        "owner_judgment": {},
        "agreed_facts": [],
        "unresolved_facts": [],
        "resolver_input": resolver_input,
        "strategy_resolution": resolution,
        "authority_pins": {
            "source_main_sha": "1" * 40,
            "strategy_version_pin": strategy_version_pin(root, resolution),
        },
        "canonical_review_projection": {},
        "research_hypotheses": [],
    }
    draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(draft)
    return draft


def _preview_capture() -> dict:
    return {
        "intended_entry": 3.4354,
        "stop_loss": 3.4,
        "take_profit": 3.5,
        # (3.5 - 3.4354) / (3.4354 - 3.4): the fixture long is measured from its actual entry.
        "planned_rr": 1.825,
        "timeframe_context": "15m synthetic context",
        "timeframe_trigger": "Synthetic Owner trigger",
        "market_context": "Synthetic preview fixture",
        "thesis": "Synthetic benchmark only.",
        "execution_environment": "prop_challenge",
        "outcome_known_at_recording": "true",
        # The fixture lifecycle exits at its entry price, so 1R measured from the actual entry gives 0R.
        "realized_r": 0.0,
        "realized_r_authority": "deterministic_execution_calculation",
    }


def test_freeze_validate_and_persist_unresolved_packet_stays_a_registration_ready_packet(tmp_path):
    root = _root(tmp_path)
    frozen = freeze_packet(root, _draft(root, {}))

    assert frozen["schema_version"] == "registration-packet.v2"
    assert frozen["semantic_state"] == "frozen"
    assert len(frozen["packet_sha256"]) == 64
    assert frozen["strategy_resolution"]["status"] == "INSUFFICIENT_EVIDENCE"

    validated = validate_frozen_packet(root, frozen)
    assert validated["valid"] is True
    assert validated["resolver_status"] == "INSUFFICIENT_EVIDENCE"
    assert validated["registration_state"]["status"] == "UNREGISTERED"

    store = LifecycleStore(root)
    first = persist_frozen_packet(store, frozen, recorded_at="2026-10-06T08:01:00Z")
    second = persist_frozen_packet(store, frozen, recorded_at="2026-10-06T08:02:00Z")

    assert first["status"] == second["status"] == "READY_FOR_RECORD_RECONCILIATION"
    assert first["strategy_attribution"] == second["strategy_attribution"] == "unresolved"
    assert first["packet_sha256"] == second["packet_sha256"] == frozen["packet_sha256"]

    packet_path = root / first["packet_path"]
    assert packet_path.exists()
    assert json.loads(packet_path.read_text(encoding="utf-8"))["packet_sha256"] == frozen["packet_sha256"]


def test_confirmation_preview_is_read_only_and_does_not_confirm_or_freeze(tmp_path):
    root = _root(tmp_path)
    draft = _draft(root, {})
    draft.pop("owner_confirmation")
    draft["strategy_resolution"] = {}
    draft["authority_pins"]["source_main_sha"] = "f" * 40
    draft["canonical_capture"] = _preview_capture()
    before = {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }

    preview = prepare_confirmation_preview(
        root,
        draft,
        source_main_sha="a" * 40,
    )

    after = {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert preview["status"] == "READY_FOR_OWNER_CONFIRMATION"
    assert preview["registration_state"]["status"] == "UNREGISTERED"
    assert preview["strategy_resolution"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert preview["authority_pins"]["source_main_sha"] == "a" * 40
    assert preview["preview_digest"] in preview["owner_confirmation_text"]
    assert preview["supporting_detail"]["canonical_capture"] == draft["canonical_capture"]
    assert "packet_sha256" not in preview
    assert "packet_sha256" not in draft
    assert draft.get("owner_confirmation") is None
    assert before == after


def test_legacy_v1_packet_is_readable_but_cannot_start_a_new_registration(tmp_path):
    root = _root(tmp_path)
    legacy = freeze_packet(root, _draft(root, {}))
    legacy["schema_version"] = "registration-packet.v1"
    legacy["owner_confirmation"].pop("preview_digest")
    legacy["packet_sha256"] = packet_sha256(legacy)

    assert validate_frozen_packet(root, legacy)["valid"] is True
    with pytest.raises(OwnerReconfirmationRequired, match="legacy v1 packets remain readable"):
        persist_frozen_packet(LifecycleStore(root), legacy)


def test_external_handoff_confirmation_preview_checks_manifest_and_exact_receiver_bytes(tmp_path):
    root = _root(tmp_path / "repo")
    evidence_root = tmp_path / "handoff"
    image = evidence_root / "images" / "owner-original.png"
    image.parent.mkdir(parents=True)
    image_bytes = b"exact owner image bytes\x00\x01"
    image.write_bytes(image_bytes)
    sha = hashlib.sha256(image_bytes).hexdigest()
    manifest = {
        "artifacts": [{
            "path": "images/owner-original.png",
            "sha256": sha,
            "byte_size": len(image_bytes),
            "original_filename": "owner-original.png",
            "media_type": "image/png",
        }]
    }
    manifest_path = evidence_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    draft = _draft(root, {})
    draft.pop("owner_confirmation")
    draft["transport_mode"] = "external_handoff"
    draft["strategy_resolution"] = {}
    draft["authority_pins"]["source_main_sha"] = "b" * 40
    draft["canonical_capture"] = _preview_capture()
    draft["image_evidence"] = [{
        "sha256": sha,
        "original_filename": "owner-original.png",
        "package_path": "images/owner-original.png",
        "category": "review",
        "timing": "post_trade",
        "type": "chart_snapshot",
    }]

    preview = prepare_confirmation_preview(
        root,
        draft,
        source_main_sha="b" * 40,
        evidence_manifest=manifest_path,
        evidence_root=evidence_root,
    )
    assert preview["transport_mode"] == "external_handoff"
    assert preview["evidence_manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert preview["evidence_bytes_verified"][0]["sha256"] == sha
    assert image.read_bytes() == image_bytes
    draft["strategy_resolution"] = preview["strategy_resolution"]
    draft["owner_confirmation"] = {
        "status": "confirmed",
        "confirmed_at": "2026-10-09T00:00:00Z",
        "preview_digest": preview["preview_digest"],
    }
    frozen = freeze_packet(root, draft, evidence_manifest=manifest_path)
    validated = validate_frozen_packet(
        root,
        frozen,
        evidence_manifest=manifest_path,
        evidence_root=evidence_root,
    )
    assert validated["valid"] is True

    image.write_bytes(b"tampered owner image")
    tampered_draft = dict(draft)
    tampered_draft.pop("owner_confirmation")
    with pytest.raises(RegistrationPacketError, match="evidence SHA256 mismatch"):
        prepare_confirmation_preview(
            root,
            tampered_draft,
            source_main_sha="b" * 40,
            evidence_manifest=manifest_path,
            evidence_root=evidence_root,
        )


def test_confirmation_preview_rejects_confirmed_or_frozen_input(tmp_path):
    root = _root(tmp_path)
    confirmed = _draft(root, {})
    with pytest.raises(RegistrationPacketError, match="before Owner confirmation"):
        prepare_confirmation_preview(root, confirmed, source_main_sha="c" * 40)

    unfrozen = dict(confirmed)
    unfrozen.pop("owner_confirmation")
    unfrozen["packet_sha256"] = "d" * 64
    with pytest.raises(RegistrationPacketError, match="unfrozen draft only"):
        prepare_confirmation_preview(root, unfrozen, source_main_sha="c" * 40)

    census = build_census(root, "propw")
    row = census["lifecycles"][0]
    assert row["status"] == "UNREGISTERED"
    assert "pending" not in row


def test_owner_no_setup_packet_stays_no_match_even_when_visual_rules_match(tmp_path):
    root = _root(tmp_path)
    resolver_input = {
        "observations": [
            {
                "rule_id": "vegas.tunnel.touch",
                "state": "PASS",
                "timing": "pre_trade",
                "evidence_refs": ["chart:owner"],
            }
        ],
        "owner_setup_attribution": {
            "setup_status": "NO_SETUP",
            "reason": "actual entry had no setup",
            "evidence_refs": ["owner:confirmed-review"],
        },
    }
    draft = _draft(root, resolver_input)
    draft.pop("owner_confirmation")
    draft["strategy_resolution"] = {}
    draft["canonical_capture"] = _preview_capture()
    preview = prepare_confirmation_preview(root, draft, source_main_sha="a" * 40)
    assert preview["strategy_resolution"]["status"] == "NO_MATCH"
    assert preview["supporting_detail"]["resolver_input"]["owner_setup_attribution"] == resolver_input["owner_setup_attribution"]
    assert draft.get("owner_confirmation") is None
    assert any(
        row["strategy_id"] == "stg-vegas-tunnel" and row["result"] == "MATCH"
        for row in preview["strategy_resolution"]["matrix"]
    )
    draft["strategy_resolution"] = preview["strategy_resolution"]
    draft["owner_confirmation"] = {
        "status": "confirmed",
        "confirmed_at": "2026-10-09T00:00:00Z",
        "preview_digest": preview["preview_digest"],
    }
    frozen = freeze_packet(root, draft)
    result = persist_frozen_packet(LifecycleStore(root), frozen)
    assert result["status"] == "READY_FOR_RECORD_RECONCILIATION"
    assert result["strategy_status"] == "NO_MATCH"


@pytest.mark.parametrize(
    ("resolver_input", "expected_status"),
    [
        ({}, "INSUFFICIENT_EVIDENCE"),
        ({
            "observations": [
                {"rule_id": "vegas.tunnel.touch", "state": "PASS", "timing": "pre_trade", "evidence_refs": ["chart:owner"]},
                {"rule_id": "po3.identity", "state": "PASS", "timing": "pre_trade", "evidence_refs": ["chart:owner"]},
            ],
            "plan": {"stop_loss_price": 3.4, "target_price": 3.5},
        }, "AMBIGUOUS"),
    ],
)
def test_confirmation_preview_surfaces_nonmatched_resolver_states(tmp_path, resolver_input, expected_status):
    root = _root(tmp_path)
    draft = _draft(root, resolver_input)
    draft.pop("owner_confirmation")
    draft["strategy_resolution"] = {}
    draft["canonical_capture"] = _preview_capture()

    preview = prepare_confirmation_preview(root, draft, source_main_sha="a" * 40)

    assert preview["strategy_resolution"]["status"] == expected_status
    assert preview["owner_confirmation_text"]
    assert preview["supporting_detail"]["strategy_resolution"] == preview["strategy_resolution"]


def test_confirmation_preview_surfaces_material_contradiction_without_overriding_owner_intent(tmp_path):
    root = _root(tmp_path)
    image = tmp_path / "replay.png"
    image_bytes = b"synthetic replay evidence"
    image.write_bytes(image_bytes)
    sha = hashlib.sha256(image_bytes).hexdigest()
    ref = f"artifact:{sha}"
    owner_attestation = {
        "timing": "retrospective",
        "setup_status": "HAS_SETUP",
        "primary_strategy_id": "stg-ict-liquidity",
        "entry_expression": "ob-mss",
        "entry_triggers": ["MSS", "OB_RETEST"],
        "confluences": ["stg-vegas-tunnel"],
        "rationale": "Owner remembers MSS and a support OB retest before entry.",
        "plan_facts": {"stop_loss_price": 3.4, "target_price": 3.5},
        "evidence_refs": [ref],
    }
    resolver_input = {
        "owner_attestation": owner_attestation,
        "retrospective_observations": [{
            "claim_id": "entry_trigger:MSS",
            "finding": "contradicts",
            "timing": "post_trade",
            "contradiction_code": "trigger_after_provider_entry",
            "observed_fact": "MSS appears after the provider-verified entry.",
            "observed_at": "2025-06-10T01:00:00Z",
            "evidence_refs": [ref],
        }],
    }
    draft = _draft(root, resolver_input)
    draft.pop("owner_confirmation")
    draft["strategy_resolution"] = {}
    draft["canonical_capture"] = _preview_capture()
    draft["image_evidence"] = [{
        "sha256": sha,
        "original_filename": image.name,
        "category": "review",
        "timing": "post_trade",
        "type": "replay_chart",
    }]

    preview = prepare_confirmation_preview(
        root,
        draft,
        source_main_sha="a" * 40,
        image_files={sha: image},
    )

    assert preview["strategy_resolution"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert preview["strategy_resolution"]["material_conflicts"][0]["observed_fact"] == resolver_input["retrospective_observations"][0]["observed_fact"]
    assert preview["supporting_detail"]["resolver_input"]["owner_attestation"]["rationale"] == owner_attestation["rationale"]


def test_compact_intake_derives_provider_packet_and_exact_image_refs(tmp_path):
    root = _root(tmp_path / "repo")
    image = tmp_path / "owner chart.png"
    raw = b"original intake image\x00bytes"
    image.write_bytes(raw)
    intake = {
        "schema": "registration-intake.v1",
        "provider": "propw",
        "provider_lifecycle_id": "2506100706543892",
        "canonical_capture": _preview_capture(),
        "owner_recollection": {
            "verbatim_text": "我認為沒有任何的進場條件，\n後續撞到套保。",
            "summary": "The Owner's stated reason.",
        },
        "owner_judgment": {"summary": "Unknown values remain unknown."},
        "agent_observation": {"summary": "Chart shows the described structure.", "evidence_refs": ["image:setup"]},
        "agent_assessment": {"summary": "Retrospective only."},
        "agreed_facts": ["Provider confirms the execution."],
        "unresolved_facts": ["Original capture time is not observable."],
        "resolver_input": {
            "owner_setup_attribution": {
                "setup_status": "NO_SETUP",
                "reason": "Owner says no named setup motivated the trade.",
                "evidence_refs": ["owner:confirmed-review"],
            }
        },
        "images": [{
            "alias": "setup",
            "path": str(image),
            "category": "review",
            "timing": "post_trade",
            "type": "chart_snapshot",
        }],
    }
    timings = {}

    draft, image_files = build_registration_intake(root, intake, timing_sink=timings)

    assert draft["provider_facts"]["provider_lifecycle_id"] == "2506100706543892"
    assert draft["provider_facts"]["instrument"] == "SUI"
    assert draft["provider_facts"]["entry_price"] == "3.4354"
    assert draft["owner_recollection"] == intake["owner_recollection"]
    assert draft["owner_recollection"]["verbatim_text"] == intake["owner_recollection"]["verbatim_text"]
    assert draft["resolver_input"] == intake["resolver_input"]
    assert draft["image_evidence"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert draft["image_evidence"][0]["original_filename"] == image.name
    assert draft["agent_observation"]["evidence_refs"] == [f"artifact:{hashlib.sha256(raw).hexdigest()}.png"]
    assert image_files[hashlib.sha256(raw).hexdigest()] == image
    assert timings["provider_facts_seconds"] >= 0
    assert timings["image_hash_seconds"] >= 0
    assert timings["packet_assembly_seconds"] >= 0


def test_owner_verbatim_is_previewed_digest_bound_and_never_trimmed(tmp_path):
    root = _root(tmp_path)
    exact_text = "我認為沒有任何的進場條件，\n後續撞到套保。"
    draft = _draft(root, {})
    draft.pop("owner_confirmation")
    draft["canonical_capture"] = _preview_capture()
    draft["owner_recollection"] = {
        "verbatim_text": exact_text,
        "summary": "Owner's concise recollection summary remains separate.",
    }
    draft["agent_assessment"] = {"summary": "Agent analysis stays separate."}

    preview = prepare_confirmation_preview(root, draft, source_main_sha="a" * 40)

    assert preview["owner_recollection"]["verbatim_text"] == exact_text
    assert preview["agent_assessment"]["summary"] == "Agent analysis stays separate."
    assert "Owner statement (verbatim):\n" + exact_text in preview["owner_confirmation_text"]
    changed = copy.deepcopy(draft)
    changed["owner_recollection"]["verbatim_text"] = exact_text + "補充"
    changed_preview = prepare_confirmation_preview(root, changed, source_main_sha="a" * 40)
    assert changed_preview["preview_digest"] != preview["preview_digest"]

    confirmed = copy.deepcopy(draft)
    confirmed["owner_confirmation"] = {
        "status": "confirmed",
        "confirmed_at": "2026-10-10T00:00:00Z",
        "preview_digest": preview["preview_digest"],
    }
    confirmed["strategy_resolution"] = preview["strategy_resolution"]
    frozen = freeze_packet(root, confirmed)
    assert frozen["owner_recollection"]["verbatim_text"] == exact_text

    changed_confirmation = copy.deepcopy(confirmed)
    changed_confirmation["owner_recollection"]["verbatim_text"] += "補充"
    with pytest.raises(OwnerReconfirmationRequired, match="OWNER_RECONFIRMATION_REQUIRED"):
        freeze_packet(root, changed_confirmation)


def test_registration_intake_rejects_missing_owner_verbatim_text(tmp_path):
    root = _root(tmp_path / "repo")
    intake = {
        "schema": "registration-intake.v1",
        "provider": "propw",
        "provider_lifecycle_id": "2506100706543892",
        "canonical_capture": _preview_capture(),
        "owner_recollection": {"summary": "Only an Agent rewrite."},
        "owner_judgment": {},
        "agent_observation": {},
        "agent_assessment": {},
        "agreed_facts": [],
        "unresolved_facts": [],
        "resolver_input": {},
        "images": [],
    }

    with pytest.raises(RegistrationPacketError, match="verbatim_text is required"):
        build_registration_intake(root, intake)


def test_owner_semantic_snapshot_change_blocks_executor_preflight_before_image_ingest(tmp_path):
    root = _root(tmp_path)
    image = tmp_path / "owner-chart.png"
    raw = b"owner original chart bytes"
    image.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    draft = _draft(root, {})
    draft.pop("owner_confirmation")
    draft["strategy_resolution"] = {}
    draft["canonical_capture"] = _preview_capture()
    draft["image_evidence"] = [{
        "sha256": sha,
        "byte_size": len(raw),
        "original_filename": image.name,
        "category": "review",
        "timing": "post_trade",
        "type": "chart_snapshot",
    }]
    preview = prepare_confirmation_preview(
        root,
        draft,
        source_main_sha="a" * 40,
        image_files={sha: image},
    )
    draft["strategy_resolution"] = preview["strategy_resolution"]
    draft["owner_confirmation"] = {
        "status": "confirmed",
        "confirmed_at": "2026-10-09T00:00:00Z",
        "preview_digest": preview["preview_digest"],
    }
    draft["canonical_capture"]["thesis"] = "Changed after Owner saw the preview."

    with pytest.raises(OwnerReconfirmationRequired, match="OWNER_RECONFIRMATION_REQUIRED: packet semantics changed"):
        preflight_packet_draft(root, draft)

    assert not (root / "canonical" / "evidence" / "artifacts").exists()

def test_positive_retrospective_owner_attestation_packet_keeps_replay_post_trade_and_registry_mapped(tmp_path):
    root = _root(tmp_path)
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
            "provenance": {
                "capture_mode": "retrospective_replay",
                "captured_at": "2026-10-08T04:00:00Z",
            },
            "represented_market_time": {
                "start": "2025-06-10T12:00:00Z",
                "end": "2025-06-10T12:15:00Z",
            },
            "role": "historical_market_reconstruction",
        }
    ]
    draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(draft)
    assert draft["strategy_resolution"]["strategy_id"] == "stg-ict-liquidity"
    assert draft["strategy_resolution"]["entry_expression"] == "ob-mss"
    assert draft["strategy_resolution"]["confluences"] == ["vegas_tunnel"]
    assert draft["strategy_resolution"]["confluence_types"] == {"vegas_tunnel": "context"}
    assert "provider_verified" in draft["strategy_resolution"]["evidence_quality"]

    frozen = freeze_packet(root, draft)
    validated = validate_frozen_packet(root, frozen)
    assert validated["valid"] is True
    assert frozen["image_evidence"][0]["timing"] == "post_trade"
    assert frozen["image_evidence"][0]["category"] == "review"
    assert frozen["image_evidence"][0]["provenance"]["captured_at"] == "2026-10-08T04:00:00Z"
    assert frozen["image_evidence"][0]["represented_market_time"]["start"] == "2025-06-10T12:00:00Z"
    assert frozen["image_evidence"][0]["role"] == "historical_market_reconstruction"
    assert frozen["authority_pins"]["retrospective_attribution_registry_digest"]
    assert validate_frozen_packet(root, frozen)["valid"] is True


def test_retrospective_owner_packet_rejects_visual_ref_provenance_mismatch(tmp_path):
    root = _root(tmp_path)
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
            "confluences": ["stg-vegas-tunnel"],
            "rationale": "MSS above Vegas Tunnel and pullback to support OB.",
            "evidence_refs": [ref],
        },
        "retrospective_observations": [
            {"claim_id": "entry_trigger:MSS", "finding": "corroborates", "timing": "pre_trade", "evidence_refs": [ref]},
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
        }
    ]
    draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(draft)
    with pytest.raises(RegistrationPacketError, match="timing conflicts with image provenance"):
        freeze_packet(root, draft)


def test_manifest_and_exact_bytes_are_validated(tmp_path):
    root = _root(tmp_path)
    evidence_root = tmp_path / "handoff"
    evidence_root.mkdir()
    image = evidence_root / "image.png"
    image.write_bytes(b"exact-owner-image")
    import hashlib

    image_sha = hashlib.sha256(image.read_bytes()).hexdigest()
    manifest = {
        "artifacts": [
            {
                "path": "image.png",
                "original_filename": "image.png",
                "sha256": image_sha,
                "byte_size": image.stat().st_size,
            }
        ]
    }
    manifest_path = evidence_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    draft = _draft(root, {})
    draft["transport_mode"] = "external_handoff"
    draft["image_evidence"] = _external_image_evidence(manifest)
    draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(
        draft,
        evidence_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    frozen = freeze_packet(root, draft, evidence_manifest=manifest_path)
    result = validate_frozen_packet(
        root,
        frozen,
        evidence_manifest=manifest_path,
        evidence_root=evidence_root,
    )
    assert result["evidence"]["artifact_count"] == 1

    image.write_bytes(b"tampered")
    with pytest.raises(RegistrationPacketError, match="evidence SHA256 mismatch"):
        validate_frozen_packet(
            root,
            frozen,
            evidence_manifest=manifest_path,
            evidence_root=evidence_root,
        )


def test_handoff_receiver_cannot_skip_evidence_package(tmp_path):
    root = _root(tmp_path)
    draft = _draft(root, {})
    draft["transport_mode"] = "external_handoff"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "path": "image.png",
                        "original_filename": "image.png",
                        "sha256": "0" * 64,
                        "byte_size": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    draft["image_evidence"] = _external_image_evidence(json.loads(manifest.read_text(encoding="utf-8")))
    draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(
        draft,
        evidence_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    frozen = freeze_packet(root, draft, evidence_manifest=manifest)
    with pytest.raises(RegistrationPacketError, match="external_handoff packet requires"):
        validate_frozen_packet(root, frozen)


def test_external_handoff_transport_automatically_requires_evidence_package(tmp_path):
    root = _root(tmp_path)
    draft = _draft(root, {})
    draft["transport_mode"] = "external_handoff"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"artifacts": []}), encoding="utf-8")
    draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(
        draft,
        evidence_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    frozen = freeze_packet(root, draft, evidence_manifest=manifest)
    with pytest.raises(RegistrationPacketError, match="external_handoff packet requires"):
        validate_frozen_packet(root, frozen)


def test_external_handoff_pending_persist_requires_and_accepts_evidence_package(tmp_path):
    root = _root(tmp_path)
    evidence_root = tmp_path / "handoff-pending"
    evidence_root.mkdir()
    image = evidence_root / "image.png"
    image.write_bytes(b"exact-owner-image")
    import hashlib

    manifest = {
        "artifacts": [
            {
                "path": "image.png",
                "original_filename": "image.png",
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "byte_size": image.stat().st_size,
            }
        ]
    }
    manifest_path = evidence_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    draft = _draft(root, {})
    draft["transport_mode"] = "external_handoff"
    draft["image_evidence"] = _external_image_evidence(manifest)
    draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(
        draft,
        evidence_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    frozen = freeze_packet(root, draft, evidence_manifest=manifest_path)

    with pytest.raises(RegistrationPacketError, match="external_handoff packet requires"):
        persist_frozen_packet(LifecycleStore(root), frozen)

    result = persist_frozen_packet(
        LifecycleStore(root),
        frozen,
        evidence_manifest=manifest_path,
        evidence_root=evidence_root,
        recorded_at="2026-10-06T08:01:00Z",
    )
    assert result["status"] == "READY_FOR_RECORD_RECONCILIATION"
    assert result["strategy_attribution"] == "unresolved"
    assert result["packet_sha256"] == frozen["packet_sha256"]


def test_external_manifest_owner_image_must_be_declared_in_frozen_packet(tmp_path):
    root = _root(tmp_path)
    evidence_root = tmp_path / "handoff-binding"
    evidence_root.mkdir()
    image = evidence_root / "image.png"
    image.write_bytes(b"owner-image")
    import hashlib

    manifest = {
        "artifacts": [
            {
                "path": "image.png",
                "original_filename": "image.png",
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "byte_size": image.stat().st_size,
            }
        ]
    }
    manifest_path = evidence_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    draft = _draft(root, {})
    draft["transport_mode"] = "external_handoff"
    draft["image_evidence"] = []
    draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(
        draft,
        evidence_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    with pytest.raises(
        RegistrationPacketError,
        match="frozen image_evidence does not match external owner-image manifest",
    ):
        freeze_packet(root, draft, evidence_manifest=manifest_path)


def test_external_packet_image_sha_must_match_manifest_and_bytes(tmp_path):
    root = _root(tmp_path)
    evidence_root = tmp_path / "handoff-sha-binding"
    evidence_root.mkdir()
    image = evidence_root / "image.png"
    image.write_bytes(b"manifest-image-bytes")
    import hashlib

    manifest_sha = hashlib.sha256(image.read_bytes()).hexdigest()
    manifest = {
        "artifacts": [
            {
                "path": "image.png",
                "original_filename": "image.png",
                "sha256": manifest_sha,
                "byte_size": image.stat().st_size,
            }
        ]
    }
    manifest_path = evidence_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    draft = _draft(root, {})
    draft["transport_mode"] = "external_handoff"
    draft["image_evidence"] = [
        {
            "sha256": "a" * 64,
            "original_filename": "image.png",
            "package_path": "image.png",
        }
    ]
    draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(
        draft,
        evidence_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    with pytest.raises(
        RegistrationPacketError,
        match="frozen image_evidence does not match external owner-image manifest",
    ):
        freeze_packet(root, draft, evidence_manifest=manifest_path)


def test_same_agent_image_evidence_must_exist_in_evidence_store(tmp_path):
    root = _root(tmp_path)
    draft = _draft(root, {})
    draft["image_evidence"] = [
        {
            "sha256": "a" * 64,
            "locator": "artifact:" + "a" * 64 + ".png",
            "original_filename": "owner.png",
        }
    ]
    draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(draft)
    with pytest.raises(RegistrationPacketError, match="not durably ingested in EvidenceStore"):
        freeze_packet(root, draft)

    image = tmp_path / "owner.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nowner-image")
    artifact = EvidenceStore(root).ingest(image)
    draft["image_evidence"] = [
        {
            "sha256": artifact["sha256"],
            "locator": artifact["locator"],
            "original_filename": artifact["original_filename"],
        }
    ]
    draft["owner_confirmation"]["preview_digest"] = confirmation_snapshot_digest(draft)
    frozen = freeze_packet(root, draft)
    assert frozen["image_evidence"][0]["sha256"] == artifact["sha256"]


def test_closed_provider_facts_are_fail_closed_before_freeze(tmp_path):
    root = _root(tmp_path)
    draft = _draft(root, {})
    for field in ("instrument", "direction", "entry_time", "entry_price", "exit_time", "weighted_exit_price", "net_profit_sum", "is_closed"):
        bad = json.loads(json.dumps(draft))
        bad["provider_facts"].pop(field)
        with pytest.raises(RegistrationPacketError, match="provider_facts"):
            freeze_packet(root, bad)


def test_tampered_packet_fails_digest_before_execution(tmp_path):
    root = _root(tmp_path)
    frozen = freeze_packet(root, _draft(root, {}))
    frozen["owner_judgment"] = {"changed": True}
    with pytest.raises(RegistrationPacketError, match="Packet SHA256 mismatch"):
        validate_frozen_packet(root, frozen)


def test_frozen_packet_cannot_be_refrozen_to_repin_authority(tmp_path):
    root = _root(tmp_path)
    frozen = freeze_packet(root, _draft(root, {}))
    with pytest.raises(RegistrationPacketError, match="cannot be re-frozen"):
        freeze_packet(root, frozen)


def test_validly_rehashed_stale_registry_pin_requires_owner_reconfirmation(tmp_path):
    # A packet frozen before strategy_version_pin existed pinned the whole registry.
    root = _root(tmp_path)
    frozen = freeze_packet(root, _draft(root, {}))
    frozen["authority_pins"].pop("strategy_version_pin")
    frozen["authority_pins"]["strategy_registry_digest"] = "0" * 64
    frozen["packet_sha256"] = packet_sha256(frozen)

    with pytest.raises(OwnerReconfirmationRequired, match="strategy registry"):
        validate_frozen_packet(root, frozen)


def test_validly_rehashed_changed_resolver_result_requires_owner_reconfirmation(tmp_path):
    root = _root(tmp_path)
    frozen = freeze_packet(root, _draft(root, {}))
    frozen["strategy_resolution"]["reason"] = "executor tried to reinterpret"
    frozen["packet_sha256"] = packet_sha256(frozen)

    with pytest.raises(OwnerReconfirmationRequired, match="resolver replay"):
        validate_frozen_packet(root, frozen)


def test_registration_packet_cli_freeze_validate_and_persist(tmp_path, monkeypatch):
    import sys

    from trading_os.__main__ import main

    root = _root(tmp_path)
    draft_path = root / "draft.json"
    frozen_path = root / "frozen.json"
    draft_path.write_text(json.dumps(_draft(root, {})), encoding="utf-8")

    monkeypatch.chdir(root)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trading_os",
            "registration-packet",
            "freeze",
            "--input",
            str(draft_path),
            "--output",
            str(frozen_path),
            "--transport-mode",
            "same_agent",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert json.loads(frozen_path.read_text(encoding="utf-8"))["semantic_state"] == "frozen"

    monkeypatch.setattr(
        sys,
        "argv",
        ["trading_os", "registration-packet", "validate", "--packet", str(frozen_path)],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trading_os",
            "registration-packet",
            "persist",
            "--packet",
            str(frozen_path),
            "--recorded-at",
            "2026-10-06T08:03:00Z",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert build_census(root, "propw")["lifecycles"][0]["status"] == "UNREGISTERED"


def _rewrite_ledger_lifecycle(root: Path, **changes) -> None:
    path = root / "provider-ledgers" / "propw" / "normalized" / "trade-lifecycles.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    row["lifecycle"].update(changes)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_ledger_row_holding_another_lifecycle_is_rejected(tmp_path):
    # Filed under the right open_id but the payload belongs to a different lifecycle.
    root = _root(tmp_path)
    draft = _draft(root, {})
    _rewrite_ledger_lifecycle(root, open_id="9999999999999999")
    with pytest.raises(OwnerReconfirmationRequired, match="provider_lifecycle_id"):
        freeze_packet(root, draft)


def test_ledger_payload_declaring_another_provider_is_rejected(tmp_path):
    root = _root(tmp_path)
    draft = _draft(root, {})
    _rewrite_ledger_lifecycle(root, provider="otherex")
    with pytest.raises(OwnerReconfirmationRequired, match="provider"):
        freeze_packet(root, draft)


class _FakeStore:
    def __init__(self, record):
        self._record = record

    def _projected_record(self, _id):
        return self._record


_NO_SETUP = {
    "owner_setup_attribution": {
        "setup_status": "NO_SETUP",
        "reason": "actual entry had no setup",
        "evidence_refs": ["owner:confirmed-review"],
    }
}


def _binding_case(tmp_path, stored_resolution):
    root = _root(tmp_path)
    frozen = freeze_packet(root, _draft(root, _NO_SETUP))
    state = {"status": "REGISTERED", "packet_sha256": frozen["packet_sha256"], "trade_id": "trd_x"}
    record = {"symbol": "SUI", "direction": "long", "strategy_resolution": stored_resolution}
    expected = normalize_strategy_resolution(
        to_lifecycle_resolution(frozen["strategy_resolution"], contemporaneous=False)
    )
    return frozen, state, record, expected


def test_existing_resolution_matches_store_persisted_shape(tmp_path):
    frozen, state, record, expected = _binding_case(tmp_path, None)
    record["strategy_resolution"] = dict(expected)
    validate_existing_registration_binding(_FakeStore(record), frozen, state)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(schema_version="operational-strategy-resolution.v0"),
        lambda r: r.pop("schema_version"),
        lambda r: r.update(unexpected_metadata="x"),
        lambda r: r.update(reason="changed"),
    ],
)
def test_existing_resolution_drift_is_rejected(tmp_path, mutate):
    frozen, state, record, expected = _binding_case(tmp_path, None)
    stored = dict(expected)
    mutate(stored)
    record["strategy_resolution"] = stored
    with pytest.raises(RegistrationPacketError, match="strategy_resolution conflicts"):
        validate_existing_registration_binding(_FakeStore(record), frozen, state)


def test_chart_roles_aliases_resolve_to_saved_artifact_refs() -> None:
    from trading_os.registration_packet import _map_intake_evidence_refs

    mapped = _map_intake_evidence_refs(
        {"chart_roles": {"entry_chart": {"evidence_refs": ["image:one"], "rationale": "r"}}},
        {"one": "artifact:abc.png"},
    )
    assert mapped["chart_roles"]["entry_chart"]["evidence_refs"] == ["artifact:abc.png"]


def test_preview_discloses_provider_recency_without_changing_digest_or_ingesting(tmp_path):
    root = _root(tmp_path)
    draft = _draft(root, {})
    draft.pop("owner_confirmation")
    draft["strategy_resolution"] = {}
    draft["canonical_capture"] = _preview_capture()
    missing = prepare_confirmation_preview(root, draft, source_main_sha="a" * 40)
    assert missing["provider_sync_recency"]["currentness"] == "not_asserted"
    assert missing["provider_sync_recency"]["last_successful_sync_at"] is None
    assert missing["provider_sync_guidance"]["live_sync_performed"] is False
    assert missing["provider_sync_guidance"]["sync_capability"] == "credentialed_local_agent_only"

    status_file = root / "provider-ledgers/propw/provider-sync-status.json"
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps({
        "schema_version": "provider-sync-status.v1",
        "provider": "propw",
        "status": "recorded",
        "last_successful_sync_at": "2026-10-10T07:08:43Z",
    }), encoding="utf-8")
    recorded = prepare_confirmation_preview(root, draft, source_main_sha="a" * 40)
    assert recorded["provider_sync_recency"]["last_successful_sync_at"] == "2026-10-10T07:08:43Z"
    assert recorded["provider_sync_recency"]["currentness"] == "not_asserted"
    assert recorded["preview_digest"] == missing["preview_digest"]
    assert recorded["supporting_detail"]["provider_sync_recency"] == recorded["provider_sync_recency"]
    assert not (root / "canonical/evidence/artifacts").exists()


def test_missing_original_sl_asks_owner_before_numeric_r_gate_and_has_no_writes(tmp_path):
    root = _root(tmp_path)
    draft = _draft(root, {})
    draft.pop("owner_confirmation")
    draft["strategy_resolution"] = {}
    draft["canonical_capture"] = _preview_capture()
    draft["canonical_capture"]["stop_loss"] = "unknown"
    draft["canonical_capture"].pop("realized_r")
    draft["canonical_capture"].pop("realized_r_authority")
    prior = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    with pytest.raises(OwnerInputRequired, match="原始停損") as exc:
        prepare_confirmation_preview(root, draft, source_main_sha="a" * 40)
    assert "不是後來移動的保護性停損" in exc.value.question
    assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()} == prior


def test_missing_original_sl_must_be_asked_even_with_owner_r_attestation(tmp_path):
    root = _root(tmp_path)
    draft = _draft(root, {})
    draft.pop("owner_confirmation")
    draft["strategy_resolution"] = {}
    draft["canonical_capture"] = _preview_capture()
    draft["canonical_capture"].update(
        stop_loss="unknown", planned_rr="unknown",
        realized_r=-0.5, realized_r_authority="owner_attestation",
    )
    with pytest.raises(OwnerInputRequired):
        prepare_confirmation_preview(root, draft, source_main_sha="a" * 40)

    # The Agent may record this only AFTER the Owner explicitly says SL cannot be recalled.
    draft["owner_judgment"]["original_sl_unavailable_confirmed"] = True
    draft["owner_recollection"]["verbatim_text"] += " 原始 SL 真的不記得了。"
    fallback = prepare_confirmation_preview(root, draft, source_main_sha="a" * 40)
    assert fallback["status"] == "READY_FOR_OWNER_CONFIRMATION"
    assert fallback["canonical_capture"]["stop_loss"] == "unknown"


def test_owner_supplied_original_sl_recomputes_under_normal_preview_validation(tmp_path):
    root = _root(tmp_path)
    draft = _draft(root, {})
    draft.pop("owner_confirmation")
    draft["strategy_resolution"] = {}
    draft["canonical_capture"] = _preview_capture()
    draft["canonical_capture"]["stop_loss"] = 3.4
    ready = prepare_confirmation_preview(root, draft, source_main_sha="a" * 40)
    assert ready["status"] == "READY_FOR_OWNER_CONFIRMATION"
    assert ready["canonical_capture"]["realized_r"] == 0.0
