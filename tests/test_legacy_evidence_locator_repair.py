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
from trading_os.operational_lifecycle import LifecycleError, LifecycleStore
from trading_os.strategy_health import refresh_health
from trading_os.trade_view import build_trade_view, make_trade_view_handler

REPO_ROOT = Path(__file__).resolve().parents[1]
TRADE_ID = "trd_01a0609c-e613-7ee7-b7e7-33a9516c6e47"
DRAFT_ID = "rec_85f156beec982551a5b0"
EVIDENCE = (
    {"source_ref": "assets/2026-08-24_BTCUSDCP_L_vt-long_chart.jpg", "sha256": "34a8a8eaaa7999de88b099574cea6bb9d0d663f95e37b4143c89d200575c32c3", "category": "pre_trade_context", "filename": "2026-08-24_BTCUSDCP_L_vt-long_chart.jpg"},
    {"source_ref": "assets/Pasted image 20260824133843.png", "sha256": "0387d7aac9c3c263771e1b67fdc98a7f82de28b7c3a3f1a28a49994aa4160de2", "category": "entry", "filename": "Pasted image 20260824133843.png"},
)


def _copy_operational_inputs(destination: Path) -> None:
    for relative in ("registry", "exports/method-validation", "exports/trader-dna/v1"):
        shutil.copytree(REPO_ROOT / relative, destination / relative)
    for relative in ("canonical/adjudications", "canonical/sequences", "canonical/records/trades", "canonical/records/observations", "canonical/records/superseded"):
        (destination / relative).mkdir(parents=True)
    (destination / "canonical/sequences/numbers.json").write_text(json.dumps({"live_next": 1, "backtest_next": 1, "allocated": [], "tombstones": []}) + "\n", encoding="utf-8")
    (destination / "canonical/sequences/finalizations.json").write_text(json.dumps({"schema_version": "finalization-index.v1", "records": {}}) + "\n", encoding="utf-8")


def _copy_t000001_legacy_state(destination: Path) -> bytes:
    lifecycle = destination / "canonical/lifecycle"
    lifecycle.mkdir(parents=True)
    events = []
    recorded_at = "2026-09-22T04:00:00Z"
    events.append({
        "schema_version": "canonical-lifecycle-event.v2", "event_id": "lev_test_draft", "idempotency_key": "draft:test-t000001",
        "event_type": "draft_created", "occurred_at": recorded_at, "recorded_at": recorded_at,
        "payload": {"draft_id": DRAFT_ID, "record_id": DRAFT_ID},
    })
    finalized_at = "2026-09-22T04:01:00Z"
    events.append({
        "schema_version": "canonical-lifecycle-event.v2", "event_id": "lev_test_finalized", "idempotency_key": "finalized:test-t000001",
        "event_type": "canonical_finalized", "occurred_at": finalized_at, "recorded_at": finalized_at,
        "payload": {"draft_id": DRAFT_ID, "trade_id": TRADE_ID, "trade_no": "T000001", "legacy_compatibility": True},
    })
    for index, item in enumerate(EVIDENCE):
        at = f"2026-09-22T04:0{index + 2}:00Z"
        events.append({
            "schema_version": "canonical-lifecycle-event.v2", "event_id": f"lev_test_evidence_{index}",
            "idempotency_key": f"evidence:test-t000001:{index}", "event_type": "evidence_attached",
            "occurred_at": at, "recorded_at": at,
            "payload": {"draft_id": DRAFT_ID, "evidence": {
                "source_ref": item["source_ref"], "category": item["category"], "sha256": item["sha256"],
                "locator": "artifact:full", "type": "chart_snapshot" if index == 0 else "manual_note",
                "timing": "post_trade" if index == 0 else "execution", "method": "chart_snapshot" if index == 0 else "manual_note",
                "recorded_at": at, "linked_draft_id": DRAFT_ID, "linked_trade_id": None,
            }},
        })
    original_events = ("".join(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n" for event in events)).encode("utf-8")
    (lifecycle / "events.jsonl").write_bytes(original_events)
    (lifecycle / "identity-links.jsonl").write_bytes(b"")
    source_trade = REPO_ROOT / "canonical/records/trades" / f"{TRADE_ID}.json"
    shutil.copy2(source_trade, destination / "canonical/records/trades" / source_trade.name)
    assets = destination / "assets"
    assets.mkdir()
    from trading_os.legacy_source import read_legacy_bytes
    for item in EVIDENCE:
        (assets / item["filename"]).write_bytes(read_legacy_bytes(REPO_ROOT, item["source_ref"]))
    return original_events


def _correct_t000001_locators(root: Path, original_events: bytes) -> list[dict]:
    evidence_store = EvidenceStore(root)
    lifecycle = LifecycleStore(root)
    corrected = []
    for index, item in enumerate(EVIDENCE):
        path = root / item["source_ref"]
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual_sha == item["sha256"]
        ingested = evidence_store.ingest(path, original_filename=item["filename"])
        event = lifecycle.correct_evidence_locator(
            DRAFT_ID,
            source_ref=item["source_ref"],
            sha256=item["sha256"],
            category=item["category"],
            prior_locator="artifact:full",
            locator=ingested["locator"],
            reason="Bind the exact preserved legacy image bytes to their verified EvidenceStore content address.",
            recorded_at=f"2026-10-10T08:00:0{index}Z",
        )
        corrected.append({**item, **ingested, "event_id": event["event_id"]})
    assert (root / "canonical/lifecycle/events.jsonl").read_bytes().startswith(original_events)
    return corrected


def test_t000001_locator_correction_is_append_only_and_trade_view_serves_original_images(tmp_path: Path) -> None:
    _copy_operational_inputs(tmp_path)
    original_events = _copy_t000001_legacy_state(tmp_path)
    orphan = tmp_path / "assets/unreferenced-local-image.png"
    orphan_bytes = b"unreferenced local evidence remains outside the canonical view"
    orphan.write_bytes(orphan_bytes)
    corrected = _correct_t000001_locators(tmp_path, original_events)

    lifecycle = LifecycleStore(tmp_path)
    record = lifecycle.current()["records"][TRADE_ID]
    assert record["trade_no"] == "T000001"
    assert lifecycle.resolve_draft_id("T000001") == DRAFT_ID
    for item in corrected:
        evidence = next(e for e in record["evidence"] if e.get("source_ref") == item["source_ref"] and e.get("category") == item["category"])
        assert evidence["locator"] == item["locator"]
        assert evidence["sha256"] == item["sha256"]

    refresh_analytics(tmp_path, generated_at="2026-10-10T08:01:00Z", source_commit="a" * 40)
    refresh_health(tmp_path, generated_at="2026-10-10T08:01:00Z", source_commit="a" * 40)
    build_trade_view(tmp_path)
    output = tmp_path / "exports/operational/trade-view"
    data = json.loads((output / "trade-view-data.json").read_text(encoding="utf-8"))
    row = next(r for r in data["rows"] if r.get("reference") == TRADE_ID)
    links = row["evidence_links"]
    expected_hrefs = {f"/evidence/{item['locator'].removeprefix('artifact:')}" for item in corrected}
    assert expected_hrefs <= {link.get("href") for link in links}, {"expected": expected_hrefs, "actual": links}
    assert orphan.name not in json.dumps(links)

    evidence_dir = tmp_path / "canonical/evidence/artifacts"
    server = HTTPServer(("127.0.0.1", 0), make_trade_view_handler(output, evidence_dir))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for item in corrected:
            expected_href = f"/evidence/{item['locator'].removeprefix('artifact:')}"
            link = next(link for link in links if link.get("href") == expected_href)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", link["href"])
            response = connection.getresponse()
            served = response.read()
            connection.close()
            assert response.status == 200
            assert served == (tmp_path / item["source_ref"]).read_bytes()
            assert hashlib.sha256(served).hexdigest() == item["sha256"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_new_lifecycle_evidence_uses_non_legacy_locator_and_rejects_artifact_full(tmp_path: Path) -> None:
    _copy_operational_inputs(tmp_path)
    lifecycle = LifecycleStore(tmp_path)
    draft = lifecycle.create_draft(
        strategy_id="stg-vegas-tunnel", rule_version="stg-vegas-tunnel:v1",
        strategy_resolution=live_resolution("stg-vegas-tunnel"), symbol="BTCUSDT", direction="long",
        intended_entry=100.0, stop_loss=95.0, take_profit=110.0, planned_rr=2.0,
        timeframe_context="1h", timeframe_trigger="15m", market_context="locator fixture",
        setup_tags=["vegas_tunnel"], confirmation_tags=[], thesis="locator fixture",
        chart_refs=["fixture://chart"], created_at="2026-09-11T00:00:00Z",
        draft_id="rec_44444444444444444444",
    )
    sha256 = hashlib.sha256(b"metadata-only source").hexdigest()
    event = lifecycle.attach_evidence(draft["draft_id"], {
        "source_ref": "fixture://metadata-only", "timing": "pre_trade", "type": "manual_note",
        "category": "pre_trade_context", "sha256": sha256,
    })
    evidence = event["payload"]["evidence"]
    assert evidence["locator"] == f"sha256:{sha256}"
    assert evidence["locator"] != "artifact:full"

    with pytest.raises(LifecycleError, match="legacy-only locator"):
        lifecycle.attach_evidence(draft["draft_id"], {
            "source_ref": "fixture://bad-locator", "timing": "pre_trade", "type": "chart_snapshot",
            "category": "pre_trade_context", "sha256": "f" * 64, "locator": "artifact:full",
        })


def test_attachment_rejects_a_valid_artifact_bound_to_a_different_sha256(tmp_path: Path) -> None:
    _copy_operational_inputs(tmp_path)
    lifecycle = LifecycleStore(tmp_path)
    draft = lifecycle.create_draft(
        strategy_id="stg-vegas-tunnel", rule_version="stg-vegas-tunnel:v1",
        strategy_resolution=live_resolution("stg-vegas-tunnel"), symbol="BTCUSDT", direction="long",
        intended_entry=100.0, stop_loss=95.0, take_profit=110.0, planned_rr=2.0,
        timeframe_context="1h", timeframe_trigger="15m", market_context="sha binding fixture",
        setup_tags=["vegas_tunnel"], confirmation_tags=[], thesis="sha binding fixture",
        chart_refs=["fixture://chart"], created_at="2026-09-11T00:00:00Z",
        draft_id="rec_55555555555555555555",
    )
    source = tmp_path / "valid-but-different.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nvalid artifact bytes")
    ingested = EvidenceStore(tmp_path).ingest(source)
    original_event_count = len(lifecycle._read_events())

    with pytest.raises(LifecycleError, match="not verified against its sha256"):
        lifecycle.attach_evidence(draft["draft_id"], {
            "source_ref": "fixture://wrong-evidence-binding", "timing": "execution", "type": "chart_snapshot",
            "category": "entry", "sha256": "a" * 64, "locator": ingested["locator"],
        })

    assert len(lifecycle._read_events()) == original_event_count


def test_same_sha_corrections_for_distinct_sources_have_distinct_idempotency(tmp_path: Path) -> None:
    _copy_operational_inputs(tmp_path)
    lifecycle = LifecycleStore(tmp_path)
    draft = lifecycle.create_draft(
        strategy_id="stg-vegas-tunnel", rule_version="stg-vegas-tunnel:v1",
        strategy_resolution=live_resolution("stg-vegas-tunnel"), symbol="BTCUSDT", direction="long",
        intended_entry=100.0, stop_loss=95.0, take_profit=110.0, planned_rr=2.0,
        timeframe_context="1h", timeframe_trigger="15m", market_context="duplicate correction fixture",
        setup_tags=["vegas_tunnel"], confirmation_tags=[], thesis="duplicate correction fixture",
        chart_refs=["fixture://chart"], created_at="2026-09-11T00:00:00Z",
        draft_id="rec_66666666666666666666", recorded_at="2026-09-11T00:00:00Z",
    )
    source = tmp_path / "shared.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nshared evidence bytes")
    ingested = EvidenceStore(tmp_path).ingest(source)
    source_refs = ("fixture://chart-a", "fixture://chart-b")
    for index, source_ref in enumerate(source_refs):
        lifecycle.attach_evidence(draft["draft_id"], {
            "source_ref": source_ref, "timing": "execution", "type": "chart_snapshot",
            "category": "entry", "sha256": ingested["sha256"],
        }, idempotency_key=f"fixture-evidence:{index}", recorded_at=f"2026-09-11T00:0{index + 1}:00Z")

    events = []
    for index, source_ref in enumerate(source_refs):
        events.append(lifecycle.correct_evidence_locator(
            draft["draft_id"], source_ref=source_ref, sha256=ingested["sha256"], category="entry",
            prior_locator=f"sha256:{ingested['sha256']}", locator=ingested["locator"],
            reason="Bind the metadata-only attachment to verified bytes.",
            recorded_at=f"2026-10-10T08:10:0{index}Z",
        ))

    assert events[0]["event_id"] != events[1]["event_id"]
    assert events[1]["payload"]["source_ref"] == source_refs[1]
    replay = lifecycle.correct_evidence_locator(
        draft["draft_id"], source_ref=source_refs[0], sha256=ingested["sha256"], category="entry",
        prior_locator=f"sha256:{ingested['sha256']}", locator=ingested["locator"],
        reason="Bind the metadata-only attachment to verified bytes.",
        recorded_at="2026-10-10T08:10:00Z",
    )
    assert replay["event_id"] == events[0]["event_id"]
