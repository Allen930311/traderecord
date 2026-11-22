"""Project-owned projection closure and freshness verification.

This module only composes the existing analytics, health, Registration Census,
Trade View and handoff implementations. It owns the dependency-chain checks so callers do
not have to remember which generated artifact must be refreshed or which
upstream digest each artifact must pin.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from .backtest_handoff import emit_handoff
from .operational_analytics import load_current, provider_fee_index, refresh_analytics
from .provider_sync_status import read_provider_sync_recency
from .registration_census import write_census
from .strategy_health import load_current_health, refresh_health
from .trade_view import build_trade_view, registration_census_read_model
from .operational_analytics import ANALYTICS_GENERATOR


PROJECTION_ORDER = ("analytics", "health", "registration_census", "trade_view", "handoff")


class ProjectionStaleError(ValueError):
    """Raised when a generated projection is stale or incomplete."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _read_json(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionStaleError(f"PROJECTION_STALE: unable to read {relative}") from exc
    if not isinstance(payload, dict):
        raise ProjectionStaleError(f"PROJECTION_STALE: {relative} is not an object")
    return payload


def _unsigned_digest(payload: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "artifact_digest"}
    return _digest(unsigned)


def _verify_payload_digest(label: str, payload: Mapping[str, Any], errors: list[str]) -> None:
    actual = payload.get("artifact_digest")
    if not isinstance(actual, str) or actual != _unsigned_digest(payload):
        errors.append(f"{label}.artifact_digest")


def _verify_manifest_artifact(root: Path, label: str, payload: Mapping[str, Any], manifest: Mapping[str, Any], errors: list[str]) -> None:
    _verify_payload_digest(label, payload, errors)
    artifact = manifest.get("artifact")
    if not isinstance(artifact, Mapping):
        errors.append(f"{label}.manifest.artifact")
        return
    artifact_path = root / str(artifact.get("path") or "")
    if not artifact_path.is_file():
        errors.append(f"{label}.manifest.artifact.path")
        return
    file_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    if artifact.get("sha256") != file_sha:
        errors.append(f"{label}.manifest.artifact.sha256")
    if artifact.get("artifact_digest") != payload.get("artifact_digest"):
        errors.append(f"{label}.manifest.artifact.artifact_digest")


def _verify_trade_view_manifest(root: Path, payload: Mapping[str, Any], manifest: Mapping[str, Any], errors: list[str]) -> None:
    _verify_payload_digest("trade_view", payload, errors)
    data_path = root / str(manifest.get("data_path") or "")
    if not data_path.is_file():
        errors.append("trade_view.manifest.data_path")
        return
    if manifest.get("data_sha256") != hashlib.sha256(data_path.read_bytes()).hexdigest():
        errors.append("trade_view.manifest.data_sha256")
    for path_key, digest_key in (("html_path", "html_sha256"), ("style_path", "style_sha256"), ("app_path", "app_sha256")):
        asset_path = root / str(manifest.get(path_key) or "")
        if not asset_path.is_file():
            errors.append(f"trade_view.manifest.{path_key}")
        elif manifest.get(digest_key) != hashlib.sha256(asset_path.read_bytes()).hexdigest():
            errors.append(f"trade_view.manifest.{digest_key}")
    for key in ("analytics_artifact_digest", "health_artifact_digest"):
        if manifest.get(key) != payload.get("sources", {}).get(key):
            errors.append(f"trade_view.manifest.{key}")
    census = payload.get("registration_census", {})
    if manifest.get("registration_census_source_identity_sha256") != census.get("source_identity_sha256"):
        errors.append("trade_view.manifest.registration_census_source_identity_sha256")
    if manifest.get("registration_census_freshness") != census.get("freshness", {}).get("status"):
        errors.append("trade_view.manifest.registration_census_freshness")
    expected_manifest_digest = _digest({key: value for key, value in manifest.items() if key != "manifest_digest"})
    if manifest.get("manifest_digest") != expected_manifest_digest:
        errors.append("trade_view.manifest.manifest_digest")


def _row_matches(row: Mapping[str, Any], target: str, trade_id: str | None, trade_no: str | None) -> bool:
    identities = {str(value) for value in (target, trade_id, trade_no) if value}
    return any(str(row.get(key)) in identities for key in ("trade_id", "trade_no", "reference", "observation_id"))


def _target_visibility(root: Path, target: str) -> dict[str, Any]:
    canonical_record: dict[str, Any] | None = None
    for path in sorted((root / "canonical" / "records" / "trades").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and target in {str(payload.get("trade_id")), str(payload.get("trade_no"))}:
            canonical_record = payload
            break
    if canonical_record is None:
        return {"canonical_present": False, "lifecycle_present": False, "analytics_present": False, "trade_view_present": False}

    trade_id = str(canonical_record.get("trade_id") or "")
    trade_no = str(canonical_record.get("trade_no") or "")
    lifecycle = _read_json(root, "canonical/lifecycle/current.json")
    lifecycle_record = lifecycle.get("records", {}).get(trade_id)
    analytics = load_current(root)
    analytics_rows = [
        row
        for key in ("canonical_read_model", "observations")
        for row in analytics.get(key, [])
        if isinstance(row, Mapping) and _row_matches(row, target, trade_id, trade_no)
    ]
    trade_view = _read_json(root, "exports/operational/trade-view/trade-view-data.json")
    view_rows = [
        row
        for row in trade_view.get("rows", [])
        if isinstance(row, Mapping) and _row_matches(row, target, trade_id, trade_no)
    ]
    return {
        "canonical_present": True,
        "lifecycle_present": isinstance(lifecycle_record, Mapping),
        "analytics_present": bool(analytics_rows),
        "trade_view_present": bool(view_rows),
        "countability": sorted({str(row.get("counted")) for row in analytics_rows}),
        "trade_id": trade_id,
        "trade_no": trade_no,
    }


def verify_projections(root: Path, *, target_trade: str | None = None, require_census_fresh: bool = False) -> dict[str, Any]:
    """Verify the generated dependency chain and optional target visibility."""

    root = root.resolve()
    errors: list[str] = []
    canonical = _read_json(root, "canonical/lifecycle/current.json")
    try:
        analytics = load_current(root)
        health = load_current_health(root)
    except (OSError, ValueError) as exc:
        raise ProjectionStaleError("PROJECTION_STALE: invalid analytics or health artifact") from exc
    trade_view = _read_json(root, "exports/operational/trade-view/trade-view-data.json")
    handoff = _read_json(root, "exports/operational/handoff/current.json")
    analytics_manifest = _read_json(root, "exports/operational/analytics/manifest.json")
    health_manifest = _read_json(root, "exports/operational/health/manifest.json")
    trade_view_manifest = _read_json(root, "exports/operational/trade-view/manifest.json")
    handoff_manifest = _read_json(root, "exports/operational/handoff/manifest.json")

    canonical_events_sha256 = canonical.get("events_sha256")
    if not isinstance(canonical_events_sha256, str) or not canonical_events_sha256:
        errors.append("canonical.lifecycle.events_sha256")
    if analytics.get("canonical_lifecycle_events_sha256") != canonical_events_sha256:
        errors.append("analytics.canonical_lifecycle_events_sha256")
    canonical_pin = analytics.get("source_version_pins", {}).get("canonical_lifecycle", {})
    if canonical_pin.get("events_sha256") != canonical_events_sha256:
        errors.append("analytics.source_version_pins.canonical_lifecycle.events_sha256")
    if analytics_manifest.get("pins", {}).get("canonical_lifecycle", {}).get("events_sha256") != canonical_events_sha256:
        errors.append("analytics.manifest.pins.canonical_lifecycle.events_sha256")
    # Net provider P&L reads the committed ledger and bills; a new sync makes it stale.
    if analytics.get("source_version_pins", {}).get("provider_fee_sources") != provider_fee_index(root)[1]:
        errors.append("analytics.source_version_pins.provider_fee_sources")

    _verify_manifest_artifact(root, "analytics", analytics, analytics_manifest, errors)
    _verify_manifest_artifact(root, "health", health, health_manifest, errors)
    _verify_trade_view_manifest(root, trade_view, trade_view_manifest, errors)
    _verify_manifest_artifact(root, "handoff", handoff, handoff_manifest, errors)
    # Projections written by an older generator are stale even if every digest chains.
    if analytics.get("source_version_pins", {}).get("analytics_generator") != ANALYTICS_GENERATOR:
        errors.append("analytics.source_version_pins.analytics_generator")

    expected_registration_census = registration_census_read_model(root)
    actual_registration_census = trade_view.get("registration_census")
    if actual_registration_census != expected_registration_census:
        errors.append("trade_view.registration_census.current_source_identity")
    expected_provider_sync_recency = read_provider_sync_recency(root, "propw")
    if trade_view.get("provider_sync_recency") != expected_provider_sync_recency:
        errors.append("trade_view.provider_sync_recency")
    if require_census_fresh and expected_registration_census.get("freshness", {}).get("status") != "fresh":
        errors.append("registration_census.freshness.status")

    analytics_digest = analytics.get("artifact_digest")
    health_digest = health.get("artifact_digest")
    if health.get("analytics_artifact_digest") != analytics_digest:
        errors.append("health.analytics_artifact_digest")
    if health_manifest.get("analytics_artifact_digest") != analytics_digest:
        errors.append("health.manifest.analytics_artifact_digest")
    if trade_view.get("sources", {}).get("analytics_artifact_digest") != analytics_digest:
        errors.append("trade_view.sources.analytics_artifact_digest")
    if trade_view.get("sources", {}).get("health_artifact_digest") != health_digest:
        errors.append("trade_view.sources.health_artifact_digest")
    if handoff.get("source_artifact_digests", {}).get("analytics") != analytics_digest:
        errors.append("handoff.source_artifact_digests.analytics")
    if handoff.get("source_artifact_digests", {}).get("health") != health_digest:
        errors.append("handoff.source_artifact_digests.health")
    if handoff.get("source_version_pins", {}).get("analytics_artifact_digest") != analytics_digest:
        errors.append("handoff.source_version_pins.analytics_artifact_digest")
    if handoff.get("source_version_pins", {}).get("health_artifact_digest") != health_digest:
        errors.append("handoff.source_version_pins.health_artifact_digest")

    target_visibility = None
    if target_trade:
        target_visibility = _target_visibility(root, target_trade)
        for key in ("canonical_present", "lifecycle_present", "analytics_present", "trade_view_present"):
            if target_visibility.get(key) is not True:
                errors.append(f"target_trade.{key}")

    result = {
        "status": "CURRENT" if not errors else "PROJECTION_STALE",
        "errors": errors,
        "canonical_lifecycle_events_sha256": canonical_events_sha256,
        "analytics_artifact_digest": analytics_digest,
        "health_artifact_digest": health_digest,
        "trade_view_analytics_artifact_digest": trade_view.get("sources", {}).get("analytics_artifact_digest"),
        "trade_view_health_artifact_digest": trade_view.get("sources", {}).get("health_artifact_digest"),
        "registration_census_source_identity_sha256": expected_registration_census.get("source_identity_sha256"),
        "registration_census_freshness": expected_registration_census.get("freshness", {}).get("status"),
        "registration_census_candidate_count": expected_registration_census.get("candidate_count", 0),
        "provider_sync_recency": expected_provider_sync_recency,
        "handoff_source_artifact_digests": handoff.get("source_artifact_digests"),
        "target_trade": target_trade,
        "target_visibility": target_visibility,
    }
    if errors:
        raise ProjectionStaleError("PROJECTION_STALE: " + ", ".join(errors))
    return result


def refresh_projections(root: Path, *, generated_at: str | None = None, source_commit: str | None = None, target_trade: str | None = None) -> dict[str, Any]:
    """Refresh and verify analytics → health → Census → Trade View → handoff.

    Census is rebuilt from the committed normalized provider ledger when one
    exists. This keeps canonical registration writes and the main read-back
    freshness contract in the same projection transaction without a provider
    API call.
    """

    root = root.resolve()
    started = time.perf_counter()
    analytics_manifest = refresh_analytics(root, generated_at=generated_at, source_commit=source_commit)
    shared_generated_at = analytics_manifest["generated_at"]
    shared_source_commit = analytics_manifest["source_commit"]
    health_manifest = refresh_health(root, generated_at=shared_generated_at, source_commit=shared_source_commit)
    refresh_order = ["analytics", "health"]
    registration_census_path = root / "provider-ledgers" / "propw" / "normalized" / "trade-lifecycles.jsonl"
    registration_census = None
    if registration_census_path.is_file():
        registration_census = write_census(root, "propw")
        refresh_order.append("registration_census")
    trade_view_manifest = build_trade_view(root)
    refresh_order.append("trade_view")
    handoff_manifest = emit_handoff(root, generated_at=shared_generated_at, source_commit=shared_source_commit)
    refresh_order.append("handoff")
    verification = verify_projections(root, target_trade=target_trade)
    duration_seconds = round(time.perf_counter() - started, 6)
    return {
        "status": verification["status"],
        "duration_seconds": duration_seconds,
        "refresh_order": refresh_order,
        "analytics": analytics_manifest,
        "health": health_manifest,
        "registration_census": registration_census.relative_to(root).as_posix() if registration_census else None,
        "trade_view": trade_view_manifest,
        "handoff": handoff_manifest,
        "verification": verification,
    }
