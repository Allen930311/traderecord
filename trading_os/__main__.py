import argparse
from pathlib import Path
from .manifest import write_manifest
from .pilot import run_pilot
from .dryrun import run_dry_run
from .contracts import validate_record
from .adjudication import apply_decision_file
from .auditor import audit_trade
from .registry import load_registry, load_strategy, load_version
from .strategy_discovery import run_step0
from .method_validation import run_b1_dataset, DEFAULT_DATASET_VERSION
from .episode_identity import run_b1_v3
from .rule_freeze import write_b3_manifest
from .performance_validation import run_b2_validation
from .trader_dna import run_trader_dna
import json
import re
import subprocess
import time
from http.server import ThreadingHTTPServer

from .operational_lifecycle import LifecycleStore, LifecycleError
from .operational_analytics import refresh_analytics, load_current
from .strategy_health import refresh_health, load_current_health
from .trade_view import build_trade_view, make_trade_view_handler
from .backtest_handoff import emit_handoff
from .projections import ProjectionStaleError, refresh_projections, verify_projections
from .registration_test_lane import add_cli as add_registration_test_lane_cli, dispatch_cli as dispatch_registration_test_lane_cli
from .registration_executor import (
    _git as _registration_git,
    _prepare_checkout as _prepare_registration_checkout,
    add_cli as add_registration_executor_cli,
    dispatch_cli as dispatch_registration_executor_cli,
)
from .evidence_store import EvidenceStore, EvidenceStoreError
from .paper_ingestion import PaperIngestionError, ingest_paper_execution_evidence
from .unified_capture import (
    capture_backtest as uc_capture_backtest,
    capture_live as uc_capture_live,
    capture_retrospective_live as uc_capture_retrospective_live,
)
from .vocabularies import SOURCE_METHODS, PLAN_FIELD_UNKNOWN_VALUES


def _parse_plan_field(raw: str):
    """argparse ``type=`` for a plan value that may be unknown.

    Returns the sentinel string unchanged (case-insensitively normalized to
    its canonical lower-case spelling) when it matches
    ``vocabularies.PLAN_FIELD_UNKNOWN_VALUES``; otherwise parses it as a
    real float. LIVE accepts incomplete plans; BACKTEST keeps its existing
    complete-plan contract and uses plain ``type=float``.
    """

    normalized = raw.strip().lower() if isinstance(raw, str) else raw
    if normalized in PLAN_FIELD_UNKNOWN_VALUES:
        return normalized
    return float(raw)


def _resolve_source_commit(root: Path, requested: str | None) -> str:
    """Resolve a requested ref (or HEAD) to a full commit SHA, fail closed."""

    ref = (requested or "HEAD").strip()
    if not ref or ref.startswith("-") or "\x00" in ref:
        raise ValueError("source commit ref is invalid")
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        return ref.lower()
    try:
        resolved = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"source commit ref cannot be resolved: {ref}") from exc
    if not re.fullmatch(r"[0-9a-fA-F]{40}", resolved):
        raise ValueError(f"source commit ref did not resolve to a full commit SHA: {ref}")
    return resolved.lower()


def _apply_evidence_time_cli_fields(evidence: dict, args, *, prefix: str = "") -> dict:
    """Copy explicitly supplied capture and represented-market times to evidence."""

    def value(name: str):
        return getattr(args, f"{prefix}{name}", None)

    captured_at = value("captured_at")
    if captured_at:
        evidence["captured_at"] = captured_at
    capture_mode = value("capture_mode")
    if capture_mode:
        provenance = {"capture_mode": capture_mode}
        if captured_at:
            provenance["captured_at"] = captured_at
        evidence["provenance"] = provenance
    represented_start = value("represented_market_start")
    represented_end = value("represented_market_end")
    if represented_start or represented_end:
        evidence["represented_market_time"] = {
            **({"start": represented_start} if represented_start else {}),
            **({"end": represented_end} if represented_end else {}),
        }
    role = value("evidence_role")
    if role:
        evidence["role"] = role
    return evidence

def main(argv=None, *, owner_approval=None):
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="command",required=True)
    for name in ("scan","pilot","dry-run"): sub.add_parser(name)
    step0 = sub.add_parser("step0")
    step0.add_argument("--baseline-sha", required=True)
    step0.add_argument("--historical-reference-commit", required=True)
    step0.add_argument("--output-dir", default="exports/strategy-discovery/step-0")
    b1 = sub.add_parser("b1-dataset")
    b1.add_argument("--dataset-version", default=DEFAULT_DATASET_VERSION)
    b1.add_argument("--generated-at", required=True)
    b1.add_argument("--source-commit", default=None)
    b1.add_argument("--output-dir", default="exports/method-validation/b1")
    b1v3 = sub.add_parser("b1-v3")
    b1v3.add_argument("--baseline-sha", required=True)
    b1v3.add_argument("--generated-at", required=True)
    b1v3.add_argument("--output-dir", default="exports/method-validation/b1-v3")
    b1v3.add_argument("--adjudication-dir", default="canonical/adjudications/method-validation")
    b3 = sub.add_parser("b3-freeze")
    b3.add_argument("--freeze-date", required=True)
    b3.add_argument("--baseline-sha", required=True)
    b3.add_argument("--output-dir", default="exports/method-validation/b3")
    b2 = sub.add_parser("b2-validate")
    b2.add_argument("--baseline-sha", required=True)
    b2.add_argument("--analysis-date", required=True)
    b2.add_argument("--output-dir", default="exports/method-validation/b2")
    dna = sub.add_parser("trader-dna")
    dna.add_argument("--baseline-sha", required=True)
    dna.add_argument("--analysis-date", required=True)
    dna.add_argument("--output-dir", default="exports/trader-dna/v1")
    capture = sub.add_parser("capture", help="governed daily trade capture")
    capture_sub = capture.add_subparsers(dest="capture_command", required=True)
    draft = capture_sub.add_parser("draft")
    for name in ("symbol", "direction", "entry", "sl", "tp", "planned-rr", "timeframe-context", "timeframe-trigger", "market-context", "thesis", "created-at"):
        draft.add_argument("--" + name, required=True)
    # LIVE drafts take strategy observations (resolver input); only BACKTEST/PAPER drafts name a strategy.
    draft.add_argument("--strategy-id")
    draft.add_argument("--rule-version")
    draft.add_argument("--strategy-observations-file", type=Path)
    draft.add_argument("--setup-tag", action="append", default=[])
    draft.add_argument("--confirmation-tag", action="append", default=[])
    draft.add_argument("--chart-ref", action="append", default=[])
    draft.add_argument("--recording-timing", default="contemporaneous")
    draft.add_argument("--type", dest="trade_type", default="live", choices=("live", "backtest"))
    draft.add_argument("--execution-environment", default="prop_challenge")
    draft.add_argument("--draft-id")
    draft.add_argument("--variant")
    draft.add_argument("--recorded-at")
    draft.add_argument("--allow-experimental", action="store_true")
    draft.add_argument("--allow-retired", action="store_true")
    draft.add_argument("--source-method", choices=sorted(SOURCE_METHODS))
    draft.add_argument("--outcome-known-at-recording", choices=("true", "false", "unknown"))
    evidence = capture_sub.add_parser("evidence")
    evidence.add_argument("--draft-id", required=True)
    evidence.add_argument("--source-ref", required=True)
    evidence.add_argument("--sha256", required=True)
    evidence.add_argument("--category", required=True, choices=("pre_trade_context", "entry", "exit", "review"))
    evidence.add_argument("--timing", required=True, choices=("pre_trade", "execution", "post_trade", "unknown"))
    evidence.add_argument("--type", required=True, choices=("chart_snapshot", "manual_note", "broker_fill", "sheet_export", "replay_chart"))
    evidence.add_argument("--locator")
    evidence.add_argument("--captured-at")
    evidence.add_argument("--capture-mode", choices=("original_capture", "retrospective_replay", "provider_generated", "unknown"))
    evidence.add_argument("--represented-market-start")
    evidence.add_argument("--represented-market-end")
    evidence.add_argument("--evidence-role")
    evidence.add_argument("--recorded-at")
    evidence_store_cmd = sub.add_parser("evidence", help="durable content-addressed evidence store")
    evidence_store_sub = evidence_store_cmd.add_subparsers(dest="evidence_store_command", required=True)
    evidence_ingest = evidence_store_sub.add_parser("ingest")
    evidence_ingest.add_argument("--file", required=True)
    evidence_ingest.add_argument("--draft-id")
    evidence_ingest.add_argument("--trade-id")
    evidence_ingest.add_argument("--category", choices=("pre_trade_context", "entry", "exit", "review"))
    evidence_ingest.add_argument("--timing", choices=("pre_trade", "execution", "post_trade", "unknown"))
    evidence_ingest.add_argument("--type", default="manual_note", choices=("chart_snapshot", "manual_note", "broker_fill", "sheet_export", "replay_chart"))
    evidence_ingest.add_argument("--captured-at")
    evidence_ingest.add_argument("--capture-mode", choices=("original_capture", "retrospective_replay", "provider_generated", "unknown"))
    evidence_ingest.add_argument("--represented-market-start")
    evidence_ingest.add_argument("--represented-market-end")
    evidence_ingest.add_argument("--evidence-role")
    evidence_ingest.add_argument("--recorded-at")
    evidence_correct_locator = evidence_store_sub.add_parser("correct-locator", help="append an audited correction for a verified legacy evidence locator")
    evidence_correct_locator.add_argument("--trade-id", required=True)
    evidence_correct_locator.add_argument("--source-ref", required=True)
    evidence_correct_locator.add_argument("--sha256", required=True)
    evidence_correct_locator.add_argument("--category", required=True, choices=("pre_trade_context", "entry", "exit", "review"))
    evidence_correct_locator.add_argument("--prior-locator", required=True)
    evidence_correct_locator.add_argument("--locator", required=True)
    evidence_correct_locator.add_argument("--reason", required=True)
    evidence_correct_locator.add_argument("--recorded-at")
    pending = capture_sub.add_parser("entry-pending")
    pending.add_argument("--draft-id", required=True)
    pending.add_argument("--provider", default="propw")
    pending.add_argument("--occurred-at")
    pending.add_argument("--recorded-at")
    identity = capture_sub.add_parser("identity-link")
    identity.add_argument("--draft-id", required=True)
    identity.add_argument("--provider", required=True)
    identity.add_argument("--provider-lifecycle-id", required=True)
    identity.add_argument("--instrument", required=True)
    identity.add_argument("--direction", required=True, choices=("long", "short"))
    identity.add_argument("--provider-execution-at", required=True)
    identity.add_argument("--candidate-source", required=True)
    identity.add_argument("--match-disposition", required=True, choices=("MATCHED", "AMBIGUOUS", "UNMATCHED", "CONFLICT"))
    identity.add_argument("--evidence-ref", action="append", default=[])
    identity.add_argument("--created-at")
    identity.add_argument("--decision-created-at")
    identity.add_argument("--recorded-at")
    identity.add_argument("--authority", default="deterministic_match")
    entry = capture_sub.add_parser("verify-entry")
    for name in ("draft-id", "instrument", "direction", "actual-entry", "execution-at", "source-ref", "provider-lifecycle-id"):
        entry.add_argument("--" + name, required=True)
    entry.add_argument("--provider", default="propw")
    entry.add_argument("--recorded-at")
    partial_close = capture_sub.add_parser("partial-close")
    for name in ("trade-id", "exit-price", "closed-at", "quantity-fraction"):
        partial_close.add_argument("--" + name, required=True)
    partial_close.add_argument("--provider-pnl", type=float)
    partial_close.add_argument("--source-ref")
    partial_close.add_argument("--recorded-at")
    final_close = capture_sub.add_parser("close")
    for name in ("trade-id", "exit-price", "closed-at", "realized-r"):
        final_close.add_argument("--" + name, required=True)
    final_close.add_argument("--provider-pnl", type=float)
    final_close.add_argument("--source-ref")
    final_close.add_argument("--recorded-at")
    exit_verified = capture_sub.add_parser("verify-exit")
    exit_verified.add_argument("--trade-id", required=True)
    exit_verified.add_argument("--authority-class", required=True, choices=("deterministic_execution_calculation", "owner_attestation", "governed_manual_adjudication"))
    exit_verified.add_argument("--evidence-ref", action="append", required=True)
    exit_verified.add_argument("--realized-r", type=float)
    exit_verified.add_argument("--provider-pnl", type=float)
    exit_verified.add_argument("--provider-pnl-authority")
    exit_verified.add_argument("--recorded-at")
    capture_finalize = capture_sub.add_parser("finalize")
    capture_finalize.add_argument("--draft-id", required=True)
    capture_finalize.add_argument("--reviewer", default="owner")
    capture_finalize.add_argument("--occurred-at")
    capture_finalize.add_argument("--recorded-at")
    review = capture_sub.add_parser("review")
    review.add_argument("--trade-id", required=True)
    review.add_argument("--reviewed-at", required=True)
    review.add_argument("--review-file", type=Path, required=True)
    review.add_argument("--evidence-intake", type=Path, help="read-only local image intake JSON used when generating the Owner-confirmed preview")
    review.add_argument("--reviewer", default="owner")
    review.add_argument("--recorded-at")
    review.add_argument("--approval-file", type=Path, help="Agent-generated confirmation for the exact approved Review preview")
    review_preview = capture_sub.add_parser("review-preview", help="read-only, digest-bound preparation for one canonical trade review")
    review_preview.add_argument("--trade-id", required=True)
    review_preview.add_argument("--review-file", type=Path, required=True)
    review_preview.add_argument("--evidence-intake", type=Path, help="optional read-only local image intake JSON")

    # --- Unified Capture v1: owner-facing single-call orchestration ---
    # These three subcommands wrap trading_os.unified_capture's facade
    # functions so the owner/agent never has to run the 4-8 low-level
    # `capture draft/evidence/entry-pending/identity-link/verify-entry/
    # confirm-backtest-entry/finalize` steps above by hand for the common
    # case. They do not add any new lifecycle logic -- see unified_capture.py.
    uc_backtest = capture_sub.add_parser(
        "backtest", help="one-call BACKTEST capture: draft -> evidence -> confirm entry -> finalize"
    )
    for name in (
        "strategy-id", "rule-version", "symbol", "direction",
        "timeframe-context", "timeframe-trigger", "market-context", "thesis",
        "created-at", "execution-at",
    ):
        uc_backtest.add_argument("--" + name, required=True)
    uc_backtest.add_argument("--entry", type=float, required=True)
    uc_backtest.add_argument("--sl", type=float, required=True)
    uc_backtest.add_argument("--tp", type=float, required=True)
    uc_backtest.add_argument("--planned-rr", type=float, required=True)
    uc_backtest.add_argument("--actual-entry", type=float)
    uc_backtest.add_argument("--setup-tag", action="append", default=[])
    uc_backtest.add_argument("--confirmation-tag", action="append", default=[])
    uc_backtest.add_argument("--chart-ref", action="append", default=[])
    uc_backtest.add_argument("--source-method", required=True, choices=sorted(SOURCE_METHODS))
    uc_backtest.add_argument("--recording-timing", default="contemporaneous")
    uc_backtest.add_argument("--outcome-known-at-recording", default="unknown", choices=("true", "false", "unknown"))
    uc_backtest.add_argument("--execution-environment", default="paper")
    uc_backtest.add_argument("--evidence-file", required=True)
    uc_backtest.add_argument("--evidence-type", default="chart_snapshot", choices=("chart_snapshot", "manual_note", "broker_fill", "sheet_export", "replay_chart"))
    uc_backtest.add_argument("--evidence-timing", default="execution", choices=("pre_trade", "execution", "post_trade", "unknown"))
    uc_backtest.add_argument("--run-id")
    uc_backtest.add_argument("--parameters-ref")
    uc_backtest.add_argument("--dataset-ref")
    uc_backtest.add_argument("--data-period")
    uc_backtest.add_argument("--allow-experimental", action="store_true")
    uc_backtest.add_argument("--allow-retired", action="store_true")
    uc_backtest.add_argument("--draft-id")
    uc_backtest.add_argument("--variant")
    uc_backtest.add_argument("--recorded-at")
    uc_backtest.add_argument("--reviewer", default="owner")

    attribution = capture_sub.add_parser(
        "strategy-attribution",
        help="append a resolver-backed attribution amendment to an unresolved LIVE trade",
    )
    attribution.add_argument("--trade-id", required=True)
    attribution.add_argument("--resolver-input-file", required=True, type=Path)
    attribution.add_argument("--amendment-id", required=True)
    attribution.add_argument("--reason")
    attribution.add_argument("--resolved-at", required=True)
    attribution.add_argument("--reviewer", default="owner")
    attribution.add_argument("--recorded-at")

    def _add_unified_live_args(parser: argparse.ArgumentParser, *, entry_type) -> None:
        for name in (
            "symbol", "direction",
            "timeframe-context", "timeframe-trigger", "market-context", "thesis",
            "created-at",
        ):
            parser.add_argument("--" + name, required=True)
        # Strategy identity is produced by the mandatory strategy resolver from these
        # per-rule observations; the caller cannot assert a strategy_id/rule_version.
        parser.add_argument("--strategy-observations-file", type=Path)
        parser.add_argument("--entry", type=entry_type, required=True)
        parser.add_argument("--sl", type=entry_type, required=True)
        parser.add_argument("--tp", type=entry_type, required=True)
        parser.add_argument("--planned-rr", type=entry_type, required=True)
        parser.add_argument("--setup-tag", action="append", default=[])
        parser.add_argument("--confirmation-tag", action="append", default=[])
        parser.add_argument("--chart-ref", action="append", default=[])
        parser.add_argument("--execution-environment", default="prop_challenge")
        # Two deliberately separate evidence slots -- never collapsed into
        # one flag, and no default lets one silently stand in for the other:
        #
        # --provider-evidence-file (required) is the actual provider/broker-
        # sourced execution evidence. LIVE identity-link and verify-entry
        # both require attached *provider execution* evidence (see
        # LifecycleStore._is_provider_evidence / PROVIDER_EXECUTION_EVIDENCE_
        # TYPES) -- this is the ONLY evidence ever referenced by those two
        # steps, i.e. the sole source of PROVIDER EXECUTION AUTHORITY.
        parser.add_argument("--provider-evidence-file", required=True)
        parser.add_argument(
            "--provider-evidence-type", default="broker_fill",
            choices=("broker_fill", "sheet_export", "provider_export", "provider_execution_artifact"),
        )
        parser.add_argument("--provider-evidence-timing", default="execution", choices=("pre_trade", "execution", "post_trade", "unknown"))
        # --chart-file (optional) is visual evidence, never execution authority.
        # Its true timing/category remain intact; a later replay can be marked
        # review/post_trade with separate capture and represented-market times.
        parser.add_argument("--chart-file")
        parser.add_argument("--chart-timing", choices=("pre_trade", "execution", "post_trade", "unknown"))
        parser.add_argument("--chart-capture-mode", choices=("original_capture", "retrospective_replay", "provider_generated", "unknown"))
        parser.add_argument("--chart-captured-at")
        parser.add_argument("--chart-represented-market-start")
        parser.add_argument("--chart-represented-market-end")
        parser.add_argument("--chart-evidence-role")
        parser.add_argument("--chart-evidence-type", choices=("chart_snapshot", "replay_chart"))
        parser.add_argument("--provider", default="propw")
        parser.add_argument("--outcome-known-at-recording", default="unknown", choices=("true", "false", "unknown"))
        parser.add_argument("--provider-lifecycle-id", required=True)
        parser.add_argument("--provider-execution-at", required=True)
        parser.add_argument("--candidate-source", required=True)
        parser.add_argument("--actual-entry", type=float, required=True)
        parser.add_argument("--identity-authority", default="deterministic_match")
        parser.add_argument("--draft-id")
        parser.add_argument("--variant")
        parser.add_argument("--recorded-at")
        parser.add_argument("--reviewer", default="owner")

    uc_live = capture_sub.add_parser(
        "live", help="one-call contemporaneous LIVE capture: draft -> evidence -> entry-pending -> identity-link -> verify-entry -> finalize"
    )
    # LIVE plan completeness is independent of provider execution verification.
    _add_unified_live_args(uc_live, entry_type=_parse_plan_field)
    uc_live.add_argument("--recording-timing", default="contemporaneous")

    uc_retro_live = capture_sub.add_parser(
        "retrospective-live", help="one-call retrospective LIVE capture for a late-reported real execution"
    )
    # Retrospective LIVE also preserves unknown plan values explicitly.
    _add_unified_live_args(uc_retro_live, entry_type=_parse_plan_field)
    uc_retro_live.add_argument("--recording-timing", default="retrospective")
    uc_retro_live.add_argument("--registration-packet-file", type=Path)
    uc_retro_live.add_argument("--registration-manifest", type=Path)
    uc_retro_live.add_argument("--registration-evidence-root", type=Path)

    analytics = sub.add_parser("analytics", help="read or refresh rolling operational analytics")
    analytics_sub = analytics.add_subparsers(dest="analytics_command", required=True)
    analytics_refresh = analytics_sub.add_parser("refresh")
    analytics_refresh.add_argument("--generated-at")
    analytics_refresh.add_argument("--source-commit")
    analytics_refresh.add_argument("--output-dir", default="exports/operational/analytics")
    analytics_sub.add_parser("show")
    health = sub.add_parser("health", help="read or refresh pre-declared strategy health")
    health_sub = health.add_subparsers(dest="health_command", required=True)
    health_refresh = health_sub.add_parser("refresh")
    health_refresh.add_argument("--generated-at")
    health_refresh.add_argument("--source-commit")
    health_refresh.add_argument("--output-dir", default="exports/operational/health")
    health_sub.add_parser("show")
    trade_view = sub.add_parser("trade-view", help="build or serve private local Trade View")
    trade_view.add_argument("--build-only", action="store_true")
    trade_view.add_argument("--port", type=int, default=8765)
    trade_view.add_argument("--output-dir", default="exports/operational/trade-view")
    handoff = sub.add_parser("handoff", help="emit the evidence-gated backtest handoff")
    handoff.add_argument("--output-dir", default="exports/operational/handoff")
    handoff.add_argument("--generated-at")
    handoff.add_argument("--source-commit")
    projections = sub.add_parser("projections", help="refresh and verify the operational projection closure")
    projections_sub = projections.add_subparsers(dest="projections_command", required=True)
    projections_refresh = projections_sub.add_parser("refresh")
    projections_refresh.add_argument("--generated-at")
    projections_refresh.add_argument("--source-commit")
    projections_refresh.add_argument("--target-trade")
    projections_verify = projections_sub.add_parser("verify")
    projections_verify.add_argument("--target-trade")
    projections_verify.add_argument("--require-census-fresh", action="store_true", help="fail unless the Trade View Census pins current provider and canonical sources")
    add_registration_test_lane_cli(sub)
    add_registration_executor_cli(sub)
    validate = sub.add_parser("validate"); validate.add_argument("path")
    adj = sub.add_parser("adjudicate"); adj_sub=adj.add_subparsers(dest="adjudicate_command", required=True); apply=adj_sub.add_parser("apply"); apply.add_argument("file")
    fin=sub.add_parser("finalize"); fin.add_argument("record_id"); fin.add_argument("--reviewer", default="cli")
    aud=sub.add_parser("audit"); aud.add_argument("trade_id"); aud.add_argument("--strategy", required=True); aud.add_argument("--version", required=True)
    paper_ingest = sub.add_parser("paper-ingest", help="ingest Crypto PAPER execution evidence into the canonical record")
    paper_ingest.add_argument("--evidence-file", required=True)
    paper_ingest.add_argument("--reviewer", default="crypto-paper-ingest")
    paper_ingest.add_argument("--recorded-at")
    psync = sub.add_parser("provider-sync", help="provider sync pipeline: fetch -> sanitized ledger/Census/Trade View -> optional data-branch commit/push/PR")
    psync.add_argument("--provider", default="propw")
    psync.add_argument("--fetch", action="store_true", help="pull fresh data with the owner-local PropW session first")
    psync.add_argument("--commit", action="store_true", help="commit ONLY provider-ledgers/<provider>/ on a data/ branch (never main)")
    psync.add_argument("--push", action="store_true", help="push the data branch (requires --commit)")
    psync.add_argument("--create-pr", action="store_true", help="after push, open or report an Owner-reviewed PR to main (requires --push)")
    psync.add_argument("--publish-main", action="store_true", help="Owner-authorized routine path: from a clean checkout equal to origin/main, commit ONLY provider-owned paths and fast-forward push them to main, then read back (requires --commit; replaces --push/--create-pr)")
    psync.add_argument("--branch")
    psync.add_argument("--db", type=Path)
    btreg = sub.add_parser("backtest-registration", help="shared BACKTEST (Replay chart) registration: read-only preview, then Owner-approved commit")
    btreg.add_argument("action", choices=("preview", "commit"))
    btreg.add_argument("--intake", type=Path, required=True, help="backtest-chart-intake.v1 JSON filled by the Agent")
    btreg.add_argument("--preview-digest", help="commit: digest from the preview the Owner approved")
    btreg.add_argument("--approval", type=Path, help="commit: JSON with confirmed_in_chat, confirmed_at, agent_id, owner_statement")
    btreg.add_argument("--publish", action="store_true", help="commit: write in an isolated worktree, run the registration test lane, push to origin/main and read back (same path as LIVE)")
    btexit = sub.add_parser("backtest-exit", help="Owner-confirmed chart exit for a registered BACKTEST: read-only preview, then Owner-approved commit")
    btexit.add_argument("action", choices=("preview", "commit"))
    btexit.add_argument("--trade-no", required=True, help="BT number of an OPEN registered sample")
    btexit.add_argument("--exit-price", required=True)
    btexit.add_argument("--closed-at", required=True, help="ISO time with UTC offset; must equal the recorded replay exit_at when one exists")
    btexit.add_argument("--exit-evidence-ref", required=True, help="artifact locator of the exit Replay image already saved on the sample")
    btexit.add_argument("--realized-r", help="only when the registered stop is unknown")
    btexit.add_argument("--preview-digest", help="commit: digest from the preview the Owner approved")
    btexit.add_argument("--approval", type=Path, help="commit: JSON with confirmed_in_chat, confirmed_at, agent_id, owner_statement")
    observe = sub.add_parser("observe", help="record a setup that was NOT executed (obs_*; no T-ID, never in performance)")
    for name in ("symbol", "direction", "observed-at", "timeframe-context", "timeframe-trigger"):
        observe.add_argument("--" + name, required=True)
    observe.add_argument("--reason-not-taken", required=True, choices=("missed", "skipped", "risk_limit", "invalidated", "other"))
    observe.add_argument("--reason-note")
    observe.add_argument("--setup-quality", default="unknown", choices=("good", "fair", "poor", "unknown"))
    observe.add_argument("--thesis", default="")
    observe.add_argument("--evidence-file", action="append", default=[], type=Path, help="chart image; repeatable; at least one required")
    observe.add_argument("--evidence-timing", required=True, choices=("pre_trade", "execution", "post_trade", "unknown"), help="when the saved image was captured relative to the setup; no default")
    observe.add_argument("--recording-timing", required=True, choices=("contemporaneous", "retrospective"), help="was this written up at the time (contemporaneous) or after the fact (retrospective)")
    observe.add_argument("--pre-trade-adjudication-id", help="required for retrospective + pre_trade evidence: accepted Owner evidence adjudication naming the images")
    observe.add_argument("--strategy-observations-file", required=True, type=Path)
    observe.add_argument("--recorded-at")
    census = sub.add_parser("census", help="reconcile a provider ledger against canonical registrations")
    census.add_argument("--provider", default="propw")
    census.add_argument("--lookup", metavar="OPEN_ID", help="print the census row for one provider lifecycle (T-ID if registered)")
    census.add_argument("--write", action="store_true", help="write provider-ledgers/<provider>/registration-census.json")
    census.add_argument("--next", action="store_true", help="print the next eligible UNREGISTERED lifecycle (default phase: CLOSED)")
    census.add_argument("--candidates", action="store_true", help="list all eligible UNREGISTERED lifecycles in stable order")
    census.add_argument("--phase", choices=("open", "partially_closed", "closed", "all"), help="provider lifecycle phase filter; --next defaults to closed, --candidates defaults to all")
    census.add_argument("--pending", action="store_true", help="list STRATEGY_PENDING lifecycles")
    census.add_argument("--instrument")
    census.add_argument("--month", help="YYYY-MM of the provider entry time")
    resolve = sub.add_parser("resolve", help="run the mandatory strategy resolver on an observations JSON file")
    resolve.add_argument("--observations", required=True)
    resolve.add_argument("--provider-entry-at", help="verified provider entry timestamp for retrospective chronology checks")
    registration_packet = sub.add_parser("registration-packet", help="freeze/validate/persist an Owner-confirmed Registration Packet")
    registration_packet_sub = registration_packet.add_subparsers(dest="registration_packet_command", required=True)
    rp_freeze = registration_packet_sub.add_parser("freeze")
    rp_freeze.add_argument("--input", type=Path, required=True)
    rp_freeze.add_argument("--output", type=Path, required=True)
    rp_freeze.add_argument("--manifest", type=Path)
    rp_freeze.add_argument("--transport-mode", required=True, choices=("same_agent", "external_handoff"))
    rp_validate = registration_packet_sub.add_parser("validate")
    rp_validate.add_argument("--packet", type=Path, required=True)
    rp_validate.add_argument("--manifest", type=Path)
    rp_validate.add_argument("--evidence-root", type=Path)
    rp_persist = registration_packet_sub.add_parser("persist")
    rp_persist.add_argument("--packet", type=Path, required=True)
    rp_persist.add_argument("--manifest", type=Path)
    rp_persist.add_argument("--evidence-root", type=Path)
    rp_persist.add_argument("--recorded-at")
    rp_preview = registration_packet_sub.add_parser(
        "prepare-confirmation",
        help="build a read-only validated Owner confirmation summary from an unfrozen draft or compact raw-image intake",
    )
    preview_input = rp_preview.add_mutually_exclusive_group(required=True)
    preview_input.add_argument("--input", type=Path, help="complete Registration Packet draft JSON")
    preview_input.add_argument("--intake", type=Path, help="compact registration-intake.v1 JSON with raw image paths")
    rp_preview.add_argument("--manifest", type=Path)
    rp_preview.add_argument("--evidence-root", type=Path)
    rp_preview.add_argument(
        "--image-file",
        action="append",
        default=[],
        metavar="SHA256=PATH",
        help="exact same-agent source image bytes; repeat once per packet image when not yet in EvidenceStore",
    )
    args=p.parse_args(argv); root=Path.cwd()
    if args.command == "backtest-registration":
        from .backtest_registration import commit_backtest_registration, preview_backtest_registration
        try:
            intake = json.loads(args.intake.read_text(encoding="utf-8"))
            if args.action == "preview":
                result = preview_backtest_registration(root, intake)
            else:
                if not args.preview_digest or not args.approval:
                    raise ValueError("commit requires --preview-digest and --approval")
                approval = json.loads(args.approval.read_text(encoding="utf-8"))
                if args.publish:
                    from .backtest_publish import publish_backtest_registration
                    result = publish_backtest_registration(
                        root, intake, preview_digest=args.preview_digest, approval=approval,
                    )
                else:
                    result = commit_backtest_registration(
                        root, intake, preview_digest=args.preview_digest, approval=approval,
                    )
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if args.command == "backtest-exit":
        from .backtest_exit import commit_backtest_exit, preview_backtest_exit
        try:
            common = dict(trade_no=args.trade_no, exit_price=args.exit_price, closed_at=args.closed_at,
                          exit_evidence_ref=args.exit_evidence_ref, realized_r=args.realized_r)
            if args.action == "preview":
                result = preview_backtest_exit(root, **common)
            else:
                if not args.preview_digest or not args.approval:
                    raise ValueError("commit requires --preview-digest and --approval")
                result = commit_backtest_exit(root, **common, preview_digest=args.preview_digest,
                                              approval=json.loads(args.approval.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if args.command == "registration-test-lane":
        try:
            dispatch_registration_test_lane_cli(root, args)
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        return
    if args.command == "registration-executor":
        try:
            dispatch_registration_executor_cli(root, args)
        except (OSError, ValueError) as exc:
            reason = str(exc)
            status = "OWNER_RECONFIRMATION_REQUIRED" if "OWNER_RECONFIRMATION_REQUIRED" in reason else "BLOCKED"
            print(json.dumps({"status": status, "reason": reason}, ensure_ascii=False, sort_keys=True, indent=2))
            raise SystemExit(2)
        return
    if args.command=="resolve":
        from .strategy_resolver import ResolverError, resolve_strategy
        try:
            result = resolve_strategy(
                root,
                json.loads(Path(args.observations).read_text(encoding="utf-8")),
                provider_entry_at=args.provider_entry_at,
            )
        except ResolverError as exc:
            print(f"error: {exc}"); raise SystemExit(2)
        print(json.dumps(result, ensure_ascii=False, indent=2)); raise SystemExit(0 if result["resolved"] else 1)
    if args.command == "registration-packet":
        from .registration_packet import (
            OwnerReconfirmationRequired,
            RegistrationPacketError,
            freeze_packet,
            load_packet,
            persist_frozen_packet,
            prepare_confirmation_preview,
            build_registration_intake,
            validate_frozen_packet,
        )
        try:
            if args.registration_packet_command == "prepare-confirmation":
                cli_started = time.perf_counter()
                source_root = root.resolve()

                def source_path(path: Path | None) -> Path | None:
                    if path is None:
                        return None
                    candidate = Path(path)
                    return (candidate if candidate.is_absolute() else source_root / candidate).resolve()

                input_path = source_path(args.intake or args.input)
                source_packet = load_packet(input_path)
                image_files = {}
                for value in args.image_file:
                    sha, separator, raw_path = value.partition("=")
                    if not separator or not re.fullmatch(r"[0-9a-fA-F]{64}", sha) or not raw_path:
                        raise RegistrationPacketError("--image-file must be SHA256=PATH")
                    candidate = Path(raw_path)
                    image_files[sha.lower()] = (candidate if candidate.is_absolute() else source_root / candidate).resolve()
                manifest_path = source_path(args.manifest)
                evidence_root_path = source_path(args.evidence_root)
                checkout_started = time.perf_counter()
                execution_root, owned_worktree, checkout_info = _prepare_registration_checkout(source_root)
                checkout_seconds = time.perf_counter() - checkout_started
                intake_timing: dict[str, float] = {}
                preview_timing: dict[str, float] = {}
                try:
                    if args.intake is not None:
                        if args.image_file:
                            raise RegistrationPacketError("--image-file cannot be combined with --intake; paths belong in intake.images")
                        source_packet, image_files = build_registration_intake(
                            execution_root,
                            source_packet,
                            image_base=source_root,
                            timing_sink=intake_timing,
                        )
                    main_sha = str(checkout_info["mutation_base_sha"]).lower()
                    result = prepare_confirmation_preview(
                        execution_root,
                        source_packet,
                        source_main_sha=main_sha,
                        evidence_manifest=manifest_path,
                        evidence_root=evidence_root_path,
                        image_files=image_files,
                        timing_sink=preview_timing,
                    )
                finally:
                    if owned_worktree is not None:
                        _registration_git(source_root, "worktree", "remove", "--force", str(owned_worktree))
                result["fresh_main_sha"] = main_sha
                result["preparation_checkout"] = {
                    key: checkout_info[key]
                    for key in ("mode", "source_head", "mutation_base_sha", "source_was_clean", "worktrees_created")
                }
                timings = {
                    "checkout_preparation": checkout_seconds,
                    **intake_timing,
                    **preview_timing,
                    "total_cli": time.perf_counter() - cli_started,
                }
                for count_key in (
                    "provider_ledger_accesses",
                    "provider_census_checks",
                    "resolver_calls",
                    "image_source_reads",
                ):
                    timings[count_key] = int(intake_timing.get(count_key, 0)) + int(
                        preview_timing.get(count_key, 0)
                    )
                result["timings_seconds"] = timings
                result["status"] = result.get("status", "READY_FOR_OWNER_CONFIRMATION")
            elif args.registration_packet_command == "freeze":
                packet = freeze_packet(
                    root,
                    load_packet(args.input),
                    transport_mode=args.transport_mode,
                    evidence_manifest=args.manifest,
                )
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
                result = {"status": "FROZEN", "packet_sha256": packet["packet_sha256"], "output": str(args.output)}
            elif args.registration_packet_command == "validate":
                result = validate_frozen_packet(
                    root,
                    load_packet(args.packet),
                    evidence_manifest=args.manifest,
                    evidence_root=args.evidence_root,
                )
                result = {"status": "VALIDATED", **result}
            else:
                store = LifecycleStore(root)
                result = persist_frozen_packet(
                    store,
                    load_packet(args.packet),
                    evidence_manifest=args.manifest,
                    evidence_root=args.evidence_root,
                    recorded_at=args.recorded_at,
                )
        except OwnerReconfirmationRequired as exc:
            print(json.dumps({"status": "OWNER_RECONFIRMATION_REQUIRED", "reason": str(exc)}, ensure_ascii=False, sort_keys=True, indent=2))
            raise SystemExit(3)
        except (RegistrationPacketError, LifecycleError, OSError, ValueError) as exc:
            print(f"error: {exc}")
            raise SystemExit(2)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        raise SystemExit(0)
    if args.command=="provider-sync":
        from .provider_sync import ProviderSyncError, run_provider_sync
        try:
            report = run_provider_sync(root, args.provider, fetch=args.fetch, commit=args.commit, push=args.push, create_pr=args.create_pr, publish_main=args.publish_main, branch=args.branch, db=args.db)
        except (ProviderSyncError, OSError, ValueError) as exc:
            print(f"error: {exc}"); raise SystemExit(2)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)); raise SystemExit(0)
    if args.command=="observe":
        from .setup_observation import ObservationError, record_setup_observation
        from .strategy_resolver import ResolverError
        try:
            result = record_setup_observation(
                root, symbol=args.symbol, direction=args.direction, observed_at=args.observed_at,
                timeframe_context=args.timeframe_context, timeframe_trigger=args.timeframe_trigger,
                reason_not_taken=args.reason_not_taken, reason_note=args.reason_note, setup_quality=args.setup_quality,
                thesis=args.thesis, evidence_files=args.evidence_file, evidence_timing=args.evidence_timing,
                recording_timing=args.recording_timing, pre_trade_adjudication_id=args.pre_trade_adjudication_id,
                strategy_observations=json.loads(args.strategy_observations_file.read_text(encoding="utf-8")),
                recorded_at=args.recorded_at,
            )
        except (OSError, ValueError, ObservationError, ResolverError, EvidenceStoreError) as exc:
            print(f"error: {exc}"); raise SystemExit(2)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)); raise SystemExit(0)
    if args.command=="census":
        from .registration_census import build_census, lookup, select_candidates, select_next, write_census
        try:
            if args.write:
                print(write_census(root, args.provider))
            result = build_census(root, args.provider)
        except FileNotFoundError as exc:
            print(f"error: {exc}"); raise SystemExit(2)
        if args.next:
            print(json.dumps(select_next(result, phase=args.phase or "closed", instrument=args.instrument, month=args.month), ensure_ascii=False, indent=2))
        elif args.candidates:
            print(json.dumps(select_candidates(result, phase=args.phase or "all", instrument=args.instrument, month=args.month), ensure_ascii=False, indent=2))
        elif args.pending:
            print(json.dumps([row for row in result["lifecycles"] if row["status"] == "STRATEGY_PENDING"], ensure_ascii=False, indent=2))
        elif args.lookup:
            print(json.dumps(lookup(result, args.provider, args.lookup), ensure_ascii=False, indent=2))
        else:
            print(json.dumps({k: result[k] for k in ("provider", "generated_at", "source_digests", "freshness", "total", "counts", "registered_identity_count", "unregistered_closed", "not_evaluated")}, ensure_ascii=False, indent=2))
        raise SystemExit(0)
    if args.command=="validate":
        payload=json.loads(Path(args.path).read_text(encoding="utf-8")); errors=validate_record(payload); print(json.dumps([e.__dict__ for e in errors],ensure_ascii=False)); raise SystemExit(1 if errors else 0)
    if args.command=="adjudicate":
        try:
            print(apply_decision_file(root, Path(args.file))["adjudication_id"])
            return
        except ValueError as exc: raise SystemExit(str(exc))
    if args.command=="finalize":
        try:
            print(LifecycleStore(root).finalize(args.record_id, args.reviewer, allow_legacy_compatibility=True)["trade_id"])
            return
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
    if args.command=="audit":
        path=root/"canonical/records/trades"/(args.trade_id+".json")
        if not path.exists(): raise SystemExit("trade_id not found")
        try:
            trade=json.loads(path.read_text(encoding="utf-8"))
            registry=load_registry(root)
            strategy=load_strategy(root, args.strategy)
            version=load_version(root, args.version)
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps(audit_trade(trade,strategy,version,registry=registry),ensure_ascii=False,sort_keys=True,indent=2)); return
    if args.command == "paper-ingest":
        try:
            result = ingest_paper_execution_evidence(
                root,
                Path(args.evidence_file),
                reviewer=args.reviewer,
                recorded_at=args.recorded_at,
            )
        except (OSError, ValueError, PaperIngestionError, EvidenceStoreError, LifecycleError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if args.command == "capture":
        store = LifecycleStore(root)
        try:
            if args.capture_command == "strategy-attribution":
                result = store.resolve_strategy_attribution(
                    args.trade_id,
                    resolver_input=json.loads(args.resolver_input_file.read_text(encoding="utf-8")),
                    amendment_id=args.amendment_id,
                    reason=args.reason,
                    resolved_at=args.resolved_at,
                    reviewer=args.reviewer,
                    recorded_at=args.recorded_at,
                )
            elif args.capture_command == "draft":
                outcome_known = {"true": True, "false": False, "unknown": "unknown"}.get(args.outcome_known_at_recording, "unknown")
                draft_strategy_id, draft_rule_version, draft_resolution = args.strategy_id, args.rule_version, None
                if args.trade_type == "live":
                    if args.strategy_id or args.rule_version:
                        raise ValueError("a LIVE draft cannot declare --strategy-id/--rule-version; strategy attribution comes from the resolver")
                    from .unified_capture import resolve_live_strategy
                    observations = json.loads(args.strategy_observations_file.read_text(encoding="utf-8")) if args.strategy_observations_file else {}
                    resolver_result, draft_resolution = resolve_live_strategy(
                        store, observations,
                        intended_entry=_parse_plan_field(args.entry),
                        stop_loss=_parse_plan_field(args.sl), take_profit=_parse_plan_field(args.tp), contemporaneous=args.recording_timing == "contemporaneous",
                        planned_rr=_parse_plan_field(args.planned_rr),
                    )
                    draft_strategy_id = resolver_result.get("strategy_id")
                    draft_rule_version = resolver_result.get("rule_version")
                elif not (args.strategy_id and args.rule_version):
                    raise ValueError("a BACKTEST draft requires --strategy-id and --rule-version")
                result = store.create_draft(
                    strategy_id=draft_strategy_id, rule_version=draft_rule_version, strategy_resolution=draft_resolution, symbol=args.symbol, direction=args.direction,
                    intended_entry=_parse_plan_field(args.entry) if args.trade_type == "live" else float(args.entry),
                    stop_loss=_parse_plan_field(args.sl) if args.trade_type == "live" else float(args.sl),
                    take_profit=_parse_plan_field(args.tp) if args.trade_type == "live" else float(args.tp),
                    planned_rr=_parse_plan_field(args.planned_rr) if args.trade_type == "live" else float(args.planned_rr),
                    timeframe_context=args.timeframe_context, timeframe_trigger=args.timeframe_trigger, market_context=args.market_context,
                    setup_tags=args.setup_tag, confirmation_tags=args.confirmation_tag, thesis=args.thesis, chart_refs=args.chart_ref,
                    created_at=args.created_at, recording_timing=args.recording_timing, trade_type=args.trade_type,
                    execution_environment=args.execution_environment, draft_id=args.draft_id, allow_experimental=args.allow_experimental,
                    variant=args.variant, recorded_at=args.recorded_at, allow_retired=args.allow_retired,
                    source_method=args.source_method, outcome_known_at_recording=outcome_known,
                )
            elif args.capture_command == "evidence":
                evidence = {"source_ref": args.source_ref, "sha256": args.sha256, "category": args.category, "timing": args.timing, "type": args.type, "locator": args.locator}
                _apply_evidence_time_cli_fields(evidence, args)
                if args.recorded_at:
                    evidence["recorded_at"] = args.recorded_at
                result = store.attach_evidence(args.draft_id, evidence)
            elif args.capture_command == "entry-pending":
                result = store.mark_entry_pending(args.draft_id, provider={"provider": args.provider}, occurred_at=args.occurred_at, recorded_at=args.recorded_at)
            elif args.capture_command == "identity-link":
                result = store.create_identity_link(
                    args.draft_id,
                    provider=args.provider,
                    provider_lifecycle_id=args.provider_lifecycle_id,
                    instrument=args.instrument,
                    direction=args.direction,
                    provider_execution_timestamp=args.provider_execution_at,
                    candidate_source=args.candidate_source,
                    match_disposition=args.match_disposition,
                    evidence_refs=args.evidence_ref,
                    created_at=args.created_at, decision_created_at=args.decision_created_at, recorded_at=args.recorded_at,
                    authority=args.authority,
                )
            elif args.capture_command == "verify-entry":
                result = store.verify_entry(args.draft_id, instrument=args.instrument, direction=args.direction, actual_entry=float(args.actual_entry), execution_at=args.execution_at, source_ref=args.source_ref, provider_lifecycle_id=args.provider_lifecycle_id, provider=args.provider, recorded_at=args.recorded_at)
            elif args.capture_command == "partial-close":
                result = store.partial_close(args.trade_id, exit_price=float(args.exit_price), closed_at=args.closed_at, quantity_fraction=float(args.quantity_fraction), provider_pnl=args.provider_pnl, source_ref=args.source_ref, recorded_at=args.recorded_at)
            elif args.capture_command == "close":
                result = store.final_close(args.trade_id, exit_price=float(args.exit_price), closed_at=args.closed_at, realized_r=float(args.realized_r), provider_pnl=args.provider_pnl, source_ref=args.source_ref, recorded_at=args.recorded_at)
            elif args.capture_command == "verify-exit":
                result = store.verify_exit(args.trade_id, authority_class=args.authority_class, evidence_refs=args.evidence_ref, realized_r=args.realized_r, provider_pnl=args.provider_pnl, provider_pnl_authority=args.provider_pnl_authority, recorded_at=args.recorded_at)
            elif args.capture_command == "finalize":
                result = store.finalize(args.draft_id, args.reviewer, occurred_at=args.occurred_at, recorded_at=args.recorded_at)
            elif args.capture_command == "backtest":
                ingestion = EvidenceStore(root).ingest(args.evidence_file)
                entry_evidence = {
                    "source_ref": ingestion["locator"],
                    "locator": ingestion["locator"],
                    "sha256": ingestion["sha256"],
                    "category": "entry",
                    "timing": args.evidence_timing,
                    "type": args.evidence_type,
                    "media_type": ingestion["media_type"],
                    "byte_size": ingestion["byte_size"],
                    "original_filename": ingestion["original_filename"],
                }
                outcome_known = {"true": True, "false": False, "unknown": "unknown"}[args.outcome_known_at_recording]
                result = uc_capture_backtest(
                    store,
                    strategy_id=args.strategy_id, rule_version=args.rule_version, symbol=args.symbol, direction=args.direction,
                    intended_entry=args.entry, stop_loss=args.sl, take_profit=args.tp, planned_rr=args.planned_rr,
                    timeframe_context=args.timeframe_context, timeframe_trigger=args.timeframe_trigger, market_context=args.market_context,
                    setup_tags=args.setup_tag, confirmation_tags=args.confirmation_tag, thesis=args.thesis, chart_refs=args.chart_ref,
                    created_at=args.created_at, source_method=args.source_method, entry_evidence=entry_evidence,
                    actual_entry=args.actual_entry if args.actual_entry is not None else args.entry,
                    execution_at=args.execution_at,
                    run_id=args.run_id, parameters_ref=args.parameters_ref, dataset_ref=args.dataset_ref, data_period=args.data_period,
                    outcome_known_at_recording=outcome_known, recording_timing=args.recording_timing,
                    execution_environment=args.execution_environment, draft_id=args.draft_id, variant=args.variant,
                    recorded_at=args.recorded_at, allow_experimental=args.allow_experimental, allow_retired=args.allow_retired,
                    reviewer=args.reviewer,
                )
            elif args.capture_command in ("live", "retrospective-live"):
                registration_packet_file = getattr(args, "registration_packet_file", None)
                if args.capture_command == "retrospective-live":
                    supplied = int(args.strategy_observations_file is not None) + int(registration_packet_file is not None)
                    if supplied != 1:
                        raise ValueError(
                            "capture retrospective-live requires exactly one of "
                            "--strategy-observations-file or --registration-packet-file"
                        )
                evidence_store = EvidenceStore(root)
                provider_ingestion = evidence_store.ingest(args.provider_evidence_file)
                provider_evidence = {
                    "source_ref": provider_ingestion["locator"],
                    "locator": provider_ingestion["locator"],
                    "sha256": provider_ingestion["sha256"],
                    "category": "entry",
                    "timing": args.provider_evidence_timing,
                    "type": args.provider_evidence_type,
                    "media_type": provider_ingestion["media_type"],
                    "byte_size": provider_ingestion["byte_size"],
                    "original_filename": provider_ingestion["original_filename"],
                }
                chart_evidence = None
                if args.chart_file:
                    chart_ingestion = evidence_store.ingest(args.chart_file)
                    replay_chart = args.chart_capture_mode == "retrospective_replay"
                    if replay_chart and (
                        args.capture_command != "retrospective-live"
                        or args.recording_timing != "retrospective"
                    ):
                        raise ValueError(
                            "retrospective_replay charts require retrospective-live capture timing"
                        )
                    if replay_chart and args.chart_timing not in {None, "post_trade"}:
                        raise ValueError("retrospective_replay must remain post_trade")
                    chart_timing = "post_trade" if replay_chart else (args.chart_timing or "pre_trade")
                    chart_category = {
                        "pre_trade": "pre_trade_context",
                        "execution": "entry",
                        "post_trade": "review",
                        "unknown": "review",
                    }[chart_timing]
                    chart_type = args.chart_evidence_type or ("replay_chart" if replay_chart else "chart_snapshot")
                    if replay_chart and chart_type != "replay_chart":
                        raise ValueError("retrospective_replay must keep type=replay_chart")
                    if replay_chart and args.chart_evidence_role not in {None, "historical_market_reconstruction"}:
                        raise ValueError("retrospective_replay role must be historical_market_reconstruction")
                    chart_evidence = {
                        "source_ref": chart_ingestion["locator"],
                        "locator": chart_ingestion["locator"],
                        "sha256": chart_ingestion["sha256"],
                        "category": chart_category,
                        "timing": chart_timing,
                        "type": chart_type,
                        "media_type": chart_ingestion["media_type"],
                        "byte_size": chart_ingestion["byte_size"],
                        "original_filename": chart_ingestion["original_filename"],
                    }
                    if args.chart_capture_mode == "retrospective_replay":
                        chart_evidence["role"] = "historical_market_reconstruction"
                    _apply_evidence_time_cli_fields(chart_evidence, args, prefix="chart_")
                capture_fn = uc_capture_live if args.capture_command == "live" else uc_capture_retrospective_live
                outcome_known = {"true": True, "false": False, "unknown": "unknown"}[args.outcome_known_at_recording]
                frozen_packet = None
                strategy_observations = None
                if registration_packet_file is not None:
                    from .registration_packet import load_packet
                    frozen_packet = load_packet(registration_packet_file)
                else:
                    strategy_observations = (
                        json.loads(args.strategy_observations_file.read_text(encoding="utf-8"))
                        if args.strategy_observations_file else {}
                    )
                result = capture_fn(
                    store,
                    strategy_observations=strategy_observations, symbol=args.symbol, direction=args.direction,
                    intended_entry=args.entry, stop_loss=args.sl, take_profit=args.tp, planned_rr=args.planned_rr,
                    timeframe_context=args.timeframe_context, timeframe_trigger=args.timeframe_trigger, market_context=args.market_context,
                    setup_tags=args.setup_tag, confirmation_tags=args.confirmation_tag, thesis=args.thesis, chart_refs=args.chart_ref,
                    created_at=args.created_at, provider_evidence=provider_evidence, chart_evidence=chart_evidence,
                    provider=args.provider, provider_lifecycle_id=args.provider_lifecycle_id,
                    provider_execution_timestamp=args.provider_execution_at, candidate_source=args.candidate_source,
                    actual_entry=args.actual_entry, recording_timing=args.recording_timing,
                    outcome_known_at_recording=outcome_known,
                    execution_environment=args.execution_environment, draft_id=args.draft_id, variant=args.variant,
                    recorded_at=args.recorded_at,
                    identity_authority=args.identity_authority, reviewer=args.reviewer,
                    frozen_registration_packet=frozen_packet,
                    registration_evidence_manifest=getattr(args, "registration_manifest", None),
                    registration_evidence_root=getattr(args, "registration_evidence_root", None),
                )
            elif args.capture_command == "review-preview":
                review_payload = json.loads(args.review_file.read_text(encoding="utf-8"))
                if not isinstance(review_payload, dict):
                    raise ValueError("review file must contain an object")
                from .review_service import prepare_review
                result = prepare_review(
                    root, args.trade_id, review_payload, evidence_intake=args.evidence_intake,
                )
            elif args.capture_command == "review":
                review_payload = json.loads(args.review_file.read_text(encoding="utf-8"))
                if not isinstance(review_payload, dict):
                    raise ValueError("review file must contain an object")
                from .review_service import commit_review
                agent_approval = (
                    json.loads(args.approval_file.read_text(encoding="utf-8"))
                    if args.approval_file is not None else None
                )
                result = commit_review(
                    root, args.trade_id, review_payload,
                    reviewed_at=args.reviewed_at, agent_approval=agent_approval,
                    trusted_owner_approval=owner_approval,
                    evidence_intake=args.evidence_intake,
                    reviewer=args.reviewer, recorded_at=args.recorded_at,
                )
            else:
                raise ValueError("unsupported capture command")
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        if isinstance(result, dict) and result.get("registered") is False:
            raise SystemExit(1)  # provider-verified registration is still in progress
        return
    if args.command == "evidence":
        try:
            if args.evidence_store_command == "ingest":
                ingestion = EvidenceStore(root).ingest(args.file)
                result = dict(ingestion)
                target_ref = args.draft_id or args.trade_id
                if target_ref:
                    if not args.category or not args.timing:
                        raise ValueError("attaching evidence to a draft/trade requires --category and --timing")
                    store = LifecycleStore(root)
                    target_draft_id = args.draft_id or store.resolve_draft_id(args.trade_id)
                    evidence = {
                        "source_ref": ingestion["locator"],
                        "locator": ingestion["locator"],
                        "sha256": ingestion["sha256"],
                        "category": args.category,
                        "timing": args.timing,
                        "type": args.type,
                        "media_type": ingestion["media_type"],
                        "byte_size": ingestion["byte_size"],
                        "original_filename": ingestion["original_filename"],
                    }
                    _apply_evidence_time_cli_fields(evidence, args)
                    if args.recorded_at:
                        evidence["recorded_at"] = args.recorded_at
                    event = store.attach_evidence(target_draft_id, evidence)
                    result["draft_id"] = target_draft_id
                    result["evidence_event_id"] = event.get("event_id")
            elif args.evidence_store_command == "correct-locator":
                store = LifecycleStore(root)
                target_draft_id = store.resolve_draft_id(args.trade_id)
                event = store.correct_evidence_locator(
                    target_draft_id,
                    source_ref=args.source_ref,
                    sha256=args.sha256,
                    category=args.category,
                    prior_locator=args.prior_locator,
                    locator=args.locator,
                    reason=args.reason,
                    recorded_at=args.recorded_at,
                )
                result = {"draft_id": target_draft_id, "trade_id": args.trade_id, "event_id": event["event_id"], "locator": args.locator}
            else:
                raise ValueError(f"unsupported evidence command: {args.evidence_store_command}")
        except (OSError, ValueError, EvidenceStoreError, LifecycleError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)); return
    if args.command == "analytics":
        try:
            if args.analytics_command == "refresh":
                result = refresh_analytics(root, generated_at=args.generated_at, source_commit=_resolve_source_commit(root, args.source_commit), output_dir=Path(args.output_dir))
            else:
                result = load_current(root)
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)); return
    if args.command == "health":
        try:
            if args.health_command == "refresh":
                result = refresh_health(root, generated_at=args.generated_at, source_commit=_resolve_source_commit(root, args.source_commit), output_dir=Path(args.output_dir))
            else:
                result = load_current_health(root)
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)); return
    if args.command == "trade-view":
        try:
            result = build_trade_view(root, output_dir=Path(args.output_dir))
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        output = (root / Path(args.output_dir)).resolve() if not Path(args.output_dir).is_absolute() else Path(args.output_dir).resolve()
        if args.build_only:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)); return
        handler = make_trade_view_handler(output, root / "canonical" / "evidence" / "artifacts")
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
        print(f"Trade View listening on http://127.0.0.1:{args.port}/")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return
    if args.command == "handoff":
        try:
            result = emit_handoff(root, generated_at=args.generated_at, source_commit=_resolve_source_commit(root, args.source_commit), output_dir=Path(args.output_dir))
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)); return
    if args.command == "projections":
        try:
            if args.projections_command == "refresh":
                result = refresh_projections(root, generated_at=args.generated_at, source_commit=_resolve_source_commit(root, args.source_commit) if args.source_commit else None, target_trade=args.target_trade)
            else:
                result = verify_projections(root, target_trade=args.target_trade, require_census_fresh=args.require_census_fresh)
        except (OSError, ValueError, ProjectionStaleError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)); return
    if args.command=="b1-dataset":
        try:
            source_commit = _resolve_source_commit(root, args.source_commit)
            manifest = run_b1_dataset(
                root,
                dataset_version=args.dataset_version,
                generated_at=args.generated_at,
                source_commit=source_commit,
                output_dir=Path(args.output_dir),
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps({
            "dataset_version": manifest["dataset_version"],
            "total_rows": manifest["row_count_invariants"]["total_rows"],
            "by_analysis_eligibility": manifest["row_count_invariants"]["by_analysis_eligibility"],
            "manifest_digest": manifest["manifest_digest"],
            "output_dir": str(Path(args.output_dir)),
        }, ensure_ascii=False, sort_keys=True))
        return
    if args.command=="b1-v3":
        try:
            manifest = run_b1_v3(
                root,
                baseline_sha=args.baseline_sha,
                generated_at=args.generated_at,
                output_dir=Path(args.output_dir),
                adjudication_dir=Path(args.adjudication_dir),
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps({
            "schema_version": manifest["schema_version"],
            "source_rows": manifest["counts"]["source_rows"],
            "resolved_episode_count": manifest["counts"]["resolved_episode_count"],
            "performance_countable_episode_count": manifest["counts"]["performance_countable_episode_count"],
            "manifest_digest": manifest["manifest_digest"],
            "output_dir": str(Path(args.output_dir)),
        }, ensure_ascii=False, sort_keys=True))
        return
    if args.command=="b3-freeze":
        try:
            manifest = write_b3_manifest(
                root,
                Path(args.output_dir),
                baseline_sha=args.baseline_sha,
                freeze_date=args.freeze_date,
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps({
            "schema_version": manifest["schema_version"],
            "freeze_baseline_sha": manifest["freeze_baseline_sha"],
            "manifest_digest": manifest["manifest_digest"],
            "output_dir": str(Path(args.output_dir)),
        }, ensure_ascii=False, sort_keys=True))
        return
    if args.command=="b2-validate":
        try:
            manifest = run_b2_validation(
                root,
                output_dir=Path(args.output_dir),
                baseline_sha=args.baseline_sha,
                analysis_date=args.analysis_date,
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps({
            "schema_version": manifest["schema_version"],
            "baseline_sha": manifest["baseline_sha"],
            "analysis_date": manifest["analysis_date"],
            "analyzed_rows": manifest["universe"]["b2_analyzed_rows"],
            "manifest_digest": manifest["manifest_digest"],
            "output_dir": str(Path(args.output_dir)),
        }, ensure_ascii=False, sort_keys=True))
        return
    if args.command=="trader-dna":
        try:
            manifest = run_trader_dna(
                root,
                baseline_sha=args.baseline_sha,
                analysis_date=args.analysis_date,
                output_dir=Path(args.output_dir),
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(json.dumps({
            "schema_version": manifest["schema_version"],
            "baseline_sha": manifest["baseline_sha"],
            "analysis_date": manifest["analysis_date"],
            "analyzed_episodes": manifest["evidence_coverage"]["analyzed_episodes"],
            "manifest_digest": manifest["manifest_digest"],
            "output_dir": str(Path(args.output_dir)),
        }, ensure_ascii=False, sort_keys=True))
        return
    if args.command=="step0":
        try:
            manifest = run_step0(
                root,
                baseline_sha=args.baseline_sha,
                historical_reference_commit=args.historical_reference_commit,
                output_dir=Path(args.output_dir),
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc))
        print(
            json.dumps(
                {
                    "baseline_main_sha": manifest["baseline_main_sha"],
                    "input_count": manifest["corpus"]["input_count"],
                    "eligible_active_review_pool": manifest["routes"]["eligible_active_review_pool"],
                    "blocked_total": manifest["routes"]["blocked_total"],
                    "manifest_digest": manifest["manifest_digest"],
                    "output_dir": str(Path(args.output_dir)),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    if args.command=="scan":
        out=root/"canonical/manifests/source.json"; data=write_manifest(root,out); print(f"entries={data['entry_count']} output={out}")
    elif args.command=="pilot":
        out=root/"exports/pilot/pilot.json"; data=run_pilot(root,out); print(f"fixtures={len(data.get('fixtures', []))} validation_ok={data.get('validation', {}).get('ok', False)}")
    else:
        out=root/"exports/dry-run/dry-run.json"; data=run_dry_run(root,out); print(f"sources={data['source_count']} dispositions={data['coverage']['disposition_count']} exactly_once={data['coverage']['exactly_once']}")
if __name__ == "__main__": main()
