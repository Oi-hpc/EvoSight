"""Connect user input to the answer actually saved in a published research report."""
from collections import Counter

from . import delivery
from .db import one


def checklist(plan, inputs):
    """Preserve the positions referenced by investigators and independent reviewers."""
    if not inputs:
        return plan, []
    questions = delivery.checklist(plan)
    positions = {q: i for i, q in enumerate(questions, 1)}
    occurrences = Counter(item["text"] for item in inputs)
    requests = []
    for item in inputs:
        question = item["text"]
        if occurrences[question] > 1:
            # The same words may refer to different exhibits, recipients or user answers.
            question = f"{question}（用户输入 #{item['id']}）"
            while question in positions or question in occurrences:
                question += f" #{item['id']}"
        if question not in positions:
            questions.append(question)
            positions[question] = len(questions)
        requests.append({**item, "report_question": question, "check_id": positions[question]})
    return {**plan, "checklist": questions}, requests


def save(c, snapshot, requests):
    # The caller freezes these IDs before synthesis. A concurrently submitted
    # request cannot gain the credit for a report its author never saw.
    c.executemany(
        """INSERT INTO research_input_reports(input_id,snapshot_id,position)
           SELECT i.id,s.id,p.position FROM research_inputs i
           JOIN knowledge_snapshots s ON s.id=? AND s.event_id=i.event_id
               AND s.goal_version=i.goal_version AND s.round_id=i.round_id
           JOIN snapshot_checklist p ON p.snapshot_id=s.id AND p.position=? AND p.question=?
           WHERE i.id=? AND i.text=?""",
        [(snapshot, item["check_id"], item["report_question"], item["id"], item["text"]) for item in requests],
    )


def for_input(c, input_id):
    row = one(
        c,
        """SELECT s.id snapshot_id,s.parent_id,s.goal_version,s.summary,
                  p.position,p.question,p.rejection,a.answer,a.state,a.citations
           FROM research_input_reports r JOIN knowledge_snapshots s ON s.id=r.snapshot_id
           JOIN snapshot_checklist p USING(snapshot_id,position)
           LEFT JOIN delivery_answers a USING(snapshot_id,position)
           WHERE r.input_id=? ORDER BY s.id DESC LIMIT 1""",
        (input_id,),
    )
    if not row:
        return None
    previous_id = delivery.latest_research_snapshot(c, row["parent_id"])
    previous = one(c, "SELECT summary,goal_version FROM knowledge_snapshots WHERE id=?", (previous_id,))
    if previous and previous["goal_version"] != row["goal_version"]:
        previous_id, previous = None, None
    prior = one(
        c,
        """SELECT a.answer,a.state,a.citations FROM snapshot_checklist p
           LEFT JOIN delivery_answers a USING(snapshot_id,position)
           LEFT JOIN research_input_reports r USING(snapshot_id,position)
           WHERE p.snapshot_id=? AND (r.input_id=? OR (r.input_id IS NULL AND p.question=?))
           ORDER BY r.input_id IS NOT NULL DESC LIMIT 1""",
        (previous_id, input_id, row["question"]),
    )
    after = row["answer"] or ""
    before = (prior or {}).get("answer") or ""
    state = row["state"] or ("rejected" if row["rejection"] else "missing")
    reason = row["rejection"] or ""
    if not row["state"]:
        outcome = state
        reason = reason or "此版终稿没有提交该核查项的答复。"
    elif not prior or not prior["state"]:
        outcome = "added"
    elif (after, state, row["citations"]) == (before, prior["state"], prior["citations"]):
        outcome = "unchanged"
    else:
        outcome = "changed"
        if before == after and state == prior["state"]:
            reason = "答复文字沿用上一版，本版更新了引文。"
    in_summary = (
        state == "confirmed" and after in row["summary"]
        and (not previous or after not in previous["summary"])
    )
    return {
        "snapshot_id": row["snapshot_id"],
        "previous_snapshot_id": previous_id,
        "position": row["position"],
        "question": row["question"],
        "outcome": outcome,
        "state": state,
        "before": before,
        "after": after,
        "reason": reason,
        "summary_excerpt": after if in_summary else "",
    }
