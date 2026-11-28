"""Independent checks on decision-relevant assertions before public research writes."""
from collections import defaultdict
from dataclasses import dataclass, field

from . import grounding, resources, workflow
from .contracts import DeliveryAnswer, Report
from .db import DispatchDeferred, enqueue, rows


def _references(report):
    return {c["fragment_id"] for f in report["findings"] for c in f["citations"]}


def targets(plan, reports, checked):
    assignments = {(a["role"], a["question"]): a for a in plan["tasks"]}
    lines = {(a.get("line") or f"{a['role']}/{a.get('target') or a['question']}"): a for a in plan["tasks"]}
    owners = defaultdict(list)
    for report in reports:
        assignment = (
            lines.get(report["调查线"]) if "调查线" in report else assignments.get((report["角色"], report["问题"]))
        )
        if assignment is None:
            continue
        positions = set(assignment.get("review_checks", []))
        if "review_checks" not in assignment:
            # Older frozen plans have no responsibility map. Only infer ownership
            # for a decisive assignment with an actual common source paragraph.
            sensitive = (
                assignment.get("priority") == 5
                or report["角色"] in ("counter", "dependencies")
                or any(f["kind"] in ("conflict", "correction") for f in report["findings"])
            )
            if sensitive:
                refs = _references(report)
                positions.update(
                    row["position"]
                    for row in checked
                    if any(c["fragment_id"] in refs for c in row["citations"])
                )
        for pos in positions:
            owners[pos].append(
                {
                    **report,
                    "调查线": assignment.get("line")
                    or f"{assignment['role']}/{assignment.get('target') or assignment['question']}",
                }
            )
    return [
        {
            "check_id": row["position"],
            "question": row["question"],
            "answer": row["answer"],
            "state": row["state"],
            "citations": row["citations"],
            "owners": owners[row["position"]],
        }
        for row in checked
        if owners.get(row["position"])
    ]


def previously_returned(c, r, tasks):
    """Never put an unchanged rejected report back into a later public version."""
    rejected = defaultdict(set)
    for row in rows(
        c,
        """
        SELECT u.task_id,u.finding_hash FROM research_review_finding_returns u JOIN research_tasks t ON t.id=u.task_id
        WHERE t.round_id=?""",
        (r["id"],),
    ):
        rejected[row["task_id"]].add(row["finding_hash"])
    return [_filter_task(t, rejected[t["id"]]) for t in tasks]


def _filter_task(task, rejected):
    if not rejected or task["state"] != "done":
        return task
    report = Report.model_validate_json(task["result"])
    kept = [f for f in report.findings if workflow.fingerprint(f.model_dump()) not in rejected]
    if len(kept) == len(report.findings):
        return task
    return {**task, "result": Report(findings=kept, gaps=report.gaps).model_dump_json()}


@dataclass
class Outcome:
    held: dict = field(default_factory=dict)
    unresolved: list = field(default_factory=list)
    retries: dict = field(default_factory=dict)
    quota: bool = False
    safe_refs: set = field(default_factory=set)
    safe_answers: dict = field(default_factory=dict)

    def contested(self, item, owner):
        disputed = {c["fragment_id"] for c in item["citations"]}
        for finding in owner["findings"]:
            refs = {c["fragment_id"] for c in finding["citations"]}
            if item["state"] != "confirmed":
                # There may be no citations for a legitimate unknown. A price
                # and an unproven benefit can even cite the same paragraph.
                # Preserve only an independently identifiable confirmed answer.
                verified = self.safe_answers.get(finding["text"].strip(), set())
                if not refs or not refs <= verified:
                    yield finding
            elif not refs or refs & disputed or not refs <= self.safe_refs:
                yield finding

    def rejected_findings(self, item, tasks):
        # Retain same-owner findings grounded in separate, confirmed checklist
        # evidence. When ownership is ambiguous withhold the finding, not the archive.
        rejected = defaultdict(set)
        disputed_texts = set()
        for owner in item["owners"]:
            for finding in self.contested(item, owner):
                rejected[owner["task_id"]].add(workflow.fingerprint(finding))
                if finding["text"].strip():
                    disputed_texts.add(finding["text"].strip())
        for task in tasks:
            if task["state"] == "done":
                for f in Report.model_validate_json(task["result"]).findings:
                    if f.text.strip() in disputed_texts:
                        rejected[task["id"]].add(workflow.fingerprint(f.model_dump()))
        return rejected

    def publish_tasks(self, tasks):
        rejected = defaultdict(set)
        for item in [*self.held.values(), *self.unresolved]:
            for tid, hashes in self.rejected_findings(item, tasks).items():
                rejected[tid].update(hashes)
        return [_filter_task(t, rejected[t["id"]]) for t in tasks]

    def persist(self, c, r, job, tasks):
        if not self.held and not self.unresolved:
            return
        from . import wakes

        for item in [*self.held.values(), *self.unresolved]:
            pos = item["check_id"]
            if pos in self.retries:
                continue
            # Keep an unconfirmed original out of subsequent versions even if
            # a later handoff supplies stronger evidence for the same assertion.
            # Missing output or transport failure is retried without rejecting the source.
            if item["state"] not in ("confirmed", "unknown"):
                continue
            reason = item.get("reason") or item["answer"]
            for tid, hashes in self.rejected_findings(item, tasks).items():
                c.executemany(
                    "INSERT OR IGNORE INTO research_review_finding_returns VALUES(?,?,?,?)",
                    [(tid, pos, key, reason) for key in hashes],
                )
            if item["state"] != "confirmed":
                continue
            for owner in item["owners"]:
                original = owner.get("原调查任务", owner["task_id"])
                line = owner["调查线"]
                docs = list(dict.fromkeys(cit["document_id"] for cit in item["citations"]))
                wakes.post(
                    c,
                    r,
                    original,
                    line,
                    item["question"],
                    item["reason"],
                    docs,
                    review_key=f"{original}/{pos}",
                )
        if self.retries:
            # One transport/invalid-result retry for the same evidence and report.
            # Quota waits use the ordinary deferred-job recovery instead of polling.
            key = workflow.fingerprint(sorted(self.retries), [(t["id"], t["result"]) for t in tasks])
            ident = enqueue(
                c,
                "research_retry",
                event=r["event_id"],
                args={
                    "round_id": r["id"],
                    "dispatch_key": f"claim-review/{key}",
                    "review_positions": sorted(self.retries),
                },
                slot=f"claim-review/{r['id']}/{key}",
            )
            if self.quota and ident != job["id"]:
                c.execute(
                    "UPDATE jobs SET status='deferred',info=? WHERE id=? AND status='queued'",
                    ("额度恢复后继续关键结论复核", ident),
                )


def brief(report):
    return {
        "task_id": report["task_id"],
        "question": report["问题"],
        "findings": [{k: f[k] for k in ("kind", "text", "citations")} for f in report["findings"]],
    }


def public_citation(ref, archive):
    frag = archive[ref.fragment_id]
    return {
        "document_id": frag["document_id"],
        "fragment_id": ref.fragment_id,
        "quote": ref.quote,
        "locator": frag.get("locator", ""),
        "link": frag.get("link") or frag.get("url", ""),
    }


def run(worker, job, r, plan, reports, checked, synthesis, fragments, shown, tasks):
    absent = [
        {"task_id": t["id"], "角色": t["role"], "问题": t["question"], "findings": []}
        for t in tasks
        if t["state"] != "done"
    ]
    selected = targets(plan, [*reports, *absent], checked)
    outcome = Outcome(unresolved=[item for item in selected if item["state"] != "confirmed"])
    items = [item for item in selected if item["state"] == "confirmed"]
    if items:
        adjudicate(worker, job, r, reports, checked, synthesis, fragments, shown, items, outcome)
    if outcome.held or outcome.unresolved:
        safe_answers = defaultdict(set)
        for row in checked:
            if row["state"] == "confirmed":
                safe_answers[row["answer"].strip()].update(cit["fragment_id"] for cit in row["citations"])
        outcome.safe_answers = dict(safe_answers)
        outcome.safe_refs = set().union(*safe_answers.values())
        # Free-form judgment, closure and discoveries can repeat the unsupported
        # premise. Publish the surviving public answers and verified facts instead.
        synthesis.fact = synthesis.judgment = synthesis.watch = synthesis.closure_reason = ""
        synthesis.items, synthesis.discoveries, synthesis.evidence_ids = [], [], []
    return outcome


def adjudicate(worker, job, r, reports, checked, synthesis, fragments, shown, items, outcome):
    archive = {f["id"]: f for f in fragments}
    ids = list(
        dict.fromkeys(
            [
                *(c["fragment_id"] for item in items for c in item["citations"]),
                *(c["fragment_id"] for report in reports for f in report["findings"] for c in f["citations"]),
                *shown,
            ]
        )
    )
    visible = {i: archive[i] for i in ids if i in archive}
    ctx = {
        "独立复核": True,
        "goal_version": r["goal_version"],
        "window_end": r["window_end"],
        "核查项": [{**item, "owners": [brief(o) for o in item["owners"]]} for item in items],
        "各调查线依据": [brief(report) for report in reports],
        "交付可引用原文": [
            {
                k: (v[:2400] if k == "text" else v)
                for k, v in f.items()
                if k in ("id", "document_id", "title", "text", "locator", "kind", "url")
            }
            for f in list(visible.values())[:42]
        ],
        "archive_fragment_ids": list(visible),
    }
    verdicts = defaultdict(list)
    error = ""
    try:
        response, _ = worker.call(job, "claim_review", ctx)
        for verdict in response.items:
            verdicts[verdict.check_id].append(verdict)
    except resources.ResourcePressure:
        raise
    except (RuntimeError, ValueError, OSError) as exc:
        error = "复核调用未完成。" + str(exc)[:160]
        outcome.quota = isinstance(exc, DispatchDeferred)
    for item in items:
        pos = item["check_id"]
        row = next(row for row in checked if row["position"] == pos)
        reviewer_refs = []
        try:
            if error or len(verdicts[pos]) != 1:
                raise ValueError(error or "复核回执缺失或重复。")
            verdict = verdicts[pos][0]
            refs = [grounding.locate(cit, visible) for cit in verdict.citations]
            reviewer_refs = [public_citation(ref, visible) for ref in refs]
            if verdict.decision == "supported":
                if not refs:
                    raise ValueError("复核通过缺少可定位的逐字原文。")
                # Save corroborating primary passages alongside the actual answer,
                # including originals located by a different investigator.
                for cite in reviewer_refs:
                    if cite not in row["citations"]:
                        row["citations"].append(cite)
                continue
            reason = verdict.reason
            evidence = [*item["citations"], *reviewer_refs]
        except ValueError as exc:
            reason = str(exc)
            evidence = item["citations"]
            outcome.retries[pos] = reason
        outcome.held[pos] = {**item, "reason": reason, "citations": evidence}
        explanation = "独立复核待补：" + reason
        synthesis.gaps.append(item["question"] + " · " + explanation)
        synthesis.delivery = [a for a in synthesis.delivery if a.check_id != pos] + [
            DeliveryAnswer(check_id=pos, state="unknown", answer=explanation)
        ]
        row.update(state="unknown", answer=explanation, citations=reviewer_refs, reason="")
