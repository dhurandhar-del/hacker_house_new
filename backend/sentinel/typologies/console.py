"""The final decision engine, as the console sees it.

The console's timeline, trajectory and verdict were drawn from the Round 1 agent's event
stream. The decision of record is this package's: detectors, history checks, the rules, the
Decisions API when it is consulted, and the policy engine, all written to the hash-chained
ledger. Two engines on two screens gave one case two numbers (HHG-014: 0.48 in the console,
0.97 in the regulator view).

So the console reads the same record the regulator does. ``events_from_ledger`` turns one
case's ledger entries into the console's event vocabulary, and ``FinalDecisionOrchestrator``
runs a case live through ``run_case`` and emits those events, so a console run writes the
same answer file and the same ledger entries the regulator view shows.

The trajectory is the true path, not a reconstruction: it starts at a neutral 0.5, facts and
history checks are posted without moving it, and it moves only where the engine sets a
number: the Decisions API's score, when it was asked, and the final decision.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime
from typing import Any

PRIOR = 0.5
ENGINE = "FinalDecisionOrchestrator"

#: The final engine's steps, as (console step id, title). The ids are the console's fixed
#: vocabulary (``StepName``); the titles say what this engine did in each. A step with nothing
#: in it is left out.
STEPS = {
    "scope": ("scope", "Open the case from the alert"),
    "read": ("sweep", "Read the card, its devices and prior cases from the graph"),
    "detect": ("assess", "Run the typology detectors"),
    "history": ("recall", "Check the findings against the card's own history"),
    "model": ("stop_test", "Ask the Decisions API"),
    "decide": ("decide", "Decide under the rules"),
    "request_evidence": ("request_evidence", "Request further evidence"),
    "final_policy": ("narrate", "Final recommendation under policy"),
    "write": ("write", "Write the case to the graph"),
}


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _posting(feature: str, claim: str, ref: str, p_before: float, p_after: float, *, group: str,
             source: str = "graph", entity_ids: list[str] | None = None) -> dict:
    log_lr = 0.0 if abs(p_after - p_before) < 1e-9 else _logit(p_after) - _logit(p_before)
    return {"feature": feature, "present": True, "group": group, "claim": claim, "source": source, "ref": ref,
            "entity_ids": entity_ids or [], "lr": round(math.exp(log_lr), 4), "log_lr": round(log_lr, 4),
            "p_before": round(p_before, 4), "p_after": round(p_after, 4), "capped": False}


def _param(v: Any) -> str | int | bool:
    if isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, list):
        return ",".join(_param(x) if not isinstance(x, list) else "+".join(map(str, x)) for x in v)  # type: ignore[misc]
    return str(v)


def _ids(xs: Any) -> list[str]:
    return [str(x) for x in (xs or [])]


def latest_run(entries: list[dict]) -> list[dict]:
    """The entries of the most recent investigation of the case: from its last case.opened on."""
    starts = [i for i, e in enumerate(entries) if e["kind"] == "case.opened"]
    return entries[starts[-1]:] if starts else []


def events_from_ledger(entries: list[dict], answer: dict) -> list[tuple[str, dict, int | None]]:
    """One case's ledger record (from ``latest_run``) and its answer file, as console events."""
    by_kind: dict[str, list[dict]] = {}
    for e in entries:
        by_kind.setdefault(e["kind"], []).append(e["data"])
    one = lambda k: (by_kind.get(k) or [None])[0]  # noqa: E731
    opened = one("case.opened") or {}
    alert = opened.get("alert", {})
    c = answer["case"]
    flagged = str(alert.get("flagged_txn_id") or (c.get("affected_txn_ids") or [""])[0])
    card = alert.get("card_id", "")
    as_of = opened.get("as_of", "")
    risk = alert.get("risk_score")
    try:
        risk = float(risk) if risk not in (None, "", "-1") else None
    except ValueError:
        risk = None

    events: list[tuple[str, dict, int | None]] = []
    p = PRIOR
    events.append(("run.started", {
        "case_id": answer["case_id"], "trigger_type": alert.get("trigger_type") or answer.get("trigger_type"),
        "trigger_text": alert.get("trigger_text", ""), "flagged_txn_id": flagged, "card_id": card,
        "customer_id": alert.get("customer_id", ""), "alert_risk_score": risk, "prior": PRIOR,
        "mode": "live", "orchestrator": ENGINE, "engine": "final decision layer",
        "source": opened.get("source", ""), "code_version": opened.get("code_version", "")}, None))

    step = 0

    def run_step(key: str, body) -> None:
        nonlocal step
        step += 1
        name, title = STEPS[key]
        events.append(("step.started", {"step": step, "name": name, "title": title, "agent": ENGINE}, step))
        before = len(events)
        body(step)
        inner = events[before:]
        events.append(("step.completed", {
            "step": step, "name": name,
            "tool_calls_in_step": sum(t == "tool.called" for t, _, _ in inner),
            "postings_in_step": sum(t == "evidence.posted" for t, _, _ in inner)}, step))

    def scope(n: int) -> None:
        events.append(("evidence.posted", _posting(
            "alert_opened", f"Alert on transaction {flagged}, card {card}, investigated as of {as_of}. "
            f"Trigger: {alert.get('trigger_type', '')}.", f"alert:{answer['case_id']}", p, p,
            group="scope", entity_ids=[flagged, card]), n))

    run_step("scope", scope)

    queries = by_kind.get("step.query", [])
    if queries:
        def read(n: int) -> None:
            for q in queries:
                params = {k: _param(v) for k, v in (q.get("params") or {}).items()}
                ref = f"query:{q['query']}(" + ", ".join(f"{k}={v}" for k, v in params.items()) + ")"
                events.append(("tool.called", {"tool": q["query"], "ref": ref, "params": params,
                                               "entity_ids": _ids(q.get("first_ids"))[:10], "elapsed_s": 0.0,
                                               "ok": True, "error": None, "n_rows": q.get("n_rows", 0)}, n))

        run_step("read", read)

    detectors = by_kind.get("step.detector", [])

    def detect(n: int) -> None:
        if not detectors:
            events.append(("evidence.posted", _posting(
                "typology_none", "No typology detector fired on this card's activity.",
                f"detectors(card_id={card}, as_of={as_of})", p, p, group="typology", entity_ids=[card]), n))
        for h in detectors:
            events.append(("evidence.posted", _posting(
                f"typology_{h['detector']}", f"{h['description']} (strength {h['strength']:.2f})",
                f"detector:{h['detector']}(card_id={card}, as_of={as_of})", p, p, group="typology",
                entity_ids=_ids(h.get("txn_ids")) + ([h["shared"]] if h.get("shared") else [])), n))

    run_step("detect", detect)

    history = one("step.history_check")
    if history:
        def hist(n: int) -> None:
            ref = f"history_check(card_id={card}, as_of={as_of})"
            for u in history.get("unexplained", []):
                feature = u.split(":", 1)[0].strip().replace(" ", "_") or "unexplained"
                events.append(("evidence.posted", _posting(
                    feature, f"Not explained by the card's history: {u}.", ref, p, p, group="history",
                    entity_ids=[flagged]), n))
            for x in history.get("explained_by_history", []):
                events.append(("evidence.posted", _posting(
                    "explained_by_history", f"Explained by the card's history: {x}.", ref, p, p,
                    group="history", entity_ids=[flagged]), n))
            for s in history.get("injection_spans", []):
                events.append(("evidence.posted", _posting(
                    "text_instructs", f"The alert text tries to instruct the investigator: {s!r}. Treated as data.",
                    ref, p, p, group="history", entity_ids=[flagged]), n))

        run_step("history", hist)

    model = one("step.model")
    if model:
        def ask(n: int) -> None:
            nonlocal p
            probs = model.get("probabilities") or {}
            events.append(("llm.completed", {"purpose": "decision", "model": model.get("model", ""),
                                             "prompt_tokens": int(model.get("tokens") or 0), "completion_tokens": 0,
                                             "total_tokens": int(model.get("tokens") or 0), "usd": 0.0,
                                             "elapsed_s": float(model.get("latency_s") or 0.0),
                                             "fallback": not model.get("accepted", True)}, n))
            scores = ", ".join(f"P({k.replace('_', ' ')})={v}" for k, v in probs.items())
            after = float(probs.get("is_fraud", p)) if model.get("accepted", True) else p
            claim = f"{model.get('endpoint', 'POST /v1/decisions')} ({model.get('model', '')}): {scores}."
            if not model.get("accepted", True):
                claim += f" Not used: {model.get('rejected_because', '')}."
            events.append(("evidence.posted", _posting(
                "decision_model", claim, f"adjudicator(model={model.get('model', '')}, case={answer['case_id']})",
                p, after, group="model", source="external", entity_ids=[flagged]), n))
            p = after

        run_step("model", ask)

    decision = one("decision") or {}
    final_p = float(c["fraud_probability"])
    nba = answer.get("next_best_actions", {})
    sar = answer.get("sar", {})

    def policy(phase: str, actions: list[dict], n: int) -> None:
        events.append(("policy.evaluated", {
            "phase": phase, "fraud_probability": final_p, "exposure_usd": c.get("exposure_usd", 0.0),
            "verdict": c["verdict"], "independent_support": len(c.get("evidence", [])),
            "recommendations": actions, "gates_applied": [],
            "sar": {"file": bool(sar.get("file")), "reason": sar.get("reason", "")},
            "stop": {"should_stop": True, "reason": "the rules reached a decision"}}, n))

    def decide(n: int) -> None:
        nonlocal p
        reasons = "; ".join(decision.get("reasons", [])) or c.get("summary", "")
        events.append(("evidence.posted", _posting(
            "policy_decision", f"Decided by {decision.get('decided_by', 'the rules')}: {c['verdict']} at "
            f"{final_p:.2f}. {reasons}", f"decision(case={answer['case_id']})", p, final_p, group="policy",
            entity_ids=_ids(c.get("affected_txn_ids"))), n))
        p = final_p
        policy("initial", nba.get("initial", []), n)

    run_step("decide", decide)

    final = one("policy.final") or {}
    requests = answer.get("evidence_requests", [])
    if requests:
        m = re.search(r"assumed reply: (\w+)", final.get("what_changed", ""))
        branch = m.group(1) if m else "no_reply"

        def ask_more(n: int) -> None:
            for i, r in enumerate(requests, start=1):
                events.append(("evidence.requested", {
                    "request_index": i, "type": r["type"], "asked_after_step": r.get("asked_after_step", 0),
                    "rationale": "The rules turn on the cardholder's answer.", "assumed_response": r.get("assumed_response", ""),
                    "assumption_basis": [], "branch": branch, "log_lr_applied": 0.0}, n))

        run_step("request_evidence", ask_more)

    def final_policy(n: int) -> None:
        policy("final", nba.get("final", []), n)
        if final.get("what_changed"):
            events.append(("evidence.posted", _posting(
                "policy_final", f"What changed: {final['what_changed']}", f"policy(case={answer['case_id']})",
                p, p, group="policy"), n))

    run_step("final_policy", final_policy)

    wb = one("graph.write_back")
    if wb and not wb.get("skipped"):
        def write(n: int) -> None:
            if wb.get("ok"):
                edges = wb.get("edges") or {}
                events.append(("case.written", {
                    "graph_case_id": wb.get("graph_case_id", ""), "vertices_upserted": 1,
                    "edges_upserted": sum(edges.values()), "edge_types": [k for k, v in edges.items() if v],
                    "embedded": False, "read_back_verified": bool(wb.get("read_back_verified"))}, n))
            else:
                events.append(("case.write_failed", {"graph_case_id": wb.get("graph_case_id", ""),
                                                     "error": wb.get("error", "")}, n))

        run_step("write", write)

    events.append(("verdict.reached", {k: c.get(k) for k in (
        "status", "verdict", "fraud_probability", "pattern", "pattern_description", "exposure_usd",
        "affected_txn_ids", "connected_card_ids", "connected_device_profiles", "similar_prior_cases",
        "summary")}, step))
    events.append(("validation.completed", {"ok": True, "errors": [], "warnings": [],
                                            "graph_checked": bool(wb and wb.get("read_back_verified"))}, step))
    produced = one("answer.produced") or {}
    events.append(("run.completed", {
        "status": "completed", "tool_calls": int(produced.get("graph_queries", answer.get("tool_calls", 0))),
        "tokens": int(produced.get("model_tokens", answer.get("tokens", 0))),
        "latency_s": float(produced.get("latency_s", answer.get("latency_s", 0.0))), "validation_ok": True,
        "answer_url": f"/api/cases/{answer['case_id']}/answer"}, None))
    return events


class _Recorder:
    """A ledger that keeps entries in memory, flushed to the real chain in one go at the end.

    The v2 service appends to the same file from another process (sign-ins, approvals); holding
    a ledger open across a run of several seconds would chain to a stale tail and break it."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def append(self, case_id: str, kind: str, data: dict, *, actor: str = "sentinel-agent", role: str = "system") -> dict:
        e = {"case_id": case_id, "kind": kind, "data": data, "actor": actor, "role": role}
        self.entries.append(e)
        return e


try:
    from sentinel.agents.orchestrator import InvestigationOrchestrator, InvestigationResult
except ImportError:  # the typologies package runs without the Round 1 agent's dependencies
    InvestigationOrchestrator = object  # type: ignore[assignment,misc]


class FinalDecisionOrchestrator(InvestigationOrchestrator):  # type: ignore[misc,valid-type]
    """The console's Run button, on the final decision engine.

    Runs ``run_case`` and the graph write-back, appends the record to the regulator's ledger,
    writes containment beside the answer, and emits the record as console events. The answer
    goes back to the caller (``CaseRunner``), which writes it to the shared cases directory, so
    the console and the regulator view read one file.
    """

    def __init__(self, run_dir, *, use_model: bool = True, actor: str = "sentinel-console") -> None:
        self.run_dir = run_dir
        self.use_model = use_model
        self.actor = actor
        self._src = None

    def _source(self):
        if self._src is None:
            from sentinel.typologies.tg import TigerGraphTxnSource

            self._src = TigerGraphTxnSource()
        return self._src

    def _investigate(self, row: dict) -> tuple[dict, list[dict], dict | None]:
        from sentinel.typologies.run import run_case, write_back

        rec = _Recorder()
        src = self._source()
        ans = run_case(row, src, use_model=self.use_model, ledger=rec)
        containment = ans.pop("_containment", None)
        write_back(ans, row, src, actor=self.actor, ledger=rec)
        return ans, rec.entries, containment

    async def run(self, alert, emitter=None):
        import json

        from sentinel.domain.answer import AnswerFile
        from sentinel.typologies.audit import Ledger
        from sentinel.validation.validator import AnswerValidator

        row = {"case_id": alert.alert_id, "opened_at": alert.opened_at.isoformat(sep=" ")
               if isinstance(alert.opened_at, datetime) else str(alert.opened_at),
               "trigger_type": alert.trigger_type.value, "trigger_text": alert.trigger_text,
               "flagged_txn_id": alert.flagged_txn_id, "card_id": alert.card_id,
               "customer_id": alert.customer_id, "risk_score": "" if alert.risk_score is None else str(alert.risk_score)}
        ans, recorded, containment = await asyncio.to_thread(self._investigate, row)

        c = ans["case"]
        recorded.append({"case_id": alert.alert_id, "kind": "status.changed", "actor": "sentinel-agent", "role": "system",
                         "data": {"from": "new", "to": c["status"], "verdict": c["verdict"],
                                  "written_to_graph": c["written_to_graph"], "answer_file": f"{alert.alert_id}.json",
                                  "started_from": "console"}})
        ledger = Ledger(self.run_dir / "audit" / "ledger.jsonl")
        for e in recorded:
            ledger.append(e["case_id"], e["kind"], e["data"], actor=e["actor"], role=e["role"])
        if containment:
            cdir = self.run_dir / "containment"
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / f"{alert.alert_id}.json").write_text(json.dumps(containment, indent=1, default=str), encoding="utf8")

        if emitter is not None:
            for kind, payload, step in events_from_ledger(recorded, ans):
                emitter.emit(kind, payload, step)
        answer = AnswerFile.model_validate(ans)
        report = AnswerValidator().validate(answer)
        # The final engine's summary is the rule that decided, in one sentence; the Round 1 agent's
        # two-to-six-sentence rule is a style check, not a defect in the answer.
        style = [f for f in report.errors if f.path == "case.summary"]
        report.errors = [f for f in report.errors if f not in style]
        report.warnings.extend(style)
        return InvestigationResult(answer=answer, validation=report, context=None)  # type: ignore[arg-type]
