"""Publish one approved BACKTEST registration through the LIVE publication path.

The LIVE executor (``registration_executor``) writes in an isolated worktree from
``origin/main``, runs the registration test lane, commits once, pushes to
``origin/main`` and reads the result back from GitHub. This module applies the same
sequence to a BACKTEST sample. The canonical writer is still
``backtest_registration.commit_backtest_registration``; only the publication
around it is shared with LIVE.

Guarantees:
* an intake ``exit`` block registers, closes and counts the sample in this one publish (read back from origin/main);
* the sample is written only after the Owner-approved preview digest matches;
* an identical retry (ALREADY_REGISTERED) pushes nothing and only verifies origin;
* origin/main must not move during the run, otherwise nothing is pushed;
* the isolated worktree is removed on success.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from .backtest_registration import BacktestRegistrationError, commit_backtest_registration
from .projections import verify_projections
from .registration_executor import (
    RegistrationExecutorError,
    _call_cli,
    _git,
    _git_bytes,
    _prepare_checkout,
    _sha,
)


def _intake_image_refs(intake: Mapping[str, Any]) -> list[str]:
    refs = [str(item.get("ref", "")) for item in intake.get("images", []) if isinstance(item, Mapping)]
    if not refs or any(not ref.startswith("registration-intake/") for ref in refs):
        raise BacktestRegistrationError("publish needs repository intake images under registration-intake/")
    return refs


def publish_backtest_registration(
    source_root: Path,
    intake: Mapping[str, Any],
    *,
    preview_digest: str,
    approval: Mapping[str, Any],
) -> dict[str, Any]:
    """Commit, test, push and read back one BACKTEST sample on origin/main."""
    source_root = Path(source_root).resolve()
    _intake_image_refs(intake)  # validates that every image ref is repository intake
    # Same checkout policy as LIVE: reuse a clean main checkout at origin/main, else isolate.
    execution_root, owned_worktree, checkout = _prepare_checkout(source_root)
    try:
        timings = {"write": 0.0, "test_lane": 0.0, "commit_push": 0.0, "readback": 0.0}
        # The test-lane base must be captured while the worktree is still clean.
        run_receipt = _call_cli(execution_root, "registration-test-lane", "begin")
        run_id = str(run_receipt["run_id"])
        # Intake originals are read from the source checkout only. They are never copied
        # into the isolated worktree, so the commit carries canonical evidence and records
        # only (the focused test lane), not the registration-intake/ files.
        started = time.monotonic()
        result = commit_backtest_registration(
            execution_root,
            intake,
            preview_digest=preview_digest,
            approval=approval,
            image_base=source_root,
        )
        timings["write"] = time.monotonic() - started
        mutated = bool(_git(execution_root, "status", "--porcelain=v1", "--untracked-files=all"))
        commit_sha = _sha(execution_root, "HEAD")
        test_receipt: dict[str, Any] | None = None
        if mutated:
            trade_no = str(result["trade_no"])
            started = time.monotonic()
            test_receipt = _call_cli(execution_root, "registration-test-lane", "run", "--run-id", run_id)
            timings["test_lane"] = time.monotonic() - started
            started = time.monotonic()
            _git(execution_root, "add", "-A")
            _call_cli(execution_root, "registration-test-lane", "verify-tested-diff", "--run-id", run_id)
            _git(execution_root, "commit", "-m", f"Register BACKTEST {trade_no} {intake.get('direction', '')}".strip())
            commit_sha = _sha(execution_root, "HEAD")
            _git(execution_root, "fetch", "--no-tags", "origin", "main")
            if _sha(execution_root, "origin/main") != str(run_receipt["mutation_base_sha"]):
                raise RegistrationExecutorError("origin/main moved during BACKTEST publish; nothing was pushed")
            _call_cli(execution_root, "registration-test-lane", "verify-tested-diff", "--run-id", run_id)
            _git(execution_root, "push", "origin", "HEAD:refs/heads/main")
            timings["commit_push"] = time.monotonic() - started
        started = time.monotonic()
        _git(execution_root, "fetch", "--no-tags", "origin", "main")
        if _sha(execution_root, "origin/main") != commit_sha:
            raise RegistrationExecutorError("origin/main does not match the BACKTEST commit; check origin/main before retrying")
        readback = _remote_backtest_readback(execution_root, result)
        projection = verify_projections(execution_root, target_trade=str(result["trade_no"]))
        if projection.get("status") != "CURRENT":
            raise RegistrationExecutorError(f"projection chain did not verify CURRENT for {result['trade_no']}")
        timings["readback"] = time.monotonic() - started
        return {
            **result,
            "git_commit": commit_sha,
            "pushed": mutated,
            "remote_readback": readback,
            "projection": projection,
            "test_lane": (test_receipt or {}).get("test_lane", "not_required_no_mutation"),
            "checkout": checkout,
            "timings_seconds": timings,
        }
    finally:
        if owned_worktree:
            _git(source_root, "worktree", "remove", "--force", str(owned_worktree))


def _remote_backtest_readback(root: Path, result: Mapping[str, Any]) -> dict[str, Any]:
    """Read the sample back from origin/main, not from this worktree."""
    trade_id = str(result["trade_id"])
    raw = _git_bytes(root, "show", f"origin/main:canonical/records/trades/{trade_id}.json")
    record = json.loads(raw.decode("utf-8"))
    if record.get("trade_no") != result["trade_no"]:
        raise RegistrationExecutorError(
            f"origin/main record {trade_id} has trade_no {record.get('trade_no')!r}, expected {result['trade_no']}"
        )
    if record.get("trade_type") not in (None, "backtest") and record.get("type") not in (None, "backtest"):
        raise RegistrationExecutorError("origin/main read-back is not a BACKTEST sample")
    timing = result.get("replay_timing")
    counted = bool(result.get("result_counted"))
    raw_events = _git_bytes(root, "show", "origin/main:canonical/lifecycle/events.jsonl")
    events = [json.loads(line) for line in raw_events.decode("utf-8").splitlines() if line.strip()]
    if counted and not any(
        event.get("event_type") == "exit_verified" and (event.get("payload") or {}).get("trade_id") == trade_id
        for event in events
    ):
        raise RegistrationExecutorError("origin/main is missing the approved BACKTEST exit verification")
    if timing is not None:
        matched = any(
            event.get("event_type") == "backtest_timing_recorded"
            and (event.get("payload") or {}).get("trade_id") == trade_id
            and (event.get("payload") or {}).get("entry_at") == timing.get("entry_at")
            and (event.get("payload") or {}).get("exit_at") == timing.get("exit_at")
            for event in events
        )
        if not matched:
            raise RegistrationExecutorError("origin/main is missing the approved BACKTEST timing event")
    return {"trade_id": trade_id, "trade_no": record.get("trade_no"), "found_on_origin_main": True,
            "timing_recorded": timing is not None, "exit_verified_on_origin_main": counted}
