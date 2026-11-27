"""An outcome can be the registered degree of change across a multi-month window."""
import copy
from datetime import timedelta
import json

from fastapi.testclient import TestClient
import pytest

from evosight import agents, db, discussion, forecast, scenarios
from evosight.api import make_app
from test_workflow import clock, HEADERS, HOST, new_event, setup


def rubric(clock):
    return {
        "window_start": clock.utc(clock.dt + timedelta(days=90)),
        "baseline": "以登记当日生效的控制范围作为比较基线。",
        "levels": [
            {"label": "维持或放宽", "criterion": "窗口内没有新增生效的限制。"},
            {"label": "定向收紧", "criterion": "新增限制仅涉及一个已登记的产品类别。"},
            {"label": "跨类收紧", "criterion": "新增生效限制涉及至少两个已登记的产品类别。"},
        ],
    }


def opinion(doc, probabilities=(25, 50, 25), primary=1):
    return {
        "probabilities": list(probabilities),
        "primary_level": primary,
        "confidence": "medium",
        "confidence_reason": "材料有直接来源，执行安排仍有待确认。",
        "trajectory": "预计在观察期内先出现定向变化。",
        "arguments": [{"side": "support", "explanation": "公告给出了具体实施安排。", "refs": [doc]}],
        "dimensions": [{"name": "适用范围", "expectation": "优先影响一个产品类别。", "refs": [doc]}],
        "reconsider_if": "若主管机构同时明确两个不同类别的生效要求，须上调主情景。",
    }


@pytest.fixture
def case(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    event = new_event(store)
    assert worker.step()
    with store.connection() as c:
        doc = c.execute(
            "select min(document_id) from event_docs where event_id=?", (event["id"],)
        ).fetchone()[0]
    with TestClient(
        make_app(store, runner, background=False, acquisition=source),
        base_url="http://127.0.0.1",
        headers=HEADERS,
    ) as api:
        yield store, runner, worker, api, event["id"], doc, clock


def test_manual_versions_historical_discussion_and_ordered_scoring(case):
    store, runner, worker, api, e, doc, clock = case
    scale = rubric(clock)
    due = clock.utc(clock.dt + timedelta(days=180))
    body = {
        "form": "ordered",
        "scale": scale,
        "outlook": opinion(doc),
        "question": "测试情景：未来第三至第六个月政策力度会到哪一档？",
        "deadline": due,
        "rule": "依窗口内正式生效的公开决定按产品类别计数。",
        "authority": "主管单位正式公告",
        "reason": "公开材料支持定向措施。",
        "refs": [doc],
    }
    path = f"/api/events/{e}/predictions"
    res = api.post(path, json=body)
    assert res.status_code == 201, res.text
    ident = res.json()["id"]
    with store.connection() as c:
        first = forecast.detail(c, ident)["versions"][0]
        assert first["p"] == 50 and first["outlook"]["primary_level"] == 1
        assert db.one(c, "select body from nodes where prediction_id=?", (ident,))["body"].startswith("定向收紧")

    def card():
        return next(
            p for c in api.get("/api/home").json()["cards"] for p in c["predictions"] if p["id"] == ident
        )

    assert card()["scenario_label"] == "定向收紧"
    version_path = f"/api/predictions/{ident}/versions"
    clock.dt += timedelta(days=120)
    second = api.post(
        version_path,
        json={"outlook": opinion(doc, (10, 30, 60), 2), "refs": [doc], "reason": "新增证据提高了跨类收紧的概率。"},
    )
    assert second.is_success, second.text
    assert card()["p"] == 60 and card()["scenario_label"] == "跨类收紧"
    with store.connection() as c:
        old = discussion.anchor(c, e, first["id"])
        ctx, _ = discussion.build_context(c, e, old, "original", "支持哪个档位？")
        assert ctx["registered_forecast"]["outlook"]["probabilities"] == [25, 50, 25]
        assert ctx["registered_forecast"]["scale"] == scale and ctx["sources"][f'P{first["id"]}'][
            "title"
        ].startswith("定向收紧")
        title = db.require(c, "events", e)["title"]
        latest, _ = discussion.build_context(c, None, None, "latest", f"在{title}事件里，目前各档预测的依据是什么？")
        rev = forecast.detail(c, ident)["versions"][-1]["id"]
        recorded = latest["sources"][f"P{rev}"]
        assert recorded["outlook"]["probabilities"] == [10, 30, 60]
        assert recorded["scale"] == scale and recorded["deadline"] == due
        assert recorded["evidence"] == [f"D{doc}"] and f"D{doc}" in latest["sources"]
    settlement = {
        "outcome": "level",
        "observed_level": 1,
        "observed_at": clock.utc(),
        "public_at": clock.utc(),
        "refs": [doc],
        "reason": "观察期内实际只涉及一个类别。",
    }
    sp = f"/api/predictions/{ident}/settle"
    assert api.post(sp, json=settlement).status_code == 422
    clock.dt += timedelta(days=61)
    assert (
        api.post(version_path, json={"outlook": opinion(doc), "reason": "过期", "refs": [doc]}).status_code
        == 422
    )
    for invalid in (
        {"outcome": "yes"},
        {"observed_level": 3},
        {"observed_at": clock.utc()},
        {"observed_at": ""},
    ):
        assert api.post(sp, json={**settlement, **invalid}).status_code == 422
    scores = api.post(sp, json=settlement)
    assert scores.is_success, scores.text
    data = scores.json()
    assert (
        data["initial"]["rps"] == 0.0625
        and data["final"]["rps"] == 0.185
        and data["observed_label"] == "定向收紧"
    )
    assert data["seven_days"]["revision"] == second.json()["id"] and data["cutoff"] == due
    assert api.post(sp, json=settlement).json() == data
    assert api.post(sp, json={**settlement, "observed_level": 0}).status_code == 422
    metric = api.get("/api/predictions").json()["metrics"]["auto"]
    assert metric["final_brier"] is None and metric["final_rps"] == 0.185 and metric["graded_scored"] == 1


def test_reject_incoherent_distributions_and_cross_event_evidence(case):
    store, _, _, api, e, doc, clock = case
    base = {
        "question": "未来政策力度测试",
        "form": "ordered",
        "deadline": clock.utc(clock.dt + timedelta(days=180)),
        "rule": "按正式公告判定",
        "authority": "官方",
        "reason": "测试材料",
        "refs": [doc],
        "scale": rubric(clock),
        "outlook": opinion(doc),
    }
    for change in (
        {"probabilities": [20, 50, 20]},
        {"probabilities": [10, 40, 50]},
        {"probabilities": [25, 50, 20, 5]},
        {"probabilities": [25, 50.5, 24.5]},
        {"arguments": [{"side": "counter", "explanation": "仅有反向依据", "refs": [doc]}]},
        {"dimensions": [{"name": "范围", "expectation": "跨类变化", "refs": [doc + 100000]}]},
    ):
        got = api.post(
            f"/api/events/{e}/predictions", json={**base, "outlook": {**base["outlook"], **change}}
        )
        assert got.status_code == 422, got.text
    with store.connection() as c:
        assert not c.execute("SELECT 1 FROM forecast_scales").fetchone()
    for p, actual, expected in (([100, 0, 0], 0, 0), ([100, 0, 0], 1, 0.5), ([100, 0, 0], 2, 1)):
        assert scenarios.score(p, actual) == expected


def test_fixed_agent_defines_scale_then_revises_on_same_scale(case, monkeypatch):
    store, runner, w, api, e, doc, clock = case
    scale = rubric(clock)
    close = clock.utc(clock.dt + timedelta(days=180))
    question = "主管单位在未来三到六个月会将合规要求提高到什么范围？"
    result = api.post(
        f"/api/events/{e}/fixed",
        json={
            "form": "ordered",
            "window_start": scale["window_start"],
            "deadline": close,
            "question": question,
            "rule": "按窗口内已正式生效的类别判断。",
            "authority": "https://transit.example.org",
        },
    )
    assert result.is_success, result.text
    job = result.json()["job"]
    with store.connection(True) as c:
        c.execute("update jobs set retry_at='2099-02-01' where status='queued' and id!=?", (job,))
    contexts = []

    def call(role, ctx):
        assert role == "forecast"
        contexts.append(copy.deepcopy(ctx))
        fixed = ctx.get("固定题")
        existing = ctx["已有预测"]
        p = ctx["evidence"][0]["id"]
        return agents.Forecast(
            action="create" if fixed else "revise",
            prediction_id=0 if fixed else existing[0]["id"],
            question=question,
            deadline=close,
            rule="按生效范围",
            authority="官方",
            p=None,
            reason="核对主管单位的材料",
            evidence_ids=[p],
            scale=scale if fixed else None,
            outlook=opinion(p) if fixed else opinion(p, (10, 30, 60), 2),
        ), (20, 8)

    monkeypatch.setattr(runner, "call", call)
    assert w.step()
    with store.connection(True) as c:
        pred = db.require(c, "fixed_questions", result.json()["id"])["prediction_id"]
        assert pred and contexts[0]["固定题"]["window_start"] == scale["window_start"]
        excerpts = contexts[0]["evidence_fragments"]
        assert any(f["document_id"] == doc and f["text"] and f["locator"] for f in excerpts)
        assert all(type(f["truncated"]) is bool for f in excerpts)
        ver = forecast.detail(c, pred)["versions"][0]
        anchor = discussion.anchor(c, e, ver["id"])
        assert discussion.archived_forecast(c, anchor)["output"]["outlook"]["confidence"] == "medium"
        c.execute("update jobs set retry_at=NULL where status='queued' and kind='forecast'")
    assert w.step()
    assert contexts[-1]["已有预测"][0]["scale"] == scale
    with store.connection(True) as c:
        data = forecast.detail(c, pred)
        assert len(data["versions"]) == 2 and data["scenario_label"] == "跨类收紧"
        db.enqueue(c, "verify", event=e, args={"prediction_id": pred})
        before = db.usage(c)["daily_used"]
    assert w.step()
    with store.connection() as c:
        assert db.usage(c)["daily_used"] == before and forecast.detail(c, pred)["status"] == "open"


@pytest.mark.parametrize("published_in_advance", [False, True])
def test_verifier_uses_registered_scale_and_official_result(case, monkeypatch, published_in_advance):
    store, runner, w, api, e, doc, clock = case
    scale = rubric(clock)
    end = clock.utc(clock.dt + timedelta(days=180))
    with store.connection(True) as c:
        pid = forecast.new(
            c,
            e,
            "本轮正式措施的力度分几档？",
            end,
            "以主管单位带日期的最终公告判断",
            HOST,
            None,
            "有直接参考材料",
            [doc],
            "登记",
            scale=scale,
            outlook=opinion(doc),
        )
    clock.dt += timedelta(days=180, hours=2)
    public = clock.utc(clock.dt - (timedelta(days=40) if published_in_advance else timedelta(hours=1)))
    w.acquisition.articles.append(("verified-graded", "海岚市域线最终实施公告", "主管单位确认正式措施涉及一个已登记类别。", public))
    with store.connection(True) as c:
        job = db.enqueue(c, "verify", event=e, args={"prediction_id": pid})

    def check(role, ctx):
        assert role == "verify" and ctx["预测"]["scale"] == scale
        decisive = next(d for d in ctx["evidence"] if d["title"] == "海岚市域线最终实施公告")
        return agents.Verdict(
            outcome="level",
            observed_level=1,
            observed_at=end,
            public_at=public,
            reason="主管单位回顾窗口内生效的正式措施。",
            lesson="区分公告日和生效日。",
            evidence_ids=[decisive["id"]],
        ), (7, 6)

    monkeypatch.setattr(runner, "call", check)
    assert w.step()
    with store.connection() as c:
        assert db.require(c, "jobs", job)["status"] == "done"
        result = forecast.detail(c, pid)
        assert result["status"] == "resolved" and result["settlements"][0]["scores"]["observed_level"] == 1
