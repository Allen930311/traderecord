import json
import pytest
from pathlib import Path
from trading_os.writer import CanonicalWriter
from trading_os.adjudication import reduce_decisions

def candidate():
    return {"schema_version":"trade.v1","record_id":"rec_00000000000000000000","record_kind":"trade_candidate","trade_id":None,"observation_id":None,"trade_no":None,"type":"live","classification_status":"resolved","execution_environment":"unknown","recording_timing":"unknown","legacy_source":{},"identity":{},"strategy":{},"plan":{},"actual":{"execution_status":"entered","position_status":"open"},"result":{"outcome":"unknown"},"evidence":[],"review":{},"dedupe":{},"analytics":{},"raw_legacy":{},"provenance":{}}

def test_writer_finalize_and_revision(tmp_path: Path):
    writer=CanonicalWriter(tmp_path); c=candidate(); c["dedupe"]={"status":"canonical"}; c["actual"]["execution_verification"]={"schema_version":"execution-verification.v1","entry":{"state":"confirmed","basis":"human_attestation","attestation_support":"artifact_supported","evidence_refs":["assets/chart.png"],"adjudication_revision_ref":"rev-test"},"exit":{"state":"not_applicable","basis":"none","attestation_support":None,"evidence_refs":[],"adjudication_revision_ref":None}}; draft=writer.create_candidate(c); assert draft["validation_errors"] == []
    (tmp_path/"canonical/adjudications").mkdir(parents=True)
    (tmp_path/"canonical/adjudications/phase4b-test-revisions.jsonl").write_text(json.dumps({"actor":"human:test","after":{"execution_claim":{"leg":"entry","state":"confirmed","basis":"human_attestation","attestation_support":"artifact_supported","evidence_refs":["assets/chart.png"]}},"authority_class":"human_adjudication_required","before":{"execution_claim":{"leg":"entry","state":"unverified","basis":"none","attestation_support":None,"evidence_refs":[]}},"decision_domain":"execution","decision_type":"execution_verification_claim","evidence_refs":["assets/chart.png"],"record_ids":["rec_00000000000000000000"],"reason":"test","reviewer":"test","revision_id":"rev-test","rule_id":"execution.live-fill-confirmation","rule_version":"1.0","schema_version":"phase4b.adjudication-revision.v1","status":"resolved","previous_revision_id":None,"reopens_revision_id":None,"supersedes_revision_id":None,"recorded_at":"2026-01-01T00:00:00Z"})+"\n",encoding="utf-8")
    writer.attach_evidence("rec_00000000000000000000", {"source_ref":"assets/chart.png","method":"chart_snapshot","timing":"pre_trade","locator":"body:1","sha256":"abc"})
    from trading_os.sequence import NumberAllocator
    trade_id=writer.finalize_trade("rec_00000000000000000000", "tester", NumberAllocator(tmp_path))
    retry_id=writer.finalize_trade("rec_00000000000000000000", "tester", NumberAllocator(tmp_path))
    assert retry_id == trade_id
    assert len(list((tmp_path/"canonical"/"records"/"trades").glob("*.json"))) == 1
    with pytest.raises(ValueError, match="direct canonical revisions are retired"):
        writer.update_revision(trade_id, {"review":{"state":"partial"}}, "tester")

def test_reducer_is_append_only_and_latest_wins():
    state=reduce_decisions([{"adjudication_id":"a","decided_at":"2026-01-01","subject_refs":["src"],"after":{"type":"live"}}, {"adjudication_id":"b","decided_at":"2026-01-02","subject_refs":["src"],"after":{"type":None}}])
    assert state["src"]["type"] is None
