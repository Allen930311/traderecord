from pathlib import Path
import json
from trading_os.writer import CanonicalWriter
from trading_os.auditor import audit_trade
from scripts.canonical_migration.execution_verification import validate_execution_contract


def test_t000001_durable_execution_attestation_chain_validates():
    root = Path(__file__).resolve().parents[1]
    trade = json.loads((root / "canonical" / "records" / "trades" / "trd_01a0609c-e613-7ee7-b7e7-33a9516c6e47.json").read_text(encoding="utf-8"))
    revisions = [json.loads(line) for line in (root / "canonical" / "adjudications" / "phase4b-execution-revisions.jsonl").read_text(encoding="utf-8").splitlines() if line]
    report = validate_execution_contract(trade, durable_revisions=revisions)
    assert report.ok, report.errors
    assert trade["actual"]["execution_verification"]["entry"]["adjudication_revision_ref"] == "adjv1-execution-20260903-btc-85f156be-identity-migration"

def test_source_to_canonical_to_audit(tmp_path: Path):
    record={"schema_version":"trade.v1","record_id":"rec_22222222222222222222","record_kind":"trade_candidate","trade_id":None,"observation_id":None,"trade_no":None,"type":"live","classification_status":"resolved","execution_environment":"unknown","recording_timing":"unknown","legacy_source":{},"identity":{},"strategy":{"strategy_id":"stg-example-b","rule_version":"stg-example-b:v1","variant":"e2e","setup_tags":[]},"plan":{"timeframe_trigger":"15m"},"actual":{"execution_status":"entered","position_status":"open"},"result":{"outcome":"loss"},"evidence":[{"source_ref":"trades/x.md","timing":"pre_trade"}],"review":{},"dedupe":{},"analytics":{},"raw_legacy":{},"provenance":{"source":"test"}}
    record["dedupe"]={"status":"canonical"}; record["evidence"][0].update({"method":"manual_note","locator":"body:1","sha256":"abc","field":"plan.timeframe_trigger","assertion":"timeframe_gte","assertion_value":"15m"}); record["actual"]["execution_verification"]={"schema_version":"execution-verification.v1","entry":{"state":"confirmed","basis":"human_attestation","attestation_support":"artifact_supported","evidence_refs":["trades/x.md"],"adjudication_revision_ref":"rev-test"},"exit":{"state":"not_applicable","basis":"none","attestation_support":None,"evidence_refs":[],"adjudication_revision_ref":None}}; writer=CanonicalWriter(tmp_path); assert writer.create_candidate(record)["validation_errors"]==[]
    (tmp_path/"canonical/adjudications").mkdir(parents=True)
    (tmp_path/"canonical/adjudications/phase4b-test-revisions.jsonl").write_text(json.dumps({"actor":"human:test","after":{"execution_claim":{"leg":"entry","state":"confirmed","basis":"human_attestation","attestation_support":"artifact_supported","evidence_refs":["trades/x.md"]}},"authority_class":"human_adjudication_required","before":{"execution_claim":{"leg":"entry","state":"unverified","basis":"none","attestation_support":None,"evidence_refs":[]}},"decision_domain":"execution","decision_type":"execution_verification_claim","evidence_refs":["trades/x.md"],"record_ids":["rec_22222222222222222222"],"reason":"test","reviewer":"test","revision_id":"rev-test","rule_id":"execution.live-fill-confirmation","rule_version":"1.0","schema_version":"phase4b.adjudication-revision.v1","status":"resolved","previous_revision_id":None,"reopens_revision_id":None,"supersedes_revision_id":None,"recorded_at":"2026-01-01T00:00:00Z"})+"\n",encoding="utf-8")
    from trading_os.sequence import NumberAllocator
    writer.finalize_trade("rec_22222222222222222222","tester",NumberAllocator(tmp_path))
    trade=next((tmp_path/"canonical/records/trades").glob("*.json")); trade=__import__("json").loads(trade.read_text())
    version={"strategy_id":"stg-example-b","version_id":"stg-example-b:v1","rules":[{"rule_id":"tf","required":True,"operator":"timeframe_gte","operands":{"field":"plan.timeframe_trigger","value":"15m"},"evidence_requirements":["manual_note"]}]}
    result=audit_trade(trade,{"strategy_id":"stg-example-b"},version)
    assert result["rule_matrix"][0]["status"]=="PASS" and result["outcome_used"] is False
