"""Read non-sensitive PropW sync recency metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "provider-sync-status.v1"
STATUS_PATH = Path("provider-ledgers") / "propw" / "provider-sync-status.json"
SOURCE = "ingestion/propw/data/propw.sqlite3:sync_runs"


def read_provider_sync_recency(root: Path, provider: str = "propw") -> dict[str, Any]:
    """Read the last successful sync record without implying currentness."""

    if provider != "propw":
        return {
            "status": "unknown",
            "last_successful_sync_at": None,
            "source": None,
            "currentness": "not_asserted",
        }
    path = root / STATUS_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = None
    valid = (
        isinstance(payload, dict)
        and payload.get("schema_version") == SCHEMA_VERSION
        and payload.get("provider") == provider
        and payload.get("status") == "recorded"
        and isinstance(payload.get("last_successful_sync_at"), str)
        and bool(payload.get("last_successful_sync_at"))
    )
    return {
        "status": "recorded" if valid else "unknown",
        "last_successful_sync_at": payload["last_successful_sync_at"] if valid else None,
        "source": SOURCE if valid else None,
        "currentness": "not_asserted",
    }


def registration_sync_disclosure(root: Path, provider: str = "propw") -> dict[str, Any]:
    """Non-authoritative freshness disclosure for every LIVE registration preview.

    This must not trigger provider network access or alter the Owner preview digest.
    A fresh committed Census only proves internal source consistency, not coverage
    of executions occurring after the last recorded provider sync.
    """
    return {
        "provider_sync_recency": read_provider_sync_recency(root, provider),
        "provider_sync_guidance": {
            "provider_facts_source": "committed_provider_ledger",
            "live_sync_performed": False,
            "sync_capability": "credentialed_local_agent_only",
            "new_or_missing_trade": "SYNC_REQUIRED_BEFORE_NO_TRADE_CLAIM",
            "note": (
                "Current exchange coverage is not asserted. If the provider lifecycle "
                "is missing, newly executed, or later than known sync coverage, "
                "a credentialed local Agent must perform Provider Sync before "
                "retrying lookup and registration preview."
            ),
        },
    }
