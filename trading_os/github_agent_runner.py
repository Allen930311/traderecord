"""GitHub-only Agent transport for existing registration Writer (no alternate Writer).

The PR-branch acceptance lane runs the unchanged main registration executor
against an isolated Git remote whose 'main' points at the exact PR snapshot.
Only an unchanged-head fast-forward pushes the verified result to the PR branch.
This module does not create canonical content or bypass the standard executor.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .registration_packet import (
    OwnerInputRequired, build_registration_intake, prepare_confirmation_preview,
    packet_path, packet_sha256,
)
from .registration_census import registration_state
from .oslock import WRITE_LOCK_FILES
from .operational_lifecycle import canonical_provider
from .provider_sync_status import registration_sync_disclosure

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "github-agent-registration-request.v1"
ANALYZE_SCHEMA = "github-agent-analysis-request.v1"
REVIEW_SCHEMA = "github-agent-review-request.v1"
PROJECTION_SCHEMA = "github-agent-projection-refresh.v1"
BACKTEST_SCHEMA = "github-agent-backtest-request.v1"
EXPECTED_ACTOR = "example-owner"
INTAKE_REF = "image-intake"
MAIN_REF = "main"
# origin/main SHA merged into the intake checkout for this run (lease base).
_MAIN_BASE: str | None = None


def git(root: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *args], text=True, capture_output=True,
    )
    if process.returncode:
        raise RuntimeError(f"git {args[0]} failed: {process.stderr[-3000:]}")
    return process.stdout.strip()


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _single_new_request() -> Path:
    if os.environ.get("GITHUB_ACTOR") != EXPECTED_ACTOR:
        raise ValueError("GitHub event actor is not the authenticated repository owner")
    if os.environ.get("GITHUB_REF_NAME") != INTAKE_REF:
        raise ValueError("Agent requests are accepted only on the image-intake branch")
    before, after = os.environ["EVENT_BEFORE"], os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", before) or not re.fullmatch(r"[0-9a-f]{40}", after):
        raise ValueError("Malformed immutable GitHub event revisions")
    paths = git(ROOT, "diff", "--name-only", "--diff-filter=A", before, after).splitlines()
    requests = [
        p for p in paths if re.fullmatch(r"agent-requests/[a-zA-Z0-9_-]+\.json", p)
    ]
    changed = git(ROOT, "diff", "--name-only", before, after).splitlines()
    if len(requests) != 1 or changed != requests:
        raise ValueError("Expected one newly added Agent request and no other changed files")
    return ROOT / requests[0]


def _read_request(path: Path) -> dict:
    req = json.loads(path.read_text(encoding="utf-8"))
    if req.get("schema_version") == PROJECTION_SCHEMA:
        if set(req) != {"schema_version", "operation", "expected_events_sha256"}:
            raise ValueError("Invalid read-model refresh request envelope")
        if req["operation"] != "refresh_projections":
            raise ValueError("Unsupported projection operation")
        if not isinstance(req["expected_events_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", req["expected_events_sha256"]
        ):
            raise ValueError("Projection refresh must pin the exact canonical event digest")
        return req
    if req.get("schema_version") == REVIEW_SCHEMA:
        expected = {"schema_version", "operation", "trade_id", "review"}
        if req.get("operation") == "review_commit":
            expected.add("approval")
        if "images" in req:
            expected.add("images")
        if set(req) != expected:
            raise ValueError("Invalid review request envelope")
        if req.get("operation") not in {"review_preview", "review_commit"} or not isinstance(req["review"], dict):
            raise ValueError("Invalid Review request; unsupported mutation")
        if "images" in req and not isinstance(req["images"], list):
            raise ValueError("Review images must be a list of pinned original GitHub paths")
        if not isinstance(req["trade_id"], str) or not re.fullmatch(
            r"trd_[a-zA-Z0-9-]{1,128}", req["trade_id"]
        ):
            raise ValueError("Review requires an exact registered trade identity")
        return req
    if req.get("schema_version") == BACKTEST_SCHEMA:
        expected = {"schema_version", "operation", "backtest_intake"}
        if req.get("operation") == "backtest_commit":
            expected.add("approval")
        if req.get("operation") not in {"backtest_preview", "backtest_commit"} or set(req) != expected:
            raise ValueError("Invalid BACKTEST request envelope")
        if not isinstance(req["backtest_intake"], dict):
            raise ValueError("BACKTEST request needs a backtest-chart-intake.v1 object")
        return req
    if req.get("schema_version") == ANALYZE_SCHEMA:
        if set(req) != {"schema_version", "operation", "analysis_mode", "population", "strategy_id"}:
            raise ValueError("Invalid read-only Analyze request envelope")
        if req["operation"] != "analyze" or req["analysis_mode"] not in {"performance", "health"}:
            raise ValueError("Invalid read-only Analyze operation")
        if req["population"] not in {"live", "paper", "backtest", "all"}:
            raise ValueError("Analyze needs a single explicit population")
        if req["strategy_id"] is not None and not re.fullmatch(
            r"[a-zA-Z0-9_:.-]{1,128}", str(req["strategy_id"])
        ):
            raise ValueError("Invalid selected strategy identity")
        return req
    if set(req) != {"schema_version", "operation", "registration_intake", "approval"}:
        raise ValueError("Invalid Agent request envelope keys")
    if req["schema_version"] != SCHEMA:
        raise ValueError("Agent request schema mismatch")
    if req["operation"] not in ("preview", "commit"):
        raise ValueError("Unsupported Agent operation")
    if not isinstance(req["registration_intake"], dict):
        raise ValueError("Missing compact registration intake")
    return req


def _preview(root: Path, intake: dict) -> dict:
    draft, images = build_registration_intake(root, intake, image_base=root)
    source_sha = git(root, "rev-parse", "HEAD")
    return prepare_confirmation_preview(
        root, draft, source_main_sha=source_sha, image_files=images
    )


def _registration_preview_receipt(result: dict, intake: dict) -> dict:
    """Pass through non-semantic sync recency alongside the preserved preview digest."""
    return {
        "status": result["status"],
        "operation": "preview",
        "intake_sha256": digest(intake),
        "preview_digest": result["preview_digest"],
        "strategy_resolution": result["strategy_resolution"],
        "registration_state": result["registration_state"],
        "provider_facts": result["provider_facts"],
        "image_evidence": result["image_evidence"],
        "canonical_capture": result["canonical_capture"],
        "provider_sync_recency": result["provider_sync_recency"],
        "provider_sync_guidance": result["provider_sync_guidance"],
    }


def _review_intake_manifest(req: dict, root: Path, workdir: Path) -> Path | None:
    """Adapt only hash-pinned, Owner-committed review images to existing Writer."""
    from .operational_lifecycle import LifecycleStore
    imgs = req.get("images") or []
    if not imgs:
        return None
    trade = LifecycleStore(root).current().get("records", {}).get(req["trade_id"]) or {}
    trade_no = trade.get("trade_no")
    if not isinstance(trade_no, str) or not re.fullmatch(r"T[0-9]{6}", trade_no):
        raise ValueError("No registered canonical T-ID for image review")
    manifest_images = []
    seen = set()
    for image in imgs:
        if not isinstance(image, dict) or set(image) != {"path", "sha256", "type"}:
            raise ValueError("Review image needs exact path, SHA-256 and image type")
        path = image["path"]
        sha = image["sha256"]
        if (not isinstance(path, str)
            or not re.fullmatch(r"review-intake/" + re.escape(trade_no) + r"/[^/\\]+\.(?:png|jpg|jpeg|webp)", path, re.IGNORECASE)
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or image["type"] not in {"chart_snapshot", "replay_chart"}):
            raise ValueError("Review image must be a GitHub-original byte path for the selected T-ID")
        if sha in seen:
            raise ValueError("Duplicate review image SHA")
        seen.add(sha)
        original = root / path
        if original.is_symlink() or not original.is_file() or not original.resolve().is_relative_to(root.resolve()):
            raise ValueError("Review original image path missing or outside the repository")
        if hashlib.sha256(original.read_bytes()).hexdigest() != sha:
            raise ValueError("Review image bytes differ from Agent-declared SHA-256")
        manifest_images.append({"path": path, "type": image["type"]})
    result = workdir / "review-image-intake.json"
    result.write_text(json.dumps({
        "schema_version": "trade-review-evidence-intake.v1",
        "images": manifest_images,
    }, ensure_ascii=False), encoding="utf-8")
    return result


def _review_preview(req: dict, *, root: Path = ROOT) -> dict:
    """Preview existing Review service plus exact optional GitHub originals."""
    from .review_session import preview_review
    with tempfile.TemporaryDirectory(prefix="review-github-images-") as tmp:
        manifest = _review_intake_manifest(req, root, Path(tmp))
        preview = preview_review(root, req["trade_id"], req["review"], evidence_intake=manifest)
    return {
        "status": "READY_FOR_OWNER_REVIEW_CONFIRMATION",
        "operation": "review_preview",
        "trade_id": preview["trade_id"],
        "trade_no": preview["trade_no"],
        "source_commit": git(root, "rev-parse", "HEAD"),
        "review_sha256": digest(req["review"]),
        "preview_digest": preview["preview_digest"],
        "strategy_pin": preview["content"]["strategy_pin"],
        "evidence_inventory": preview["content"]["evidence_inventory"],
        "review_candidate": preview["content"]["review_candidate"],
        "target_lifecycle_event_sha256": preview["content"]["target_lifecycle_event_sha256"],
    }

_REVIEW_WRITE_PATHS = {
    "canonical/lifecycle/events.jsonl",
    "canonical/lifecycle/current.json",
    "PERFORMANCE.md",
    "provider-ledgers/propw/registration-census.json",
    "exports/operational/analytics/current.json",
    "exports/operational/analytics/manifest.json",
    "exports/operational/health/current.json",
    "exports/operational/health/manifest.json",
    "exports/operational/trade-view/trade-view-data.json",
    "exports/operational/trade-view/manifest.json",
    "exports/operational/trade-view/app.js",
    "exports/operational/trade-view/index.html",
    "exports/operational/trade-view/style.css",
    "exports/operational/handoff/current.json",
    "exports/operational/handoff/manifest.json",
}


def _review_commit(req: dict):
    """Run only the existing Review service; publish via isolated remote lease."""
    from .review_session import save_confirmed_review
    from .review_preview import prepare_review_evidence_additions
    from .operational_lifecycle import LifecycleStore
    from .projections import verify_projections

    approval = req.get("approval")
    expected = {"preview_request", "preview_digest", "confirmed_in_chat",
                "confirmed_at", "agent_id", "owner_statement"}
    if not isinstance(approval, dict) or set(approval) != expected:
        raise ValueError("Missing exact in-chat Review authorization")
    if approval["confirmed_in_chat"] is not True or not isinstance(approval["owner_statement"], str) or not approval["owner_statement"].strip():
        raise ValueError("No explicit Owner Review decision attested")
    preview_id = approval["preview_request"]
    if not isinstance(preview_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", preview_id):
        raise ValueError("Invalid Review preview reference")
    saved_path = ROOT / "agent-receipts" / (preview_id + ".json")
    if not saved_path.is_file():
        raise ValueError("No saved Review preview")
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    if (saved.get("status") != "READY_FOR_OWNER_REVIEW_CONFIRMATION"
        or saved.get("trade_id") != req["trade_id"]
        or saved.get("review_sha256") != digest(req["review"])
        or saved.get("preview_digest") != approval["preview_digest"]):
        raise ValueError("Review content or digest differs from Owner preview")
    tmp, bare, work = _staging_repository()
    try:
        record = LifecycleStore(work).current().get("records", {}).get(req["trade_id"])
        if not isinstance(record, dict):
            raise ValueError("Missing exact canonical Review trade")
        retry = record.get("state") == "REVIEWED"
        if retry:
            # A retry uses the *original* Owner decision and registered review.
            # The shared Review service independently verifies unchanged content.
            confirmed = (record.get("review") or {}).get("owner_confirmation") or {}
            authorized = (record.get("review") or {}).get("owner_authorization") or {}
            if confirmed.get("preview_digest") != saved["preview_digest"]:
                raise ValueError("Stored Review confirmation differs from Owner preview")
            if confirmed.get("confirmed_at") != approval["confirmed_at"]:
                raise ValueError("Review retry altered the original confirmation time")
            if authorized.get("source") != f"agent:{approval['agent_id']}:owner_confirmed_preview":
                raise ValueError("Review retry altered the original Agent authorization")
            trade_no = record.get("trade_no")
        else:
            checked = _review_preview(req, root=work)
            if checked["preview_digest"] != saved["preview_digest"]:
                raise ValueError("Review evidence or lifecycle drifted; Owner must reconfirm")
            trade_no = checked["trade_no"]
        prior_events = (work / "canonical/lifecycle/events.jsonl").read_bytes()
        prior_numbers = (work / "canonical/sequences/numbers.json").read_bytes()
        # The sole Writer appends Review evidence to the selected canonical
        # draft as well as its append-only lifecycle events. Capture the prior
        # draft so the bridge can authorize *only* this additive mutation.
        draft_id = record.get("draft_id")
        if not isinstance(draft_id, str) or not re.fullmatch(r"rec_[a-zA-Z0-9_-]+", draft_id):
            raise ValueError("Review target has no valid bound canonical draft")
        draft_rel = "canonical/drafts/" + draft_id + ".json"
        draft_path = work / draft_rel
        draft_before = json.loads(draft_path.read_text(encoding="utf-8"))
        draft_original_evidence = draft_before.get("evidence", [])
        if not isinstance(draft_original_evidence, list):
            raise ValueError("Review target draft has invalid evidence")
        agent_approval = {
            "schema_version": "agent-review-approval.v1",
            "decision": "approved",
            "trade_id": req["trade_id"],
            "preview_digest": saved["preview_digest"],
            "confirmed_by": EXPECTED_ACTOR,
            "confirmed_at": approval["confirmed_at"],
            "agent_id": approval["agent_id"],
        }
        with tempfile.TemporaryDirectory(prefix="review-confirmed-images-") as evidence_tmp:
            manifest = _review_intake_manifest(req, work, Path(evidence_tmp))
            additions = prepare_review_evidence_additions(work, manifest)
            expected_artifacts = set()
            for addition in additions:
                artifact_file = addition["locator"].removeprefix("artifact:")
                expected_artifacts.add("canonical/evidence/artifacts/" + artifact_file)
                expected_artifacts.add(
                    "canonical/evidence/artifacts/" + addition["sha256"] + ".metadata.json"
                )
            receipt = save_confirmed_review(
                work, req["trade_id"], req["review"],
                reviewed_at=approval["confirmed_at"],
                shown_preview_digest=saved["preview_digest"],
                agent_approval=agent_approval,
                evidence_intake=manifest,
            )
        expected_status = "ALREADY_REVIEWED" if retry else "REVIEW_COMPLETE"
        if receipt.get("status") != expected_status:
            raise ValueError("Review service returned unexpected Review transition")
        if receipt.get("verify", {}).get("status") != "CURRENT" or verify_projections(work).get("status") != "CURRENT":
            raise ValueError("Review generated stale projections")
        after_events = (work / "canonical/lifecycle/events.jsonl").read_bytes()
        if not after_events.startswith(prior_events):
            raise ValueError("Review rewrote historical lifecycle events")
        appended = [json.loads(row) for row in after_events[len(prior_events):].splitlines()]
        if retry:
            if appended:
                raise ValueError("Identical Review retry appended unexpected lifecycle events")
            attached_events = []
        else:
            if [event.get("event_type") for event in appended[-2:]] != ["review_started", "reviewed"]:
                raise ValueError("Review must finish in exactly one review_started and reviewed pair")
            if any(event.get("payload", {}).get("trade_id") != req["trade_id"] for event in appended[-2:]):
                raise ValueError("Review pair belongs to an unexpected canonical trade")
            attached_events = appended[:-2]
            expected_shas = {item["sha256"] for item in additions}
            if (len(attached_events) > len(expected_shas)
                or any(event.get("event_type") != "evidence_attached"
                       or event.get("payload", {}).get("draft_id") != record.get("draft_id")
                       or (event.get("payload", {}).get("evidence") or {}).get("sha256") not in expected_shas
                       for event in attached_events)):
                raise ValueError("New Review evidence lifecycle events do not match approved original images")
        if (work / "canonical/sequences/numbers.json").read_bytes() != prior_numbers:
            raise ValueError("Review must never allocate or modify trade identities")
        draft_after = json.loads(draft_path.read_text(encoding="utf-8"))
        old_base = {key: value for key, value in draft_before.items() if key != "evidence"}
        new_base = {key: value for key, value in draft_after.items() if key != "evidence"}
        if old_base != new_base:
            raise ValueError("Review Writer changed non-evidence original draft facts")
        new_evidence = draft_after.get("evidence", [])
        if not isinstance(new_evidence, list) or new_evidence[:len(draft_original_evidence)] != draft_original_evidence:
            raise ValueError("Review Writer replaced or reordered original draft evidence")
        appended_draft = new_evidence[len(draft_original_evidence):]
        if retry and appended_draft:
            raise ValueError("Review retry changed the original draft evidence")
        expected_shas = {item["sha256"] for item in additions}
        if len(appended_draft) > len(expected_shas) or any(
            not isinstance(item, dict)
            or item.get("sha256") not in expected_shas
            or item.get("category") != "review"
            for item in appended_draft
        ):
            raise ValueError("Review Writer appended unapproved draft evidence")
        if len(attached_events) != len(appended_draft):
            raise ValueError("Review lifecycle attachments and draft evidence disagree")
        changed = git(work, "diff", "--name-only", "HEAD").splitlines()
        created = _untracked_content_files(work)
        allowed_changed = _REVIEW_WRITE_PATHS | {draft_rel}
        if (not retry and not changed and not created) or any(p not in allowed_changed for p in changed):
            raise ValueError("Review changed paths outside this exact draft: "
                             + ", ".join(sorted(set(changed) - allowed_changed)))
        if any(p not in expected_artifacts for p in created) or (retry and created):
            unexpected = sorted(set(created) - expected_artifacts)
            raise ValueError(
                "Review generated unexpected evidence files or rewrote images on retry: "
                + json.dumps({"unexpected": unexpected, "created": created,
                              "approved": sorted(expected_artifacts), "retry": retry}, ensure_ascii=False)
            )
        # This is a focused review-data write; the branch's code changes get
        # the full regression separately through its normal Project Tests CI.
        tests = subprocess.run(
            [sys.executable, "-m", "pytest", "-q",
             "tests/test_review_session.py", "tests/test_review_preview.py",
             "tests/test_projections.py"], cwd=work, text=True, capture_output=True,
        )
        if tests.returncode:
            raise ValueError("Focused Review regression failed: " + (tests.stdout + tests.stderr)[-2500:])
        if changed or created:
            git(work, "add", "--", *(changed + created))
            git(work, "commit", "-m", "Review " + trade_no + " via existing Review writer")
            git(work, "push", "origin", "HEAD:refs/heads/main")
        staged_head = git(ROOT, "--git-dir", str(bare), "rev-parse", "refs/heads/main")
        if staged_head != git(work, "rev-parse", "HEAD"):
            raise ValueError("Isolated Review Git remote read-back mismatch")
        return tmp, bare, work, {
            "status": "ALREADY_REVIEWED_VERIFIED" if retry else "REVIEW_WRITER_VERIFIED",
            "operation": "review_commit",
            "trade_no": trade_no,
            "trade_id": req["trade_id"],
            "preview_digest": saved["preview_digest"],
            "review_sha256": digest(req["review"]),
            "writer_commit": staged_head,
            "projection_status": "CURRENT",
            "remote_readback": "VERIFIED",
            "appended_review_events": 0 if retry else 1,
            "appended_evidence_events": len(attached_events),
        }
    except BaseException:
        tmp.cleanup()
        raise


def _committed_images(root: Path, intake: dict) -> None:
    """BACKTEST originals must be exact committed GitHub bytes, not local edits."""
    for item in intake.get("images") or []:
        ref = item.get("ref") if isinstance(item, dict) else None
        if not isinstance(ref, str):
            raise ValueError("BACKTEST image needs a repository path")
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", "--", ref],
                                 capture_output=True).returncode == 0
        clean = subprocess.run(["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", ref],
                               capture_output=True).returncode == 0
        if not tracked or not clean:
            raise ValueError("BACKTEST image is not an unmodified committed original: " + ref)


def _backtest_preview(req: dict, *, root: Path | None = None) -> dict:
    from .backtest_registration import preview_backtest_registration
    root = root or ROOT
    _committed_images(root, req["backtest_intake"])
    preview = preview_backtest_registration(root, req["backtest_intake"])
    return {
        "status": {"ALREADY_REGISTERED": "ALREADY_REGISTERED",
                   "POSSIBLE_DUPLICATE": "POSSIBLE_DUPLICATE"}.get(
                       preview["status"], "READY_FOR_OWNER_BACKTEST_CONFIRMATION"),
        "possible_duplicates": preview["possible_duplicates"],
        "existing_plan_matches": preview["existing_plan_matches"],
        "operation": "backtest_preview",
        "source_commit": git(root, "rev-parse", "HEAD"),
        "intake_sha256": digest(req["backtest_intake"]),
        "preview_digest": preview["preview_digest"],
        "expected_trade_no": preview["expected_trade_no"],
        "existing_trade_no": preview["existing_trade_no"],
        "planned_rr_from_chart": preview["planned_rr_from_chart"],
        "images": preview["content"]["images"],
        "strategy_pin": preview["content"]["strategy_pin"],
        "result_counted": False,
    }



def is_write_lock_file(path: str) -> bool:
    """One of the exact OS lock files of the canonical write paths (oslock): never content."""
    return path in WRITE_LOCK_FILES


def _untracked_content_files(work: Path) -> list[str]:
    """Untracked files a Writer created, without its lock files (gitignored in the real repo)."""
    return [p for p in git(work, "ls-files", "--others", "--exclude-standard").splitlines()
            if not is_write_lock_file(p)]


_BACKTEST_CREATED_PREFIXES = (
    "canonical/drafts/", "canonical/records/trades/", "canonical/evidence/artifacts/",
    "canonical/lifecycle/",
)
_BACKTEST_WRITE_PATHS = _REVIEW_WRITE_PATHS | {
    "canonical/sequences/numbers.json", "canonical/sequences/finalizations.json",
}


def _backtest_commit(req: dict):
    """Run only the shared BACKTEST service; publish via isolated remote lease."""
    from .backtest_registration import commit_backtest_registration
    approval = req.get("approval")
    expected = {"preview_request", "preview_digest", "confirmed_in_chat",
                "confirmed_at", "agent_id", "owner_statement"}
    # distinct_from_confirmed: the Owner's explicit "different sample" answer to POSSIBLE_DUPLICATE.
    if not isinstance(approval, dict) or set(approval) - {"distinct_from_confirmed"} != expected:
        raise ValueError("Missing exact in-chat BACKTEST authorization")
    if approval["confirmed_in_chat"] is not True or not str(approval["owner_statement"]).strip():
        raise ValueError("No explicit Owner BACKTEST decision attested")
    preview_id = approval["preview_request"]
    if not isinstance(preview_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", preview_id):
        raise ValueError("Invalid BACKTEST preview reference")
    saved_path = ROOT / "agent-receipts" / (preview_id + ".json")
    if not saved_path.is_file():
        raise ValueError("No saved BACKTEST preview")
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    if (saved.get("operation") != "backtest_preview"
            or saved.get("status") not in {"READY_FOR_OWNER_BACKTEST_CONFIRMATION", "ALREADY_REGISTERED"}
            or saved.get("intake_sha256") != digest(req["backtest_intake"])
            or saved.get("preview_digest") != approval["preview_digest"]):
        raise ValueError("BACKTEST intake or digest differs from Owner preview")
    _committed_images(ROOT, req["backtest_intake"])
    tmp, bare, work = _staging_repository()
    try:
        prior_events_path = work / "canonical/lifecycle/events.jsonl"
        prior_events = prior_events_path.read_bytes() if prior_events_path.exists() else b""
        prior_numbers = json.loads((work / "canonical/sequences/numbers.json").read_text(encoding="utf-8"))
        result = commit_backtest_registration(
            work, req["backtest_intake"], preview_digest=approval["preview_digest"],
            approval={k: approval[k] for k in ("confirmed_in_chat", "confirmed_at", "agent_id", "owner_statement",
                                               "distinct_from_confirmed") if k in approval},
            image_base=work,
        )
        retry = result["status"] == "ALREADY_REGISTERED"
        after_events = prior_events_path.read_bytes() if prior_events_path.exists() else b""
        if not after_events.startswith(prior_events):
            raise ValueError("BACKTEST registration rewrote historical lifecycle events")
        numbers = json.loads((work / "canonical/sequences/numbers.json").read_text(encoding="utf-8"))
        prior_alloc, alloc = prior_numbers.get("allocated", []), numbers.get("allocated", [])
        if alloc[:len(prior_alloc)] != prior_alloc:
            raise ValueError("BACKTEST registration modified existing trade identities")
        added = alloc[len(prior_alloc):]
        if retry:
            if added or after_events != prior_events:
                raise ValueError("Identical BACKTEST retry changed canonical state")
        elif added != [result["trade_no"]] or not result["trade_no"].startswith("BT"):
            raise ValueError("BACKTEST registration must allocate exactly one new BT number")
        changed = git(work, "diff", "--name-only", "HEAD").splitlines()
        created = _untracked_content_files(work)  # lock files are never content and are not published
        if any(p not in _BACKTEST_WRITE_PATHS for p in changed):
            raise ValueError("BACKTEST registration changed paths outside the governed allowlist: "
                             + ", ".join(sorted(set(changed) - _BACKTEST_WRITE_PATHS)))
        if any(not p.startswith(_BACKTEST_CREATED_PREFIXES) for p in created) or (retry and created):
            raise ValueError("BACKTEST registration created unexpected files")
        if changed or created:
            git(work, "add", "--", *(changed + created))
            git(work, "commit", "-m", "Register " + result["trade_no"] + " via shared BACKTEST service")
            git(work, "push", "origin", "HEAD:refs/heads/main")
        staged_head = git(ROOT, "--git-dir", str(bare), "rev-parse", "refs/heads/main")
        if staged_head != git(work, "rev-parse", "HEAD"):
            raise ValueError("Isolated BACKTEST Git remote read-back mismatch")
        return tmp, bare, work, {
            **result,
            "status": "ALREADY_REGISTERED_VERIFIED" if retry else "BACKTEST_REGISTERED_VERIFIED",
            "operation": "backtest_commit",
            "preview_digest": approval["preview_digest"],
            "intake_sha256": digest(req["backtest_intake"]),
            "writer_commit": staged_head,
            "remote_readback": "VERIFIED",
        }
    except BaseException:
        tmp.cleanup()
        raise


def _refresh_read_models(req: dict):
    """Owner-triggered derived-data refresh, with zero canonical/provider mutations."""
    from .projections import refresh_projections, verify_projections
    from .operational_lifecycle import LifecycleStore

    tmp, bare, work = _staging_repository()
    try:
        before_projection = LifecycleStore(work).current()
        before_events_sha = before_projection.get("events_sha256")
        if before_events_sha != req["expected_events_sha256"]:
            raise ValueError("Canonical events changed since Agent requested a projection refresh")
        events_file = work / "canonical/lifecycle/events.jsonl"
        numbers_file = work / "canonical/sequences/numbers.json"
        prior_events = events_file.read_bytes()
        prior_numbers = numbers_file.read_bytes()
        before_view = json.loads((work / "exports/operational/trade-view/trade-view-data.json").read_text(encoding="utf-8"))
        previous_live = (before_view.get("summary_by_mode") or {}).get("live") or {}
        refreshed = refresh_projections(work)
        checked = verify_projections(work, require_census_fresh=True)
        if refreshed.get("status") != "CURRENT" or checked.get("status") != "CURRENT":
            raise ValueError("Derived read-model refresh did not verify CURRENT")
        if events_file.read_bytes() != prior_events or numbers_file.read_bytes() != prior_numbers:
            raise ValueError("Derived-data refresh attempted canonical event or number mutation")
        changes = git(work, "diff", "--name-only", "HEAD").splitlines()
        if not changes or any(path not in _REVIEW_WRITE_PATHS for path in changes):
            raise ValueError("Derived-data refresh changed paths outside the governed projection allowlist")
        after_view = json.loads((work / "exports/operational/trade-view/trade-view-data.json").read_text(encoding="utf-8"))
        updated_live = (after_view.get("summary_by_mode") or {}).get("live") or {}
        if (
            updated_live.get("provider_pnl_observation_count") != previous_live.get("provider_pnl_observation_count")
            or updated_live.get("verified_provider_pnl") != previous_live.get("verified_provider_pnl")
        ):
            raise ValueError("Projection-only R authority fix unexpectedly changed verified provider P&L")
        git(work, "add", "--", *changes)
        git(work, "commit", "-m", "Refresh verified projections under structured R authority")
        git(work, "push", "origin", "HEAD:refs/heads/main")
        staged_head = git(ROOT, "--git-dir", str(bare), "rev-parse", "refs/heads/main")
        if staged_head != git(work, "rev-parse", "HEAD"):
            raise ValueError("Isolated derived-data Git remote read-back mismatch")
        return tmp, bare, work, {
            "status": "PROJECTIONS_REFRESHED_VERIFIED",
            "operation": "refresh_projections",
            "canonical_events_sha256": before_events_sha,
            "source_trade_count": len(before_projection.get("records") or {}),
            "changed_projection_paths": changes,
            "writer_commit": staged_head,
            "verified_provider_pnl_before": previous_live.get("verified_provider_pnl"),
            "verified_provider_pnl_after": updated_live.get("verified_provider_pnl"),
            "verified_provider_pnl_contributors": updated_live.get("provider_pnl_observation_count"),
            "live_r_count_before": previous_live.get("count"),
            "live_r_count_after": updated_live.get("count"),
        }
    except BaseException:
        tmp.cleanup()
        raise


def _analyze(req: dict) -> dict:
    """No writer, provider lookup, trade mutation, or implicit mixed population."""
    from .projections import verify_projections

    verified = verify_projections(ROOT)
    if verified.get("status") != "CURRENT":
        raise ValueError("Analytics projection is stale; refuse unsupported performance claims")
    view = json.loads((ROOT / "exports/operational/trade-view/trade-view-data.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "exports/operational/analytics/manifest.json").read_text(encoding="utf-8"))
    health = json.loads((ROOT / "exports/operational/health/current.json").read_text(encoding="utf-8"))
    summaries = view.get("summary_by_mode") or {}
    population = req["population"]
    selected = summaries.get(population)
    if not isinstance(selected, dict):
        raise ValueError("Selected performance population is not available")
    if req["strategy_id"] is not None:
        by_strategy = view.get("named_strategy_summary_by_mode") or {}
        scoped = by_strategy.get(req["strategy_id"]) or {}
        selected = scoped.get(population)
        if not isinstance(selected, dict):
            raise ValueError("Selected strategy has no verified population in this mode")
    result = {
        "status": "READ_ONLY_ANALYSIS_VERIFIED",
        "operation": "analyze",
        "analysis_mode": req["analysis_mode"],
        "population": population,
        "strategy_id": req["strategy_id"],
        "source_commit": git(ROOT, "rev-parse", "HEAD"),
        "canonical_events_sha256": manifest.get("canonical_lifecycle_events_sha256"),
        "projection_status": verified["status"],
        "count": selected.get("count"),
        "summary": {
            key: selected.get(key)
            for key in ("count", "win_rate", "expectancy_r", "total_r",
                        "verified_provider_pnl", "provider_pnl_observation_count",
                        "verified_provider_net_pnl", "provider_net_pnl_observation_count",
                        "provider_net_pnl_incomplete_count", "provider_net_pnl_funding_gap_count",
                        "verified_provider_net_pnl_funding_reconciled", "provider_net_pnl_funding_reconciled_count",
                        "provider_net_pnl_funding_missing_settlements",
                        "provider_open_fee_total", "provider_funding_fee_total")
        },
    }
    if req["analysis_mode"] == "health":
        strategies = health.get("strategies") or {}
        if req["strategy_id"] is not None:
            if isinstance(strategies, dict):
                data = strategies.get(req["strategy_id"])
            elif isinstance(strategies, list):
                data = next((item for item in strategies
                             if isinstance(item, dict) and item.get("strategy_id") == req["strategy_id"]), None)
            else:
                data = None
            if data is None:
                raise ValueError("Selected strategy is not represented in health projection")
            result["strategy_health"] = data
        else:
            result["health_status_summary"] = health.get("status_summary")
            result["sample_origin_summary"] = health.get("sample_origin_summary")
    return result


def _staging_repository() -> tuple[tempfile.TemporaryDirectory, Path, Path]:
    tmp = tempfile.TemporaryDirectory(prefix="trading-record-agent-")
    top = Path(tmp.name)
    bare, work = top / "bare.git", top / "work"
    git(ROOT, "init", "--bare", "--initial-branch=main", str(bare))
    git(ROOT, "push", str(bare), "HEAD:refs/heads/main")
    git(ROOT, "clone", str(bare), str(work))
    # Always pin the *isolated repository's* committer; do not rely on
    # Actions global Git config, which may not exist for Fresh Agents/tests.
    git(work, "config", "user.name", "github-actions[bot]")
    git(work, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    return tmp, bare, work


def _execute(req: dict) -> dict:
    approval = req["approval"]
    if not isinstance(approval, dict) or set(approval) != {
        "preview_request", "preview_digest", "owner_statement", "confirmed_in_chat"
    }:
        raise ValueError("The explicit Owner approval attestation is missing")
    if approval["confirmed_in_chat"] is not True:
        raise ValueError("The Owner has not confirmed the human-readable registration")
    if not isinstance(approval["owner_statement"], str) or not approval["owner_statement"].strip():
        raise ValueError("Empty Owner authorization")
    preview_id = approval["preview_request"]
    if not isinstance(preview_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", preview_id):
        raise ValueError("Invalid source preview ID")
    receipt_path = ROOT / "agent-receipts" / f"{preview_id}.json"
    if not receipt_path.is_file():
        raise ValueError("Read-only preview receipt does not exist")
    saved = json.loads(receipt_path.read_text(encoding="utf-8"))
    if saved.get("status") != "READY_FOR_OWNER_CONFIRMATION":
        raise ValueError("Previous preview was not eligible")
    if saved.get("intake_sha256") != digest(req["registration_intake"]):
        raise ValueError("Owner-confirmed intake differs from read-only preview")
    if saved.get("preview_digest") != approval["preview_digest"]:
        raise ValueError("Owner approval digest differs from saved preview")

    tmp, bare, work = _staging_repository()
    try:
        # A registered lifecycle intentionally cannot receive a new first-pass
        # preview. Identical retries reuse the saved confirmation, then rely on
        # the existing Executor to verify the immutable frozen packet.
        intake = req["registration_intake"]
        state = registration_state(
            work, str(intake.get("provider") or ""),
            str(intake.get("provider_lifecycle_id") or ""),
        )
        retry = state.get("status") == "REGISTERED"
        if state.get("status") not in {"UNREGISTERED", "REGISTERED"}:
            raise ValueError("Unusable registration state; refusing Agent request")
        if not retry:
            preview = _preview(work, intake)
            if preview["preview_digest"] != approval["preview_digest"]:
                raise ValueError("Recomputed preview changed; Owner reconfirmation required")
        lifecycle_path = work / "canonical/lifecycle/events.jsonl"
        numbers_path = work / "canonical/sequences/numbers.json"
        prior_events = lifecycle_path.read_bytes() if retry else None
        prior_numbers = numbers_path.read_bytes() if retry else None
        confirmed_at = datetime.now(timezone.utc).isoformat()
        if retry:
            bound_sha = state.get("packet_sha256")
            if not isinstance(bound_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", bound_sha):
                raise ValueError("Registered lifecycle lacks a valid frozen packet identity")
            stored_packet = json.loads(packet_path(work, bound_sha).read_text(encoding="utf-8"))
            if stored_packet.get("packet_sha256") != bound_sha or packet_sha256(stored_packet) != bound_sha:
                raise ValueError("Frozen packet failed immutable hash verification")
            stored_confirmation = stored_packet.get("owner_confirmation") or {}
            if stored_confirmation.get("preview_digest") != approval["preview_digest"]:
                raise ValueError("Registered packet has different Owner-approved preview digest")
            confirmed_at = stored_confirmation.get("confirmed_at")
            if not isinstance(confirmed_at, str) or not confirmed_at:
                raise ValueError("Registered packet has no preserved confirmation timestamp")

        confirmed = {
            "schema_version": "registration-executor-input.v2",
            "registration_intake": req["registration_intake"],
            "owner_confirmation": {
                "status": "confirmed",
                "confirmed_at": confirmed_at,
                "preview_digest": approval["preview_digest"],
            },
        }
        input_path = Path(tmp.name) / "confirmed.json"
        input_path.write_text(json.dumps(confirmed, ensure_ascii=False), encoding="utf-8")

        # This executes the unchanged canonical registration Writer and its
        # tests, commit, push, and remote-main readback against local bare main.
        output = subprocess.run(
            [sys.executable, "-m", "trading_os", "registration-executor",
             "run", "--input", str(input_path)],
            cwd=work, capture_output=True, text=True
        )
        if output.returncode:
            raise RuntimeError("Existing registration executor rejected request: "
                               + (output.stdout + output.stderr)[-5500:])

        if retry:
            if lifecycle_path.read_bytes() != prior_events:
                raise ValueError("Retry appended lifecycle events; refusing publication")
            if numbers_path.read_bytes() != prior_numbers:
                raise ValueError("Retry changed allocated numbers; refusing publication")
            try:
                result = json.loads(output.stdout)
            except json.JSONDecodeError as exc:
                raise ValueError("Registered retry produced no machine-verifiable Writer result") from exc
            if (result.get("remote_readback") or {}).get("status") != "VERIFIED":
                raise ValueError("Registered retry has no verified remote read-back")
            if result.get("provider_lifecycle") != (
                str(intake.get("provider")).lower() + ":" + str(intake.get("provider_lifecycle_id"))
            ):
                raise ValueError("Retry Writer read-back has a different provider identity")
        staged_head = git(ROOT, "--git-dir", str(bare), "rev-parse", "refs/heads/main")
        receipt = {
            "status": "ALREADY_REGISTERED_VERIFIED" if retry else "EXECUTOR_SUCCEEDED",
            "operation": "commit",
            "writer_commit": staged_head,
            "preview_digest": approval["preview_digest"],
            "intake_sha256": digest(req["registration_intake"]),
            "writer_receipt_tail": output.stdout[-7500:],
        }
        # Add only the receipt; canonical mutations were produced solely by
        # the existing Writer, never by this bridge.
        (work / "agent-receipts").mkdir(exist_ok=True)
        return tmp, bare, work, receipt
    except BaseException:
        tmp.cleanup()
        raise


def _remote_head(ref: str) -> str:
    output = git(ROOT, "ls-remote", "origin", f"refs/heads/{ref}")
    return output.split()[0] if output else ""


def _sync_intake_with_main() -> None:
    """Bring the intake checkout up to current main before any Writer runs.

    image-intake = main + Owner-uploaded originals + Agent requests/receipts.
    Canonical truth always comes from main; intake-only paths never conflict.
    """
    global _MAIN_BASE
    git(ROOT, "fetch", "origin", f"refs/heads/{MAIN_REF}:refs/remotes/origin/{MAIN_REF}")
    _MAIN_BASE = git(ROOT, "rev-parse", f"refs/remotes/origin/{MAIN_REF}")
    merged = subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", _MAIN_BASE, "HEAD"],
        capture_output=True,
    ).returncode == 0
    if not merged:
        git(ROOT, "config", "user.name", "github-actions[bot]")
        git(ROOT, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        git(ROOT, "merge", "--no-edit", "-m", "agent: sync image-intake with main [skip ci]", _MAIN_BASE)
    _require_intake_only_divergence()


# image-intake may differ from main only by Owner uploads and Agent
# requests/receipts. Anything else (canonical data, code, workflows) would ride
# along into main with the next Writer publish, bypassing the Writer.
INTAKE_ONLY_PREFIXES = ("agent-requests/", "agent-receipts/", "registration-intake/", "review-intake/")


def _require_intake_only_divergence() -> None:
    # --no-renames: a rename out of canonical/ must show its deleted source,
    # not only an allowed destination.
    diverged = git(ROOT, "diff", "--name-only", "--no-renames", _MAIN_BASE, "HEAD").splitlines()
    foreign = sorted(p for p in diverged if not p.startswith(INTAKE_ONLY_PREFIXES))
    if foreign:
        raise ValueError(
            "image-intake differs from main outside intake-only paths; refusing to run: "
            + ", ".join(foreign[:20])
        )


def _check_remote_lease() -> None:
    if _remote_head(INTAKE_REF) != os.environ["GITHUB_SHA"]:
        raise ValueError("image-intake changed since event; refusing to publish")
    if _MAIN_BASE is None or _remote_head(MAIN_REF) != _MAIN_BASE:
        raise ValueError("main changed since this run started; refusing to publish")


def _publish_receipt(path: Path, receipt: dict, *, working: Path, bare: Path | None) -> None:
    dst = working / "agent-receipts" / (path.stem + ".json")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise ValueError("Receipt already exists; refusing request replay")
    dst.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    git(working, "config", "user.name", "github-actions[bot]")
    git(working, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git(working, "add", "--", f"agent-receipts/{path.stem}.json")
    git(working, "commit", "-m", f"agent: record {path.stem} {receipt['status']} [skip ci]")
    _check_remote_lease()
    if bare is not None:
        # Canonical write: publish the verified Writer result to main and keep
        # image-intake on the same commit, atomically (fast-forward only).
        git(working, "push", "origin", "HEAD:refs/heads/main")
        git(ROOT, "fetch", str(bare), "main:refs/remotes/agent-staging/main")
        target = git(ROOT, "rev-parse", "refs/remotes/agent-staging/main")
        git(ROOT, "push", "--atomic", "origin",
            f"{target}:refs/heads/{MAIN_REF}", f"{target}:refs/heads/{INTAKE_REF}")
        if _remote_head(MAIN_REF) != target or _remote_head(INTAKE_REF) != target:
            raise ValueError("Remote main/image-intake read-back differs from published Writer result")
    else:
        # Read-only operations record their receipt on image-intake only.
        git(working, "push", "origin", f"HEAD:refs/heads/{INTAKE_REF}")


def main() -> None:
    path = _single_new_request()
    req = _read_request(path)
    _sync_intake_with_main()
    staging = None
    try:
        if req["operation"] == "refresh_projections":
            staging = _refresh_read_models(req)
            tmp, bare, work, receipt = staging
            _publish_receipt(path, receipt, working=work, bare=bare)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return
        if req["operation"] == "review_commit":
            staging = _review_commit(req)
            tmp, bare, work, receipt = staging
            _publish_receipt(path, receipt, working=work, bare=bare)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return
        if req["operation"] == "backtest_commit":
            staging = _backtest_commit(req)
            tmp, bare, work, receipt = staging
            _publish_receipt(path, receipt, working=work, bare=bare)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return
        if req["operation"] == "backtest_preview":
            receipt = _backtest_preview(req)
            _publish_receipt(path, receipt, working=ROOT, bare=None)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return
        if req["operation"] == "review_preview":
            receipt = _review_preview(req)
            _publish_receipt(path, receipt, working=ROOT, bare=None)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return
        if req["operation"] == "analyze":
            receipt = _analyze(req)
            _publish_receipt(path, receipt, working=ROOT, bare=None)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            return
        if req["operation"] == "preview":
            if req["approval"] is not None:
                raise ValueError("A read-only preview must have no pre-existing approval")
            result = _preview(ROOT, req["registration_intake"])
            receipt = _registration_preview_receipt(result, req["registration_intake"])
            _publish_receipt(path, receipt, working=ROOT, bare=None)
        else:
            staging = _execute(req)
            tmp, bare, work, receipt = staging
            _publish_receipt(path, receipt, working=work, bare=bare)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        # Fail closed. An error receipt is for diagnosis and is never approval.
        error = {
            "status": "BLOCKED", "operation": req["operation"],
            "error": type(exc).__name__ + ": " + str(exc),
            "request_sha256": digest(req),
        }
        if req.get("operation") == "preview" and isinstance(req.get("registration_intake"), dict):
            try:
                error.update(registration_sync_disclosure(
                    ROOT, canonical_provider(req["registration_intake"].get("provider"))))
            except Exception:
                pass  # Advisory only; never mask the original error or lose the receipt.
            if isinstance(exc, OwnerInputRequired):
                error.update({
                    "status": "OWNER_INPUT_REQUIRED",
                    "owner_question": exc.question,
                    "preview_digest": None,
                    "owner_confirmation_allowed": False,
                })
        if staging is None:
            try:
                _publish_receipt(path, error, working=ROOT, bare=None)
            except Exception:
                if isinstance(exc, OwnerInputRequired):
                    raise  # Never report success if the Owner question was not saved.
        print(json.dumps(error, ensure_ascii=False))
        if isinstance(exc, OwnerInputRequired) and req.get("operation") == "preview":
            return  # Owner question is an expected, read-only result, not a failed Action.
        raise
    finally:
        if staging is not None:
            staging[0].cleanup()


if __name__ == "__main__":
    main()
