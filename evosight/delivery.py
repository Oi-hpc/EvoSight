"""Freeze an expected checklist and reconcile each item against the report actually stored."""
import json
import re
from collections import defaultdict
from decimal import Decimal

from . import grounding
from .contracts import Report
from .db import dump, rows


def checklist(plan):
    return list(dict.fromkeys(plan.get("checklist") or [t["question"] for t in plan.get("tasks", [])]))


def reconcile_handoff_gaps(tasks, original_tasks):
    """Project resolved owner gaps out of this publication. Keep both investigation receipts intact."""
    resolved = defaultdict(set)
    for task in tasks:
        original = original_tasks.get(task["id"])
        if not original or task["state"] != "done":
            continue
        report = Report.model_validate_json(task["result"])
        supporting = {
            ref.fragment_id for finding in report.findings if finding.kind in ("fact", "reported", "correction")
            for ref in finding.citations
        }
        resolved[original["原调查任务"]].update(
            item.gap for item in report.resolved_gaps if item.fragment_id in supporting
        )
    projected = []
    for task in tasks:
        if task["state"] == "done" and resolved.get(task["id"]):
            report = Report.model_validate_json(task["result"])
            kept = [gap for gap in report.gaps if gap not in resolved[task["id"]]]
            if kept != report.gaps:
                report.gaps = kept
                task = {**task, "result": report.model_dump_json()}
        projected.append(task)
    return projected


def earlier_work(c, r):
    # Only receipts from this event and goal can supply leads. Intermediate tool calls are
    # kept because an investigator's final reply can accidentally omit a previously found fact.
    receipts = rows(
        c,
        """SELECT m.run_id,m.response,i.payload FROM model_receipts m
        JOIN run_inputs i ON i.run_id=m.run_id JOIN jobs j ON j.id=m.job_id
        JOIN tracking_rounds t ON t.id=json_extract(j.args,'$.round_id')
        WHERE t.event_id=? AND t.goal_version=? AND t.id<=? AND t.window_end<=?
        AND t.id IN (SELECT id FROM tracking_rounds WHERE event_id=t.event_id AND goal_version=t.goal_version
                     AND id<=? ORDER BY id DESC LIMIT 2)
        AND json_type(i.payload,'$.task')='object' AND json_type(m.response,'$.lookup')='object'
        ORDER BY m.run_id DESC LIMIT 32""",
        (r["event_id"], r["goal_version"], r["id"], r["window_end"], r["id"]),
    )
    leads, seen = [], set()
    for record in receipts:
        ctx = json.loads(record["payload"])
        visible = {f["id"]: f for f in ctx.get("fragments", [])}
        for f in json.loads(record["response"]).get("findings", []):
            if f.get("text") in seen or not f.get("citations"):
                continue
            refs = []
            for ref in f["citations"]:
                fragment = visible.get(ref.get("fragment_id"))
                if fragment and grounding.verbatim(fragment["text"], ref["quote"]):
                    refs.append(fragment["id"])
            if refs:
                leads.append(
                    {
                        "run": record["run_id"],
                        "subject": f.get("subject", ""),
                        "text": f["text"][:500],
                        "fragment_ids": list(dict.fromkeys(refs)),
                    }
                )
                seen.add(f["text"])
    predictions = rows(
        c,
        """SELECT v.id,substr(v.reason,1,1000) reason,v.refs FROM revisions v JOIN predictions p
                           ON p.id=v.prediction_id WHERE p.event_id=? AND v.created_at<=? ORDER BY v.id DESC LIMIT 3""",
        (r["event_id"], r["window_end"]),
    )
    return leads[:28], predictions


def review_inputs(c, r, plan, fragments, earlier=None, reports=()):
    leads, predictions = earlier if earlier is not None else earlier_work(c, r)
    allowed = {f["id"]: f for f in fragments}
    # Preserve older cited paragraphs when a long new document uses most of the prompt.
    # Start with each line's direct citations. A late handoff or corrective
    # filing may be near the end of a large archive.
    report_refs = [[ref["fragment_id"] for f in report["findings"] for ref in f["citations"]]
                   for report in reports]
    cited = [refs[offset] for offset in range(max(map(len, report_refs), default=0))
             for refs in report_refs if offset < len(refs) and refs[offset] in allowed]
    wanted = list(
        dict.fromkeys(
            cited + [f["id"] for f in fragments[:24]]
            + [i for lead in leads for i in lead["fragment_ids"] if i in allowed]
        )
    )[:42]
    shown = [
        {
            k: (v[:2400] if k == "text" else v)
            for k, v in allowed[i].items()
            if k in ("id", "document_id", "title", "text", "locator", "kind", "url")
        }
        for i in wanted
    ]
    return {
        "交付清单": [{"check_id": i, "question": q} for i, q in enumerate(checklist(plan), 1)],
        "中间调查线索": leads,
        "此前预测理由（仅作查找线索）": predictions,
        "交付可引用原文": shown,
    }


NUMBER = r"(?<![\d.])([+-]?\d+(?:,\d{3})*(?:\.\d+)?)"
SCALES = {"万": 10_000, "亿": 100_000_000, "million": 1_000_000, "billion": 1_000_000_000}


def numbers(text, *, measured=False):
    # Compare quantities across source and report units. Dates and filing identifiers without
    # a stated unit do not impose extra numeric requirements on the author's explanation.
    tail = r"\s*(万|亿|million\b|billion\b)?"
    unit = r"\s*(?:美?元|股(?!东)|美元|%|％|shares?\b|dollars?\b|USD\b)"
    text = re.sub(r"负(?=\d)", "-", text.replace("−", "-"))
    matches = list(re.finditer(NUMBER + tail + (unit if measured else ""), text, re.I))
    if measured:
        matches.extend(re.finditer(r"\$\s*" + NUMBER + tail, text, re.I))
    return {Decimal(m[1].replace(",", "")) * SCALES.get((m[2] or "").lower(), 1) for m in matches}


def supported_quantities(answer, quotations):
    available = set().union(*(numbers(q) for q in quotations))
    missing = numbers(answer, measured=True) - available
    if not missing:
        return True
    # Only derive from operands the author actually states and the archive independently
    # contains. Use the operation named in the answer and require the stated precision.
    operands = numbers(answer) & available
    derived = set()
    for left in operands:
        for right in operands:
            if re.search(r"差额|价差|高[出于]|低[于了]|相差|减去|[−-]\s*\d|difference", answer, re.I):
                derived.update((left - right, right - left))
            if re.search(r"合计|相加|共计|\+\s*\d|sum of", answer, re.I):
                derived.add(left + right)
            if right and re.search(r"溢价率|涨幅|跌幅|变化率|百分比|premium|percent", answer, re.I):
                derived.update(((left / right - 1) * 100, (1 - left / right) * 100))
    rounding = bool(re.search(r"约|近似|四舍五入|≈|about|rounded", answer, re.I))
    return all(
        target in derived
        or rounding
        and any(abs(value - target) < Decimal("0.5").scaleb(target.as_tuple().exponent) for value in derived)
        for target in missing
    )


def inspect(plan, answers, fragments):
    proposed = defaultdict(list)
    for answer in answers:
        proposed[answer.check_id].append(answer)
    result = []
    for i, question in enumerate(checklist(plan), 1):
        row = {
            "position": i,
            "question": question,
            "state": "missing",
            "answer": "",
            "citations": [],
            "reason": "",
        }
        result.append(row)
        replies = proposed[i]
        if not replies:
            continue
        if len(replies) > 1:
            row.update(state="rejected", reason="同一核查项提交了多份答复")
            continue
        answer = replies[0]
        try:
            if answer.state == "confirmed" and not answer.citations:
                raise ValueError("确认的答复缺少原文引文")
            citations = []
            for ref in answer.citations:
                ref = grounding.locate(ref, fragments)
                f = fragments[ref.fragment_id]
                citations.append(
                    {
                        "fragment_id": f["id"],
                        "document_id": f["document_id"],
                        "quote": ref.quote,
                        "locator": f.get("locator", ""),
                        "link": f.get("link") or f.get("url", ""),
                    }
                )
            if answer.state == "confirmed":
                if re.search(
                    r"股数|收盘价(?:是多少|为多少)|发行价(?:是多少|为多少)|how many|how much", question, re.I
                ) and not re.search(r"\d", answer.answer):
                    raise ValueError("数量核查项缺少具体读数")
                if not supported_quantities(answer.answer, [ref["quote"] for ref in citations]):
                    raise ValueError("答复中的数值与单位未能对应所引原文")
        except ValueError as err:
            row.update(state="rejected", reason=str(err))
        else:
            row.update(state=answer.state, answer=answer.answer, citations=citations)
    return result


def save(c, snapshot, inspected):
    for row in inspected:
        item = (snapshot, row["position"])
        c.execute("INSERT INTO snapshot_checklist VALUES(?,?,?,?)", (*item, row["question"], row["reason"]))
        if row["state"] in ("confirmed", "unknown"):
            c.execute(
                "INSERT INTO delivery_answers VALUES(?,?,?,?,?)",
                (*item, row["state"], row["answer"], dump(row["citations"])),
            )
    return view(c, snapshot)


def view(c, snapshot):
    result = rows(
        c,
        """SELECT p.position,p.question,coalesce(a.state,CASE WHEN p.rejection!='' THEN 'rejected'
                   ELSE 'missing' END) state,coalesce(a.answer,'') answer,coalesce(a.citations,'[]') citations,
                   p.rejection reason FROM snapshot_checklist p LEFT JOIN delivery_answers a
                   USING(snapshot_id,position) WHERE p.snapshot_id=? ORDER BY p.position""",
        (snapshot,),
    )
    for item in result:
        item["citations"] = json.loads(item["citations"])
    return result


def latest_research_snapshot(c, snapshot):
    """Index edits have their own versions. Label the report with the research version that produced it."""
    if not snapshot:
        return None
    match = c.execute(
        """SELECT k.id FROM knowledge_snapshots k JOIN knowledge_snapshots s ON s.id=?
        AND k.event_id=s.event_id AND k.goal_version=s.goal_version
        WHERE k.id<=s.id AND k.round_id IS NOT NULL ORDER BY k.id DESC LIMIT 1""",
        (snapshot,),
    ).fetchone()
    return match[0] if match else None
