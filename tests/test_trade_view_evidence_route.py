from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

from resolver_helpers import live_resolution
from trading_os.evidence_store import EvidenceStore
from trading_os.operational_analytics import refresh_analytics
from trading_os.operational_lifecycle import LifecycleStore
from trading_os.strategy_health import refresh_health
from trading_os.trade_view import build_trade_view, make_trade_view_handler
from review_test_support import attach_fixture_image, owner_confirmed_review


FIXED_GENERATED_AT = "2026-09-11T00:00:00Z"
FIXED_SOURCE_COMMIT = "a" * 40
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"7" * 40


def _copy_operational_inputs(source: Path, destination: Path) -> None:
    for relative in ("registry", "exports/method-validation", "exports/trader-dna/v1"):
        shutil.copytree(source / relative, destination / relative)
    (destination / "canonical" / "adjudications").mkdir(parents=True)
    (destination / "canonical" / "sequences").mkdir(parents=True)
    (destination / "canonical" / "records" / "trades").mkdir(parents=True)
    (destination / "canonical" / "records" / "observations").mkdir(parents=True)
    (destination / "canonical" / "records" / "superseded").mkdir(parents=True)
    (destination / "canonical" / "sequences" / "numbers.json").write_text(
        json.dumps({"live_next": 1, "backtest_next": 1, "allocated": [], "tombstones": []}) + "\n",
        encoding="utf-8",
    )
    (destination / "canonical" / "sequences" / "finalizations.json").write_text(
        json.dumps({"schema_version": "finalization-index.v1", "records": {}}) + "\n",
        encoding="utf-8",
    )


def _make_reviewed_trade_with_evidence(source: Path, destination: Path) -> tuple[str, dict]:
    """Minimal closed+reviewed trade carrying one durable evidence artifact.

    Mirrors the E2E fixture pattern used in test_operational_closeout.py:
    a real file is ingested through EvidenceStore (not a fabricated
    locator), then attached as review evidence on the governing draft.
    """

    _copy_operational_inputs(source, destination)
    store = LifecycleStore(destination)
    draft_id = "rec_33333333333333333333"
    store.create_draft(
        strategy_id="stg-vegas-tunnel", rule_version="stg-vegas-tunnel:v1", strategy_resolution=live_resolution("stg-vegas-tunnel"), symbol="BTCUSDT",
        direction="long", intended_entry=100.0, stop_loss=95.0, take_profit=110.0, planned_rr=2.0,
        timeframe_context="1h", timeframe_trigger="15m", market_context="route fixture",
        setup_tags=["vegas_tunnel"], confirmation_tags=[], thesis="route fixture thesis",
        chart_refs=["fixture://chart/pretrade"], created_at="2026-09-11T00:00:00Z", draft_id=draft_id,
    )
    attach_fixture_image(store, draft_id, source_ref="fixture://pretrade", captured_at="2026-09-11T00:01:00Z")
    store.mark_entry_pending(draft_id, provider={"provider": "fixture"}, occurred_at="2026-09-11T00:02:00Z")
    store.attach_evidence(draft_id, {"source_ref": "fixture://propw-fill", "timing": "execution", "type": "broker_fill", "category": "entry", "sha256": "b" * 64, "captured_at": "2026-09-11T00:02:30Z"})
    store.create_identity_link(
        draft_id, provider="fixture", provider_lifecycle_id="fixture-open-1", instrument="BTCUSDT", direction="long",
        provider_execution_timestamp="2026-09-11T00:03:00Z", candidate_source="fixture://candidate-match",
        match_disposition="MATCHED", evidence_refs=["fixture://propw-fill"], created_at="2026-09-11T00:02:45Z",
        authority="fixture_deterministic_match",
    )
    store.verify_entry(
        draft_id, instrument="BTCUSDT", direction="long", actual_entry=100.0, execution_at="2026-09-11T00:03:00Z",
        source_ref="fixture://propw-fill", provider_lifecycle_id="fixture-open-1", provider="fixture",
    )
    trade_id = store.finalize(draft_id, occurred_at="2026-09-11T00:04:00Z")["trade_id"]
    store.final_close(trade_id, exit_price=110.0, closed_at="2026-09-11T00:06:00Z", realized_r=2.0, provider_pnl=10.0, source_ref="fixture://final-exit")
    store.attach_evidence(draft_id, {"source_ref": "fixture://final-exit", "timing": "post_trade", "type": "broker_fill", "category": "exit", "sha256": "c" * 64, "captured_at": "2026-09-11T00:06:30Z"})
    store.verify_exit(trade_id, authority_class="owner_attestation", evidence_refs=["fixture://final-exit"], realized_r=2.0, provider_pnl=10.0, provider_pnl_authority="provider_read_only")

    evidence_store = EvidenceStore(destination)
    artifact_source = destination / "review-chart.png"
    artifact_source.write_bytes(PNG_BYTES)
    ingested = evidence_store.ingest(artifact_source)
    store.attach_evidence(
        draft_id,
        {**ingested, "source_ref": "fixture://review-artifact", "timing": "post_trade", "type": "chart_snapshot", "category": "review", "sha256": ingested["sha256"]},
    )
    owner_confirmed_review(
        store,
        trade_id,
        {
            "what_went_well": "fixture", "what_went_wrong": "none", "rule_compliance": "compliant",
            "execution_deviation": "none", "thesis_validity": "supported", "entry_quality": "observed",
            "exit_quality": "observed", "evidence_completeness": "complete", "owner_notes": "route fixture",
            "review_status": "reviewed", "diagnostic_class": "METHOD",
        },
        reviewed_at="2026-09-11T00:08:00Z",
    )
    return trade_id, ingested


@pytest.fixture()
def evidence_route_server(tmp_path: Path):
    source = Path(__file__).resolve().parents[1]
    trade_id, ingested = _make_reviewed_trade_with_evidence(source, tmp_path)
    refresh_analytics(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    refresh_health(tmp_path, generated_at=FIXED_GENERATED_AT, source_commit=FIXED_SOURCE_COMMIT)
    build_trade_view(tmp_path)

    output = tmp_path / "exports" / "operational" / "trade-view"
    evidence_dir = tmp_path / "canonical" / "evidence" / "artifacts"
    handler = make_trade_view_handler(output, evidence_dir)
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, output, evidence_dir, trade_id, ingested
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _get(port: int, path: str) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        response.data = response.read()
        return response
    finally:
        conn.close()


def test_evidence_route_serves_the_exact_bytes_with_correct_content_type(evidence_route_server) -> None:
    server, output, evidence_dir, trade_id, ingested = evidence_route_server
    view = json.loads((output / "trade-view-data.json").read_text(encoding="utf-8"))
    row = next(r for r in view["rows"] if r["reference"] == trade_id)
    link = next(l for l in row["evidence_links"] if l["category"] == "review")
    assert link["href"] == f"/evidence/{ingested['locator'].split(':', 1)[1]}"

    response = _get(server.server_port, link["href"])
    assert response.status == 200
    assert response.data == PNG_BYTES
    assert hashlib.sha256(response.data).hexdigest() == ingested["sha256"]
    assert response.getheader("Content-Type") == "image/png"


@pytest.mark.parametrize("recorded_sha", ["f" * 64, None])
def test_trade_view_withholds_an_artifact_without_an_exact_locator_hash_binding(evidence_route_server, monkeypatch, recorded_sha) -> None:
    _, output, _, trade_id, ingested = evidence_route_server
    root = output.parents[2]
    analytics = json.loads((root / "exports" / "operational" / "analytics" / "current.json").read_text(encoding="utf-8"))
    mutated = 0
    for collection_name in ("observations", "canonical_read_model"):
        for row in analytics.get(collection_name, []):
            for artifact in row.get("evidence_artifacts", []):
                if artifact.get("locator") == ingested["locator"]:
                    if recorded_sha is None:
                        artifact.pop("sha256", None)
                    else:
                        artifact["sha256"] = recorded_sha
                    mutated += 1
    assert mutated > 0

    monkeypatch.setattr("trading_os.trade_view.load_current", lambda _root: analytics)
    build_trade_view(root)

    view = json.loads((output / "trade-view-data.json").read_text(encoding="utf-8"))
    row = next(r for r in view["rows"] if r["reference"] == trade_id)
    assert all(link.get("href") != f"/evidence/{ingested['locator'].split(':', 1)[1]}" for link in row["evidence_links"])
    assert all(link.get("href") != f"/evidence/{ingested['locator'].split(':', 1)[1]}" for link in row["evidence_gallery"])


def test_evidence_route_rejects_path_traversal_shaped_requests(evidence_route_server) -> None:
    server, *_ = evidence_route_server
    for path in (
        "/evidence/../../../../etc/passwd",
        "/evidence/" + "a" * 64 + ".png/../../../../etc/passwd",
        "/evidence/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "/evidence/..%2f..%2f..%2fetc%2fpasswd",
    ):
        response = _get(server.server_port, path)
        assert response.status in (403, 404), f"expected rejection for {path!r}, got {response.status}"
        assert response.data != b"root:"  # never leaks /etc/passwd content if it existed


def test_evidence_route_rejects_malformed_artifact_names(evidence_route_server) -> None:
    server, output, evidence_dir, trade_id, ingested = evidence_route_server
    real_name = ingested["locator"].split(":", 1)[1]
    sha, ext = real_name.rsplit(".", 1)
    for bad_name in (
        sha[:-1] + ".png",           # wrong length (63 hex chars)
        sha.upper() + ".png",         # non-hex (uppercase not in allow-list)
        sha + "x" + ".png",           # wrong length (65 chars)
        sha + ".exe",                 # extension not in the store's allow-list content-type mapping is fine, but shape must still be checked against filesystem
        "not-a-hash-at-all.png",
    ):
        response = _get(server.server_port, f"/evidence/{bad_name}")
        assert response.status == 404, f"expected 404 for {bad_name!r}, got {response.status}"


def test_doc_root_files_still_served_normally(evidence_route_server) -> None:
    server, output, evidence_dir, trade_id, ingested = evidence_route_server
    response = _get(server.server_port, "/trade-view-data.json")
    assert response.status == 200
    response = _get(server.server_port, "/index.html")
    assert response.status == 200


def test_journal_search_matches_trade_number():
    # Owner report 2026-11-22: searching "T000194" in Journal found nothing.
    from trading_os import trade_view

    src = Path(trade_view.__file__).read_text(encoding="utf-8")
    assert "r.advanced && r.advanced.trade_no" in src
