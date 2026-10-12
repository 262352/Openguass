from types import SimpleNamespace

from app.chain.plan_evidence import collect_plan_evidence


def test_collect_plan_evidence_extracts_plan_shape(monkeypatch):
    payload = '[{"Plan":{"Node Type":"Index Scan","Plan Rows":1,"Total Cost":8.3}}]'
    monkeypatch.setattr("app.chain.plan_evidence.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=""))
    result = collect_plan_evidence("twitter")
    assert result["status"] == "observed"
    assert result["uses_index"] is True
    assert result["estimated_rows"] == 1
