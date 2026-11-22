from __future__ import annotations

import json
import hashlib
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import validate_record
from .evidence_store import evidence_identity
from .oslock import os_file_lock
from .identifiers import is_trade_number, is_uuid7_identifier, new_identifier
from scripts.canonical_migration.execution_verification import validate_execution_contract


class CanonicalWriter:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.drafts = self.root / "canonical" / "drafts"
        self.records = self.root / "canonical" / "records"
        self.revisions = self.root / "canonical" / "revisions"
        self.finalization_index = self.root / "canonical" / "sequences" / "finalizations.json"
        self.finalization_lock = self.finalization_index.with_suffix(".lock")

    def _write_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        for _ in range(200):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                time.sleep(0.05)
        raise TimeoutError(f"timed out replacing {path.name}")

    @contextmanager
    def _finalization_locked(self):
        with os_file_lock(self.finalization_lock, "timed out waiting for finalization lock"):
            yield

    def _load_finalization_index(self) -> dict[str, Any]:
        if not self.finalization_index.exists():
            return {"schema_version": "finalization-index.v1", "records": {}}
        payload = json.loads(self.finalization_index.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != "finalization-index.v1" or not isinstance(payload.get("records"), dict):
            raise ValueError("invalid finalization index")
        for record_id, entry in payload["records"].items():
            if not isinstance(record_id, str) or not isinstance(entry, dict):
                raise ValueError("invalid finalization index entry")
            trade_id = entry.get("trade_id")
            trade_no = entry.get("trade_no")
            trade_type = "backtest" if isinstance(trade_no, str) and trade_no.startswith("BT") else "live"
            if not is_uuid7_identifier(trade_id, "trd") or not is_trade_number(trade_no, trade_type):
                raise ValueError("invalid finalization index identity")
        return payload

    def _find_canonical_records(self, record_id: str) -> list[dict[str, Any]]:
        matches = []
        trade_dir = self.records / "trades"
        if not trade_dir.exists():
            return matches
        for path in sorted(trade_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("record_id") == record_id:
                matches.append(payload)
        return matches

    def _existing_finalization(self, record_id: str, index: dict[str, Any]) -> dict[str, Any] | None:
        matches = self._find_canonical_records(record_id)
        if len(matches) > 1:
            raise ValueError("record_id has multiple canonical trade records")
        indexed = index["records"].get(record_id)
        if indexed is not None:
            if not matches:
                raise ValueError("finalization index points to a missing canonical record")
            record = matches[0]
            if indexed.get("trade_id") != record.get("trade_id") or indexed.get("trade_no") != record.get("trade_no"):
                raise ValueError("finalization index disagrees with canonical record")
            return record
        if matches:
            record = matches[0]
            index["records"][record_id] = {
                "trade_id": record.get("trade_id"),
                "trade_no": record.get("trade_no"),
            }
            return record
        return None

    def _durable_revisions(self) -> list[dict[str, Any]]:
        revisions: list[dict[str, Any]] = []
        directory = self.root / "canonical" / "adjudications"
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("revision_id"):
                revisions.append(payload)
        for path in sorted(directory.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("revision_id"):
                    revisions.append(payload)
        return revisions

    def create_candidate(self, record: dict[str, Any]) -> dict[str, Any]:
        errors = validate_record(record)
        if errors:
            return {"draft_id": None, "validation_errors": [e.__dict__ for e in errors]}
        draft_id = record.get("record_id") or new_identifier("draft")
        record = dict(record); record["record_id"] = draft_id
        self._write_atomic(self.drafts / f"{draft_id}.json", record)
        return {"draft_id": draft_id, "validation_errors": []}

    def record_observation(self, record: dict[str, Any]) -> str:
        record = dict(record); record["record_kind"] = "observation"; record["type"] = None; record["trade_id"] = None; record["trade_no"] = None
        observation_id = record.get("observation_id") or new_identifier("obs")
        record["observation_id"] = observation_id
        errors = validate_record(record)
        if errors: raise ValueError("invalid observation: " + "; ".join(e.message for e in errors))
        self._write_atomic(self.records / "observations" / f"{observation_id}.json", record)
        return observation_id

    def attach_evidence(self, draft_id: str, evidence: dict[str, Any]) -> str:
        path = self.drafts / f"{draft_id}.json"
        record = json.loads(path.read_text(encoding="utf-8")); ref = evidence.get("source_ref")
        if not ref or not evidence.get("method") or not evidence.get("timing") or not evidence.get("locator") or not evidence.get("sha256"):
            raise ValueError("evidence requires source_ref, method, timing, locator, and sha256")
        existing_evidence = record.setdefault("evidence", [])
        if not isinstance(existing_evidence, list):
            raise ValueError("draft evidence must be a list")
        candidate_identity = evidence_identity(evidence)
        for existing in existing_evidence:
            if not isinstance(existing, dict):
                continue
            if (
                existing.get("source_ref") == ref
                and existing.get("category") == evidence.get("category")
                and evidence_identity(existing) != candidate_identity
            ):
                raise ValueError(
                    "evidence identity conflict for draft/category/source_ref"
                )
        existing_evidence.append(dict(evidence))
        errors = validate_record(record)
        if errors: raise ValueError("invalid evidence revision: " + "; ".join(e.message for e in errors))
        self._write_atomic(path, record)
        return ref

    def finalize_trade(self, draft_id: str, reviewer: str, allocator) -> str:
        if not reviewer: raise ValueError("reviewer is required")
        path = self.drafts / f"{draft_id}.json"
        with self._finalization_locked():
            record = json.loads(path.read_text(encoding="utf-8"))
            errors = validate_record(record)
            if errors: raise ValueError("invalid draft: " + "; ".join(e.message for e in errors))
            index = self._load_finalization_index()
            existing = self._existing_finalization(record["record_id"], index)
            if existing is not None:
                existing_errors = validate_record(existing)
                if existing_errors:
                    raise ValueError("existing canonical trade is invalid: " + "; ".join(e.message for e in existing_errors))
                self._write_atomic(self.finalization_index, index)
                return existing["trade_id"]
            if record.get("record_kind") != "trade_candidate": raise ValueError("finalization requires a trade candidate draft")
            if record.get("classification_status") != "resolved": raise ValueError("resolved classification required")
            if record.get("dedupe", {}).get("status") != "canonical": raise ValueError("identity/duplicate adjudication required")
            if record.get("type") not in {"live", "backtest"}: raise ValueError("resolved live or backtest type required")
            verification = record.get("actual", {}).get("execution_verification")
            if not isinstance(verification, dict) or verification.get("entry", {}).get("state") != "confirmed": raise ValueError("confirmed execution verification required")
            revisions = self._durable_revisions()
            probe = dict(record)
            probe.update({"record_kind": "trade", "trade_id": "pending-finalization"})
            report = validate_execution_contract(probe, durable_revisions=revisions)
            if not report.ok: raise ValueError("invalid execution verification: " + "; ".join(report.errors))
            trade_id = new_identifier("trd")
            finalized = dict(record)
            finalized.update({"record_kind": "trade", "trade_id": trade_id, "trade_no": "T000000" if record["type"] == "live" else "BT000000", "classification_status": "resolved"})
            errors = validate_record(finalized)
            if errors: raise ValueError("cannot finalize: " + "; ".join(e.message for e in errors))
            trade_no = allocator.allocate(record["type"])
            finalized["trade_no"] = trade_no
            errors = validate_record(finalized)
            if errors: raise ValueError("cannot finalize: " + "; ".join(e.message for e in errors))
            finalized["provenance"] = {**finalized.get("provenance", {}), "finalized_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
            try:
                self._write_atomic(self.records / "trades" / f"{trade_id}.json", finalized)
            except Exception:
                # A number is never left allocated without either its durable
                # record or a complete tombstone explaining the failed write.
                allocator.tombstone(trade_no, "canonical_write_failed", reviewer)
                raise
            index["records"][record["record_id"]] = {"trade_id": trade_id, "trade_no": trade_no}
            self._write_atomic(self.finalization_index, index)
            return trade_id

    def update_revision(self, trade_id: str, patch: dict[str, Any], reviewer: str) -> str:
        raise ValueError(
            "direct canonical revisions are retired; durable truth must use the adjudication authority boundary"
        )

    def apply_review_correction(
        self,
        trade_id: str,
        *,
        review_patch: dict[str, Any],
        strategy_resolution: dict[str, Any],
        correction_id: str,
        reviewer: str,
        recorded_at: str,
    ) -> str:
        """Apply the narrow governed review/provenance correction boundary.

        This is deliberately not a general canonical revision API.  It may
        change only review interpretation and strategy-resolution provenance;
        provider facts, T-ID, identity linkage, execution, and evidence stay
        byte-for-byte represented by the existing canonical record.
        """

        if not trade_id or not correction_id or not reviewer:
            raise ValueError("trade_id, correction_id, and reviewer are required")
        if not isinstance(review_patch, dict) or not isinstance(strategy_resolution, dict):
            raise ValueError("review correction payloads must be objects")
        path = self.records / "trades" / f"{trade_id}.json"
        with self._finalization_locked():
            if not path.exists():
                raise ValueError(f"canonical trade not found: {trade_id}")
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or record.get("trade_id") != trade_id:
                raise ValueError("canonical trade identity mismatch")
            existing_review = record.get("review") if isinstance(record.get("review"), dict) else {}
            updated = dict(record)
            updated["review"] = {**existing_review, **review_patch}
            updated["strategy_resolution"] = dict(strategy_resolution)
            provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
            updated["provenance"] = {
                **provenance,
                "review_correction": {
                    "correction_id": correction_id,
                    "reviewer": reviewer,
                    "recorded_at": recorded_at,
                },
            }
            errors = validate_record(updated)
            if errors:
                raise ValueError("invalid corrected canonical trade: " + "; ".join(error.message for error in errors))
            self._write_atomic(path, updated)
        return trade_id
