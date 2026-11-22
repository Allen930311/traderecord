from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
import tempfile

import pytest

from trading_os.evidence_store import EvidenceStore
from trading_os.registration_executor import (
    RegistrationExecutorError,
    _draft_matches_frozen_packet,
    _execute_in_checkout,
    _ingest_packet_images,
    _packet_capture,
    _prepare_checkout,
    _request_payload,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
    )
    return result.stdout


def _source_with_bare_origin(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(source)], check=True, capture_output=True)
    _git(source, "config", "user.name", "Registration Executor Test")
    _git(source, "config", "user.email", "executor-test@example.invalid")
    (source / "seed.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", "seed.txt")
    _git(source, "commit", "-m", "seed")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-u", "origin", "main")
    return source, remote


def test_prepare_checkout_reuses_clean_main_checkout(tmp_path: Path) -> None:
    source, _ = _source_with_bare_origin(tmp_path)

    execution_root, owned_worktree, info = _prepare_checkout(source)

    assert execution_root == source.resolve()
    assert owned_worktree is None
    assert info["mode"] == "reused_checkout"
    assert info["worktrees_created"] == 0


def test_prepare_checkout_uses_one_worktree_and_preserves_dirty_source(tmp_path: Path) -> None:
    source, _ = _source_with_bare_origin(tmp_path)
    marker = source / "owner-notes.txt"
    marker.write_text("keep these uncommitted notes\n", encoding="utf-8")

    execution_root, owned_worktree, info = _prepare_checkout(source)

    try:
        assert owned_worktree == execution_root
        assert execution_root != source.resolve()
        assert execution_root.is_dir()
        assert info["mode"] == "isolated_worktree"
        assert info["worktrees_created"] == 1
        assert info["source_changes_preserved"] is True
        assert marker.read_text(encoding="utf-8") == "keep these uncommitted notes\n"
        assert (execution_root / "owner-notes.txt").exists() is False
    finally:
        if owned_worktree is not None:
            _git(source, "worktree", "remove", "--force", str(owned_worktree))


@pytest.mark.parametrize("staged", [False, True])
def test_prepare_checkout_preserves_tracked_dirty_source_exactly(tmp_path: Path, staged: bool) -> None:
    source, _ = _source_with_bare_origin(tmp_path)
    tracked = source / "seed.txt"
    tracked.write_bytes(b"Owner's tracked working copy\r\n")
    if staged:
        _git(source, "add", "seed.txt")
    before_head = _git(source, "rev-parse", "HEAD")
    before_index = _git_bytes(source, "show", ":seed.txt")
    before_status = _git(source, "status", "--porcelain=v1", "--untracked-files=all")

    execution_root, owned_worktree, info = _prepare_checkout(source)
    try:
        assert execution_root == owned_worktree
        assert info["mode"] == "isolated_worktree"
        assert info["worktrees_created"] == 1
        assert (execution_root / "seed.txt").read_text(encoding="utf-8") == "base\n"
        assert tracked.read_bytes() == b"Owner's tracked working copy\r\n"
        assert _git(source, "rev-parse", "HEAD") == before_head
        assert _git_bytes(source, "show", ":seed.txt") == before_index
        assert _git(source, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    finally:
        if owned_worktree is not None:
            _git(source, "worktree", "remove", "--force", str(owned_worktree))


def test_prepare_checkout_uses_one_worktree_when_primary_checkout_is_behind_main(tmp_path: Path) -> None:
    source, remote = _source_with_bare_origin(tmp_path)
    behind_head = _git(source, "rev-parse", "HEAD")
    advancer = tmp_path / "advancer"
    subprocess.run(["git", "clone", str(remote), str(advancer)], check=True, capture_output=True)
    _git(advancer, "config", "user.name", "Registration Executor Test")
    _git(advancer, "config", "user.email", "executor-test@example.invalid")
    (advancer / "main-only.txt").write_text("fresh main\n", encoding="utf-8")
    _git(advancer, "add", "main-only.txt")
    _git(advancer, "commit", "-m", "advance main")
    _git(advancer, "push", "origin", "main")

    execution_root, owned_worktree, info = _prepare_checkout(source)
    try:
        assert owned_worktree == execution_root
        assert info["mode"] == "isolated_worktree"
        assert info["worktrees_created"] == 1
        assert info["source_head"] == behind_head
        assert (execution_root / "main-only.txt").read_text(encoding="utf-8") == "fresh main\n"
        assert _git(source, "rev-parse", "HEAD") == behind_head
        assert not (source / "main-only.txt").exists()
        assert _git(source, "status", "--porcelain=v1", "--untracked-files=all") == ""
    finally:
        if owned_worktree is not None:
            _git(source, "worktree", "remove", "--force", str(owned_worktree))


def test_confirmed_compact_intake_executor_input_builds_packet_and_exact_image_transport(tmp_path: Path) -> None:
    source, _ = _source_with_bare_origin(tmp_path)
    lifecycle_id = "COMPACTINTAKE0001"
    lifecycle = {
        "open_id": lifecycle_id,
        "instrument": "BTCUSDT",
        "direction": "long",
        "entry_time": "2026-10-08T10:00:00Z",
        "entry_price": "100",
        "is_closed": 0,
        "normalization_status": "ok",
    }
    provider_file = source / "provider-ledgers/propw/normalized/trade-lifecycles.jsonl"
    provider_file.parent.mkdir(parents=True)
    provider_file.write_text(
        json.dumps({"open_id": lifecycle_id, "lifecycle": lifecycle}) + "\n",
        encoding="utf-8",
    )
    image = source / "Owner chart.png"
    raw = b"exact compact-intake source bytes"
    image.write_bytes(raw)
    intake = {
        "schema": "registration-intake.v1",
        "provider": "propw",
        "provider_lifecycle_id": lifecycle_id,
        "canonical_capture": {"thesis": "Owner-confirmed test thesis"},
        "owner_recollection": {
            "verbatim_text": "Owner's exact wording, preserved without edits.",
            "summary": "Owner's stated reason",
        },
        "owner_judgment": {},
        "agent_observation": {"summary": "Image observation", "evidence_refs": ["image:entry"]},
        "agent_assessment": {},
        "agreed_facts": [],
        "unresolved_facts": [],
        "resolver_input": {},
        "images": [{
            "alias": "entry",
            "path": image.name,
            "category": "review",
            "timing": "post_trade",
            "type": "chart_snapshot",
        }],
    }
    confirmation = {
        "status": "confirmed",
        "confirmed_at": "2026-10-09T00:00:00Z",
        "preview_digest": "a" * 64,
    }
    request_path = tmp_path / "confirmed-intake.json"
    request_path.write_text(json.dumps({
        "schema_version": "registration-executor-input.v2",
        "registration_intake": intake,
        "owner_confirmation": confirmation,
    }), encoding="utf-8")

    packet, transport = _request_payload(request_path, source, execution_root=source)

    expected_sha = hashlib.sha256(raw).hexdigest()
    assert packet["provider_facts"]["provider_lifecycle_id"] == lifecycle_id
    assert packet["owner_recollection"]["verbatim_text"] == intake["owner_recollection"]["verbatim_text"]
    assert packet["owner_confirmation"] == confirmation
    assert packet["agent_observation"]["evidence_refs"] == [f"artifact:{expected_sha}.png"]
    assert packet["image_evidence"][0]["sha256"] == expected_sha
    assert transport["image_files"] == {expected_sha: image}
    assert transport["manifest"] is None
    assert packet.get("schema_version") is None

    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["evidence_manifest"] = "manifest.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(RegistrationExecutorError, match="same_agent compact intake fields only"):
        _request_payload(request_path, source, execution_root=source)


def test_closed_capture_requires_owner_confirmed_realized_r_authority() -> None:
    capture = {
        "intended_entry": 100,
        "stop_loss": 99,
        "take_profit": 102,
        "planned_rr": 2,
        "timeframe_context": "15m",
        "timeframe_trigger": "bullish rejection",
        "market_context": "synthetic test context",
        "thesis": "synthetic test thesis",
        "execution_environment": "synthetic",
        "outcome_known_at_recording": "true",
    }

    with pytest.raises(RegistrationExecutorError, match="Owner-confirmed canonical_capture.realized_r"):
        _packet_capture({"canonical_capture": capture}, is_closed=True)

    capture["realized_r"] = 1.5
    capture["realized_r_authority"] = "owner_attestation"
    assert _packet_capture({"canonical_capture": capture}, is_closed=True)["realized_r"] == 1.5


@pytest.mark.parametrize("unsafe_name", ["../escape.png", r"C:\\outside.png", r"\\\\server\\share\\x.png"])
def test_image_ingest_rejects_unsafe_names_before_any_write(tmp_path: Path, monkeypatch, unsafe_name: str) -> None:
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    outside = system_temp / "escape.png"
    outside.write_bytes(b"owner file must survive")
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))

    source_root = tmp_path / "input"
    source_root.mkdir()
    first_bytes = b"\x89PNG\r\n\x1a\nsynthetic-first-image"
    second_bytes = b"\x89PNG\r\n\x1a\nsynthetic-second-image"
    first_sha = hashlib.sha256(first_bytes).hexdigest()
    second_sha = hashlib.sha256(second_bytes).hexdigest()
    first_source = source_root / "first.png"
    second_source = source_root / "second.png"
    first_source.write_bytes(first_bytes)
    second_source.write_bytes(second_bytes)
    root = tmp_path / "checkout"
    root.mkdir()
    packet = {
        "transport_mode": "same_agent",
        "image_evidence": [
            {"sha256": first_sha, "original_filename": "first.png"},
            {"sha256": second_sha, "original_filename": unsafe_name},
        ],
    }
    transport = {"image_files": {first_sha: str(first_source), second_sha: str(second_source)}}

    with pytest.raises(RegistrationExecutorError, match="original_filename is unsafe"):
        _ingest_packet_images(root, source_root, packet, transport)

    assert outside.read_bytes() == b"owner file must survive"
    assert not (root / "canonical" / "evidence" / "artifacts").exists()


def test_same_agent_ingest_derives_locator_and_preserves_original_filename(tmp_path: Path) -> None:
    source_root = tmp_path / "input"
    source_root.mkdir()
    root = tmp_path / "checkout"
    root.mkdir()
    raw = b"\x89PNG\r\n\x1a\nsynthetic-appshot"
    sha = hashlib.sha256(raw).hexdigest()
    source = source_root / "image.png"
    source.write_bytes(raw)
    packet = {
        "transport_mode": "same_agent",
        "image_evidence": [{"sha256": sha, "original_filename": "Owner screenshot 01.png"}],
    }

    bindings, _ = _ingest_packet_images(
        root,
        source_root,
        packet,
        {"image_files": {sha: str(source)}},
    )

    assert packet["image_evidence"][0]["locator"] == f"artifact:{sha}.png"
    assert bindings[0]["original_filename"] == "Owner screenshot 01.png"
    assert EvidenceStore(root).metadata(sha)["original_filenames"] == ["Owner screenshot 01.png"]


def test_supplied_same_agent_locator_mismatch_is_rejected_before_evidence_write(tmp_path: Path) -> None:
    source_root = tmp_path / "input"
    source_root.mkdir()
    root = tmp_path / "checkout"
    root.mkdir()
    raw = b"\x89PNG\r\n\x1a\nsynthetic-appshot"
    sha = hashlib.sha256(raw).hexdigest()
    source = source_root / "image.png"
    source.write_bytes(raw)
    packet = {
        "transport_mode": "same_agent",
        "image_evidence": [{
            "sha256": sha,
            "original_filename": "image.png",
            "locator": "artifact:" + "f" * 64 + ".png",
        }],
    }

    with pytest.raises(RegistrationExecutorError, match="locator does not match its image bytes before ingest"):
        _ingest_packet_images(root, source_root, packet, {"image_files": {sha: str(source)}})

    assert not (root / "canonical" / "evidence" / "artifacts").exists()


def test_noncanonical_existing_locator_is_rejected_before_packet_registration(tmp_path: Path) -> None:
    source_root = tmp_path / "input"
    source_root.mkdir()
    root = tmp_path / "checkout"
    artifacts = root / "canonical" / "evidence" / "artifacts"
    artifacts.mkdir(parents=True)
    raw = b"\x89PNG\r\n\x1a\nsynthetic-appshot"
    sha = hashlib.sha256(raw).hexdigest()
    noncanonical_locator = f"artifact:{sha}.jpg"
    (artifacts / f"{sha}.jpg").write_bytes(raw)
    metadata_path = artifacts / f"{sha}.metadata.json"
    metadata = {
        "schema_version": "evidence-artifact.v1",
        "sha256": sha,
        "locator": noncanonical_locator,
        "media_type": "image/png",
        "byte_size": len(raw),
        "original_filename": "image.png",
        "original_filenames": ["image.png"],
        "ingested_at": "2026-10-08T00:00:00Z",
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    packet = {
        "transport_mode": "same_agent",
        "image_evidence": [{
            "sha256": sha,
            "original_filename": "image.png",
            "locator": noncanonical_locator,
        }],
    }

    with pytest.raises(RegistrationExecutorError, match="existing EvidenceStore locator is noncanonical"):
        _ingest_packet_images(root, source_root, packet, {"image_files": {}})

    assert (artifacts / f"{sha}.jpg").read_bytes() == raw
    assert metadata_path.read_text(encoding="utf-8") == json.dumps(metadata)
    assert sorted(path.name for path in artifacts.iterdir()) == [
        f"{sha}.jpg",
        f"{sha}.metadata.json",
    ]


def test_draft_retry_comparison_ignores_only_executor_generated_fields() -> None:
    draft = {
        "packet_version": 1,
        "authority_pins": {},
        "image_evidence": [{"sha256": "a" * 64, "original_filename": "chart.png"}],
    }
    frozen = {
        **draft,
        "schema_version": "registration-packet.v1",
        "semantic_state": "frozen",
        "packet_sha256": "b" * 64,
        "authority_pins": {
            "source_main_sha": "c" * 40,
            "provider_lifecycle_key": "propw:synthetic",
            "strategy_registry_digest": "d" * 64,
            "resolver_version": "strategy-resolver.v1",
        },
        "image_evidence": [{
            "sha256": "a" * 64,
            "original_filename": "chart.png",
            "locator": "artifact:" + "a" * 64 + ".png",
        }],
    }

    assert _draft_matches_frozen_packet(draft, frozen)

    compact_retry = {
        **draft,
        "authority_pins": {"source_main_sha": "e" * 40},
    }
    assert not _draft_matches_frozen_packet(compact_retry, frozen)
    assert _draft_matches_frozen_packet(
        compact_retry,
        frozen,
        ignore_source_main_pin=True,
    )

    changed = {**draft, "packet_version": 2}
    assert not _draft_matches_frozen_packet(changed, frozen)


def test_packet_preflight_rejects_before_same_agent_evidence_ingest(tmp_path: Path, monkeypatch) -> None:
    from trading_os import registration_executor

    root = tmp_path / "checkout"
    root.mkdir()
    source_root = tmp_path / "source"
    source_root.mkdir()
    input_path = tmp_path / "input.json"
    input_path.write_text(
        '{"schema_version":"registration-executor-input.v1","registration_packet":{'
        '"transport_mode":"same_agent",'
        '"owner_confirmation":{"status":"not_confirmed","confirmed_at":"2026-10-08T00:00:00Z"},'
        '"provider_facts":{"provider":"propw","provider_lifecycle_id":"SYNTHETIC-PREFLIGHT"},'
        '"image_evidence":[]},"image_files":{}}',
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []

    def fake_cli(_root: Path, *arguments: str, **_kwargs):
        calls.append(arguments)
        return {"run_id": "synthetic-run", "mutation_base_sha": "a" * 40}

    def fail_if_ingested(*_args, **_kwargs):
        raise AssertionError("evidence ingestion ran before packet preflight")

    monkeypatch.setattr(registration_executor, "_call_cli", fake_cli)
    monkeypatch.setattr(registration_executor, "_ingest_packet_images", fail_if_ingested)

    with pytest.raises(RegistrationExecutorError, match="packet preflight failed: owner_confirmation.status"):
        _execute_in_checkout(
            root,
            source_root,
            input_path,
            manifest_override=None,
            evidence_root_override=None,
        )

    assert calls == [("registration-test-lane", "begin")]
    assert not (root / "canonical" / "evidence" / "artifacts").exists()


def test_v1_missing_owner_verbatim_rejects_before_evidence_or_worktree_mutation(tmp_path: Path, monkeypatch) -> None:
    from trading_os import registration_executor

    root = tmp_path / "checkout"
    root.mkdir()
    source_root = tmp_path / "source"
    source_root.mkdir()
    image_path = source_root / "chart.png"
    image_bytes = b"synthetic chart evidence"
    image_path.write_bytes(image_bytes)
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps({
            "schema_version": "registration-executor-input.v1",
            "registration_packet": {
                "transport_mode": "same_agent",
                "packet_version": 1,
                "owner_confirmation": {
                    "status": "confirmed",
                    "confirmed_at": "2026-10-10T00:00:00Z",
                    "preview_digest": "a" * 64,
                },
                "owner_recollection": {"summary": "Agent summary without the Owner's original wording"},
                "provider_facts": {
                    "provider": "propw",
                    "provider_lifecycle_id": "SYNTHETIC-MISSING-OWNER-VERBATIM",
                },
                "image_evidence": [{"sha256": image_sha, "original_filename": "chart.png"}],
            },
            "image_files": {image_sha: str(image_path)},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []

    def fake_cli(_root: Path, *arguments: str, **_kwargs):
        calls.append(arguments)
        return {"run_id": "synthetic-run", "mutation_base_sha": "a" * 40}

    def fail_if_ingested(*_args, **_kwargs):
        raise AssertionError("legacy v1 image evidence was ingested before Owner verbatim validation")

    def snapshot(directory: Path) -> dict[str, bytes]:
        return {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }

    before_root = snapshot(root)
    before_source = snapshot(source_root)
    monkeypatch.setattr(registration_executor, "_call_cli", fake_cli)
    monkeypatch.setattr(registration_executor, "_ingest_packet_images", fail_if_ingested)

    with pytest.raises(RegistrationExecutorError, match="packet preflight failed: owner_recollection.verbatim_text"):
        _execute_in_checkout(
            root,
            source_root,
            input_path,
            manifest_override=None,
            evidence_root_override=None,
        )

    assert calls == [("registration-test-lane", "begin")]
    assert snapshot(root) == before_root
    assert snapshot(source_root) == before_source
    assert not (root / "canonical").exists()


def _closed_packet(direction="long", entry="100", exit_="104", stop=96, r=1.0, authority="deterministic_execution_calculation") -> dict:
    capture = {
        "intended_entry": "unknown", "stop_loss": stop, "take_profit": "unknown", "planned_rr": "unknown",
        "timeframe_context": "unknown", "timeframe_trigger": "15m", "market_context": "unknown",
        "thesis": "Owner words.", "execution_environment": "prop_challenge",
        "outcome_known_at_recording": "true", "realized_r": r, "realized_r_authority": authority,
    }
    facts = {"direction": direction, "entry_price": entry, "weighted_exit_price": exit_, "is_closed": True}
    return {"canonical_capture": capture, "provider_facts": facts}


def test_calculated_r_must_be_actual_entry_to_chart_stop() -> None:
    # Long filled at 100, chart stop 96 -> 1R = 4; exit 104 is +1R.
    assert _packet_capture(_closed_packet(r=1.0), is_closed=True)["realized_r"] == 1.0
    # Short filled at 77401.4, chart stop 78818, exit 78053.6: the loss is 0.46R.
    short = _closed_packet("short", "77401.4", "78053.6", 78818, -0.46)
    assert _packet_capture(short, is_closed=True)["realized_r"] == -0.46


@pytest.mark.parametrize("wrong", [2.0, -1.0, 0.0, 1.5])
def test_a_calculated_r_that_is_not_that_measure_is_rejected_with_the_right_number(wrong: float) -> None:
    with pytest.raises(RegistrationExecutorError, match=r"gives 1\.0"):
        _packet_capture(_closed_packet(r=wrong), is_closed=True)


def test_a_fixed_amount_or_proxy_r_cannot_pass_as_a_calculation() -> None:
    # 25 USD of P&L over a "fixed 25U" risk would be -0.58R here, but 1R is the chart distance (-0.413R).
    proxy = _closed_packet("long", "79010.1", "78367.6", 77452, -0.58)
    with pytest.raises(RegistrationExecutorError, match=r"gives -0\.412"):
        _packet_capture(proxy, is_closed=True)
    assert _packet_capture(
        _closed_packet("long", "79010.1", "78367.6", 77452, -0.412), is_closed=True
    )["realized_r"] == -0.412


def test_a_calculation_needs_a_stop_on_the_losing_side_of_the_actual_entry() -> None:
    with pytest.raises(RegistrationExecutorError, match="losing side"):
        _packet_capture(_closed_packet(stop=101), is_closed=True)
    with pytest.raises(RegistrationExecutorError, match="needs a numeric chart stop_loss"):
        _packet_capture(_closed_packet(stop="unknown"), is_closed=True)


def test_an_owner_stated_r_is_recorded_as_given_and_not_recomputed() -> None:
    owner = _closed_packet(r=-0.55, authority="owner_attestation")
    assert _packet_capture(owner, is_closed=True)["realized_r"] == -0.55


def test_planned_rr_must_follow_the_chart_levels_measured_from_the_actual_entry() -> None:
    # Long filled at 100, chart stop 96, chart target 106: 6 / 4 = 1.5.
    ok = _closed_packet(r=1.0)
    ok["canonical_capture"].update(take_profit=106, planned_rr=1.5)
    assert _packet_capture(ok, is_closed=True)["planned_rr"] == 1.5
    for wrong in (99, 2.0, 1.4):
        bad = _closed_packet(r=1.0)
        bad["canonical_capture"].update(take_profit=106, planned_rr=wrong)
        with pytest.raises(RegistrationExecutorError, match=r"= 1\.5"):
            _packet_capture(bad, is_closed=True)
    # A target on the wrong side of the entry contradicts the trade.
    contradicting = _closed_packet(r=1.0)
    contradicting["canonical_capture"].update(take_profit=98, planned_rr=1.5)
    with pytest.raises(RegistrationExecutorError, match="contradict"):
        _packet_capture(contradicting, is_closed=True)
    # With an unknown level there is nothing to contradict.
    partial = _closed_packet(r=1.0)
    partial["canonical_capture"].update(take_profit="unknown", planned_rr=99)
    assert _packet_capture(partial, is_closed=True)["planned_rr"] == 99


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_cannot_pass_the_capture_check(bad: float) -> None:
    for authority in ("deterministic_execution_calculation", "owner_attestation"):
        with pytest.raises(RegistrationExecutorError, match="finite"):
            _packet_capture(_closed_packet(r=bad, authority=authority), is_closed=True)
    for key in ("intended_entry", "stop_loss", "take_profit", "planned_rr"):
        packet = _closed_packet(r=1.0)
        packet["canonical_capture"][key] = bad
        with pytest.raises(RegistrationExecutorError, match="finite"):
            _packet_capture(packet, is_closed=True)


def test_unknown_chart_stop_does_not_authorize_fabricated_calculated_r() -> None:
    with pytest.raises(RegistrationExecutorError, match="needs a numeric chart stop_loss"):
        _packet_capture(_closed_packet(stop="unknown", r=1.0), is_closed=True)
    with pytest.raises(RegistrationExecutorError, match="Owner-confirmed canonical_capture.realized_r"):
        _packet_capture(_closed_packet(stop="unknown", r=None), is_closed=True)


def test_closed_unknown_chart_stop_allows_only_explicit_owner_r_authority() -> None:
    packet = _closed_packet(stop="unknown", r=-0.5, authority="owner_attestation")
    assert _packet_capture(packet, is_closed=True)["realized_r"] == -0.5
    assert packet["canonical_capture"]["stop_loss"] == "unknown"


def test_batch_registration_tests_commits_and_pushes_once_for_all_trades(tmp_path: Path, monkeypatch) -> None:
    from trading_os import registration_executor

    calls: list[str] = []
    staged_paths: list[Path] = []

    def fake_cli(_root: Path, *arguments: str, **_kwargs):
        calls.append(" ".join(arguments))
        return {"run_id": "synthetic-run", "mutation_base_sha": "a" * 40}

    def fake_stage(_root, _source, input_path, *, run_receipt, manifest_override, evidence_root_override):
        assert run_receipt["run_id"] == "synthetic-run"
        assert manifest_override is None and evidence_root_override is None
        staged_paths.append(input_path)
        number = len(staged_paths)
        return {"trade_no": f"T{number}", "trade_id": f"trd_{number}", "provider": "propw", "lifecycle_id": str(number)}

    persisted: list[list[dict]] = []

    def fake_persist(_root, run_receipt, staged):
        persisted.append(list(staged))
        return [{"trade_no": item["trade_no"]} for item in staged]

    monkeypatch.setattr(registration_executor, "_call_cli", fake_cli)
    monkeypatch.setattr(registration_executor, "_stage_registration", fake_stage)
    monkeypatch.setattr(registration_executor, "_persist_staged_registrations", fake_persist)

    inputs = [tmp_path / "a.json", tmp_path / "b.json"]
    results = registration_executor._execute_batch_in_checkout(tmp_path, tmp_path, inputs)

    assert calls == ["registration-test-lane begin"]  # one mutation base for the whole batch
    assert staged_paths == inputs
    assert len(persisted) == 1 and len(persisted[0]) == 2  # one test/commit/push for both trades
    assert [item["trade_no"] for item in results] == ["T1", "T2"]


def test_run_batch_needs_two_distinct_inputs(tmp_path: Path) -> None:
    from trading_os.registration_executor import execute_registration_batch

    only = tmp_path / "a.json"
    with pytest.raises(RegistrationExecutorError, match="at least two"):
        execute_registration_batch(tmp_path, [only])
    with pytest.raises(RegistrationExecutorError, match="distinct"):
        execute_registration_batch(tmp_path, [only, only])


def _batch_fixture(monkeypatch, *, git_calls: list, stage=None, origin_moved: bool = False):
    from trading_os import registration_executor

    monkeypatch.setattr(
        registration_executor, "_call_cli",
        lambda _root, *arguments, **_kwargs: {"run_id": "synthetic-run", "mutation_base_sha": "b" * 40},
    )

    def default_stage(_root, _source, input_path, **_kwargs):
        return {"trade_no": f"T9{input_path.stem}", "trade_id": f"trd_{input_path.stem}", "provider": "propw", "lifecycle_id": input_path.stem}

    monkeypatch.setattr(registration_executor, "_stage_registration", stage or default_stage)
    monkeypatch.setattr(registration_executor, "_git", lambda _root, *args: git_calls.append(args) or "")
    monkeypatch.setattr(registration_executor, "_sha", lambda _root, ref: "c" * 40 if origin_moved else "b" * 40)
    return registration_executor


def test_batch_failure_never_touches_the_checkout_and_reports_what_was_staged(tmp_path: Path, monkeypatch) -> None:
    git_calls: list = []

    def stage(_root, _source, input_path, **_kwargs):
        if input_path.name == "b.json":
            raise RegistrationExecutorError("second trade failed validation")
        return {"trade_no": "T900001", "trade_id": "trd_a", "provider": "propw", "lifecycle_id": "a"}

    executor = _batch_fixture(monkeypatch, git_calls=git_calls, stage=stage)
    monkeypatch.setattr(
        executor, "_persist_staged_registrations",
        lambda *_a, **_k: pytest.fail("a partial batch must not reach test/commit/push"),
    )

    with pytest.raises(RegistrationExecutorError, match=r"second trade failed validation.*T900001.*nothing was pushed"):
        executor._execute_batch_in_checkout(tmp_path, tmp_path, [tmp_path / "a.json", tmp_path / "b.json"])

    # No reset/clean: an abandoned batch lives in an isolated worktree, never in a shared checkout.
    assert not any(call[0] in {"reset", "clean", "checkout"} for call in git_calls)


def test_batch_failure_warns_when_origin_main_moved(tmp_path: Path, monkeypatch) -> None:
    executor = _batch_fixture(monkeypatch, git_calls=[], origin_moved=True)
    monkeypatch.setattr(
        executor, "_persist_staged_registrations",
        lambda *_a, **_k: (_ for _ in ()).throw(RegistrationExecutorError("push failed")),
    )

    with pytest.raises(RegistrationExecutorError, match=r"push failed.*origin/main has MOVED.*before retrying"):
        executor._execute_batch_in_checkout(tmp_path, tmp_path, [tmp_path / "a.json", tmp_path / "b.json"])


def test_batch_rejects_two_inputs_for_the_same_trade(tmp_path: Path, monkeypatch) -> None:
    def stage(_root, _source, input_path, **_kwargs):
        return {"trade_no": "T900001", "trade_id": "trd_same", "provider": "propw", "lifecycle_id": "2610151048096699"}

    executor = _batch_fixture(monkeypatch, git_calls=[], stage=stage)
    monkeypatch.setattr(
        executor, "_persist_staged_registrations",
        lambda *_a, **_k: pytest.fail("a duplicate trade must not reach test/commit/push"),
    )

    with pytest.raises(RegistrationExecutorError, match=r"b\.json registers the same provider lifecycle.*2610151048096699"):
        executor._execute_batch_in_checkout(tmp_path, tmp_path, [tmp_path / "a.json", tmp_path / "b.json"])


def test_batch_always_runs_in_an_isolated_worktree(tmp_path: Path, monkeypatch) -> None:
    from trading_os import registration_executor

    seen: dict = {}

    def fake_prepare(_source, *, force_isolated=False):
        seen["force_isolated"] = force_isolated
        return tmp_path, None, {"mode": "isolated_worktree", "worktrees_created": 1}

    monkeypatch.setattr(registration_executor, "_prepare_checkout", fake_prepare)
    monkeypatch.setattr(
        registration_executor, "_execute_batch_in_checkout",
        lambda *_a, **_k: [{"telemetry": {}} for _ in range(2)],
    )

    result = registration_executor.execute_registration_batch(tmp_path, [tmp_path / "a.json", tmp_path / "b.json"])

    assert seen["force_isolated"] is True
    assert result["batch_size"] == 2 and "batch_elapsed_seconds" in result


def test_force_isolated_never_reuses_even_a_clean_checkout(tmp_path: Path) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(source)], check=True, capture_output=True)
    _git(source, "config", "user.name", "t")
    _git(source, "config", "user.email", "t@example.com")
    (source / "f.txt").write_text("x\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "seed")
    _git(source, "push", "-q", "origin", "HEAD:refs/heads/main")

    reused, owned_none, _ = _prepare_checkout(source)
    assert reused == source.resolve() and owned_none is None

    isolated, owned, info = _prepare_checkout(source, force_isolated=True)
    try:
        assert owned is not None and isolated != source.resolve() and info["mode"] == "isolated_worktree"
    finally:
        _git(source, "worktree", "remove", "--force", str(owned))


def test_run_batch_needs_two_distinct_inputs(tmp_path: Path) -> None:
    from trading_os.registration_executor import execute_registration_batch

    only = tmp_path / "a.json"
    with pytest.raises(RegistrationExecutorError, match="at least two"):
        execute_registration_batch(tmp_path, [only])
    with pytest.raises(RegistrationExecutorError, match="distinct"):
        execute_registration_batch(tmp_path, [only, only])
