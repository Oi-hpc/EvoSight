"""Immutable prediction definitions, probability versions and as-of scoring."""
import json
from datetime import datetime, timedelta

from .db import dump, instant, node, one, require, rows, utc
from . import scenarios, skills, workflow


def referenced(c, event, ids, required=True):
    ids = list(dict.fromkeys(ids))
    if required and not ids:
        raise ValueError("需要引用本事件的证据")
    if any(
        not one(c, "SELECT 1 FROM event_docs WHERE event_id=? AND document_id=?", (event, i)) for i in ids
    ):
        raise ValueError("引用的证据不属于本事件")
    return ids


def new(
    c,
    event,
    question,
    deadline,
    rule,
    authority,
    p,
    reason,
    refs,
    model,
    *,
    at=None,
    cohort="auto",
    grace_hours=72,
    scale=None,
    outlook=None,
):
    when = instant(at) if at else utc()
    due = instant(deadline)
    if due <= when or not all(s.strip() for s in (question, rule, authority, reason)):
        raise ValueError("预测需要将来的截止时间、明确的题面、判定规则、认可来源和理由")
    refs = referenced(c, event, refs)
    if not 0 <= grace_hours <= 720:
        raise ValueError("证据宽限期应为 0–720 小时")
    specification = scenarios.Scale.model_validate(scale).registered(when, due) if scale is not None else None
    ident = c.execute(
        "INSERT INTO predictions(event_id,question,deadline,rule,authority,cohort,created_at,grace_hours) VALUES(?,?,?,?,?,?,?,?)",
        (event, question.strip(), due, rule.strip(), authority.strip(), cohort, when, grace_hours),
    ).lastrowid
    if specification:
        c.execute("INSERT INTO forecast_scales VALUES(?,?)", (ident, dump(specification)))
    revise(c, ident, p, reason, refs, model, at=when, outlook=outlook)
    return ident


def revise(c, ident, p, reason, refs, model, *, at=None, outlook=None):
    pred = require(c, "predictions", ident)
    require(c, "events", pred["event_id"])
    workflow.assert_revisable(c, pred)
    when = instant(at) if at else utc()
    last = one(c, "SELECT created_at FROM revisions WHERE prediction_id=? ORDER BY id DESC LIMIT 1", (ident,))
    if pred["status"] != "open" or when >= pred["deadline"] or (last and when < last["created_at"]):
        raise ValueError("预测已冻结，或该版本早于现有版本")
    refs = referenced(c, pred["event_id"], refs)
    scale = scenarios.definition(c, ident)
    if bool(scale) != (outlook is not None):
        raise ValueError("分级预测的每个版本均需给出与原分档一致的情景研判")
    if scale:
        outlook = scenarios.Outlook.model_validate(outlook).checked(scale, refs)
        primary = outlook["probabilities"][outlook["primary_level"]]
        if p is not None and p != primary:
            raise ValueError("提供的摘要概率与主情景概率不一致")
        p = primary
    if type(p) is not int or not 0 <= p <= 100 or not reason.strip():
        raise ValueError("请填写 0–100 的整数概率和修订依据")
    version = c.execute(
        "INSERT INTO revisions(prediction_id,p,reason,refs,created_at,model) VALUES(?,?,?,?,?,?)",
        (ident, p, reason, dump(refs), when, model),
    ).lastrowid
    if outlook:
        c.execute("INSERT INTO forecast_outlooks VALUES(?,?)", (version, dump(outlook)))
    snap = one(
        c,
        """SELECT id FROM knowledge_snapshots WHERE event_id=? AND created_at<=?
                     ORDER BY id DESC LIMIT 1""",
        (pred["event_id"], when),
    )
    if snap:
        c.execute("INSERT INTO revision_contexts(revision_id,snapshot_id) VALUES(?,?)", (version, snap["id"]))
    node(
        c,
        pred["event_id"],
        "prediction",
        pred["question"],
        (scenarios.summary(scale, outlook) + " · " if scale else "") + f"{p}% · {reason}",
        refs=refs,
        prediction=ident,
        at=when,
    )
    return version


def settle(
    c, ident, outcome, public_at, reason, refs, lesson, actor, *, at=None, observed_level=None, observed_at="",
    proposed_method=None, extract_lesson=False
):
    pred = require(c, "predictions", ident)
    event = require(c, "events", pred["event_id"])
    when = instant(at) if at else utc()
    scale = scenarios.definition(c, ident)
    allowed_outcomes = ("level",) if scale else ("yes", "no")
    if outcome not in (*allowed_outcomes, "unverifiable", "conflict") or not reason.strip():
        raise ValueError("需要明确的裁定与理由")
    decisive = outcome in allowed_outcomes
    if outcome == "level":
        if type(observed_level) is not int or not 0 <= observed_level < len(scale["levels"]):
            raise ValueError("实际力度须对应登记时的一档")
        if when < pred["deadline"]:
            raise ValueError("完整观察窗口结束后才能裁定最终力度")
    elif observed_level is not None:
        raise ValueError("仅分级结果可登记实际档位")
    refs = referenced(c, pred["event_id"], refs, outcome != "unverifiable")
    public = instant(public_at) if public_at else None
    if outcome == "level":
        observed = instant(observed_at) if observed_at else None
        if not observed or not scale["window_start"] <= observed <= pred["deadline"]:
            raise ValueError("分级裁定须给出登记窗口内的实际观察时点")
    if decisive and (not public or public > when):
        raise ValueError("需提供已公开决定性证据的首次公布时间")
    if outcome == "unverifiable" and when < utc(
        datetime.fromisoformat(pred["deadline"]) + timedelta(hours=pred["grace_hours"])
    ):
        raise ValueError("证据宽限期尚未结束，可以暂缓裁定或登记证据冲突")
    status = {
        "yes": "resolved",
        "no": "resolved",
        "level": "resolved",
        "unverifiable": "unverifiable",
        "conflict": "review",
    }[outcome]
    scores = {}
    if decisive:
        cutoff = pred["deadline"] if scale else min(public, pred["deadline"])
        versions = rows(
            c,
            "SELECT * FROM revisions WHERE prediction_id=? AND created_at<? ORDER BY created_at,id",
            (ident, cutoff),
        )
        fixed_cut = utc(datetime.fromisoformat(pred["deadline"].replace("Z", "+00:00")) - timedelta(days=7))
        fixed = [v for v in versions if v["created_at"] <= fixed_cut]
        y = int(outcome == "yes")

        def point(v):
            return {
                "revision": v["id"],
                "p": v["p"],
                **(
                    {"rps": scenarios.score(scenarios.version(c, v["id"])["probabilities"], observed_level)}
                    if scale
                    else {"brier": round((v["p"] / 100 - y) ** 2, 4)}
                ),
                "lead_hours": round(
                    (datetime.fromisoformat(cutoff) - datetime.fromisoformat(v["created_at"])).total_seconds()
                    / 3600,
                    2,
                ),
            }

        scores = {
            **(
                {
                    "observed_level": observed_level,
                    "observed_at": observed,
                    "observed_label": scale["levels"][observed_level]["label"],
                }
                if scale
                else {}
            ),
            "cutoff": cutoff,
            "initial": point(versions[0]) if versions else None,
            "final": point(versions[-1]) if versions else None,
            "seven_days": point(fixed[-1]) if fixed else None,
            "late_only": not versions,
        }
    prior = one(c, "SELECT * FROM resolutions WHERE prediction_id=? ORDER BY id DESC LIMIT 1", (ident,))
    if prior and pred["status"] in ("resolved", "unverifiable"):
        if (
            prior["outcome"] == outcome
            and prior["public_at"] == public
            and json.loads(prior["refs"]) == refs
            and json.loads(prior["scores"]).get("observed_level") == observed_level
            and (outcome != "level" or json.loads(prior["scores"]).get("observed_at") == observed)
        ):
            return json.loads(prior["scores"])
        raise ValueError("预测已结算，需要由来源更正或复核重新打开")
    resolution_id = c.execute(
        "INSERT INTO resolutions(prediction_id,outcome,public_at,reason,refs,scores,lesson,created_at,actor) VALUES(?,?,?,?,?,?,?,?,?)",
        (ident, outcome, public, reason, dump(refs), dump(scores), lesson, when, actor),
    ).lastrowid
    c.execute("UPDATE predictions SET status=? WHERE id=?", (status, ident))
    node(
        c,
        pred["event_id"],
        "resolution",
        pred["question"],
        (scores.get("observed_label", "") + " · " if outcome == "level" else "") + reason,
        refs=refs,
        prediction=ident,
        at=when,
    )
    if lesson.strip() and (decisive or outcome == "unverifiable"):
        workflow.observe(
            c,
            pred["event_id"],
            "待验证经验",
            lesson,
            "在后续独立案例核对适用条件与反例",
            refs=refs,
            pred=ident,
            resolution=resolution_id,
        )
        node(c, pred["event_id"], "retrospective", "预测复盘", lesson, refs=refs, prediction=ident, at=when)
        if (proposed_method is not None or extract_lesson) and not event["demo"]:
            source = one(c, "SELECT id FROM observations WHERE resolution_id=? AND origin='待验证经验'",
                         (resolution_id,))
            if proposed_method is not None:
                skills.from_verdict(c, event["id"], source["id"], proposed_method)
            else:
                skills.request(c, event["id"], source["id"])
    return scores


def detail(c, ident):
    pred = require(c, "predictions", ident)
    pred["scale"] = scenarios.definition(c, ident)
    pred["versions"] = rows(
        c, "SELECT * FROM revisions WHERE prediction_id=? ORDER BY created_at,id", (ident,)
    )
    pred["settlements"] = rows(c, "SELECT * FROM resolutions WHERE prediction_id=? ORDER BY id", (ident,))
    for r in pred["settlements"]:
        r["refs"] = json.loads(r["refs"])
        r["scores"] = json.loads(r["scores"])
    latest = pred["settlements"][-1] if pred["settlements"] else {}
    cutoff = latest.get("scores", {}).get("cutoff")
    for v in pred["versions"]:
        if pred["scale"]:
            v["outlook"] = scenarios.version(c, v["id"])
            v["scenario_label"] = scenarios.summary(pred["scale"], v["outlook"])
        v["refs"] = json.loads(v["refs"])
        v["late"] = bool(cutoff and v["created_at"] >= cutoff)
        context = one(
            c, "SELECT snapshot_id,dependencies FROM revision_contexts WHERE revision_id=?", (v["id"],)
        )
        v["snapshot_id"] = context["snapshot_id"] if context else None
        v["dependencies"] = json.loads(context["dependencies"]) if context else []
        v["lessons"] = rows(
            c,
            """SELECT o.id,o.event_id,o.question FROM revision_lessons r JOIN observations o ON o.id=r.lesson_id
                             WHERE r.revision_id=?""",
            (v["id"],),
        )
    pred["p"] = pred["versions"][-1]["p"]
    pred["scenario_label"] = pred["versions"][-1].get("scenario_label", "")
    if pred["status"] == "open" and pred["deadline"] <= utc():
        pred["status"] = "frozen"
    return pred


def metrics(c):
    groups = {}
    for cohort in ("auto", "fixed"):
        group = rows(
            c,
            """SELECT p.id,p.status FROM predictions p JOIN events e ON e.id=p.event_id
               WHERE p.cohort=? AND e.demo=0 AND e.status!='deleted'""",
            (cohort,),
        )
        due = (
            rows(
                c,
                """SELECT f.prediction_id FROM fixed_questions f JOIN events e ON e.id=f.event_id
                   WHERE e.demo=0 AND e.status!='deleted' AND f.deadline<=?""",
                (utc(),),
            )
            if cohort == "fixed"
            else []
        )
        counted = [detail(c, p["id"]) for p in group if p["status"] == "resolved"]
        scores = [p["settlements"][-1]["scores"] for p in counted]

        def mean(key, field):
            values = [s[key][field] for s in scores if s.get(key) and field in s[key]]
            return round(sum(values) / len(values), 4) if values else None

        groups[cohort] = {
            "submitted": len(group),
            "resolved": len(counted),
            "scored": sum(bool(s.get("final")) for s in scores),
            "unverifiable": sum(p["status"] == "unverifiable" for p in group),
            "late": sum(bool(s.get("late_only")) for s in scores),
            "due": len(due),
            "coverage": sum(bool(x["prediction_id"]) for x in due) / len(due) if due else None,
            "initial_brier": mean("initial", "brier"),
            "seven_days_brier": mean("seven_days", "brier"),
            "final_brier": mean("final", "brier"),
            "graded_scored": sum(bool(s.get("final")) and "rps" in s["final"] for s in scores),
            "initial_rps": mean("initial", "rps"),
            "seven_days_rps": mean("seven_days", "rps"),
            "final_rps": mean("final", "rps"),
            "mean_lead_hours": mean("final", "lead_hours"),
        }
    return groups
