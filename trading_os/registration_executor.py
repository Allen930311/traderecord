"""One-command, Project-owned execution of an Owner-confirmed registration.

This module is orchestration only. Packet, evidence, lifecycle, projection,
test-lane, and Git mutations remain owned by their existing runtime surfaces.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_store import EvidenceStore, evidence_locator_for_bytes, validate_original_filename
from .live_chart_plan import LiveChartPlanError, measure_r
from .operational_lifecycle import LifecycleStore
from .projections import ProjectionStaleError, refresh_projections, verify_projections
from .registration_packet import (
    LEGACY_PACKET_SCHEMA_VERSION,
    PACKET_SCHEMA_VERSION,
    build_registration_intake,
    load_packet,
    packet_sha256,
    packet_path,
    preflight_packet_draft,
    strategy_resolutions_equivalent,
    validate_evidence_package,
    validate_frozen_packet,
)
from .registration_census import registration_completeness, registration_state
from .unified_capture import capture_retrospective_live

EXECUTOR_INPUT_SCHEMA = "registration-executor-input.v1"
COMPACT_EXECUTOR_INPUT_SCHEMA = "registration-executor-input.v2"
PROVIDER_EVIDENCE_TYPE = "provider_execution_artifact"
_CAPTURE_REQUIRED = (
    "intended_entry",
    "stop_loss",
    "take_profit",
    "planned_rr",
    "timeframe_context",
    "timeframe_trigger",
    "market_context",
    "thesis",
    "execution_environment",
    "outcome_known_at_recording",
)
_PROJECTION_PATHS = (
    "exports/operational/analytics/current.json",
    "exports/operational/analytics/manifest.json",
    "exports/operational/health/current.json",
    "exports/operational/health/manifest.json",
    "exports/operational/trade-view/trade-view-data.json",
    "exports/operational/trade-view/manifest.json",
    "exports/operational/handoff/current.json",
    "exports/operational/handoff/manifest.json",
)


class RegistrationExecutorError(ValueError):
    """A fail-closed registration execution blocker."""


def _git(root: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RegistrationExecutorError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    completed = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RegistrationExecutorError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _sha(root: Path, revision: str) -> str:
    value = _git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise RegistrationExecutorError(f"Git revision is not a commit SHA: {revision}")
    return value


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
        check=False,
    ).returncode == 0


def _prepare_checkout(source_root: Path, *, force_isolated: bool = False) -> tuple[Path, Path | None, dict[str, Any]]:
    """Reuse an eligible checkout, otherwise create exactly one safe worktree.

    ``force_isolated`` always uses a fresh worktree, so work that may need to be abandoned never
    touches a checkout other agents or the Owner are using.
    """

    source_root = source_root.resolve()
    _git(source_root, "fetch", "--no-tags", "origin", "main")
    base = _sha(source_root, "origin/main")
    head = _sha(source_root, "HEAD")
    status = _git(source_root, "status", "--porcelain=v1", "--untracked-files=all")
    clean = not status
    eligible = clean and head == base and not force_isolated
    if eligible:
        return source_root, None, {
            "mode": "reused_checkout",
            "source_head": head,
            "mutation_base_sha": base,
            "source_was_clean": True,
            "worktrees_created": 0,
        }

    if not _is_ancestor(source_root, head, base):
        raise RegistrationExecutorError(
            "checkout contains local commits absent from origin/main; preserve them and rerun from a safe main checkout"
        )
    parent = Path(tempfile.gettempdir()) / "trading-record-registration"
    parent.mkdir(parents=True, exist_ok=True)
    worktree = parent / uuid.uuid4().hex
    _git(source_root, "worktree", "add", "--detach", str(worktree), base)
    return worktree.resolve(), worktree.resolve(), {
        "mode": "isolated_worktree",
        "source_head": head,
        "mutation_base_sha": base,
        "source_was_clean": clean,
        "worktrees_created": 1,
        "source_changes_preserved": bool(status),
    }


def _json_from_text(text: str, *, label: str) -> dict[str, Any]:
    candidates = [index for index, char in enumerate(text) if char == "{"]
    for index in reversed(candidates):
        try:
            value = json.loads(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RegistrationExecutorError(f"{label} did not return a JSON object")


def _call_cli(root: Path, *arguments: str, json_output: bool = True) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "trading_os", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode:
        detail = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        raise RegistrationExecutorError(
            f"trading_os {' '.join(arguments)} failed ({completed.returncode}): {detail[-6000:]}"
        )
    if not json_output:
        return {"status": "OK"}
    return _json_from_text(completed.stdout, label="trading_os " + " ".join(arguments))


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistrationExecutorError(f"{label} is missing or invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RegistrationExecutorError(f"{label} must be a JSON object")
    return value


def _request_payload(
    path: Path,
    source_root: Path,
    *,
    execution_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _read_json(path, label="registration executor input")
    if payload.get("schema_version") == COMPACT_EXECUTOR_INPUT_SCHEMA:
        unexpected = sorted(set(payload) - {"schema_version", "registration_intake", "owner_confirmation"})
        if unexpected:
            raise RegistrationExecutorError(
                "registration-executor-input.v2 supports same_agent compact intake fields only; unexpected: "
                + ", ".join(unexpected)
            )
        intake = payload.get("registration_intake")
        confirmation = payload.get("owner_confirmation")
        if not isinstance(intake, dict):
            raise RegistrationExecutorError("registration_executor_input.registration_intake must be an object")
        if not isinstance(confirmation, dict):
            raise RegistrationExecutorError("registration_executor_input.owner_confirmation must be an object")
        unexpected_confirmation = sorted(set(confirmation) - {"status", "confirmed_at", "preview_digest"})
        if unexpected_confirmation:
            raise RegistrationExecutorError(
                "registration-executor-input.v2 owner_confirmation has unsupported fields: "
                + ", ".join(unexpected_confirmation)
            )
        if (
            confirmation.get("status") != "confirmed"
            or not isinstance(confirmation.get("confirmed_at"), str)
            or not confirmation["confirmed_at"].strip()
            or not isinstance(confirmation.get("preview_digest"), str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", confirmation["preview_digest"])
        ):
            raise RegistrationExecutorError(
                "registration_executor_input.owner_confirmation requires confirmed status, confirmed_at, and preview_digest"
            )
        packet, image_files = build_registration_intake(
            Path(execution_root or source_root),
            intake,
            image_base=source_root,
        )
        packet["owner_confirmation"] = copy.deepcopy(confirmation)
        return packet, {
            "manifest": None,
            "evidence_root": None,
            "image_files": image_files,
            "compact_intake_v2": True,
        }
    if payload.get("schema_version") == EXECUTOR_INPUT_SCHEMA:
        packet = payload.get("registration_packet")
        if not isinstance(packet, dict):
            raise RegistrationExecutorError("registration_executor_input.registration_packet must be an object")
        transport = {
            "manifest": payload.get("evidence_manifest"),
            "evidence_root": payload.get("evidence_root"),
            "image_files": payload.get("image_files", {}),
        }
        for key in ("manifest", "evidence_root"):
            if transport[key]:
                candidate = Path(str(transport[key]))
                transport[key] = str((source_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve())
        files = transport["image_files"]
        if files is not None and not isinstance(files, Mapping):
            raise RegistrationExecutorError("image_files must map evidence SHA256 to source file")
        transport["image_files"] = {
            str(key).lower(): str((source_root / Path(str(value))).resolve() if not Path(str(value)).is_absolute() else Path(str(value)).resolve())
            for key, value in (files or {}).items()
        }
        return packet, transport
    if payload.get("schema_version") in {LEGACY_PACKET_SCHEMA_VERSION, PACKET_SCHEMA_VERSION}:
        return payload, {"manifest": None, "evidence_root": None, "image_files": {}}
    raise RegistrationExecutorError(
        f"input schema_version must be {EXECUTOR_INPUT_SCHEMA}, {LEGACY_PACKET_SCHEMA_VERSION}, or {PACKET_SCHEMA_VERSION}"
    )


def _require_chart_risk_r(packet: Mapping[str, Any], capture: Mapping[str, Any], reported_r: float) -> None:
    """A calculated R must be the provider's price move over actual entry -> chart stop.

    1R is the distance from the actual filled entry to the chart's stop-loss. It is never a fixed
    amount, a later stop, the position size or the P&L. An R the Owner states himself is recorded
    as `owner_attestation` and is not recomputed here.
    """

    facts = packet.get("provider_facts")
    if not isinstance(facts, Mapping):
        return  # a bare capture with no provider facts cannot be recomputed
    try:
        measured = measure_r(
            str(facts["direction"]).lower(), facts["entry_price"], capture["stop_loss"],
            exit_price=facts["weighted_exit_price"],
        )
    except (KeyError, TypeError, ValueError, LiveChartPlanError) as exc:
        raise RegistrationExecutorError(
            "a calculated realized R needs a numeric chart stop_loss and the provider's direction, "
            "entry and exit; use owner_attestation if the Owner states the R himself, or leave R unknown"
        ) from exc
    if measured["risk"] <= 0:
        raise RegistrationExecutorError(
            f"chart stop_loss {capture['stop_loss']} is not on the losing side of the actual entry "
            f"{facts['entry_price']}; 1R cannot be measured"
        )
    expected = measured["realized_r"]
    if abs(Decimal(str(reported_r)) - expected) > Decimal("0.0015"):
        raise RegistrationExecutorError(
            f"realized_r {reported_r} does not match 1R = actual entry {facts['entry_price']} to chart stop "
            f"{capture['stop_loss']}; that calculation gives {float(expected)}"
        )


def _require_chart_planned_rr(packet: Mapping[str, Any], capture: Mapping[str, Any]) -> None:
    """planned_rr, when the chart gives numeric stop and target, is (target - entry) / (entry - stop).

    Measured from the actual fill, like realized R. If any of the three is unknown the Agent has
    nothing to contradict, so nothing is checked.
    """

    facts = packet.get("provider_facts")
    if not isinstance(facts, Mapping) or "entry_price" not in facts or "direction" not in facts:
        return
    values = [_plan_value(capture[key]) for key in ("stop_loss", "take_profit", "planned_rr")]
    if any(isinstance(value, str) for value in values):
        return
    stop, target, planned_rr = values
    try:
        measured = measure_r(str(facts["direction"]).lower(), facts["entry_price"], stop, target)
    except (TypeError, ValueError, LiveChartPlanError):
        return
    if measured["risk"] <= 0 or measured["reward"] <= 0:
        raise RegistrationExecutorError(
            f"chart stop_loss {stop} and take_profit {target} contradict the {facts['direction']} entry {facts['entry_price']}"
        )
    if abs(Decimal(str(planned_rr)) - measured["planned_rr"]) > Decimal("0.0015"):
        raise RegistrationExecutorError(
            f"planned_rr {planned_rr} does not match (take_profit - actual entry) / (actual entry - stop_loss) "
            f"= {float(measured['planned_rr'])}"
        )


def _packet_capture(packet: Mapping[str, Any], *, is_closed: bool) -> dict[str, Any]:
    capture = packet.get("canonical_capture")
    if not isinstance(capture, Mapping):
        raise RegistrationExecutorError(
            "Owner-confirmed packet requires canonical_capture; do not infer plan/context values during execution"
        )
    missing = [key for key in _CAPTURE_REQUIRED if key not in capture]
    if missing:
        raise RegistrationExecutorError("canonical_capture missing frozen fields: " + ", ".join(missing))
    for key in ("timeframe_context", "timeframe_trigger", "market_context", "thesis", "execution_environment"):
        if not isinstance(capture.get(key), str) or not capture[key].strip():
            raise RegistrationExecutorError(f"canonical_capture.{key} must be a non-empty confirmed string")
    for key in ("intended_entry", "stop_loss", "take_profit", "planned_rr"):
        _plan_value(capture[key])  # numeric and finite, or unknown / not_observable
    _require_chart_planned_rr(packet, capture)
    if capture.get("outcome_known_at_recording") not in {"true", "false", "unknown"}:
        raise RegistrationExecutorError("canonical_capture.outcome_known_at_recording must be true, false, or unknown")
    for key in ("setup_tags", "confirmation_tags", "chart_refs"):
        value = capture.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            raise RegistrationExecutorError(f"canonical_capture.{key} must be a list of non-empty strings")
    if is_closed:
        try:
            capture["realized_r"]
            float(capture["realized_r"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistrationExecutorError(
                "closed provider lifecycle requires Owner-confirmed canonical_capture.realized_r"
            ) from exc
        if capture.get("realized_r_authority") not in {
            "deterministic_execution_calculation",
            "owner_attestation",
            "governed_manual_adjudication",
        }:
            raise RegistrationExecutorError("closed provider lifecycle requires a supported canonical_capture.realized_r_authority")
        if not math.isfinite(float(capture["realized_r"])):
            raise RegistrationExecutorError("canonical_capture.realized_r must be a finite number")
        if capture.get("realized_r_authority") == "deterministic_execution_calculation":
            _require_chart_risk_r(packet, capture, float(capture["realized_r"]))
    return dict(capture)


def _provider_row_bytes(root: Path, provider: str, lifecycle_id: str) -> bytes:
    path = root / "provider-ledgers" / provider / "normalized" / "trade-lifecycles.jsonl"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RegistrationExecutorError(f"provider lifecycle ledger is unavailable: {path}") from exc
    matches: list[bytes] = []
    for line in raw.splitlines(keepends=True):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RegistrationExecutorError(f"provider lifecycle ledger contains invalid JSON: {path}") from exc
        if str(row.get("open_id") or "") == lifecycle_id:
            matches.append(line if line.endswith(b"\n") else line + b"\n")
    if len(matches) != 1:
        raise RegistrationExecutorError(
            f"expected exactly one normalized provider lifecycle row for {provider}:{lifecycle_id}; found {len(matches)}"
        )
    return matches[0]


def _artifact_source_path(root: Path, sha: str) -> Path | None:
    directory = root / "canonical" / "evidence" / "artifacts"
    if not directory.exists():
        return None
    for candidate in directory.glob(sha + ".*"):
        if candidate.name.endswith(".metadata.json") or not candidate.is_file():
            continue
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest == sha:
            return candidate
    return None


_EXECUTOR_PACKET_FIELDS = {"schema_version", "semantic_state", "packet_sha256"}
_EXECUTOR_AUTHORITY_PINS = {
    "provider_lifecycle_key",
    "strategy_registry_digest",
    "strategy_version_pin",
    "resolver_version",
    "retrospective_attribution_registry_digest",
}


def _draft_matches_frozen_packet(
    draft: Mapping[str, Any],
    frozen: Mapping[str, Any],
    *,
    ignore_source_main_pin: bool = False,
) -> bool:
    """Compare a repeated executor input with its immutable registered packet.

    Only builder-owned fields may be absent from the original draft: authority
    pins, the source-main pin when the caller omitted it, EvidenceStore locators
    derived during ingestion, and normalized evidence-time metadata.
    """

    if draft.get("packet_sha256") is not None or draft.get("semantic_state") == "frozen":
        return False
    left = copy.deepcopy(dict(draft))
    right = copy.deepcopy(dict(frozen))
    for key in _EXECUTOR_PACKET_FIELDS:
        left.pop(key, None)
        right.pop(key, None)
    right.pop("evidence_manifest_sha256", None)

    left_pins = left.get("authority_pins")
    right_pins = right.get("authority_pins")
    if isinstance(left_pins, Mapping) and isinstance(right_pins, Mapping):
        comparable_left_pins = dict(left_pins)
        comparable_pins = dict(right_pins)
        for key in _EXECUTOR_AUTHORITY_PINS:
            comparable_pins.pop(key, None)
        if ignore_source_main_pin:
            comparable_left_pins.pop("source_main_sha", None)
            comparable_pins.pop("source_main_sha", None)
        elif not left_pins.get("source_main_sha"):
            comparable_pins.pop("source_main_sha", None)
        left["authority_pins"] = comparable_left_pins
        right["authority_pins"] = comparable_pins

    # A compact intake carries no strategy_resolution: the resolver derives it from resolver_input.
    # The same resolver_input is then the same Owner-confirmed input even though today's registry
    # (a strategy or version added or stopped since) would derive a different resolution. A
    # resolution the caller does supply must still reproduce the frozen decision and every frozen
    # matrix entry (later strategies and descriptive fields may appear).
    left_resolution = left.get("strategy_resolution")
    right_resolution = right.get("strategy_resolution")
    if isinstance(right_resolution, Mapping) and (
        (not left_resolution and left.get("resolver_input") == right.get("resolver_input"))
        or (
            isinstance(left_resolution, Mapping)
            and strategy_resolutions_equivalent(left_resolution, right_resolution)
        )
    ):
        left["strategy_resolution"] = copy.deepcopy(dict(right_resolution))

    left_images = left.get("image_evidence")
    right_images = right.get("image_evidence")
    if isinstance(left_images, list) and isinstance(right_images, list) and len(left_images) == len(right_images):
        comparable_images: list[Any] = []
        for supplied, stored in zip(left_images, right_images):
            if not isinstance(supplied, Mapping) or not isinstance(stored, Mapping):
                comparable_images.append(stored)
                continue
            normalized = copy.deepcopy(dict(stored))
            if not supplied.get("locator"):
                normalized.pop("locator", None)
            if "timing_relative_to_trade" not in supplied:
                normalized.pop("timing_relative_to_trade", None)
            supplied_provenance = supplied.get("provenance")
            stored_provenance = normalized.get("provenance")
            if isinstance(stored_provenance, dict) and isinstance(supplied_provenance, Mapping):
                if "capture_time_status" not in supplied_provenance:
                    stored_provenance.pop("capture_time_status", None)
                if "captured_at" not in supplied_provenance:
                    stored_provenance.pop("captured_at", None)
            supplied_market_time = supplied.get("represented_market_time")
            stored_market_time = normalized.get("represented_market_time")
            if isinstance(stored_market_time, dict) and isinstance(supplied_market_time, Mapping):
                if "status" not in supplied_market_time:
                    stored_market_time.pop("status", None)
            comparable_images.append(normalized)
        right["image_evidence"] = comparable_images

    return json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _recover_registered_draft(
    root: Path,
    draft: Mapping[str, Any],
    *,
    ignore_source_main_pin: bool = False,
) -> dict[str, Any] | None:
    """Recover an exact registered packet before any draft image ingestion."""

    facts = draft.get("provider_facts")
    if not isinstance(facts, Mapping):
        return None
    provider = str(facts.get("provider") or "")
    lifecycle_id = str(facts.get("provider_lifecycle_id") or "")
    if not provider or not lifecycle_id:
        return None
    state = registration_state(root, provider, lifecycle_id)
    status = state.get("status")
    if status == "UNREGISTERED":
        return None
    if status != "REGISTERED":
        raise RegistrationExecutorError(
            f"provider lifecycle is {status}; refusing to ingest or rebuild a packet before lifecycle review"
        )
    bound_sha = str(state.get("packet_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", bound_sha):
        raise RegistrationExecutorError("registered lifecycle has no canonical frozen packet binding")
    frozen_path = packet_path(root, bound_sha)
    frozen = _read_json(frozen_path, label="registered frozen Registration Packet")
    if str(frozen.get("packet_sha256") or "").lower() != bound_sha or packet_sha256(frozen) != bound_sha:
        raise RegistrationExecutorError("registered frozen Registration Packet fails its immutable SHA read-back")
    if not _draft_matches_frozen_packet(
        draft,
        frozen,
        ignore_source_main_pin=ignore_source_main_pin,
    ):
        raise RegistrationExecutorError(
            "provider lifecycle is already REGISTERED to a different Owner-confirmed input; refusing to re-ingest or re-freeze"
        )
    return frozen


def _ingest_packet_images(
    root: Path,
    source_root: Path,
    packet: Mapping[str, Any],
    transport: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    images = packet.get("image_evidence")
    if not isinstance(images, list):
        raise RegistrationExecutorError("packet.image_evidence must be a list")
    evidence_store = EvidenceStore(root)
    source_by_sha = transport.get("image_files", {})
    if source_by_sha is not None and not isinstance(source_by_sha, Mapping):
        raise RegistrationExecutorError("image_files must map evidence SHA256 to source file")
    external_by_sha: dict[str, Path] = {}
    if packet.get("transport_mode") == "external_handoff":
        manifest = transport.get("manifest")
        evidence_root = transport.get("evidence_root")
        if not manifest or not evidence_root:
            raise RegistrationExecutorError("external_handoff requires evidence_manifest and evidence_root")
        package = validate_evidence_package(packet, Path(str(manifest)), Path(str(evidence_root)))
        for artifact in package["artifacts"]:
            external_by_sha[str(artifact["sha256"]).lower()] = Path(str(evidence_root)) / str(artifact["path"])

    # Validate every user-supplied name and every source digest before the first
    # temporary file or EvidenceStore mutation. A later unsafe item must not
    # leave earlier images ingested as a side effect.
    prepared: list[dict[str, Any]] = []
    image_shas: set[str] = set()
    ingested_images: list[dict[str, Any]] = []
    for index, item in enumerate(images):
        if not isinstance(item, Mapping):
            raise RegistrationExecutorError(f"packet.image_evidence[{index}] must be an object")
        sha = str(item.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise RegistrationExecutorError(f"packet.image_evidence[{index}].sha256 is invalid")
        if sha in image_shas:
            raise RegistrationExecutorError("packet.image_evidence contains duplicate sha256")
        image_shas.add(sha)
        try:
            original_filename = validate_original_filename(item.get("original_filename"))
        except ValueError as exc:
            raise RegistrationExecutorError(
                f"packet.image_evidence[{index}].original_filename is unsafe: {exc}"
            ) from exc
        existing = evidence_store.metadata(sha)
        artifact_source = _artifact_source_path(root, sha) if existing is not None else None
        if existing is not None and artifact_source is None:
            raise RegistrationExecutorError(f"EvidenceStore bytes missing or corrupt for packet image {sha}")
        names = set(existing.get("original_filenames") or []) if isinstance(existing, Mapping) else set()
        if isinstance(existing, Mapping) and existing.get("original_filename"):
            names.add(str(existing["original_filename"]))
        needs_alias = existing is not None and original_filename not in names

        source: Path | None = None
        raw: bytes | None = None
        if existing is None:
            source = external_by_sha.get(sha)
            if source is None:
                candidate = (source_by_sha or {}).get(sha)
                source = Path(str(candidate)).resolve() if candidate else _artifact_source_path(source_root, sha)
            if source is None or not source.is_file():
                raise RegistrationExecutorError(f"exact source bytes missing for packet image {sha}")
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != sha:
                raise RegistrationExecutorError(f"source bytes do not match packet image SHA256 {sha}")
        else:
            source = artifact_source
            assert source is not None
            raw = source.read_bytes()
            if hashlib.sha256(raw).hexdigest() != sha:
                raise RegistrationExecutorError(f"stored bytes do not match packet image SHA256 {sha}")

        if packet.get("transport_mode") == "same_agent":
            try:
                assert raw is not None
                planned_locator = evidence_locator_for_bytes(raw, original_filename)
            except ValueError as exc:
                raise RegistrationExecutorError(
                    f"packet.image_evidence[{index}] cannot derive a safe EvidenceStore locator: {exc}"
                ) from exc
            if existing is not None and not needs_alias and existing.get("locator") != planned_locator:
                raise RegistrationExecutorError(
                    f"existing EvidenceStore locator is noncanonical for image bytes {sha}"
                )
            if item.get("locator") is not None and item.get("locator") != planned_locator:
                raise RegistrationExecutorError(
                    f"packet.image_evidence[{index}].locator does not match its image bytes before ingest"
                )

        prepared.append({
            "index": index,
            "item": item,
            "sha": sha,
            "original_filename": original_filename,
            "existing": existing,
            "source": source,
            "raw": raw,
            "needs_alias": needs_alias,
        })

    extra_sources = set((source_by_sha or {}).keys()) - image_shas
    if extra_sources:
        raise RegistrationExecutorError(
            "image_files contains SHA256 values absent from packet.image_evidence: "
            + ", ".join(sorted(str(value) for value in extra_sources))
        )

    for entry in prepared:
        index = int(entry["index"])
        item = entry["item"]
        sha = str(entry["sha"])
        original_filename = str(entry["original_filename"])
        existing = entry["existing"]
        source = entry["source"]
        if existing is not None and not entry["needs_alias"]:
            artifact = _artifact_source_path(root, sha)
            assert artifact is not None
            ingested = {
                "sha256": sha,
                "locator": existing.get("locator"),
                "media_type": existing.get("media_type"),
                "byte_size": artifact.stat().st_size,
                "original_filename": original_filename,
                "deduped": True,
            }
        else:
            if entry["raw"] is not None:
                raw = entry["raw"]
            elif isinstance(source, Path):
                raw = source.read_bytes()
            else:
                raise RegistrationExecutorError(f"exact source bytes missing for packet image {sha}")
            if hashlib.sha256(raw).hexdigest() != sha:
                raise RegistrationExecutorError(f"source bytes do not match packet image SHA256 {sha}")
            suffix = Path(original_filename).suffix.lower()
            temp_suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"
            with tempfile.TemporaryDirectory(prefix="trading-record-evidence-") as temp:
                named = Path(temp) / f"image-{index}{temp_suffix}"
                if named.resolve().parent != Path(temp).resolve():
                    raise RegistrationExecutorError("runtime evidence staging path escaped its temporary directory")
                named.write_bytes(raw)
                ingested = evidence_store.ingest(named, original_filename=original_filename)
        if ingested.get("sha256") != sha:
            raise RegistrationExecutorError(f"EvidenceStore changed packet image identity {sha}")
        expected_locator = item.get("locator")
        if packet.get("transport_mode") == "same_agent" and expected_locator is not None and expected_locator != ingested.get("locator"):
            raise RegistrationExecutorError(f"EvidenceStore locator differs from frozen packet for {sha}")
        if packet.get("transport_mode") == "same_agent" and expected_locator is None and isinstance(item, dict):
            item["locator"] = ingested.get("locator")
        ingested_images.append({**dict(item), **ingested})
    return ingested_images, round(time.perf_counter() - started, 6)


def _evidence_binding(
    root: Path,
    ingestion: Mapping[str, Any],
    *,
    category: str,
    timing: str,
    evidence_type: str,
    source_ref: str | None = None,
    packet_item: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "source_ref": source_ref or ingestion["locator"],
        "locator": ingestion["locator"],
        "sha256": ingestion["sha256"],
        "category": category,
        "timing": timing,
        "type": evidence_type,
        "media_type": ingestion.get("media_type"),
        "byte_size": ingestion.get("byte_size"),
        "original_filename": (packet_item or {}).get("original_filename") or ingestion.get("original_filename"),
    }
    if packet_item:
        for key in ("captured_at", "provenance", "represented_market_time", "role", "timing_relative_to_trade"):
            if key in packet_item:
                result[key] = packet_item[key]
    return result


def _plan_value(value: Any) -> float | str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        raise RegistrationExecutorError("canonical_capture plan values cannot be booleans")
    if isinstance(value, str) and value.strip().lower() in {"unknown", "not_observable"}:
        return value.strip().lower()
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RegistrationExecutorError("canonical_capture plan values must be numeric or unknown") from exc
    if not math.isfinite(number):
        raise RegistrationExecutorError("canonical_capture plan values must be finite numbers or unknown")
    return number


def _packet_image_bindings(root: Path, images: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output = []
    evidence_store = EvidenceStore(root)
    for item in images:
        sha = str(item["sha256"]).lower()
        metadata = evidence_store.metadata(sha)
        artifact = _artifact_source_path(root, sha)
        if not isinstance(metadata, Mapping) or artifact is None:
            raise RegistrationExecutorError(f"packet EvidenceStore read-back failed for {sha}")
        ingestion = {
            "sha256": sha,
            "locator": metadata.get("locator"),
            "media_type": metadata.get("media_type"),
            "byte_size": artifact.stat().st_size,
            "original_filename": item.get("original_filename"),
        }
        output.append(
            _evidence_binding(
                root,
                ingestion,
                category=str(item["category"]),
                timing=str(item["timing"]),
                evidence_type=str(item["type"]),
                packet_item=item,
            )
        )
    return output


def _require_registered_packet_images(root: Path, trade_id: str, packet: Mapping[str, Any]) -> None:
    path = root / "canonical" / "records" / "trades" / f"{trade_id}.json"
    record = _read_json(path, label="registered canonical trade")
    attached = [item for item in record.get("evidence", []) if isinstance(item, Mapping)]
    for image in packet.get("image_evidence", []):
        if not isinstance(image, Mapping):
            raise RegistrationExecutorError("frozen packet contains malformed image evidence")
        if not any(
            item.get("sha256") == image.get("sha256")
            and item.get("category") == image.get("category")
            and item.get("timing") == image.get("timing")
            and item.get("type") == image.get("type")
            for item in attached
        ):
            raise RegistrationExecutorError(
                "already-registered canonical record is missing frozen packet evidence; refusing to repair an existing trade during retry"
            )


_SUPPLEMENT_EVENT_TYPES = {
    "chart_roles": "chart_roles_recorded",
    "owner_words_unrecoverable": "owner_words_unrecoverable_recorded",
    "plan_r_correction": "plan_r_corrected",
}


def _supplement_record_ok(kind: str, record: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    event_id = event.get("event_id")
    if kind == "chart_roles":
        return record.get("chart_roles") == event.get("payload", {}).get("chart_roles") and any(
            isinstance(item, Mapping) and item.get("event_id") == event_id
            for item in record.get("chart_roles_events", [])
        )
    if kind == "plan_r_correction":
        correction = record.get("plan_correction")
        return (
            isinstance(correction, Mapping)
            and correction.get("plan") == event.get("payload", {}).get("plan")
            and any(
                isinstance(item, Mapping) and item.get("event_id") == event_id
                for item in record.get("plan_correction_events", [])
            )
        )
    return any(
        isinstance(item, Mapping) and item.get("event_id") == event_id
        for item in record.get("owner_words_unrecoverable_events", [])
    )


def _remote_supplement_readback(
    root: Path,
    *,
    items: Sequence[Mapping[str, Any]],
    remote_sha: str,
) -> dict[str, Any]:
    """Read back one or more append-only lifecycle supplements from origin/main."""

    paths = [
        "canonical/lifecycle/current.json",
        "canonical/lifecycle/events.jsonl",
        *_PROJECTION_PATHS,
    ]
    content = {path: _git_bytes(root, "show", f"origin/main:{path}") for path in paths}
    try:
        events = [
            json.loads(line)
            for line in content["canonical/lifecycle/events.jsonl"].decode("utf-8").splitlines()
            if line.strip()
        ]
        lifecycle = json.loads(content["canonical/lifecycle/current.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistrationExecutorError("remote supplement lifecycle read-back is missing or invalid") from exc

    for path, data in content.items():
        if path.startswith("exports/operational/") and path.endswith(".json"):
            try:
                json.loads(data)
            except json.JSONDecodeError as exc:
                raise RegistrationExecutorError(f"remote projection artifact is invalid JSON: {path}") from exc
    analytics = json.loads(content["exports/operational/analytics/current.json"])
    trade_view = json.loads(content["exports/operational/trade-view/trade-view-data.json"])

    verified = []
    for item in items:
        kind, trade_id, trade_no, event = item["kind"], item["trade_id"], item["trade_no"], item["event"]
        event_id = event.get("event_id")
        matching = [
            row for row in events
            if isinstance(row, Mapping)
            and row.get("event_type") == _SUPPLEMENT_EVENT_TYPES[kind]
            and row.get("event_id") == event_id
        ]
        if len(matching) != 1 or matching[0] != dict(event):
            raise RegistrationExecutorError(f"remote {kind} lifecycle event read-back mismatch for {trade_no}")
        record = lifecycle.get("records", {}).get(trade_id)
        if (
            not isinstance(record, Mapping)
            or record.get("trade_id") != trade_id
            or record.get("trade_no") != trade_no
            or not _supplement_record_ok(kind, record, event)
        ):
            raise RegistrationExecutorError(f"remote {kind} current projection read-back mismatch for {trade_no}")
        identities = {trade_id, trade_no}
        analytics_rows = [
            row
            for key in ("canonical_read_model", "observations")
            for row in analytics.get(key, [])
            if isinstance(row, Mapping)
            and any(str(row.get(name)) in identities for name in ("trade_id", "trade_no", "reference"))
        ]
        view_rows = [
            row
            for row in trade_view.get("rows", [])
            if isinstance(row, Mapping)
            and any(str(row.get(name)) in identities for name in ("trade_id", "trade_no", "reference"))
        ]
        if not analytics_rows or not view_rows:
            raise RegistrationExecutorError(f"remote projection read-back is missing {trade_no}")
        verified.append({"trade_id": trade_id, "trade_no": trade_no, "event_id": event_id, "kind": kind})
    try:
        projection_verification = verify_projections(root, target_trade=items[0]["trade_no"] or items[0]["trade_id"])
    except (OSError, ValueError) as exc:
        raise RegistrationExecutorError(f"remote supplement projection verification failed: {exc}") from exc
    return {
        "status": "VERIFIED",
        "remote_ref": "origin/main",
        "commit": remote_sha,
        "verified": verified,
        "projection_artifacts": list(_PROJECTION_PATHS),
        "target_visible": {"canonical": True, "analytics": True, "trade_view": True},
        "projection_verification": projection_verification,
        "fresh_checkout_created": False,
    }


def _remote_readback(
    root: Path,
    *,
    trade_id: str,
    trade_no: str,
    expected_commit: str,
    provider: str | None = None,
    lifecycle_id: str | None = None,
    packet: Mapping[str, Any] | None = None,
    supplement_items: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    _git(root, "fetch", "--no-tags", "origin", "main")
    remote_sha = _sha(root, "origin/main")
    if remote_sha != expected_commit:
        raise RegistrationExecutorError(
            f"remote origin/main read-back points to {remote_sha}, expected persisted commit {expected_commit}"
        )
    if _sha(root, "HEAD") != remote_sha:
        raise RegistrationExecutorError("local registration checkout does not match the exact remote main read-back commit")
    if supplement_items is not None:
        return _remote_supplement_readback(root, items=supplement_items, remote_sha=remote_sha)
    if not provider or not lifecycle_id or packet is None:
        raise RegistrationExecutorError("registration remote read-back requires provider, lifecycle, and packet")
    packet_hash = str(packet["packet_sha256"]).lower()
    paths = [
        f"canonical/records/trades/{trade_id}.json",
        "canonical/lifecycle/current.json",
        "canonical/lifecycle/events.jsonl",
        "canonical/lifecycle/identity-links.jsonl",
        f"canonical/registration/packets/{packet_hash}.json",
        *_PROJECTION_PATHS,
    ]
    if provider == "propw":
        paths.append("provider-ledgers/propw/registration-census.json")
    content: dict[str, bytes] = {}
    for path in paths:
        content[path] = _git_bytes(root, "show", f"origin/main:{path}")
    census_verification = None
    if provider == "propw":
        try:
            census = json.loads(content["provider-ledgers/propw/registration-census.json"])
        except (KeyError, json.JSONDecodeError) as exc:
            raise RegistrationExecutorError("remote Registration Census read-back is missing or invalid") from exc
        census_path = root / "provider-ledgers" / "propw" / "registration-census.json"
        try:
            local_census_bytes = census_path.read_bytes()
        except OSError as exc:
            raise RegistrationExecutorError("local Registration Census is missing after main persistence") from exc
        if local_census_bytes != content["provider-ledgers/propw/registration-census.json"]:
            raise RegistrationExecutorError("local Registration Census differs from the exact remote main object")
        if census.get("freshness", {}).get("status") != "fresh":
            raise RegistrationExecutorError("remote Registration Census read-back is not fresh")
        try:
            census_verification = verify_projections(root, target_trade=trade_no, require_census_fresh=True)
        except (OSError, ValueError) as exc:
            raise RegistrationExecutorError(f"remote main read-back projection/Census verification failed: {exc}") from exc
    record = json.loads(content[f"canonical/records/trades/{trade_id}.json"])
    if record.get("trade_id") != trade_id or record.get("trade_no") != trade_no:
        raise RegistrationExecutorError("remote canonical record identity does not match execution receipt")
    lifecycle = json.loads(content["canonical/lifecycle/current.json"])
    current = lifecycle.get("records", {}).get(trade_id)
    if not isinstance(current, Mapping) or current.get("registration_packet_sha256") != packet_hash:
        raise RegistrationExecutorError("remote lifecycle read-back is missing the exact frozen packet binding")
    links = [
        json.loads(line)
        for line in content["canonical/lifecycle/identity-links.jsonl"].splitlines()
        if line.strip()
    ]
    matching_links = [
        item for item in links
        if item.get("provider") == provider
        and item.get("provider_lifecycle_id") == lifecycle_id
        and item.get("match_disposition") == "MATCHED"
        and item.get("registration_packet_sha256") == packet_hash
    ]
    if len(matching_links) != 1:
        raise RegistrationExecutorError("remote lifecycle identity-link read-back does not match frozen provider identity")
    frozen = json.loads(content[f"canonical/registration/packets/{packet_hash}.json"])
    if frozen != dict(packet) or packet_sha256(frozen) != packet_hash:
        raise RegistrationExecutorError("remote frozen Registration Packet read-back mismatch")

    record_evidence = record.get("evidence", [])
    evidence_paths: list[str] = []
    for item in record_evidence:
        if not isinstance(item, Mapping):
            continue
        locator = item.get("locator")
        sha = str(item.get("sha256") or "").lower()
        if not isinstance(locator, str) or not locator.startswith("artifact:"):
            continue
        suffix = locator.removeprefix("artifact:")
        artifact_path = f"canonical/evidence/artifacts/{suffix}"
        artifact_bytes = _git_bytes(root, "show", f"origin/main:{artifact_path}")
        if hashlib.sha256(artifact_bytes).hexdigest() != sha:
            raise RegistrationExecutorError(f"remote EvidenceStore bytes do not match lifecycle SHA256 {sha}")
        content[artifact_path] = artifact_bytes
        evidence_paths.append(artifact_path)
    for path, data in content.items():
        if path.startswith("exports/operational/") and path.endswith(".json"):
            try:
                json.loads(data)
            except json.JSONDecodeError as exc:
                raise RegistrationExecutorError(f"remote projection artifact is invalid JSON: {path}") from exc
    analytics = json.loads(content["exports/operational/analytics/current.json"])
    trade_view = json.loads(content["exports/operational/trade-view/trade-view-data.json"])
    identities = {trade_id, trade_no}
    analytics_rows = [
        row for key in ("canonical_read_model", "observations")
        for row in analytics.get(key, [])
        if isinstance(row, Mapping)
        and any(str(row.get(key_name)) in identities for key_name in ("trade_id", "trade_no", "reference"))
    ]
    view_rows = [
        row for row in trade_view.get("rows", [])
        if isinstance(row, Mapping)
        and any(str(row.get(key_name)) in identities for key_name in ("trade_id", "trade_no", "reference"))
    ]
    if not analytics_rows or not view_rows:
        raise RegistrationExecutorError("remote projection read-back is missing the registered trade")
    return {
        "status": "VERIFIED",
        "remote_ref": "origin/main",
        "commit": remote_sha,
        "canonical_trade": trade_id,
        "trade_no": trade_no,
        "provider_lifecycle": f"{provider}:{lifecycle_id}",
        "packet_sha256": packet_hash,
        "evidence_artifacts": sorted(set(evidence_paths)),
        "projection_artifacts": list(_PROJECTION_PATHS) + (["provider-ledgers/propw/registration-census.json"] if provider == "propw" else []),
        "registration_census_freshness": census_verification["registration_census_freshness"] if census_verification else None,
        "target_visible": {"canonical": True, "analytics": True, "trade_view": True},
        "fresh_checkout_created": False,
    }


def _verify_or_refresh_owner_statement_projections(root: Path, target_trade: str) -> dict[str, Any]:
    """Verify an idempotent Owner-statement retry, refreshing derived views if stale."""

    try:
        return verify_projections(root, target_trade=target_trade)
    except ProjectionStaleError:
        # The lifecycle append may have succeeded immediately before an
        # interruption. Rebuild only the existing derived read models; the
        # LifecycleStore call above is idempotent and no Canonical writer runs.
        return refresh_projections(root, target_trade=target_trade)


def _stage_registration(
    root: Path,
    source_root: Path,
    input_path: Path,
    *,
    run_receipt: Mapping[str, Any],
    manifest_override: Path | None,
    evidence_root_override: Path | None,
) -> dict[str, Any]:
    """Run one confirmed registration up to a refreshed projection, leaving the diff uncommitted.

    Tests, commit, push and remote read-back are shared and run once by
    ``_persist_staged_registrations`` for one or several staged registrations.
    """

    started = time.perf_counter()
    telemetry: dict[str, Any] = {
        "schema_version": "registration-executor-telemetry.v1",
        "packet_build_seconds": 0.0,
        "packet_validation_seconds": 0.0,
        "packet_persist_seconds": 0.0,
        "provider_seconds": 0.0,
        "evidence_seconds": 0.0,
        "resolver_seconds": 0.0,
        "canonical_capture_seconds": 0.0,
        "projection_seconds": 0.0,
        "focused_tests_seconds": 0.0,
        "full_tests_seconds": 0.0,
        "git_persistence_readback_seconds": 0.0,
    }
    validation_phases: dict[str, float] = {}
    packet_draft, transport = _request_payload(input_path, source_root, execution_root=root)
    compact_intake_v2 = bool(transport.pop("compact_intake_v2", False))
    if compact_intake_v2 and (manifest_override or evidence_root_override):
        raise RegistrationExecutorError(
            "registration-executor-input.v2 accepts same_agent raw-image paths only; use the existing external HANDOFF v1 route"
        )
    if manifest_override:
        transport["manifest"] = str(manifest_override.resolve())
    if evidence_root_override:
        transport["evidence_root"] = str(evidence_root_override.resolve())
    manifest = Path(transport["manifest"]) if transport.get("manifest") else None
    evidence_root = Path(transport["evidence_root"]) if transport.get("evidence_root") else None
    temp_context = tempfile.TemporaryDirectory(prefix="trading-registration-run-")
    temp_dir = Path(temp_context.name)
    try:
        frozen_path = temp_dir / "frozen-packet.json"
        evidence_started = time.perf_counter()
        image_bindings: list[dict[str, Any]] = []
        preexisting = False
        recovered_frozen: dict[str, Any] | None = None
        is_draft = packet_draft.get("schema_version") != PACKET_SCHEMA_VERSION
        if compact_intake_v2 and is_draft:
            # A lifecycle that is already registered is verified against its frozen packet. The
            # Owner confirmed the strategy decision of that day, so it must not be re-derived with
            # today's registry (a later strategy or version would change the derived resolution and
            # make the old preview digest look stale on an idempotent retry).
            recovered_frozen = _recover_registered_draft(root, packet_draft, ignore_source_main_pin=True)
        if compact_intake_v2 and is_draft and recovered_frozen is None:
            pins = packet_draft.get("authority_pins")
            if not isinstance(pins, dict):
                pins = {}
                packet_draft["authority_pins"] = pins
            if not pins.get("source_main_sha"):
                pins["source_main_sha"] = str(run_receipt["mutation_base_sha"])
            try:
                preflight_started = time.perf_counter()
                preflight = preflight_packet_draft(
                    root,
                    packet_draft,
                    evidence_manifest=manifest,
                    derive_strategy_resolution=True,
                    timing_sink=validation_phases,
                )
                packet_draft["strategy_resolution"] = preflight["strategy_resolution"]
                telemetry["packet_validation_seconds"] += time.perf_counter() - preflight_started
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, RegistrationExecutorError):
                    raise
                raise RegistrationExecutorError(f"registration packet preflight failed: {exc}") from exc
        if packet_draft.get("schema_version") == PACKET_SCHEMA_VERSION:
            facts_before = packet_draft.get("provider_facts")
            if isinstance(facts_before, Mapping):
                state_before = registration_state(
                    root,
                    str(facts_before.get("provider") or ""),
                    str(facts_before.get("provider_lifecycle_id") or ""),
                )
                preexisting = state_before.get("status") in {"REGISTERED", "IN_PROGRESS"}
        else:
            if recovered_frozen is None:
                recovered_frozen = _recover_registered_draft(
                    root,
                    packet_draft,
                    ignore_source_main_pin=compact_intake_v2,
                )
            preexisting = recovered_frozen is not None
        if not preexisting and is_draft and not compact_intake_v2:
            pins = packet_draft.get("authority_pins")
            if not isinstance(pins, dict):
                pins = {}
                packet_draft["authority_pins"] = pins
            if not pins.get("source_main_sha"):
                pins["source_main_sha"] = str(run_receipt["mutation_base_sha"])
            try:
                preflight_started = time.perf_counter()
                preflight = preflight_packet_draft(
                    root,
                    packet_draft,
                    evidence_manifest=manifest,
                    timing_sink=validation_phases,
                )
                telemetry["packet_validation_seconds"] += time.perf_counter() - preflight_started
                facts_draft = packet_draft.get("provider_facts")
                if not isinstance(facts_draft, Mapping):
                    raise RegistrationExecutorError("packet.provider_facts must be an object")
                capture_draft = _packet_capture(packet_draft, is_closed=bool(facts_draft.get("is_closed")))
                for field in ("intended_entry", "stop_loss", "take_profit", "planned_rr"):
                    _plan_value(capture_draft[field])
                if facts_draft.get("is_closed"):
                    float(facts_draft["weighted_exit_price"])
                    float(facts_draft["net_profit_sum"])
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, RegistrationExecutorError):
                    raise
                raise RegistrationExecutorError(f"registration packet preflight failed: {exc}") from exc
        # The formal builder validates same-agent images during freeze, so exact
        # bytes must be present first. The executor derives a missing durable
        # locator from those exact bytes; the Owner's filename remains metadata,
        # never a temporary path. External packages are ingested after freeze,
        # when the packet's manifest digest has been fixed.
        if not preexisting and is_draft and packet_draft.get("transport_mode") == "same_agent":
            image_bindings, _ = _ingest_packet_images(root, source_root, packet_draft, transport)
        if recovered_frozen is not None:
            frozen = recovered_frozen
            frozen_path.write_text(json.dumps(frozen, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        elif packet_draft.get("schema_version") == PACKET_SCHEMA_VERSION:
            frozen = dict(packet_draft)
            frozen_path.write_text(json.dumps(frozen, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        else:
            draft_path = temp_dir / "registration-input.json"
            draft_path.write_text(json.dumps(packet_draft, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            build_started = time.perf_counter()
            from .registration_packet import freeze_packet as official_freeze_packet
            frozen = official_freeze_packet(
                root,
                load_packet(draft_path),
                evidence_manifest=manifest,
                timing_sink=validation_phases,
            )
            telemetry["packet_build_seconds"] = round(time.perf_counter() - build_started, 6)
            frozen_path.write_text(json.dumps(frozen, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

        if not preexisting and frozen.get("transport_mode") == "external_handoff":
            preflight_started = time.perf_counter()
            validate_frozen_packet(
                root,
                frozen,
                evidence_manifest=manifest,
                evidence_root=evidence_root,
                timing_sink=validation_phases,
            )
            telemetry["packet_validation_seconds"] += time.perf_counter() - preflight_started
            image_bindings, _ = _ingest_packet_images(root, source_root, frozen, transport)
        telemetry["evidence_seconds"] = round(time.perf_counter() - evidence_started, 6)

        facts = frozen.get("provider_facts")
        if not isinstance(facts, Mapping):
            raise RegistrationExecutorError("packet.provider_facts must be an object")
        provider = str(facts.get("provider") or "")
        lifecycle_id = str(facts.get("provider_lifecycle_id") or "")
        capture = _packet_capture(frozen, is_closed=bool(facts.get("is_closed")))
        entry_at = time.perf_counter()
        validation = validate_frozen_packet(
            root,
            frozen,
            evidence_manifest=manifest,
            evidence_root=evidence_root,
            timing_sink=validation_phases,
        )
        telemetry["packet_validation_seconds"] += time.perf_counter() - entry_at
        telemetry["packet_validation_seconds"] = round(telemetry["packet_validation_seconds"], 6)
        if not preexisting and frozen.get("transport_mode") == "same_agent" and not image_bindings:
            image_bindings = _packet_image_bindings(root, frozen.get("image_evidence", []))
        for phase in ("provider_seconds", "evidence_seconds", "resolver_seconds"):
            telemetry[phase] = round(validation_phases.get(phase, 0.0) + telemetry[phase], 6)
        if validation["registration_state"].get("status") in {"AMBIGUOUS", "INVALID"}:
            raise RegistrationExecutorError(
                f"provider lifecycle state is {validation['registration_state'].get('status')}: {validation['registration_state'].get('detail')}"
            )

        persist_started = time.perf_counter()
        persist_args = ["registration-packet", "persist", "--packet", str(frozen_path)]
        if manifest:
            persist_args.extend(["--manifest", str(manifest)])
        if evidence_root:
            persist_args.extend(["--evidence-root", str(evidence_root)])
        persisted = _call_cli(root, *persist_args)
        telemetry["packet_persist_seconds"] = round(time.perf_counter() - persist_started, 6)

        capture_result: dict[str, Any]
        projection_result: dict[str, Any] | None = None
        if persisted.get("status") == "ALREADY_REGISTERED":
            _require_registered_packet_images(root, str(persisted.get("trade_id") or ""), frozen)
            capture_result = {
                "registered": True,
                "already_registered": True,
                "trade_id": persisted.get("trade_id"),
                "trade_no": persisted.get("trade_no"),
                "draft_id": persisted.get("draft_id"),
                "registration_status": "REGISTRATION_COMPLETE",
            }
        elif persisted.get("status") == "REGISTRATION_IN_PROGRESS":
            raise RegistrationExecutorError(
                "matching packet is already IN_PROGRESS; inspect the existing lifecycle before retrying"
            )
        else:
            provider_started = time.perf_counter()
            provider_bytes = _provider_row_bytes(root, provider, lifecycle_id)
            provider_file = temp_dir / f"{provider}-{lifecycle_id}.jsonl"
            provider_file.write_bytes(provider_bytes)
            provider_ingestion = EvidenceStore(root).ingest(provider_file)
            telemetry["evidence_seconds"] = round(telemetry["evidence_seconds"] + time.perf_counter() - provider_started, 6)
            capture_started = time.perf_counter()
            provider_binding = _evidence_binding(
                root,
                provider_ingestion,
                category="entry",
                timing="execution",
                evidence_type=PROVIDER_EVIDENCE_TYPE,
            )
            additional_evidence = _packet_image_bindings(root, image_bindings)
            close_values: dict[str, Any] | None = None
            if facts.get("is_closed") is True:
                close_values = {
                    "exit_price": float(facts["weighted_exit_price"]),
                    "closed_at": str(facts["exit_time"]),
                    "realized_r": float(capture["realized_r"]),
                    "provider_pnl": float(facts["net_profit_sum"]),
                }
                additional_evidence.append(
                    _evidence_binding(
                        root,
                        provider_ingestion,
                        category="exit",
                        timing="execution",
                        evidence_type=PROVIDER_EVIDENCE_TYPE,
                    )
                )
            outcome_known: bool | str = {
                "true": True,
                "false": False,
                "unknown": "unknown",
            }[str(capture["outcome_known_at_recording"])]
            store = LifecycleStore(root)
            capture_result = capture_retrospective_live(
                store,
                strategy_observations=None,
                symbol=str(facts["instrument"]),
                direction=str(facts["direction"]),
                intended_entry=_plan_value(capture["intended_entry"]),
                stop_loss=_plan_value(capture["stop_loss"]),
                take_profit=_plan_value(capture["take_profit"]),
                planned_rr=_plan_value(capture["planned_rr"]),
                timeframe_context=str(capture["timeframe_context"]),
                timeframe_trigger=str(capture["timeframe_trigger"]),
                market_context=str(capture["market_context"]),
                setup_tags=list(capture.get("setup_tags", [])),
                confirmation_tags=list(capture.get("confirmation_tags", [])),
                thesis=str(capture["thesis"]),
                chart_refs=list(capture.get("chart_refs", [])),
                created_at=str(facts["entry_time"]),
                provider_evidence=provider_binding,
                provider=provider,
                provider_lifecycle_id=lifecycle_id,
                provider_execution_timestamp=str(facts["entry_time"]),
                candidate_source="normalized_provider_ledger",
                actual_entry=float(facts["entry_price"]),
                outcome_known_at_recording=outcome_known,
                recording_timing="retrospective",
                execution_environment=str(capture["execution_environment"]),
                reviewer="owner",
                frozen_registration_packet=frozen,
                registration_evidence_manifest=manifest,
                registration_evidence_root=evidence_root,
                additional_evidence=additional_evidence,
            )
            if not capture_result.get("registered") or capture_result.get("already_registered"):
                if capture_result.get("already_registered"):
                    capture_result["already_registered"] = True
                elif capture_result.get("registration_status") != "REGISTRATION_COMPLETE":
                    raise RegistrationExecutorError("canonical entry capture did not complete")

            trade_id = str(capture_result.get("trade_id") or "")
            if not trade_id:
                raise RegistrationExecutorError("canonical capture did not return trade_id")
            if not capture_result.get("already_registered"):
                if close_values:
                    store.final_close(
                        trade_id,
                        exit_price=close_values["exit_price"],
                        closed_at=close_values["closed_at"],
                        realized_r=close_values["realized_r"],
                        provider_pnl=close_values["provider_pnl"],
                        source_ref=str(provider_ingestion["locator"]),
                    )
                    store.verify_exit(
                        trade_id,
                        authority_class=str(capture["realized_r_authority"]),
                        evidence_refs=[str(provider_ingestion["locator"])],
                        realized_r=close_values["realized_r"],
                        provider_pnl=close_values["provider_pnl"],
                        provider_pnl_authority="provider_read_only",
                    )
            telemetry["canonical_capture_seconds"] = round(time.perf_counter() - capture_started, 6)

        if not capture_result.get("trade_id"):
            raise RegistrationExecutorError("registration result is missing trade_id")
        trade_id = str(capture_result["trade_id"])
        trade_no = str(capture_result.get("trade_no") or "")
        canonical = _read_json(
            root / "canonical" / "records" / "trades" / f"{trade_id}.json",
            label="canonical trade record",
        )
        if not trade_no:
            trade_no = str(canonical.get("trade_no") or "")
        if not trade_no:
            raise RegistrationExecutorError("canonical record has no T-ID")

        owner_recollection = frozen.get("owner_recollection")
        owner_verbatim = (
            owner_recollection.get("verbatim_text")
            if isinstance(owner_recollection, Mapping)
            else None
        )
        if not isinstance(owner_verbatim, str) or not owner_verbatim.strip():
            if not capture_result.get("already_registered"):
                raise RegistrationExecutorError(
                    "new canonical registration requires owner_recollection.verbatim_text"
                )
            owner_verbatim = None  # Older immutable packets remain readable on retry.

        lifecycle_store = LifecycleStore(root)
        owner_source_id = f"registration:{frozen['packet_sha256']}"
        projected_record = lifecycle_store.project().get("records", {}).get(trade_id, {})
        has_owner_statement = any(
            isinstance(item, Mapping) and item.get("source_id") == owner_source_id
            for item in projected_record.get("owner_statements", [])
        ) if isinstance(projected_record, Mapping) else False
        if owner_verbatim is not None:
            lifecycle_store.record_owner_statement(
                trade_id,
                verbatim_text=owner_verbatim,
                source_id=owner_source_id,
            )

        projection_started = time.perf_counter()
        target_trade = trade_no or trade_id
        if owner_verbatim is not None and not has_owner_statement:
            # First registration and a recoverable interrupted retry persist the
            # statement through the same append-only LifecycleStore before the
            # matching projections are refreshed. Replaying this packet is a no-op.
            projection_result = refresh_projections(root, target_trade=target_trade)
        elif owner_verbatim is not None:
            # A previous run can append the statement and stop before projection
            # refresh. Verify first to keep current no-change retries read-only;
            # rebuild derived artifacts only when that interrupted state is stale.
            projection_result = _verify_or_refresh_owner_statement_projections(root, target_trade)
        else:
            # A no-change retry verifies existing projections without rewriting them.
            projection_result = verify_projections(root, target_trade=target_trade)
        telemetry["projection_seconds"] = round(time.perf_counter() - projection_started, 6)

        telemetry["stage_seconds"] = round(time.perf_counter() - started, 6)
        return {
            "started": started,
            "telemetry": telemetry,
            "frozen": frozen,
            "facts": facts,
            "provider": provider,
            "lifecycle_id": lifecycle_id,
            "trade_id": trade_id,
            "trade_no": trade_no,
            "canonical": canonical,
            "projection_result": projection_result,
        }
    finally:
        temp_context.cleanup()


def _persist_staged_registrations(
    root: Path,
    run_receipt: Mapping[str, Any],
    staged: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Test, commit, push and read back every staged registration once, as a single commit."""

    run_id = str(run_receipt["run_id"])
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    mutated = bool(status)
    commit_sha = _sha(root, "HEAD")
    test_receipt: dict[str, Any] | None = None
    focused_seconds = 0.0
    full_seconds = 0.0
    if mutated:
        test_receipt = _call_cli(root, "registration-test-lane", "run", "--run-id", run_id)
        test_timing = test_receipt.get("telemetry", {})
        focused_seconds = float(test_timing.get("focused_tests_seconds") or 0)
        full_seconds = float(test_timing.get("full_tests_seconds") or 0)
        git_started = time.perf_counter()
        _git(root, "add", "-A")
        _call_cli(root, "registration-test-lane", "verify-tested-diff", "--run-id", run_id)
        if len(staged) == 1:
            facts = staged[0]["facts"]
            message = f"Register {facts['instrument']} {facts['direction']} {staged[0]['lifecycle_id']}"
        else:
            message = f"Register {len(staged)} trades\n\n" + "\n".join(
                f"- {item['trade_no']} {item['facts']['instrument']} {item['facts']['direction']} {item['lifecycle_id']}"
                for item in staged
            )
        _git(root, "commit", "-m", message)
        commit_sha = _sha(root, "HEAD")
        _git(root, "fetch", "--no-tags", "origin", "main")
        if _sha(root, "origin/main") != str(run_receipt["mutation_base_sha"]):
            raise RegistrationExecutorError("origin/main moved during registration; do not push an un-reconciled mutation")
        _call_cli(root, "registration-test-lane", "verify-tested-diff", "--run-id", run_id)
        _git(root, "push", "origin", "HEAD:refs/heads/main")
        _git(root, "fetch", "--no-tags", "origin", "main")
        if _sha(root, "origin/main") != commit_sha:
            raise RegistrationExecutorError("origin/main did not advance to the committed registration")
    else:
        # An unchanged retry must refer to an already persisted canonical record.
        git_started = time.perf_counter()
        commit_sha = _sha(root, "origin/main")

    results: list[dict[str, Any]] = []
    for item in staged:
        telemetry = item["telemetry"]
        frozen = item["frozen"]
        facts = item["facts"]
        provider = item["provider"]
        lifecycle_id = item["lifecycle_id"]
        trade_id = item["trade_id"]
        trade_no = item["trade_no"]
        projection_result = item["projection_result"]
        telemetry["focused_tests_seconds"] = focused_seconds
        telemetry["full_tests_seconds"] = full_seconds
        telemetry["batch_size"] = len(staged)
        if len(staged) > 1:
            # Each trade's projection was verified when it was staged; later trades rewrite the shared
            # read models, so report the verification of the state that was actually committed.
            projection_result = verify_projections(root, target_trade=trade_no or trade_id)
        try:
            readback = _remote_readback(
                root,
                trade_id=trade_id,
                trade_no=trade_no,
                provider=provider,
                lifecycle_id=lifecycle_id,
                packet=frozen,
                expected_commit=commit_sha,
            )
        except Exception as exc:
            if not mutated:
                raise
            raise RegistrationExecutorError(
                f"commit {commit_sha} WAS pushed to origin/main but the remote read-back of {trade_no or trade_id} "
                f"failed: {exc}. Do not re-run the registration; check origin/main first"
            ) from exc
        if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise RegistrationExecutorError("registration checkout is not clean after Git persistence")
        telemetry["git_persistence_readback_seconds"] = round(time.perf_counter() - git_started, 6)
        telemetry["owner_confirmed_to_remote_registered_seconds"] = round(time.perf_counter() - item["started"], 6)
        telemetry["worktrees_created"] = 0
        completeness = registration_completeness(
            LifecycleStore(root).project().get("records", {}).get(trade_id), frozen, root=root
        )
        results.append({
            # REGISTERED only when the Owner's words and both charts are saved;
            # otherwise the T-ID exists but the Owner is told what is missing.
            "status": "REGISTERED" if completeness["complete"] else "REGISTERED_INCOMPLETE",
            "missing": completeness["missing"],
            "trade_no": trade_no,
            "trade_id": trade_id,
            "packet_version": frozen.get("packet_version"),
            "provider_lifecycle": f"{provider}:{lifecycle_id}",
            "packet_sha256": frozen["packet_sha256"],
            "evidence": [
                {
                    key: evidence.get(key)
                    for key in ("source_ref", "locator", "sha256", "category", "timing", "type")
                    if key in evidence
                }
                for evidence in item["canonical"].get("evidence", [])
                if isinstance(evidence, Mapping)
            ],
            "strategy_resolution": frozen["strategy_resolution"].get("status"),
            "canonical_state": "CLOSED" if facts.get("is_closed") else "OPEN",
            "exit_verification": "confirmed" if facts.get("is_closed") else "not_applicable",
            "review_status": "not_started",
            "projection_status": projection_result.get("status"),
            "projection_verification": projection_result.get("verification", projection_result),
            "test_lane": test_receipt.get("test_lane") if test_receipt else "not_required_no_mutation",
            "git_commit": commit_sha,
            "remote_readback": readback,
            "telemetry": telemetry,
        })
    return results


def _execute_in_checkout(
    root: Path,
    source_root: Path,
    input_path: Path,
    *,
    manifest_override: Path | None,
    evidence_root_override: Path | None,
) -> dict[str, Any]:
    run_receipt = _call_cli(root, "registration-test-lane", "begin")
    staged = _stage_registration(
        root,
        source_root,
        input_path,
        run_receipt=run_receipt,
        manifest_override=manifest_override,
        evidence_root_override=evidence_root_override,
    )
    return _persist_staged_registrations(root, run_receipt, [staged])[0]


def _batch_state_note(root: Path, run_receipt: Mapping[str, Any], staged: Sequence[Mapping[str, Any]]) -> str:
    done = ", ".join(str(item.get("trade_no") or item.get("trade_id")) for item in staged) or "none"
    try:
        _git(root, "fetch", "--no-tags", "origin", "main")
        moved = _sha(root, "origin/main") != str(run_receipt["mutation_base_sha"])
    except Exception:
        return f"trades staged: {done}; could not check origin/main, verify it before retrying"
    if moved:
        return (
            f"trades staged: {done}; origin/main has MOVED since the batch started - "
            "check whether the batch commit was pushed before retrying"
        )
    return f"trades staged: {done}; nothing was pushed to origin/main"


def _execute_batch_in_checkout(
    root: Path,
    source_root: Path,
    input_paths: Sequence[Path],
) -> list[dict[str, Any]]:
    """Stage every confirmed registration, then test, commit and push them together once.

    ``root`` must be an isolated worktree (see ``execute_registration_batch``): staging writes
    canonical files before anything is committed, and a failed batch is abandoned there rather than
    rolled back in a checkout someone else may be using. Any failure says what had been staged and
    whether anything reached origin/main.
    """

    run_receipt = _call_cli(root, "registration-test-lane", "begin")
    staged: list[dict[str, Any]] = []
    try:
        for input_path in input_paths:
            item = _stage_registration(
                root,
                source_root,
                input_path,
                run_receipt=run_receipt,
                manifest_override=None,
                evidence_root_override=None,
            )
            if any(earlier["trade_id"] == item["trade_id"] for earlier in staged):
                raise RegistrationExecutorError(
                    f"{input_path.name} registers the same provider lifecycle as an earlier input "
                    f"({item['provider']}:{item['lifecycle_id']}); each trade may appear in only one input"
                )
            staged.append(item)
        return _persist_staged_registrations(root, run_receipt, staged)
    except Exception as exc:
        raise RegistrationExecutorError(f"{exc}; {_batch_state_note(root, run_receipt, staged)}") from exc


def execute_registration(
    source_root: Path,
    input_path: Path,
    *,
    manifest: Path | None = None,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    source_root = source_root.resolve()
    input_path = input_path.resolve()
    overall_started = time.perf_counter()
    execution_root, owned_worktree, checkout_info = _prepare_checkout(source_root)
    try:
        result = _execute_in_checkout(
            execution_root,
            source_root,
            input_path,
            manifest_override=manifest,
            evidence_root_override=evidence_root,
        )
        result["checkout"] = checkout_info
        result["telemetry"]["worktrees_created"] = checkout_info["worktrees_created"]
        if owned_worktree:
            _git(source_root, "worktree", "remove", "--force", str(owned_worktree))
        result["telemetry"]["checkout_preparation_and_cleanup_seconds"] = round(
            time.perf_counter() - overall_started
            - result["telemetry"]["owner_confirmed_to_remote_registered_seconds"],
            6,
        )
        result["telemetry"]["owner_confirmed_to_remote_registered_seconds"] = round(
            time.perf_counter() - overall_started, 6
        )
        return result
    except Exception as exc:
        if owned_worktree:
            raise RegistrationExecutorError(
                f"{exc}; isolated worktree retained for diagnosis at {owned_worktree}"
            ) from exc
        raise


def execute_registration_batch(source_root: Path, input_paths: Sequence[Path]) -> dict[str, Any]:
    """Register several Owner-confirmed compact intakes with one test run, one commit and one push.

    Each input keeps its own preview digest and Owner confirmation, so every trade is validated
    exactly as in a single run. Only the shared tail (tests, commit, push) is done once.
    """

    source_root = source_root.resolve()
    resolved = [Path(path).resolve() for path in input_paths]
    if len(resolved) < 2:
        raise RegistrationExecutorError("run-batch needs at least two --input files; use run for a single registration")
    if len(set(resolved)) != len(resolved):
        raise RegistrationExecutorError("run-batch inputs must be distinct files")
    overall_started = time.perf_counter()
    execution_root, owned_worktree, checkout_info = _prepare_checkout(source_root, force_isolated=True)
    try:
        results = _execute_batch_in_checkout(execution_root, source_root, resolved)
        if owned_worktree:
            _git(source_root, "worktree", "remove", "--force", str(owned_worktree))
        elapsed = round(time.perf_counter() - overall_started, 6)
        for result in results:
            result["checkout"] = checkout_info
            result["telemetry"]["worktrees_created"] = checkout_info["worktrees_created"]
            # One clock for the whole batch; stage_seconds is each trade's own staging time.
            result["telemetry"]["owner_confirmed_to_remote_registered_seconds"] = elapsed
        return {"batch_size": len(results), "batch_elapsed_seconds": elapsed, "registrations": results}
    except Exception as exc:
        if owned_worktree:
            raise RegistrationExecutorError(
                f"{exc}; isolated worktree retained for diagnosis at {owned_worktree}"
            ) from exc
        raise


_CHART_ROLES_MUTATION_PATHS = {
    "PERFORMANCE.md",
    "canonical/lifecycle/current.json",
    "canonical/lifecycle/events.jsonl",
    "provider-ledgers/propw/registration-census.json",
    *_PROJECTION_PATHS,
    # The Trade View static files are generated by the same projection refresh and change
    # whenever its generator changes (for example after a merged UI change).
    "exports/operational/trade-view/index.html",
    "exports/operational/trade-view/style.css",
    "exports/operational/trade-view/app.js",
}


def _changed_paths_since(root: Path, base_sha: str) -> list[str]:
    tracked = _git(root, "diff", "--name-only", base_sha, "--").splitlines()
    untracked = _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({path for path in (*tracked, *untracked) if path})


_SUPPLEMENT_SCHEMAS = {
    "chart-roles-confirmation.v1": "chart_roles",
    "chart-roles-confirmation.v2": "chart_roles",
    "owner-words-unrecoverable.v1": "owner_words_unrecoverable",
    "plan-r-correction.v1": "plan_r_correction",
}
_SUPPLEMENT_LABELS = {
    "chart_roles": "Record chart roles",
    "owner_words_unrecoverable": "Record unrecoverable Owner words",
    "plan_r_correction": "Correct chart plan and R",
}


def _supplement_plan(payload: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Turn a confirmation file into (kind, one entry per trade). Single and batch shapes are accepted."""

    schema = payload.get("schema")
    kind = _SUPPLEMENT_SCHEMAS.get(schema)
    if kind is None:
        raise RegistrationExecutorError(
            "input must be chart-roles-confirmation.v1/v2, owner-words-unrecoverable.v1 or plan-r-correction.v1"
        )
    if schema == "chart-roles-confirmation.v1":
        entries = [{"trade_id": payload.get("trade_id"), "chart_roles": payload.get("chart_roles")}]
    elif schema == "chart-roles-confirmation.v2":
        raw = payload.get("items")
        if not isinstance(raw, list) or not raw or not all(isinstance(item, Mapping) for item in raw):
            raise RegistrationExecutorError("chart roles batch requires a non-empty items list")
        entries = [{"trade_id": item.get("trade_id"), "chart_roles": item.get("chart_roles")} for item in raw]
    elif schema == "plan-r-correction.v1":
        raw = payload.get("items")
        if not isinstance(raw, list) or not raw or not all(isinstance(item, Mapping) for item in raw):
            raise RegistrationExecutorError("plan/R correction requires a non-empty items list")
        entries = [{"trade_id": item.get("trade_id"), "chart": item.get("chart")} for item in raw]
    else:
        trade_ids = payload.get("trade_ids")
        if not isinstance(trade_ids, list) or not trade_ids:
            raise RegistrationExecutorError("unrecoverable-words declaration requires a non-empty trade_ids list")
        entries = [{"trade_id": trade_id, "reason": payload.get("reason")} for trade_id in trade_ids]
    ids = [entry["trade_id"] for entry in entries]
    if not all(isinstance(trade_id, str) and trade_id for trade_id in ids):
        raise RegistrationExecutorError("every entry requires a trade_id")
    if len(set(ids)) != len(ids):
        raise RegistrationExecutorError("a trade_id may appear only once in a supplement batch")
    return kind, entries


def _record_supplement_in_checkout(root: Path, input_path: Path, *, expected_kind: str | None = None) -> dict[str, Any]:
    payload = _read_json(input_path, label="supplement confirmation")
    kind, entries = _supplement_plan(payload)
    if expected_kind is not None and kind != expected_kind:
        raise RegistrationExecutorError(f"this command records {expected_kind}, but the input is {kind}")
    source_id, confirmed_at = payload.get("source_id"), payload.get("confirmed_at")

    run_receipt = _call_cli(root, "registration-test-lane", "begin")
    run_id = str(run_receipt["run_id"])
    store = LifecycleStore(root)
    written: list[dict[str, Any]] = []
    for entry in entries:
        if kind == "chart_roles":
            event = store.record_chart_roles(
                entry["trade_id"], entry["chart_roles"], source_id=source_id, confirmed_at=confirmed_at
            )
        elif kind == "plan_r_correction":
            event = store.record_plan_r_correction(
                entry["trade_id"], entry["chart"], source_id=source_id, confirmed_at=confirmed_at
            )
        else:
            event = store.record_owner_words_unrecoverable(
                entry["trade_id"], source_id=source_id, confirmed_at=confirmed_at, reason=entry["reason"]
            )
        written.append({"entry": entry, "event": event})
    records = store.project().get("records", {})
    for item in written:
        record = records.get(item["entry"]["trade_id"])
        if not isinstance(record, Mapping):
            raise RegistrationExecutorError("supplement lifecycle projection is missing a canonical trade")
        item["record"] = record
        item["trade_no"] = str(record.get("trade_no") or item["entry"]["trade_id"])
    target_trade = written[0]["trade_no"]

    lifecycle_changed = bool(_git(root, "status", "--porcelain=v1", "--untracked-files=all"))
    if lifecycle_changed:
        projection = refresh_projections(root, target_trade=target_trade)
    else:
        projection = verify_projections(root, target_trade=target_trade)

    changed_paths = _changed_paths_since(root, str(run_receipt["mutation_base_sha"]))
    unexpected_paths = sorted(set(changed_paths) - _CHART_ROLES_MUTATION_PATHS)
    if unexpected_paths:
        raise RegistrationExecutorError(
            "supplement changed unrelated or forbidden paths: " + ", ".join(unexpected_paths)
        )

    mutated = bool(changed_paths)
    test_receipt: dict[str, Any] | None = None
    commit_sha = str(run_receipt["mutation_base_sha"])
    persistence_status = "NO_CHANGE"
    if mutated:
        trade_nos = [item["trade_no"] for item in written]
        shown = ", ".join(trade_nos[:6]) + (f" and {len(trade_nos) - 6} more" if len(trade_nos) > 6 else "")
        test_receipt = _call_cli(root, "registration-test-lane", "run", "--run-id", run_id)
        _git(root, "add", "--", *changed_paths)
        _call_cli(root, "registration-test-lane", "verify-tested-diff", "--run-id", run_id)
        _git(root, "commit", "-m", f"{_SUPPLEMENT_LABELS[kind]} for {shown}")
        commit_sha = _sha(root, "HEAD")
        _git(root, "fetch", "--no-tags", "origin", "main")
        if _sha(root, "origin/main") != str(run_receipt["mutation_base_sha"]):
            raise RegistrationExecutorError("origin/main moved during the supplement; do not push an un-reconciled mutation")
        _call_cli(root, "registration-test-lane", "verify-tested-diff", "--run-id", run_id)
        _git(root, "push", "origin", "HEAD:refs/heads/main")
        _git(root, "fetch", "--no-tags", "origin", "main")
        if _sha(root, "origin/main") != commit_sha:
            raise RegistrationExecutorError("origin/main did not advance to the committed supplement")
        persistence_status = "PUSHED"

    readback = _remote_readback(
        root,
        trade_id=written[0]["entry"]["trade_id"],
        trade_no=target_trade,
        expected_commit=commit_sha,
        supplement_items=[
            {"kind": kind, "trade_id": item["entry"]["trade_id"], "trade_no": item["trade_no"], "event": item["event"]}
            for item in written
        ],
    )
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RegistrationExecutorError("supplement checkout is not clean after Git persistence")
    results = [
        {
            "trade_id": item["entry"]["trade_id"],
            "trade_no": item["trade_no"],
            "event_id": item["event"]["event_id"],
            "completeness": registration_completeness(item["record"], root=root),
            **({"chart_roles": item["record"].get("chart_roles", {})} if kind == "chart_roles" else {}),
            **(
                {
                    "plan_correction": item["record"].get("plan_correction", {}).get("plan"),
                    "realized_r": item["record"].get("realized_r"),
                }
                if kind == "plan_r_correction"
                else {}
            ),
        }
        for item in written
    ]
    result = {
        "kind": kind,
        "status": (
            {
                "chart_roles": "CHART_ROLES_RECORDED",
                "owner_words_unrecoverable": "OWNER_WORDS_UNRECOVERABLE_RECORDED",
                "plan_r_correction": "PLAN_R_CORRECTED",
            }[kind]
            if mutated
            else "NO_CHANGE"
        ),
        "results": results,
        "projection": projection,
        "test_lane": test_receipt.get("test_lane") if test_receipt else "not_required_no_mutation",
        "test_receipt": test_receipt,
        "changed_paths": changed_paths,
        "git_commit": commit_sha,
        "persistence": {"status": persistence_status, "remote_ref": "origin/main", "commit": commit_sha},
        "remote_readback": readback,
    }
    if len(results) == 1:
        # Keep the single-trade receipt shape that callers already use.
        result.update(
            trade_id=results[0]["trade_id"],
            event_id=results[0]["event_id"],
            completeness=results[0]["completeness"],
        )
        if kind == "chart_roles":
            result["chart_roles"] = results[0]["chart_roles"]
    return result


def record_chart_roles_supplement(
    source_root: Path, input_path: Path, *, expected_kind: str | None = None
) -> dict[str, Any]:
    """Persist an Agent-attested lifecycle supplement (chart roles, or unrecoverable Owner words).

    One call may carry many trades; they share one test run, one commit and one read-back.
    """

    source_root = source_root.resolve()
    input_path = input_path.resolve()
    execution_root, owned_worktree, checkout_info = _prepare_checkout(source_root)
    try:
        result = _record_supplement_in_checkout(execution_root, input_path, expected_kind=expected_kind)
        result["checkout"] = checkout_info
        if owned_worktree:
            _git(source_root, "worktree", "remove", "--force", str(owned_worktree))
        return result
    except Exception as exc:
        if owned_worktree:
            raise RegistrationExecutorError(
                f"{exc}; isolated worktree retained for diagnosis at {owned_worktree}"
            ) from exc
        raise


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "registration-executor",
        help="execute one Owner-confirmed registration through validation, capture, projections, tests, and Git read-back",
    )
    actions = parser.add_subparsers(dest="registration_executor_action", required=True)
    run = actions.add_parser("run", help="run the deterministic one-command registration executor")
    run.add_argument(
        "--input",
        type=Path,
        required=True,
        help="registration-executor-input.v1/v2 or frozen registration packet JSON",
    )
    run.add_argument("--manifest", type=Path, help="external handoff evidence manifest (optional when present in input)")
    run.add_argument("--evidence-root", type=Path, help="external handoff evidence package root")
    batch = actions.add_parser(
        "run-batch",
        help="register several Owner-confirmed intakes with one test run, one commit and one push",
    )
    batch.add_argument(
        "--input",
        type=Path,
        action="append",
        required=True,
        help="registration-executor-input.v2 JSON (repeat once per trade, each with its own Owner confirmation)",
    )
    roles = actions.add_parser(
        "record-chart-roles",
        help="Agent-only: record the Agent's confirmed entry/exit chart judgment on a registered trade",
    )
    roles.add_argument(
        "--input", type=Path, required=True,
        help="chart-roles-confirmation.v1 (one trade) or v2 (a batch) JSON generated by the Agent",
    )
    plan_fix = actions.add_parser(
        "record-plan-r-correction",
        help="Agent-only: record chart-derived plan levels and realized R (1R = actual fill to chart stop) on registered trades",
    )
    plan_fix.add_argument(
        "--input", type=Path, required=True,
        help="plan-r-correction.v1 JSON generated by the Agent after the Owner's confirmation",
    )
    words = actions.add_parser(
        "record-owner-words-unrecoverable",
        help="Agent-only: record that the Owner's verbatim words for old trades cannot be recovered",
    )
    words.add_argument(
        "--input", type=Path, required=True,
        help="owner-words-unrecoverable.v1 JSON generated by the Agent after the Owner's confirmation",
    )


def dispatch_cli(root: Path, args: argparse.Namespace) -> None:
    source_root = root.resolve()
    supplement_kinds = {
        "record-chart-roles": "chart_roles",
        "record-owner-words-unrecoverable": "owner_words_unrecoverable",
        "record-plan-r-correction": "plan_r_correction",
    }
    if args.registration_executor_action in supplement_kinds:
        input_path = args.input.resolve() if args.input.is_absolute() else (source_root / args.input).resolve()
        result = record_chart_roles_supplement(
            source_root, input_path, expected_kind=supplement_kinds[args.registration_executor_action]
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if args.registration_executor_action == "run-batch":
        input_paths = [
            path.resolve() if path.is_absolute() else (source_root / path).resolve() for path in args.input
        ]
        print(json.dumps(execute_registration_batch(source_root, input_paths), ensure_ascii=False, sort_keys=True, indent=2))
        return
    if args.registration_executor_action != "run":
        raise RegistrationExecutorError("unsupported registration-executor action")
    input_path = args.input.resolve() if args.input.is_absolute() else (source_root / args.input).resolve()
    manifest = args.manifest.resolve() if args.manifest and args.manifest.is_absolute() else ((source_root / args.manifest).resolve() if args.manifest else None)
    evidence_root = args.evidence_root.resolve() if args.evidence_root and args.evidence_root.is_absolute() else ((source_root / args.evidence_root).resolve() if args.evidence_root else None)
    result = execute_registration(source_root, input_path, manifest=manifest, evidence_root=evidence_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
