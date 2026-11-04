"""The console's timeline of a case is the regulator view's record of it, number for number."""

import json
from pathlib import Path

import pytest

from api.controllers.cases import _trace_from
from api.sse.journal import SseEnvelope
from sentinel.typologies.console import events_from_ledger, latest_run

RUN = Path(__file__).resolve().parents[3] / "demo" / "regulator_run"


def _trace(case_id: str):
    ledger = [json.loads(line) for line in open(RUN / "audit" / "ledger.jsonl", encoding="utf8") if line.strip()]
    answer = json.loads((RUN / "cases" / f"{case_id}.json").read_text(encoding="utf8"))
    events = events_from_ledger(latest_run([e for e in ledger if e["case_id"] == case_id]), answer)
    envelopes = [SseEnvelope(v=1, seq=i, run_id="r", case_id=case_id, type=t, at="", step=s, payload=p)
                 for i, (t, p, s) in enumerate(events)]
    return answer, _trace_from(case_id, "r", envelopes)


@pytest.mark.parametrize("case_id", sorted(p.stem for p in (RUN / "cases").glob("*.json")))
def test_the_trajectory_ends_on_the_answer(case_id: str) -> None:
    answer, trace = _trace(case_id)
    assert trace.trajectory[0] == 0.5
    assert trace.trajectory[-1] == pytest.approx(answer["case"]["fraud_probability"])


def test_the_model_score_and_the_decision_are_the_only_moves() -> None:
    _, trace = _trace("HHG-015")
    moves = [p.feature for p in trace.postings if p.p_after != p.p_before]
    assert moves == ["decision_model", "policy_decision"]
    assert [round(x, 2) for x in dict.fromkeys(trace.trajectory)] == [0.5, 0.81, 0.69]
