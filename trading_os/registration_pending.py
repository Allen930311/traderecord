"""Legacy durable STRATEGY_PENDING registry, keyed by provider lifecycle.

Before the registration-state repair, AMBIGUOUS or INSUFFICIENT_EVIDENCE created a
pending row instead of a registered LIVE trade. Keep this reader/writer to preserve
existing rows and old callers. New registration flows store unresolved attribution on
the canonical LIVE lifecycle and do not write here. This module can still record old
attempts (append-only JSONL, committed with the repo) so that

* the Registration Census can show the lifecycle as STRATEGY_PENDING instead of a
  brand-new UNREGISTERED one, and
* old "next trade" selection behavior remains stable until the lifecycle is retried.

It is not a canonical trade record and never carries a strategy identity or T-ID.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .operational_lifecycle import LifecycleError, LifecycleStore, canonical_provider

SCHEMA_VERSION = "registration-pending.v1"
PENDING_RELATIVE_PATH = Path("canonical") / "registration" / "pending-strategy.jsonl"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def pending_path(root: Path) -> Path:
    return Path(root) / PENDING_RELATIVE_PATH


def lifecycle_key(provider: str, provider_lifecycle_id: str) -> str:
    return f"{canonical_provider(provider)}:{provider_lifecycle_id}"


def load_pending(root: Path) -> dict[str, dict[str, Any]]:
    """Latest pending record per provider lifecycle key (file order = recorded order)."""

    path = pending_path(root)
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LifecycleError(f"invalid pending-strategy record at line {number}") from exc
        if record.get("schema_version") != SCHEMA_VERSION or "provider_lifecycle_key" not in record:
            raise LifecycleError(f"invalid pending-strategy record schema at line {number}")
        latest[record["provider_lifecycle_key"]] = record
    return latest


def record_pending(
    store: LifecycleStore,
    *,
    provider: str,
    provider_lifecycle_id: str,
    instrument: str,
    direction: str,
    provider_execution_timestamp: str,
    result: Mapping[str, Any],
    observations: Mapping[str, Any],
    recorded_at: str | None = None,
    packet_sha256: str | None = None,
) -> dict[str, Any]:
    """Append a pending record unless the latest one for this lifecycle already says the same thing."""

    if result.get("resolved"):
        raise LifecycleError("only an unresolved strategy result can be recorded as pending")
    key = lifecycle_key(provider, provider_lifecycle_id)
    observations_sha = _sha(observations)
    undecided = {
        m["strategy_id"]: [r["rule_id"] for r in m["rules"] if r["state"] == "UNKNOWN"]
        for m in result.get("matrix", [])
        if m.get("result") == "INSUFFICIENT"
    }
    if packet_sha256 is not None:
        if not isinstance(packet_sha256, str) or len(packet_sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in packet_sha256):
            raise LifecycleError("packet_sha256 must be a 64-character hex digest")

    core = {
        "schema_version": SCHEMA_VERSION,
        "provider_lifecycle_key": key,
        "provider": canonical_provider(provider),
        "provider_lifecycle_id": provider_lifecycle_id,
        "instrument": instrument,
        "direction": direction,
        "provider_execution_timestamp": provider_execution_timestamp,
        "resolver_version": result.get("resolver_version"),
        "resolver_status": result.get("status"),
        "reason": result.get("reason"),
        "strategy_results": {m["strategy_id"]: m["result"] for m in result.get("matrix", [])},
        "undecided_rules": undecided,
        "observations_sha256": observations_sha,
    }
    if packet_sha256 is not None:
        core["packet_sha256"] = packet_sha256.lower()
    path = pending_path(store.root)
    with store._locked():
        latest = load_pending(store.root).get(key)
        if latest and all(latest.get(k) == v for k, v in core.items()):
            return latest
        attempt = (latest.get("attempt", 0) + 1) if latest else 1
        record = {**core, "attempt": attempt, "recorded_at": recorded_at or _now()}
        record["digest"] = _sha(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical(record) + "\n")
    return record


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
