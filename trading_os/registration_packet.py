"""Deterministic Registration Packet preparation and frozen intake.

This module is the runtime counterpart of workflows/registration-execution.yaml.
Its read-only confirmation preview validates an unfrozen draft without Owner
confirmation or persistence. The frozen intake still accepts only the confirmed
semantic packet, verifies authority/evidence pins, and persists the immutable
packet. Neither path reinterprets a reviewed trade. Unresolved attribution is
carried into the registered LIVE lifecycle; the legacy registration_pending
reader remains for old pending attempts only.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

from .evidence_store import EvidenceStore
from .evidence_time import (
    EvidenceTimeError,
    normalize_evidence_time_metadata,
    represented_market_time_contains,
)
from .historical_attribution import historical_attribution_registry_digest
from .operational_lifecycle import (
    LifecycleError,
    LifecycleStore,
    canonical_provider,
    normalize_strategy_resolution,
)
from .registration_census import registration_state
from .provider_sync_status import registration_sync_disclosure
from .registry import (
    load_registry,
    registry_digest,
    registry_as_of_b3,
    resolve_version_path,
    strategy_version_digest,
)
from .strategy_resolver import RESOLVER_VERSION, resolve_strategy, to_lifecycle_resolution

PACKET_SCHEMA_VERSION = "registration-packet.v2"
LEGACY_PACKET_SCHEMA_VERSION = "registration-packet.v1"
PACKET_DIR = Path("canonical") / "registration" / "packets"
ALLOWED_RESOLVER_STATUSES = {"MATCHED", "NO_MATCH", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE"}
TRANSPORT_MODES = {"same_agent", "external_handoff"}

BASE_PROVIDER_FACT_FIELDS = (
    "provider",
    "provider_lifecycle_id",
    "instrument",
    "direction",
    "entry_time",
    "entry_price",
    "is_closed",
)
CLOSED_PROVIDER_FACT_FIELDS = BASE_PROVIDER_FACT_FIELDS + (
    "exit_time",
    "weighted_exit_price",
    "net_profit_sum",
)

REQUIRED_SECTIONS = (
    "provider_facts",
    "image_evidence",
    "agent_observation",
    "agent_assessment",
    "owner_recollection",
    "owner_judgment",
    "agreed_facts",
    "unresolved_facts",
    "resolver_input",
    "strategy_resolution",
    "authority_pins",
    "canonical_review_projection",
    "research_hypotheses",
)


class RegistrationPacketError(ValueError):
    """Malformed or integrity-invalid frozen Registration Packet."""


class OwnerReconfirmationRequired(RegistrationPacketError):
    """Frozen semantic/authority facts drifted and the executor may not repair them."""


class OwnerInputRequired(RegistrationPacketError):
    """A missing Owner plan value requires a chat question, not a fake estimate."""

    def __init__(self, question: str):
        self.question = question
        super().__init__("OWNER_INPUT_REQUIRED: " + question)


ORIGINAL_SL_QUESTION = (
    "這筆交易進場時的原始停損（SL）精確價位是多少？"
    "不是後來移動的保護性停損或交易所最後的停損。"
    "如果真的不記得，請直接告訴我。"
)


def require_owner_original_sl_before_preview(packet: Mapping[str, Any]) -> None:
    """Ask first if the original SL is absent; preserve explicit non-recall exceptions.

    This is a conversational intake guard, NOT a numeric SL inference, a Writer,
    or a new Owner confirmation. The final packet still must pass the existing
    provider, realized-R and digest gates.
    """
    capture = packet.get("canonical_capture")
    stop = capture.get("stop_loss") if isinstance(capture, Mapping) else None
    missing = stop is None or (isinstance(stop, str) and stop.strip().lower() in {
        "unknown", "not_observable", "",
    })
    if not missing:
        return
    judgment = packet.get("owner_judgment")
    if isinstance(judgment, Mapping) and judgment.get("original_sl_unavailable_confirmed") is True:
        return
    raise OwnerInputRequired(ORIGINAL_SL_QUESTION)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_value(value: Any) -> str:
    return _sha_bytes(_canonical(value).encode("utf-8"))


def confirmation_snapshot_digest(
    packet: Mapping[str, Any],
    *,
    evidence_manifest_sha256: str | None = None,
) -> str:
    """Digest the exact semantic packet represented to and confirmed by the Owner.

    Authority pins are checked again at freeze and execution time. Runtime-only
    EvidenceStore locators are derived after preview, so neither belongs in the
    Owner's semantic snapshot. Image hashes, provenance, manifest bytes, plan,
    thesis, observations, and resolver result do belong in it.
    """

    snapshot = copy.deepcopy(dict(packet))
    pins = snapshot.get("authority_pins")
    for key in (
        "owner_confirmation",
        "packet_sha256",
        "semantic_state",
        "schema_version",
        "authority_pins",
    ):
        snapshot.pop(key, None)
    if isinstance(pins, Mapping) and "strategy_version_pin" in pins:
        # The exact rule version the Owner confirmed is part of what was confirmed: a version
        # that changes between preview and freeze needs a fresh preview even when the resolver
        # output is unchanged. Packets frozen before this pin existed have no such key.
        snapshot["strategy_version_pin"] = pins["strategy_version_pin"]
    snapshot["evidence_manifest_sha256"] = evidence_manifest_sha256
    images = snapshot.get("image_evidence")
    if isinstance(images, list):
        snapshot["image_evidence"] = [
            {key: value for key, value in item.items() if key != "locator"}
            if isinstance(item, Mapping)
            else item
            for item in images
        ]
    return _sha_value(snapshot)


def _validate_confirmation_snapshot(
    packet: Mapping[str, Any],
    *,
    evidence_manifest_sha256: str | None = None,
) -> None:
    if packet.get("schema_version") != PACKET_SCHEMA_VERSION:
        return  # Frozen v1 packets remain readable as historical records.
    confirmation = packet.get("owner_confirmation")
    digest = confirmation.get("preview_digest") if isinstance(confirmation, Mapping) else None
    expected = confirmation_snapshot_digest(
        packet,
        evidence_manifest_sha256=evidence_manifest_sha256,
    )
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest.lower()):
        raise OwnerReconfirmationRequired(
            "OWNER_RECONFIRMATION_REQUIRED: Owner confirmation must bind a preview_digest"
        )
    if digest.lower() != expected:
        raise OwnerReconfirmationRequired(
            "OWNER_RECONFIRMATION_REQUIRED: packet semantics changed after Owner preview; rerun preview and confirm again"
        )


def _packet_body(packet: Mapping[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(dict(packet))
    body.pop("packet_sha256", None)
    return body


def packet_sha256(packet: Mapping[str, Any]) -> str:
    return _sha_value(_packet_body(packet))


def manifest_sha256(path: Path) -> str:
    return _sha_bytes(Path(path).read_bytes())


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RegistrationPacketError(f"{field} must be an object")
    return value


def _validate_transport_mode(packet: Mapping[str, Any]) -> str:
    mode = packet.get("transport_mode")
    if mode not in TRANSPORT_MODES:
        raise RegistrationPacketError(
            "transport_mode must be one of: " + ", ".join(sorted(TRANSPORT_MODES))
        )
    return str(mode)


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def _validate_image_evidence_schema(
    packet: Mapping[str, Any],
    *,
    require_same_agent_locator: bool = True,
) -> list[dict[str, Any]]:
    raw = packet.get("image_evidence")
    if not isinstance(raw, list):
        raise RegistrationPacketError("image_evidence must be a list")
    mode = _validate_transport_mode(packet)
    normalized: list[dict[str, Any]] = []
    seen_sha: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise RegistrationPacketError(f"image_evidence[{index}] must be an object")
        sha = item.get("sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            raise RegistrationPacketError(f"image_evidence[{index}].sha256 must be a 64-character hex digest")
        sha = sha.lower()
        if sha in seen_sha:
            raise RegistrationPacketError("image_evidence contains duplicate sha256")
        seen_sha.add(sha)

        original_filename = item.get("original_filename")
        if not isinstance(original_filename, str) or not original_filename.strip():
            raise RegistrationPacketError(f"image_evidence[{index}].original_filename is required")

        entry = dict(item)
        entry["sha256"] = sha
        try:
            entry.update(normalize_evidence_time_metadata(entry))
        except EvidenceTimeError as exc:
            raise RegistrationPacketError(f"image_evidence[{index}] timing metadata is invalid: {exc}") from exc
        if mode == "same_agent":
            locator = entry.get("locator")
            if locator is not None and (
                not isinstance(locator, str) or not locator.startswith("artifact:")
            ):
                raise RegistrationPacketError(
                    f"image_evidence[{index}].locator must be an artifact: locator when supplied"
                )
            if require_same_agent_locator and locator is None:
                raise RegistrationPacketError(
                    f"image_evidence[{index}].locator must be a durable artifact: locator for same_agent"
                )
        else:
            package_path = entry.get("package_path") or entry.get("zip_path") or entry.get("path")
            if not isinstance(package_path, str) or not package_path.strip():
                raise RegistrationPacketError(
                    f"image_evidence[{index}] requires package_path/zip_path/path for external_handoff"
                )
            entry["package_path"] = package_path
        normalized.append(entry)
    return normalized


def _load_evidence_manifest(path: Path) -> Mapping[str, Any]:
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegistrationPacketError("evidence manifest is not valid JSON") from exc
    if not isinstance(manifest, Mapping):
        raise RegistrationPacketError("evidence manifest must be an object")
    return manifest


def _manifest_artifact_path(artifact: Mapping[str, Any]) -> str | None:
    value = artifact.get("zip_path") or artifact.get("path")
    return value if isinstance(value, str) and value.strip() else None


def _is_owner_image_artifact(artifact: Mapping[str, Any]) -> bool:
    media_type = str(artifact.get("media_type") or "").lower()
    if media_type.startswith("image/"):
        return True
    name = artifact.get("original_filename") or _manifest_artifact_path(artifact) or ""
    return Path(str(name)).suffix.lower() in IMAGE_EXTENSIONS


def _external_image_identity_from_packet(item: Mapping[str, Any]) -> tuple[str, str, str]:
    path = item.get("package_path") or item.get("zip_path") or item.get("path")
    return (
        str(item["sha256"]).lower(),
        str(path).replace("\\", "/"),
        str(item["original_filename"]),
    )


def _external_image_identity_from_manifest(item: Mapping[str, Any]) -> tuple[str, str, str]:
    path = _manifest_artifact_path(item)
    return (
        str(item.get("sha256") or "").lower(),
        str(path or "").replace("\\", "/"),
        str(item.get("original_filename") or ""),
    )


def _validate_external_image_manifest_binding(packet: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    packet_images = _validate_image_evidence_schema(packet)
    manifest_images = [item for item in _manifest_artifacts(manifest) if _is_owner_image_artifact(item)]

    packet_identities = {_external_image_identity_from_packet(item) for item in packet_images}
    manifest_identities = {_external_image_identity_from_manifest(item) for item in manifest_images}
    if packet_identities != manifest_identities:
        raise RegistrationPacketError(
            "frozen image_evidence does not match external owner-image manifest"
        )


def _validate_same_agent_image_store(root: Path, packet: Mapping[str, Any]) -> None:
    images = _validate_image_evidence_schema(packet)
    store = EvidenceStore(Path(root))
    for item in images:
        sha = str(item["sha256"]).lower()
        metadata = store.metadata(sha)
        if not isinstance(metadata, Mapping):
            raise RegistrationPacketError(
                f"same_agent image_evidence is not durably ingested in EvidenceStore: {sha}"
            )
        if metadata.get("sha256") != sha or metadata.get("locator") != item.get("locator"):
            raise RegistrationPacketError(
                f"same_agent image_evidence identity conflicts with EvidenceStore: {sha}"
            )
        names = set(metadata.get("original_filenames") or [])
        if metadata.get("original_filename"):
            names.add(str(metadata["original_filename"]))
        if item.get("original_filename") not in names:
            raise RegistrationPacketError(
                f"same_agent image_evidence original_filename conflicts with EvidenceStore: {sha}"
            )
        locator = str(metadata["locator"])
        relative = locator.removeprefix("artifact:")
        if not relative or "/" in relative or "\\" in relative:
            raise RegistrationPacketError(f"EvidenceStore locator is invalid for image: {sha}")
        artifact_path = store.directory / relative
        if not artifact_path.is_file() or _sha_bytes(artifact_path.read_bytes()) != sha:
            raise RegistrationPacketError(
                f"same_agent EvidenceStore artifact bytes do not match image_evidence: {sha}"
            )


def _validate_provider_facts_schema(provider_facts: Mapping[str, Any]) -> None:
    missing = [
        key
        for key in BASE_PROVIDER_FACT_FIELDS
        if key not in provider_facts or provider_facts.get(key) in (None, "")
    ]
    if missing:
        raise RegistrationPacketError(
            "provider_facts missing required fields: " + ", ".join(missing)
        )
    if not isinstance(provider_facts.get("is_closed"), bool):
        raise RegistrationPacketError("provider_facts.is_closed must be boolean")
    if provider_facts["is_closed"]:
        missing_closed = [
            key
            for key in CLOSED_PROVIDER_FACT_FIELDS
            if key not in provider_facts or provider_facts.get(key) in (None, "")
        ]
        if missing_closed:
            raise RegistrationPacketError(
                "closed provider_facts missing required fields: " + ", ".join(missing_closed)
            )


def _provider_key(provider_facts: Mapping[str, Any]) -> str:
    provider = provider_facts.get("provider")
    lifecycle_id = provider_facts.get("provider_lifecycle_id")
    if not isinstance(provider, str) or not provider.strip():
        raise RegistrationPacketError("provider_facts.provider is required")
    if not isinstance(lifecycle_id, str) or not lifecycle_id.strip():
        raise RegistrationPacketError("provider_facts.provider_lifecycle_id is required")
    return f"{canonical_provider(provider)}:{lifecycle_id}"


def _owner_verbatim_text(packet: Mapping[str, Any]) -> str:
    """Return the unmodified Owner wording required for a new registration."""

    recollection = packet.get("owner_recollection")
    value = recollection.get("verbatim_text") if isinstance(recollection, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        raise RegistrationPacketError(
            "owner_recollection.verbatim_text is required and must preserve the Owner's exact wording"
        )
    return value


def _validate_owner_confirmation(packet: Mapping[str, Any]) -> None:
    confirmation = _require_mapping(packet.get("owner_confirmation"), "owner_confirmation")
    if confirmation.get("status") != "confirmed":
        raise RegistrationPacketError("owner_confirmation.status must equal confirmed")
    if not isinstance(confirmation.get("confirmed_at"), str) or not confirmation["confirmed_at"].strip():
        raise RegistrationPacketError("owner_confirmation.confirmed_at is required")
    if packet.get("schema_version") == PACKET_SCHEMA_VERSION:
        preview_digest = confirmation.get("preview_digest")
        if not isinstance(preview_digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", preview_digest):
            raise OwnerReconfirmationRequired(
                "OWNER_RECONFIRMATION_REQUIRED: Owner confirmation must include the reviewed preview_digest"
            )
    if packet.get("semantic_state") != "frozen":
        raise RegistrationPacketError("semantic_state must equal frozen")
    version = packet.get("packet_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RegistrationPacketError("packet_version must be a positive integer")

    transport_mode = packet.get("transport_mode")
    if transport_mode not in TRANSPORT_MODES:
        raise RegistrationPacketError(
            "transport_mode must be one of: " + ", ".join(sorted(TRANSPORT_MODES))
        )


def _validate_required_sections(
    packet: Mapping[str, Any],
    *,
    require_same_agent_locator: bool = True,
) -> None:
    missing = [field for field in REQUIRED_SECTIONS if field not in packet]
    if missing:
        raise RegistrationPacketError("missing Registration Packet sections: " + ", ".join(missing))
    provider_facts = _require_mapping(packet["provider_facts"], "provider_facts")
    _validate_provider_facts_schema(provider_facts)
    _validate_transport_mode(packet)
    _validate_image_evidence_schema(
        packet,
        require_same_agent_locator=require_same_agent_locator,
    )
    _require_mapping(packet["resolver_input"], "resolver_input")
    _require_mapping(packet["strategy_resolution"], "strategy_resolution")
    _require_mapping(packet["authority_pins"], "authority_pins")


def _current_registry_digest(root: Path) -> str:
    return registry_digest(load_registry(Path(root)))


def _current_retrospective_attribution_digest(root: Path) -> str:
    return historical_attribution_registry_digest(Path(root))


def strategy_version_pin(root: Path, strategy_resolution: Mapping[str, Any]) -> dict[str, str] | None:
    """Pin only the frozen rule version this registration used, not the whole registry.

    A NO_MATCH or unresolved attribution uses no strategy version, so it pins nothing
    (None). Adding an unrelated strategy to the registry never changes this pin.
    """

    strategy_id = strategy_resolution.get("strategy_id")
    rule_version = strategy_resolution.get("rule_version")
    if not isinstance(strategy_id, str) or not strategy_id or not isinstance(rule_version, str) or not rule_version:
        return None
    return {
        "strategy_id": strategy_id,
        "rule_version": rule_version,
        "version_digest": strategy_version_digest(Path(root), rule_version),
    }


def _validate_strategy_version_pin(root: Path, packet: Mapping[str, Any], pin: Any) -> None:
    resolution = packet.get("strategy_resolution")
    resolution = resolution if isinstance(resolution, Mapping) else {}
    if pin is None:
        if resolution.get("strategy_id") or resolution.get("rule_version"):
            raise OwnerReconfirmationRequired(
                "strategy_version_pin is empty but the frozen strategy_resolution names a strategy"
            )
        return
    if not isinstance(pin, Mapping) or not all(
        isinstance(pin.get(key), str) and pin[key] for key in ("strategy_id", "rule_version", "version_digest")
    ):
        raise RegistrationPacketError(
            "authority_pins.strategy_version_pin must be null or carry strategy_id, rule_version and version_digest"
        )
    if resolution.get("strategy_id") != pin["strategy_id"] or resolution.get("rule_version") != pin["rule_version"]:
        raise OwnerReconfirmationRequired(
            "strategy_version_pin does not match the frozen strategy_resolution"
        )
    try:
        current = strategy_version_digest(Path(root), pin["rule_version"])
    except (OSError, ValueError) as exc:
        raise OwnerReconfirmationRequired(
            f"pinned strategy rule version is no longer available: {pin['rule_version']}"
        ) from exc
    if current != pin["version_digest"]:
        raise OwnerReconfirmationRequired(
            f"pinned strategy rule version changed since Owner confirmation: {pin['rule_version']}"
        )


# Descriptive fields a later strategy-resolver added to every rule result without a version
# bump. They are the only additions a replay may carry inside a frozen matrix entry.
ADDITIVE_RULE_RESULT_KEYS = frozenset({"supports_fact_timing", "basis"})


def _rule_result_preserved(replay: Any, frozen: Any) -> bool:
    if not isinstance(replay, Mapping) or not isinstance(frozen, Mapping):
        return False
    if set(frozen) - set(replay) or set(replay) - set(frozen) - ADDITIVE_RULE_RESULT_KEYS:
        return False
    return all(_canonical(replay[key]) == _canonical(value) for key, value in frozen.items())


def _matrix_entry_preserved(replay: Any, frozen: Any) -> bool:
    """A frozen matrix entry is reproduced with an identical key set, rule by rule.

    The only tolerated difference is ADDITIVE_RULE_RESULT_KEYS on a rule result. Any other added
    key (for example `blocking`, `overridden` or `superseded_by`), at any depth, is drift.
    """

    if not isinstance(replay, Mapping) or not isinstance(frozen, Mapping) or set(replay) != set(frozen):
        return False
    for key, value in frozen.items():
        if key == "rules":
            replay_rules = replay["rules"]
            if (
                not isinstance(value, list)
                or not isinstance(replay_rules, list)
                or len(replay_rules) != len(value)
                or not all(_rule_result_preserved(r, f) for r, f in zip(replay_rules, value))
            ):
                return False
        elif _canonical(replay[key]) != _canonical(value):
            return False
    return True


def strategy_resolutions_equivalent(replay: Mapping[str, Any], frozen: Mapping[str, Any]) -> bool:
    """The frozen decision must still hold; strategies and descriptive fields added later may appear.

    Every field except the evaluation matrix must be identical. Every strategy entry in the
    frozen matrix must be reproduced with an identical key set; the only tolerated difference is
    a later resolver's ADDITIVE_RULE_RESULT_KEYS on its rule results, and the replay matrix may
    list strategies added to the registry later. If a newer strategy changed the decision
    itself, the top-level fields differ and this is False.
    """

    replay_rest, frozen_rest = dict(replay), dict(frozen)
    replay_matrix, frozen_matrix = replay_rest.pop("matrix", None), frozen_rest.pop("matrix", None)
    if _canonical(replay_rest) != _canonical(frozen_rest):
        return False
    if not isinstance(replay_matrix, list) or not isinstance(frozen_matrix, list):
        return _canonical(replay_matrix) == _canonical(frozen_matrix)

    def key(entry: Any) -> tuple[Any, Any]:
        return (entry.get("strategy_id"), entry.get("rule_version")) if isinstance(entry, Mapping) else (None, None)

    replay_by_key = {key(entry): entry for entry in replay_matrix}
    return all(
        key(entry) in replay_by_key and _matrix_entry_preserved(replay_by_key[key(entry)], entry)
        for entry in frozen_matrix
    )


def _validate_legacy_baseline_version_files(root: Path, packet: Mapping[str, Any]) -> None:
    """A legacy packet pinned only the registry file, never a version file.

    Legacy packets (frozen before strategy_version_pin) exist only for registrations made when the
    four B3-frozen strategies were the active set, and the B3 manifest pins those version files
    byte for byte. Each version such a packet evaluated must therefore be one the manifest pins and
    must still match it; anything else cannot be authenticated and needs reconfirmation. Line
    endings are normalised so a checkout with CRLF conversion does not raise a false alarm.
    """

    manifest_path = Path(root) / "exports" / "method-validation" / "b3" / "manifest.json"
    resolution = packet.get("strategy_resolution")
    matrix = resolution.get("matrix") if isinstance(resolution, Mapping) else None
    if not manifest_path.is_file() or not isinstance(matrix, list):
        return
    expected = {
        item["rule_version"]: item["version_file_sha256"]
        for item in json.loads(manifest_path.read_text(encoding="utf-8")).get("strategy_versions", [])
        if isinstance(item, Mapping) and item.get("rule_version") and item.get("version_file_sha256")
    }
    for entry in matrix:
        version = entry.get("rule_version") if isinstance(entry, Mapping) else None
        if not isinstance(version, str):
            continue
        if version not in expected:
            raise OwnerReconfirmationRequired(
                f"a legacy packet evaluated a rule version the B3 manifest does not pin: {version}"
            )
        content = resolve_version_path(Path(root), version).read_bytes().replace(bytes((13, 10)), bytes((10,)))
        if _sha_bytes(content) != expected[version]:
            raise OwnerReconfirmationRequired(
                f"a frozen B3 rule version file changed since Owner confirmation: {version}"
            )


def _validate_authority_pins(root: Path, packet: Mapping[str, Any]) -> None:
    pins = _require_mapping(packet["authority_pins"], "authority_pins")
    source_main_sha = pins.get("source_main_sha")
    if not isinstance(source_main_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", source_main_sha):
        raise RegistrationPacketError("authority_pins.source_main_sha must be a full commit SHA")

    expected_key = _provider_key(packet["provider_facts"])
    if pins.get("provider_lifecycle_key") != expected_key:
        raise RegistrationPacketError("authority_pins.provider_lifecycle_key does not match provider_facts")

    if pins.get("resolver_version") != RESOLVER_VERSION:
        raise OwnerReconfirmationRequired(
            f"resolver authority drift: packet={pins.get('resolver_version')} current={RESOLVER_VERSION}"
        )

    if "strategy_version_pin" in pins:
        # Current format: only the rule version this registration used is pinned, so adding
        # an unrelated strategy to the registry leaves registered packets valid.
        # strategy_registry_digest is still recorded as provenance but is not enforced.
        _validate_strategy_version_pin(root, packet, pins["strategy_version_pin"])
    else:
        # Packets frozen before strategy_version_pin existed pinned the whole registry.
        # Accept the registry as it was then: unchanged, or equal to the frozen B3 baseline
        # (new strategies, new rule versions and retired versions added since do not matter).
        accepted = {
            _current_registry_digest(root),
            registry_digest(registry_as_of_b3(load_registry(Path(root)))),
        }
        if pins.get("strategy_registry_digest") not in accepted:
            raise OwnerReconfirmationRequired(
                "strategy registry/rule authority changed since Owner confirmation"
            )
        _validate_legacy_baseline_version_files(root, packet)
    resolver_input = packet.get("resolver_input")
    uses_retrospective_authority = isinstance(resolver_input, Mapping) and isinstance(
        resolver_input.get("owner_attestation"), Mapping
    )
    historical_digest = pins.get("retrospective_attribution_registry_digest")
    if uses_retrospective_authority and not isinstance(historical_digest, str):
        raise OwnerReconfirmationRequired(
            "retrospective attribution authority is not pinned in this packet"
        )
    if historical_digest is not None and historical_digest != _current_retrospective_attribution_digest(root):
        raise OwnerReconfirmationRequired(
            "retrospective attribution registry changed since Owner confirmation"
        )


def _validate_resolver_replay(root: Path, packet: Mapping[str, Any]) -> dict[str, Any]:
    _validate_retrospective_evidence_bindings(packet)
    facts = packet.get("provider_facts") if isinstance(packet.get("provider_facts"), Mapping) else {}
    frozen = dict(packet["strategy_resolution"])
    # Replay exactly the rule versions the frozen decision evaluated, so versions added or
    # stopped since (a new v2, a retired v1) cannot change the outcome being verified.
    frozen_matrix = frozen.get("matrix")
    frozen_versions = (
        {entry["rule_version"] for entry in frozen_matrix if isinstance(entry, Mapping) and entry.get("rule_version")}
        if isinstance(frozen_matrix, list)
        else set()
    )
    replay = resolve_strategy(
        Path(root),
        packet["resolver_input"],
        provider_entry_at=facts.get("entry_time"),
        provider_execution_verified=True,
        only_versions=frozen_versions or None,
    )
    if not strategy_resolutions_equivalent(replay, frozen):
        raise OwnerReconfirmationRequired(
            "deterministic resolver replay differs from the frozen strategy_resolution"
        )
    return replay


def _validate_retrospective_evidence_bindings(
    packet: Mapping[str, Any],
    *,
    require_same_agent_locator: bool = True,
) -> None:
    """Keep replay provenance truthful while allowing it into retrospective adjudication."""

    resolver_input = packet.get("resolver_input")
    if not isinstance(resolver_input, Mapping) or not isinstance(resolver_input.get("owner_attestation"), Mapping):
        return
    by_ref: dict[str, Mapping[str, Any]] = {}
    for item in _validate_image_evidence_schema(
        packet,
        require_same_agent_locator=require_same_agent_locator,
    ):
        locator = item.get("locator")
        if isinstance(locator, str):
            by_ref[locator] = item
        sha = item.get("sha256")
        if isinstance(sha, str):
            by_ref[f"artifact:{sha}"] = item
            # EvidenceStore locators include their real extension.
            for candidate in (".png", ".jpg", ".jpeg", ".webp"):
                by_ref[f"artifact:{sha}{candidate}"] = item

    facts = packet.get("provider_facts") if isinstance(packet.get("provider_facts"), Mapping) else {}

    def require_truthful_binding(ref: Any, timing: str | None = None, observed_at: str | None = None) -> None:
        if not isinstance(ref, str) or not ref.strip():
            raise RegistrationPacketError("retrospective evidence refs must be non-empty strings")
        if ref.startswith("owner:"):
            if timing is not None:
                raise RegistrationPacketError(
                    "retrospective visual observations must cite attached evidence, not owner-attestation refs"
                )
            return  # The attestation text itself is frozen inline in the packet.
        item = by_ref.get(ref)
        if item is None:
            raise RegistrationPacketError(f"retrospective evidence ref is absent from image_evidence: {ref}")
        actual_timing = item.get("timing")
        actual_category = item.get("category")
        expected_category = {
            "pre_trade": "pre_trade_context",
            "execution": "entry",
            "post_trade": "review",
        }.get(actual_timing)
        if timing is not None and timing != actual_timing:
            raise RegistrationPacketError(f"retrospective observation timing conflicts with image provenance: {ref}")
        if expected_category is not None and actual_category != expected_category:
            raise RegistrationPacketError(f"image category/timing provenance is inconsistent: {ref}")
        try:
            normalized_time = normalize_evidence_time_metadata(item)
            item.update(normalized_time)
        except EvidenceTimeError as exc:
            raise RegistrationPacketError(f"image evidence timing metadata is invalid for {ref}: {exc}") from exc
        if observed_at is not None:
            in_window = represented_market_time_contains(item, observed_at)
            if in_window is False:
                raise RegistrationPacketError(
                    f"observed chart fact time {observed_at} is outside represented_market_time for {ref}"
                )

    owner = resolver_input["owner_attestation"]
    for ref in owner.get("evidence_refs", []):
        require_truthful_binding(ref)
    for observation in resolver_input.get("retrospective_observations", []):
        if not isinstance(observation, Mapping):
            raise RegistrationPacketError("retrospective observation must be an object")
        for ref in observation.get("evidence_refs", []):
            require_truthful_binding(ref, observation.get("timing"), observation.get("observed_at"))


def _load_provider_lifecycle(root: Path, provider: str, lifecycle_id: str) -> Mapping[str, Any]:
    provider = canonical_provider(provider)
    path = Path(root) / "provider-ledgers" / provider / "normalized" / "trade-lifecycles.jsonl"
    if not path.exists():
        raise RegistrationPacketError(f"provider ledger not found: {path}")
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RegistrationPacketError(f"invalid provider ledger JSON at line {number}") from exc
        if str(item.get("open_id") or "") == lifecycle_id:
            lifecycle = item.get("lifecycle")
            if not isinstance(lifecycle, Mapping):
                raise RegistrationPacketError("provider lifecycle row is malformed")
            return lifecycle
    raise RegistrationPacketError(f"provider lifecycle not found in committed ledger: {provider}:{lifecycle_id}")


def _same_provider_value(key: str, frozen: Any, current: Any) -> bool:
    if key == "is_closed":
        return bool(frozen) == bool(current)
    if frozen is None and current is None:
        return True
    return str(frozen) == str(current)


def _current_provider_fact(key: str, provider: str, lifecycle: Mapping[str, Any]) -> Any:
    """Read the ledger's current value for a frozen provider fact.

    Lifecycle payloads carry their identity as ``open_id`` (not
    ``provider_lifecycle_id``) and do not repeat the provider, which the ledger
    path binds. Compare the payload's own ``open_id`` so a row filed under the
    right key but holding another lifecycle is still rejected; if a payload does
    carry ``provider``, it must match too.
    """

    if key == "provider_lifecycle_id":
        return lifecycle.get("open_id")
    if key == "provider":
        return lifecycle.get("provider", provider)
    return lifecycle.get(key)


def _validate_provider_facts(root: Path, packet: Mapping[str, Any]) -> Mapping[str, Any]:
    facts = packet["provider_facts"]
    _validate_provider_facts_schema(facts)
    is_closed = facts["is_closed"]

    provider = canonical_provider(str(facts["provider"]))
    lifecycle_id = str(facts["provider_lifecycle_id"])
    lifecycle = _load_provider_lifecycle(root, provider, lifecycle_id)
    if lifecycle.get("normalization_status") != "ok":
        raise OwnerReconfirmationRequired("provider lifecycle is no longer normalization_status=ok")

    fields = CLOSED_PROVIDER_FACT_FIELDS if is_closed else BASE_PROVIDER_FACT_FIELDS
    for key in fields:
        if not _same_provider_value(key, facts.get(key), _current_provider_fact(key, provider, lifecycle)):
            raise OwnerReconfirmationRequired(
                f"provider fact changed since Owner confirmation: {key}"
            )
    return lifecycle


def _normalized_instrument(value: Any) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    for quote in ("USDT", "USDC", "USD"):
        if normalized.endswith(quote) and len(normalized) > len(quote):
            return normalized[: -len(quote)]
    return normalized


def validate_existing_registration_binding(
    store: LifecycleStore,
    packet: Mapping[str, Any],
    state: Mapping[str, Any],
) -> None:
    """Prove REGISTERED/IN_PROGRESS state belongs to this exact frozen packet."""

    if state.get("status") not in {"REGISTERED", "IN_PROGRESS"}:
        return
    packet_hash = str(packet.get("packet_sha256") or "").lower()
    bound_hash = state.get("packet_sha256")
    if bound_hash != packet_hash:
        if bound_hash is None:
            raise RegistrationPacketError(
                "existing provider lifecycle has no durable registration_packet_sha256; "
                "cannot prove frozen-packet retry identity"
            )
        raise RegistrationPacketError(
            "existing provider lifecycle is bound to a different frozen Registration Packet"
        )

    record = store._projected_record(
        str(state.get("trade_id") or state.get("draft_id") or "")
    )
    if not isinstance(record, Mapping):
        raise RegistrationPacketError(
            "existing registration state cannot be read back for packet compatibility"
        )
    facts = packet["provider_facts"]
    if _normalized_instrument(record.get("symbol")) != _normalized_instrument(facts["instrument"]):
        raise RegistrationPacketError(
            "existing canonical instrument conflicts with frozen Registration Packet"
        )
    if str(record.get("direction") or "").lower() != str(facts["direction"]).lower():
        raise RegistrationPacketError(
            "existing canonical direction conflicts with frozen Registration Packet"
        )

    # Build the expectation through the store's own normalizer so it has exactly
    # the persisted shape (schema_version included) and compare strictly.
    expected_resolution = normalize_strategy_resolution(
        to_lifecycle_resolution(
            packet["strategy_resolution"],
            contemporaneous=False,
        )
    )
    existing_resolution = record.get("strategy_resolution")
    if not isinstance(existing_resolution, Mapping) or dict(existing_resolution) != expected_resolution:
        raise RegistrationPacketError(
            "existing canonical strategy_resolution conflicts with frozen Registration Packet"
        )


def _manifest_artifacts(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    artifacts: list[Mapping[str, Any]] = []
    top = manifest.get("artifacts")
    if isinstance(top, list):
        artifacts.extend(a for a in top if isinstance(a, Mapping))
    trades = manifest.get("trades")
    if isinstance(trades, list):
        for trade in trades:
            if isinstance(trade, Mapping) and isinstance(trade.get("artifacts"), list):
                artifacts.extend(a for a in trade["artifacts"] if isinstance(a, Mapping))
    return artifacts


def validate_evidence_package(packet: Mapping[str, Any], manifest_path: Path, evidence_root: Path) -> dict[str, Any]:
    manifest_path = Path(manifest_path)
    evidence_root = Path(evidence_root).resolve()
    expected_manifest_sha = packet.get("evidence_manifest_sha256")
    actual_manifest_sha = manifest_sha256(manifest_path)
    if expected_manifest_sha != actual_manifest_sha:
        raise RegistrationPacketError("evidence manifest SHA256 mismatch")

    manifest = _load_evidence_manifest(manifest_path)
    _validate_external_image_manifest_binding(packet, manifest)

    artifacts = _manifest_artifacts(manifest)
    if not artifacts:
        raise RegistrationPacketError("evidence manifest contains no artifacts")

    checked = []
    for artifact in artifacts:
        rel = artifact.get("zip_path") or artifact.get("path")
        expected_sha = artifact.get("sha256")
        original_filename = artifact.get("original_filename")
        if not isinstance(rel, str) or not rel:
            raise RegistrationPacketError("manifest artifact requires zip_path/path")
        if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha):
            raise RegistrationPacketError("manifest artifact requires sha256")
        if not isinstance(original_filename, str) or not original_filename:
            raise RegistrationPacketError("manifest artifact requires original_filename")
        candidate = (evidence_root / rel).resolve()
        try:
            candidate.relative_to(evidence_root)
        except ValueError as exc:
            raise RegistrationPacketError("evidence artifact path escapes evidence root") from exc
        if not candidate.is_file():
            raise RegistrationPacketError(f"evidence artifact missing: {rel}")
        actual_sha = _sha_bytes(candidate.read_bytes())
        if actual_sha.lower() != expected_sha.lower():
            raise RegistrationPacketError(f"evidence SHA256 mismatch: {rel}")
        if artifact.get("byte_size") is not None and int(artifact["byte_size"]) != candidate.stat().st_size:
            raise RegistrationPacketError(f"evidence byte_size mismatch: {rel}")
        if candidate.name != original_filename:
            raise RegistrationPacketError(f"evidence original_filename mismatch: {rel}")
        checked.append({
            "path": rel,
            "sha256": actual_sha,
            "byte_size": candidate.stat().st_size,
            "original_filename": original_filename,
        })
    return {"manifest_sha256": actual_manifest_sha, "artifact_count": len(checked), "artifacts": checked}


def _prepare_packet_envelope(
    root: Path,
    packet_draft: Mapping[str, Any],
    *,
    transport_mode: str | None,
    evidence_manifest: Path | None,
    require_same_agent_locator: bool,
    timing_sink: dict[str, float] | None,
    derive_strategy_resolution: bool = False,
) -> dict[str, Any]:
    if packet_draft.get("packet_sha256") is not None or packet_draft.get("semantic_state") == "frozen":
        raise RegistrationPacketError("already-frozen packet cannot be re-frozen; use validate/persist")
    effective_transport_mode = transport_mode or packet_draft.get("transport_mode")
    if effective_transport_mode not in TRANSPORT_MODES:
        raise RegistrationPacketError(
            "transport_mode must be one of: " + ", ".join(sorted(TRANSPORT_MODES))
        )
    if effective_transport_mode == "external_handoff" and evidence_manifest is None:
        raise RegistrationPacketError("external_handoff freeze requires an evidence manifest")
    packet = copy.deepcopy(dict(packet_draft))
    if packet.get("transport_mode") not in (None, effective_transport_mode):
        raise RegistrationPacketError("transport_mode conflicts with packet draft")
    packet["transport_mode"] = effective_transport_mode
    _validate_transport_mode(packet)
    packet["schema_version"] = PACKET_SCHEMA_VERSION
    packet["semantic_state"] = "frozen"
    packet["transport_mode"] = effective_transport_mode
    evidence_manifest_digest = None
    if effective_transport_mode == "external_handoff":
        evidence_manifest_digest = manifest_sha256(Path(evidence_manifest))
        supplied_manifest_digest = packet.get("evidence_manifest_sha256")
        if supplied_manifest_digest not in (None, evidence_manifest_digest):
            raise OwnerReconfirmationRequired(
                "OWNER_RECONFIRMATION_REQUIRED: evidence manifest changed after Owner preview"
            )
        packet["evidence_manifest_sha256"] = evidence_manifest_digest
    _validate_owner_confirmation(packet)
    # Owner wording is a write prerequisite, not a late freeze-time check.
    # The executor calls this shared preflight before any same-agent image is
    # ingested, so a legacy v1 envelope cannot leave evidence behind when its
    # registration is rejected for missing verbatim text.
    _owner_verbatim_text(packet)
    _validate_required_sections(
        packet,
        require_same_agent_locator=require_same_agent_locator,
    )

    facts = packet["provider_facts"]
    pins = dict(packet["authority_pins"])
    pins["provider_lifecycle_key"] = _provider_key(facts)
    pins["strategy_registry_digest"] = _current_registry_digest(root)
    pins["strategy_version_pin"] = strategy_version_pin(Path(root), packet["strategy_resolution"])
    pins["resolver_version"] = RESOLVER_VERSION
    if isinstance(packet.get("resolver_input", {}).get("owner_attestation"), Mapping):
        pins["retrospective_attribution_registry_digest"] = _current_retrospective_attribution_digest(root)
    packet["authority_pins"] = pins

    _validate_authority_pins(Path(root), packet)
    phase_started = time.perf_counter()
    _validate_provider_facts(Path(root), packet)
    if timing_sink is not None:
        timing_sink["provider_ledger_accesses"] = timing_sink.get("provider_ledger_accesses", 0) + 1
        timing_sink["provider_seconds"] = timing_sink.get("provider_seconds", 0.0) + time.perf_counter() - phase_started
    _validate_retrospective_evidence_bindings(
        packet,
        require_same_agent_locator=require_same_agent_locator,
    )
    phase_started = time.perf_counter()
    replay = resolve_strategy(
        Path(root),
        packet["resolver_input"],
        provider_entry_at=facts.get("entry_time"),
        provider_execution_verified=True,
    )
    if derive_strategy_resolution and not packet.get("strategy_resolution"):
        packet["strategy_resolution"] = replay
    elif _canonical(replay) != _canonical(packet["strategy_resolution"]):
        raise OwnerReconfirmationRequired(
            "strategy_resolution does not match the frozen resolver_input/current authority"
        )
    # A compact intake arrives with an empty strategy_resolution that is derived just above,
    # so the pin must be taken from the final resolution, exactly as the preview did, before
    # the Owner's confirmation snapshot (which includes the pin) is checked.
    packet["authority_pins"]["strategy_version_pin"] = strategy_version_pin(
        Path(root), packet["strategy_resolution"]
    )
    _validate_confirmation_snapshot(
        packet,
        evidence_manifest_sha256=evidence_manifest_digest,
    )
    if timing_sink is not None:
        timing_sink["resolver_seconds"] = timing_sink.get("resolver_seconds", 0.0) + time.perf_counter() - phase_started

    return packet


def preflight_packet_draft(
    root: Path,
    packet_draft: Mapping[str, Any],
    *,
    transport_mode: str | None = None,
    evidence_manifest: Path | None = None,
    derive_strategy_resolution: bool = False,
    timing_sink: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Validate draft semantics and current authorities before same-agent ingest.

    The formal freeze path requires durable EvidenceStore bytes. This preflight
    runs the same envelope, provider, authority, provenance, and resolver checks
    first while allowing a same-agent locator to be derived from the exact
    supplied source bytes that the executor will ingest next.
    """

    packet = _prepare_packet_envelope(
        root,
        packet_draft,
        transport_mode=transport_mode,
        evidence_manifest=evidence_manifest,
        require_same_agent_locator=False,
        derive_strategy_resolution=derive_strategy_resolution,
        timing_sink=timing_sink,
    )
    return {
        "valid": True,
        "provider_lifecycle_key": packet["authority_pins"]["provider_lifecycle_key"],
        "strategy_status": packet["strategy_resolution"].get("status"),
        "strategy_resolution": packet["strategy_resolution"],
    }


def _map_intake_evidence_refs(value: Any, aliases: Mapping[str, str]) -> Any:
    """Resolve explicit intake image aliases without inventing claim bindings."""

    if isinstance(value, Mapping):
        mapped: dict[str, Any] = {}
        for key, item in value.items():
            if key == "evidence_refs" and isinstance(item, list):
                refs: list[Any] = []
                for ref in item:
                    if isinstance(ref, str) and ref.startswith("image:"):
                        alias = ref.removeprefix("image:")
                        if alias not in aliases:
                            raise RegistrationPacketError(f"intake evidence alias has no image: {ref}")
                        refs.append(aliases[alias])
                    else:
                        refs.append(ref)
                mapped[key] = refs
            else:
                mapped[key] = _map_intake_evidence_refs(item, aliases)
        return mapped
    if isinstance(value, list):
        return [_map_intake_evidence_refs(item, aliases) for item in value]
    return copy.deepcopy(value)


def build_registration_intake(
    root: Path,
    intake: Mapping[str, Any],
    *,
    image_base: Path | None = None,
    timing_sink: dict[str, float] | None = None,
) -> tuple[dict[str, Any], dict[str, Path]]:
    """Derive deterministic packet plumbing from compact semantics and raw images.

    Visual interpretation and historical Owner judgement remain inputs. This
    function reads Provider execution facts and exact image bytes, then prepares
    the same Registration Packet draft consumed by the existing preview and
    Executor paths.
    """

    if intake.get("schema") != "registration-intake.v1":
        raise RegistrationPacketError("intake.schema must equal registration-intake.v1")
    allowed_fields = {
        "schema", "provider", "provider_lifecycle_id", "transport_mode", "canonical_capture",
        "owner_recollection", "owner_judgment", "agent_observation", "agent_assessment",
        "agreed_facts", "unresolved_facts", "resolver_input", "images",
    }
    unexpected = sorted(set(intake) - allowed_fields)
    if unexpected:
        raise RegistrationPacketError("registration-intake.v1 has unsupported fields: " + ", ".join(unexpected))
    provider = intake.get("provider")
    lifecycle_id = intake.get("provider_lifecycle_id")
    if not isinstance(provider, str) or not provider.strip():
        raise RegistrationPacketError("intake.provider is required")
    if not isinstance(lifecycle_id, str) or not lifecycle_id.strip():
        raise RegistrationPacketError("intake.provider_lifecycle_id is required")
    if intake.get("transport_mode", "same_agent") != "same_agent":
        raise RegistrationPacketError("registration-intake.v1 supports same_agent raw-image intake only")

    phase_started = time.perf_counter()
    lifecycle = _load_provider_lifecycle(Path(root), provider, lifecycle_id)
    if timing_sink is not None:
        timing_sink["provider_ledger_accesses"] = timing_sink.get("provider_ledger_accesses", 0) + 1
    if lifecycle.get("normalization_status") != "ok":
        raise RegistrationPacketError("Provider lifecycle normalization_status must equal ok")
    is_closed = bool(lifecycle.get("is_closed"))
    provider_facts: dict[str, Any] = {
        "provider": canonical_provider(provider),
        "provider_lifecycle_id": lifecycle_id,
        "instrument": lifecycle.get("instrument"),
        "direction": lifecycle.get("direction"),
        "entry_time": lifecycle.get("entry_time"),
        "entry_price": lifecycle.get("entry_price"),
        "is_closed": is_closed,
    }
    if is_closed:
        for key in ("exit_time", "weighted_exit_price", "net_profit_sum"):
            provider_facts[key] = lifecycle.get(key)
    _validate_provider_facts_schema(provider_facts)
    if timing_sink is not None:
        timing_sink["provider_facts_seconds"] = timing_sink.get("provider_facts_seconds", 0.0) + time.perf_counter() - phase_started

    capture = intake.get("canonical_capture")
    if not isinstance(capture, Mapping):
        raise RegistrationPacketError("intake.canonical_capture must be an object with the Owner-reviewed plan and thesis")
    resolver_input = intake.get("resolver_input", {})
    if not isinstance(resolver_input, Mapping):
        raise RegistrationPacketError("intake.resolver_input must be an object")
    for key in ("owner_recollection", "owner_judgment", "agent_observation", "agent_assessment"):
        if not isinstance(intake.get(key), Mapping):
            raise RegistrationPacketError(f"intake.{key} must be an object; use {{}} when explicitly empty")
    owner_recollection = intake["owner_recollection"]
    if not isinstance(owner_recollection.get("verbatim_text"), str) or not owner_recollection["verbatim_text"].strip():
        raise RegistrationPacketError(
            "intake.owner_recollection.verbatim_text is required; preserve the Owner's exact wording"
        )
    for key in ("agreed_facts", "unresolved_facts"):
        if not isinstance(intake.get(key), list):
            raise RegistrationPacketError(f"intake.{key} must be a list; use [] when explicitly empty")
    images = intake.get("images", [])
    if not isinstance(images, list):
        raise RegistrationPacketError("intake.images must be a list")

    phase_started = time.perf_counter()
    root = Path(root).resolve()
    image_base = Path(image_base or root).resolve()
    aliases: dict[str, str] = {}
    image_evidence: list[dict[str, Any]] = []
    image_files: dict[str, Path] = {}
    for index, raw_image in enumerate(images):
        if not isinstance(raw_image, Mapping):
            raise RegistrationPacketError(f"intake.images[{index}] must be an object")
        image_fields = {
            "alias", "path", "category", "timing", "type", "captured_at", "provenance",
            "represented_market_time", "role", "timing_relative_to_trade",
        }
        unexpected_image_fields = sorted(set(raw_image) - image_fields)
        if unexpected_image_fields:
            raise RegistrationPacketError(
                f"intake.images[{index}] has unsupported fields: " + ", ".join(unexpected_image_fields)
            )
        alias = raw_image.get("alias")
        raw_path = raw_image.get("path")
        if not isinstance(alias, str) or not alias.strip() or alias in aliases:
            raise RegistrationPacketError(f"intake.images[{index}].alias must be unique and non-empty")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise RegistrationPacketError(f"intake.images[{index}].path is required")
        path = Path(raw_path)
        path = path if path.is_absolute() else image_base / path
        if not path.is_file():
            raise RegistrationPacketError(f"source image is missing: {path}")
        filename = path.name
        if Path(filename).suffix.lower() not in IMAGE_EXTENSIONS:
            raise RegistrationPacketError(f"intake image must use a supported image extension: {filename}")
        for required in ("category", "timing", "type"):
            if not isinstance(raw_image.get(required), str) or not raw_image[required].strip():
                raise RegistrationPacketError(f"intake.images[{index}].{required} is required explicitly")
        data = path.read_bytes()
        if timing_sink is not None:
            timing_sink["image_source_reads"] = timing_sink.get("image_source_reads", 0) + 1
        sha = _sha_bytes(data)
        if sha in image_files:
            raise RegistrationPacketError("intake.images contains duplicate source image bytes")
        entry = {
            "sha256": sha,
            "byte_size": len(data),
            "original_filename": filename,
            "category": raw_image["category"],
            "timing": raw_image["timing"],
            "type": raw_image["type"],
        }
        for key in (
            "captured_at",
            "provenance",
            "represented_market_time",
            "role",
            "timing_relative_to_trade",
        ):
            if key in raw_image:
                entry[key] = copy.deepcopy(raw_image[key])
        aliases[alias] = f"artifact:{sha}{Path(filename).suffix.lower()}"
        image_evidence.append(entry)
        image_files[sha] = path
    if timing_sink is not None:
        timing_sink["image_hash_seconds"] = timing_sink.get("image_hash_seconds", 0.0) + time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    packet: dict[str, Any] = {
        "packet_version": 1,
        "transport_mode": "same_agent",
        "provider_facts": provider_facts,
        "image_evidence": image_evidence,
        "agent_observation": _map_intake_evidence_refs(intake.get("agent_observation", {}), aliases),
        "agent_assessment": _map_intake_evidence_refs(intake.get("agent_assessment", {}), aliases),
        "owner_recollection": _map_intake_evidence_refs(intake.get("owner_recollection", {}), aliases),
        "owner_judgment": _map_intake_evidence_refs(intake.get("owner_judgment", {}), aliases),
        "agreed_facts": _map_intake_evidence_refs(intake.get("agreed_facts", []), aliases),
        "unresolved_facts": _map_intake_evidence_refs(intake.get("unresolved_facts", []), aliases),
        "resolver_input": _map_intake_evidence_refs(resolver_input, aliases),
        "strategy_resolution": {},
        "authority_pins": {},
        "canonical_review_projection": {},
        "research_hypotheses": [],
        "canonical_capture": copy.deepcopy(dict(capture)),
    }
    if timing_sink is not None:
        timing_sink["packet_assembly_seconds"] = timing_sink.get("packet_assembly_seconds", 0.0) + time.perf_counter() - phase_started
    return packet, image_files


def prepare_confirmation_preview(
    root: Path,
    packet_draft: Mapping[str, Any],
    *,
    source_main_sha: str,
    evidence_manifest: Path | None = None,
    evidence_root: Path | None = None,
    image_files: Mapping[str, str | Path] | None = None,
    timing_sink: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build a read-only, validated Owner confirmation view from one draft.

    This runs before Owner confirmation. It never freezes or persists a packet and
    never mutates lifecycle/canonical state. Provider facts, current authority,
    resolver output, and exact image bytes are checked through the existing
    project-owned validation paths; execution repeats those checks after the
    Owner confirms.
    """

    started = time.perf_counter()
    if packet_draft.get("packet_sha256") is not None or packet_draft.get("semantic_state") == "frozen":
        raise RegistrationPacketError("confirmation preview accepts an unfrozen draft only")
    confirmation = packet_draft.get("owner_confirmation")
    if confirmation is not None:
        if not isinstance(confirmation, Mapping) or confirmation.get("status") not in {None, "pending"}:
            raise RegistrationPacketError("confirmation preview must run before Owner confirmation")
    version = packet_draft.get("packet_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RegistrationPacketError("packet_version must be a positive integer")
    if not isinstance(source_main_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", source_main_sha):
        raise RegistrationPacketError("source_main_sha must be a full commit SHA")

    packet = copy.deepcopy(dict(packet_draft))
    owner_verbatim_text = _owner_verbatim_text(packet)
    packet.pop("owner_confirmation", None)
    packet.pop("packet_sha256", None)
    packet.pop("semantic_state", None)
    packet["schema_version"] = PACKET_SCHEMA_VERSION
    packet["transport_mode"] = _validate_transport_mode(packet)
    _validate_required_sections(packet, require_same_agent_locator=False)

    facts = packet["provider_facts"]
    provider = canonical_provider(str(facts["provider"]))
    lifecycle_id = str(facts["provider_lifecycle_id"])
    phase_started = time.perf_counter()
    state = registration_state(Path(root), provider, lifecycle_id)
    if timing_sink is not None:
        timing_sink["provider_census_checks"] = timing_sink.get("provider_census_checks", 0) + 1
        timing_sink["registration_state_seconds"] = timing_sink.get("registration_state_seconds", 0.0) + time.perf_counter() - phase_started
    if state.get("status") != "UNREGISTERED":
        raise RegistrationPacketError(
            f"confirmation preview requires an UNREGISTERED lifecycle; found {state.get('status')}"
        )

    pins = dict(packet.get("authority_pins") or {})
    pins.update({
        "provider_lifecycle_key": _provider_key(facts),
        "source_main_sha": source_main_sha.lower(),
        "strategy_registry_digest": _current_registry_digest(Path(root)),
        "resolver_version": RESOLVER_VERSION,
    })
    if isinstance(packet.get("resolver_input", {}).get("owner_attestation"), Mapping):
        pins["retrospective_attribution_registry_digest"] = _current_retrospective_attribution_digest(Path(root))
    packet["authority_pins"] = pins
    _validate_authority_pins(Path(root), packet)

    phase_started = time.perf_counter()
    _validate_provider_facts(Path(root), packet)
    if timing_sink is not None:
        timing_sink["provider_ledger_accesses"] = timing_sink.get("provider_ledger_accesses", 0) + 1
        timing_sink["provider_seconds"] = timing_sink.get("provider_seconds", 0.0) + time.perf_counter() - phase_started

    # Missing original SL is a question for the Owner before numerical R
    # validation. Do not turn missing SL into an opaque BLOCKED receipt or
    # bypass the question with a guessed or automatically owner-attested R.
    # An explicit Owner statement of non-recall (recorded in owner_judgment)
    # is the only exception; the unchanged Writer still validates numeric R.
    require_owner_original_sl_before_preview(packet)

    # Reuse the executor's field/type validator here so an incomplete draft is
    # caught before asking the Owner to confirm the completed preview. This
    # validation is read-only; execution runs it again after confirmation.
    from .registration_executor import _packet_capture, _plan_value

    capture = _packet_capture(packet, is_closed=bool(facts.get("is_closed")))
    for field in ("intended_entry", "stop_loss", "take_profit", "planned_rr"):
        _plan_value(capture[field])

    _validate_retrospective_evidence_bindings(packet, require_same_agent_locator=False)
    phase_started = time.perf_counter()
    resolution = resolve_strategy(
        Path(root),
        packet["resolver_input"],
        provider_entry_at=facts.get("entry_time"),
        provider_execution_verified=True,
    )
    supplied_resolution = packet.get("strategy_resolution")
    if isinstance(supplied_resolution, Mapping) and supplied_resolution and _canonical(resolution) != _canonical(supplied_resolution):
        raise OwnerReconfirmationRequired(
            "strategy_resolution differs from the current mandatory resolver result"
        )
    if timing_sink is not None:
        timing_sink["resolver_calls"] = timing_sink.get("resolver_calls", 0) + 1
        timing_sink["resolver_seconds"] = timing_sink.get("resolver_seconds", 0.0) + time.perf_counter() - phase_started
    packet["strategy_resolution"] = resolution
    pins["strategy_version_pin"] = strategy_version_pin(Path(root), resolution)

    evidence = packet["image_evidence"]
    expected_shas = {str(item["sha256"]).lower() for item in evidence}
    phase_started = time.perf_counter()
    if packet["transport_mode"] == "external_handoff":
        if timing_sink is not None:
            timing_sink["image_source_reads"] = timing_sink.get("image_source_reads", 0) + len(evidence)
        if evidence_manifest is None or evidence_root is None:
            raise RegistrationPacketError("external_handoff confirmation preview requires manifest and evidence root")
        actual_manifest_sha = manifest_sha256(Path(evidence_manifest))
        manifest_packet = dict(packet)
        manifest_packet["evidence_manifest_sha256"] = actual_manifest_sha
        evidence_check = validate_evidence_package(
            manifest_packet,
            Path(evidence_manifest),
            Path(evidence_root),
        )
        evidence_identities = evidence_check["artifacts"]
    else:
        supplied_files = {str(key).lower(): Path(value) for key, value in (image_files or {}).items()}
        if supplied_files:
            if set(supplied_files) != expected_shas:
                raise RegistrationPacketError("image_files SHA set must exactly match packet image_evidence")
            evidence_identities = []
            for item in evidence:
                sha = str(item["sha256"]).lower()
                path = supplied_files[sha]
                if not path.is_file():
                    raise RegistrationPacketError(f"source image is missing: {path}")
                data = path.read_bytes()
                if timing_sink is not None:
                    timing_sink["image_source_reads"] = timing_sink.get("image_source_reads", 0) + 1
                actual_sha = _sha_bytes(data)
                if actual_sha != sha:
                    raise RegistrationPacketError(f"source image SHA256 mismatch: {item['original_filename']}")
                if item.get("byte_size") is not None and int(item["byte_size"]) != len(data):
                    raise RegistrationPacketError(f"source image byte_size mismatch: {item['original_filename']}")
                evidence_identities.append({
                    "sha256": actual_sha,
                    "byte_size": len(data),
                    "original_filename": item["original_filename"],
                })
        elif evidence:
            _validate_same_agent_image_store(Path(root), packet)
            evidence_identities = [
                {
                    "locator": item.get("locator"),
                    "sha256": str(item["sha256"]).lower(),
                    "original_filename": item["original_filename"],
                }
                for item in evidence
            ]
        else:
            evidence_identities = []
        actual_manifest_sha = None
    if packet["transport_mode"] == "external_handoff":
        packet["evidence_manifest_sha256"] = actual_manifest_sha
    else:
        packet.pop("evidence_manifest_sha256", None)
    if timing_sink is not None:
        timing_sink["evidence_bytes_seconds"] = timing_sink.get("evidence_bytes_seconds", 0.0) + time.perf_counter() - phase_started

    digest = confirmation_snapshot_digest(
        packet,
        evidence_manifest_sha256=actual_manifest_sha,
    )
    capture = packet["canonical_capture"]
    summary = {
        "status": "READY_FOR_OWNER_CONFIRMATION",
        "notice": "Read-only preview: no packet was frozen or persisted; explicit Owner confirmation is still required.",
        "owner_confirmation_text": (
            f"{facts['provider']} {facts['instrument']} {facts['direction']} entry "
            f"{facts['entry_time']} @ {facts['entry_price']}; plan "
            f"entry/SL/TP {capture['intended_entry']}/{capture['stop_loss']}/{capture['take_profit']} "
            f"(RR {capture['planned_rr']}); strategy {resolution.get('status')} "
            f"({resolution.get('strategy_id') or 'unresolved'}); thesis: {capture['thesis']}; "
            f"{len(evidence)} image(s) verified. Owner statement (verbatim):\n"
            f"{owner_verbatim_text}\nConfirm digest: {digest}."
        ),
        "preview_digest": digest,
        "provider_facts": dict(facts),
        "registration_state": state,
        "image_evidence": [
            {
                key: item[key]
                for key in (
                    "sha256", "original_filename", "category", "timing", "type",
                    "captured_at", "provenance", "represented_market_time", "role",
                    "timing_relative_to_trade",
                )
                if key in item
            }
            for item in evidence
        ],
        "evidence_bytes_verified": evidence_identities,
        "evidence_manifest_sha256": actual_manifest_sha,
        "agent_observation": packet["agent_observation"],
        "agent_assessment": packet["agent_assessment"],
        "owner_recollection": packet["owner_recollection"],
        "owner_judgment": packet["owner_judgment"],
        "agreed_facts": packet["agreed_facts"],
        "unresolved_facts": packet["unresolved_facts"],
        "strategy_resolution": resolution,
        "canonical_capture": capture,
        "authority_pins": pins,
        "transport_mode": packet["transport_mode"],
    }
    summary["supporting_detail"] = {
        "provider_facts": summary["provider_facts"],
        "image_evidence": summary["image_evidence"],
        "evidence_bytes_verified": summary["evidence_bytes_verified"],
        "resolver_input": packet["resolver_input"],
        "strategy_resolution": summary["strategy_resolution"],
        "canonical_capture": summary["canonical_capture"],
        "owner_recollection": summary["owner_recollection"],
        "owner_judgment": summary["owner_judgment"],
        "agent_observation": summary["agent_observation"],
        "agent_assessment": summary["agent_assessment"],
        "agreed_facts": summary["agreed_facts"],
        "unresolved_facts": summary["unresolved_facts"],
    }
    # Advisory only: never include sync metadata in the Owner semantic digest.
    # It does not assert fresh exchange coverage or run a provider request.
    sync_disclosure = registration_sync_disclosure(Path(root), provider)
    summary.update(sync_disclosure)
    summary["supporting_detail"].update(sync_disclosure)
    summary["preconfirmation_seconds"] = time.perf_counter() - started
    return summary


def freeze_packet(
    root: Path,
    packet_draft: Mapping[str, Any],
    *,
    transport_mode: str | None = None,
    evidence_manifest: Path | None = None,
    timing_sink: dict[str, float] | None = None,
) -> dict[str, Any]:
    _owner_verbatim_text(packet_draft)
    packet = _prepare_packet_envelope(
        root,
        packet_draft,
        transport_mode=transport_mode,
        evidence_manifest=evidence_manifest,
        require_same_agent_locator=True,
        timing_sink=timing_sink,
    )

    phase_started = time.perf_counter()
    if packet["transport_mode"] == "same_agent":
        _validate_same_agent_image_store(Path(root), packet)
    else:
        manifest = _load_evidence_manifest(Path(evidence_manifest))
        _validate_external_image_manifest_binding(packet, manifest)
    if timing_sink is not None:
        timing_sink["evidence_seconds"] = timing_sink.get("evidence_seconds", 0.0) + time.perf_counter() - phase_started

    if packet["transport_mode"] == "external_handoff" and evidence_manifest is None:
        raise RegistrationPacketError("external_handoff freeze requires evidence manifest")
    if evidence_manifest is not None:
        packet["evidence_manifest_sha256"] = manifest_sha256(Path(evidence_manifest))

    packet["packet_sha256"] = packet_sha256(packet)
    return packet


def validate_frozen_packet(
    root: Path,
    packet: Mapping[str, Any],
    *,
    evidence_manifest: Path | None = None,
    evidence_root: Path | None = None,
    timing_sink: dict[str, float] | None = None,
) -> dict[str, Any]:
    if packet.get("schema_version") not in {LEGACY_PACKET_SCHEMA_VERSION, PACKET_SCHEMA_VERSION}:
        raise RegistrationPacketError(f"schema_version must equal {PACKET_SCHEMA_VERSION}")
    _validate_owner_confirmation(packet)
    _validate_required_sections(packet)

    expected_sha = packet.get("packet_sha256")
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha):
        raise RegistrationPacketError("packet_sha256 is required")
    actual_sha = packet_sha256(packet)
    if actual_sha.lower() != expected_sha.lower():
        raise RegistrationPacketError("Registration Packet SHA256 mismatch")

    _validate_authority_pins(root, packet)
    phase_started = time.perf_counter()
    lifecycle = _validate_provider_facts(root, packet)
    if timing_sink is not None:
        timing_sink["provider_seconds"] = timing_sink.get("provider_seconds", 0.0) + time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    replay = _validate_resolver_replay(root, packet)
    if timing_sink is not None:
        timing_sink["resolver_seconds"] = timing_sink.get("resolver_seconds", 0.0) + time.perf_counter() - phase_started
    _validate_confirmation_snapshot(
        packet,
        evidence_manifest_sha256=(
            str(packet.get("evidence_manifest_sha256"))
            if packet.get("transport_mode") == "external_handoff"
            else None
        ),
    )

    phase_started = time.perf_counter()
    evidence = None
    if packet.get("transport_mode") == "same_agent":
        _validate_same_agent_image_store(Path(root), packet)
    require_evidence_package = packet.get("transport_mode") == "external_handoff"
    if require_evidence_package and (evidence_manifest is None or evidence_root is None):
        raise RegistrationPacketError("external_handoff packet requires both evidence manifest and evidence_root")
    if evidence_manifest is not None or evidence_root is not None:
        if evidence_manifest is None or evidence_root is None:
            raise RegistrationPacketError("evidence validation requires both manifest and evidence_root")
        evidence = validate_evidence_package(packet, Path(evidence_manifest), Path(evidence_root))
    if timing_sink is not None:
        timing_sink["evidence_seconds"] = timing_sink.get("evidence_seconds", 0.0) + time.perf_counter() - phase_started

    facts = packet["provider_facts"]
    state = registration_state(
        Path(root),
        str(facts["provider"]),
        str(facts["provider_lifecycle_id"]),
    )
    return {
        "valid": True,
        "packet_sha256": actual_sha,
        "provider_lifecycle_key": _provider_key(facts),
        "resolver_status": replay["status"],
        "registration_state": state,
        "provider_is_closed": bool(lifecycle.get("is_closed")),
        "evidence": evidence,
    }


def packet_path(root: Path, packet_hash: str) -> Path:
    return Path(root) / PACKET_DIR / f"{packet_hash}.json"


def require_persisted_frozen_packet(root: Path, packet: Mapping[str, Any]) -> Path:
    """Require exact packet read-back before any resolved canonical trade mutation."""

    packet_hash = str(packet.get("packet_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", packet_hash):
        raise RegistrationPacketError("packet_sha256 is required before canonical capture")
    target = packet_path(root, packet_hash)
    if not target.exists():
        raise RegistrationPacketError(
            "frozen Registration Packet must be durably persisted before canonical capture"
        )
    try:
        persisted = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistrationPacketError("persisted Registration Packet is unreadable") from exc
    if not isinstance(persisted, Mapping) or _canonical(persisted) != _canonical(packet):
        raise RegistrationPacketError(
            "persisted Registration Packet does not match the validated frozen packet"
        )
    return target


def persist_frozen_packet(
    store: LifecycleStore,
    packet: Mapping[str, Any],
    *,
    evidence_manifest: Path | None = None,
    evidence_root: Path | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    validation = validate_frozen_packet(
        store.root,
        packet,
        evidence_manifest=evidence_manifest,
        evidence_root=evidence_root,
    )
    state = validation["registration_state"]
    if (
        state["status"] == "UNREGISTERED"
        and packet.get("schema_version") == LEGACY_PACKET_SCHEMA_VERSION
    ):
        raise OwnerReconfirmationRequired(
            "OWNER_RECONFIRMATION_REQUIRED: legacy v1 packets remain readable, but a new registration requires a v2 preview_digest"
        )
    if state["status"] == "UNREGISTERED" and "strategy_version_pin" not in (packet.get("authority_pins") or {}):
        raise OwnerReconfirmationRequired(
            "OWNER_RECONFIRMATION_REQUIRED: a new registration must pin authority_pins.strategy_version_pin; "
            "a packet without it is only valid for a lifecycle that is already registered"
        )
    if state["status"] in {"REGISTERED", "IN_PROGRESS"}:
        validate_existing_registration_binding(store, packet, state)
        if state["status"] == "REGISTERED":
            return {
                "status": "ALREADY_REGISTERED",
                "packet_sha256": validation["packet_sha256"],
                "trade_no": state.get("trade_no"),
                "trade_id": state.get("trade_id"),
                "draft_id": state.get("draft_id"),
            }
        return {
            "status": "REGISTRATION_IN_PROGRESS",
            "packet_sha256": validation["packet_sha256"],
            "draft_id": state.get("draft_id"),
            "detail": state.get("detail"),
        }
    if state["status"] == "AMBIGUOUS":
        raise LifecycleError(
            f"provider lifecycle cannot accept a frozen packet in state {state['status']}: {state.get('detail')}"
        )

    _owner_verbatim_text(packet)

    frozen = copy.deepcopy(dict(packet))
    target = packet_path(store.root, validation["packet_sha256"])
    serialized = _canonical(frozen) + "\n"
    with store._locked():
        if target.exists():
            if target.read_text(encoding="utf-8") != serialized:
                raise LifecycleError("immutable Registration Packet path collision")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(serialized, encoding="utf-8", newline="\n")

    result = packet["strategy_resolution"]
    if result["status"] not in ALLOWED_RESOLVER_STATUSES:
        raise RegistrationPacketError(f"unsupported strategy resolution status: {result['status']}")

    return {
        "status": "READY_FOR_RECORD_RECONCILIATION",
        "packet_sha256": validation["packet_sha256"],
        "packet_path": str(target.relative_to(store.root)),
        "strategy_status": result["status"],
        "strategy_attribution": "unresolved" if not result.get("resolved") else "resolved",
    }


def load_packet(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegistrationPacketError("Registration Packet is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RegistrationPacketError("Registration Packet must be an object")
    return payload
