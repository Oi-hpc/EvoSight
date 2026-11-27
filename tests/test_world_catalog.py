"""Agent-facing discovery and historical typed records."""
import json
import pytest
from fastapi.testclient import TestClient

from evosight import feeds, materials, research, world, world_catalog as catalog, world_search
from evosight.api import make_app
from evosight.contracts import Citation, Finding, Report
from evosight.db import Store, add_event, require
from test_workflow import clock, setup, new_event


def metric(c, event, value, *, previous=0, period="2026-10", region=""):
    month = "十月份" if period == "2026-10" else period
    quote = f"2026-11-25 23:59:59 UTC 公布{month}{region}居民消费价格同比变化 {value}%。"
    url = f"https://statistics.example.org/publication/{event}/{period}/{region}/{value}"
    doc = feeds.ingest(
        c,
        event,
        url,
        {"url": url, "title": "十月份价格公告", "summary": quote, "published_at": "2026-11-25T23:59:59Z"},
    )
    materials.summary_fragment(c, require(c, "documents", doc))
    fragments = materials.fragments_for(c, [doc])
    finding = Finding(
        section="news",
        kind="correction" if previous else "fact",
        category="indicators",
        subject="居民消费价格月度数据",
        text=quote,
        citations=[Citation(fragment_id=fragments[0]["id"], quote=quote)],
        occurred_at="2026-11-25T23:59:59Z",
        time_quote=quote,
        replaces=previous,
        attributes={
            "measure": "居民消费价格",
            "value": value,
            "unit": "%",
            "period": period,
            **({"region": region} if region else {}),
            "comparison": "同比",
        },
    )
    ctx = {"prior": [{"id": previous}]} if previous else {}
    research.check_report(Report(findings=[finding]), fragments, ctx, "news")
    revision, _ = research.claim(c, event, "news", finding, {f["id"]: f for f in fragments})
    return world.version(c, revision)["entry_id"], revision


def test_route_finds_an_empty_directory_and_its_schema(tmp_path):
    store = Store(tmp_path)
    with TestClient(
        make_app(store, background=False), base_url="http://127.0.0.1", headers={"X-EvoSight": "local"}
    ) as api:
        manifest = api.get("/api/world/manifest").json()
        path = "indicators/prices/consumer"
        assert path in {row["path"] for row in manifest["paths"]}
        route = api.get(manifest["routes"]["route"], params={"q": "ＣＰＩ 的统计期和单位"}).json()["routes"][0]
        assert route["path"] == path and set(route["matched_fields"]) == {"period", "unit"}
        assert api.get("/api/world/schemas/indicators").json()["properties"]["value"]["type"] == "number"
        assert api.get("/api/world/catalog", params={"path": path}).json()["count"] == 0
        assert api.get("/api/world/search", params={"path": path, "q": "CPI"}).json()["entries"] == []
        assert api.get("/api/world/route", params={"q": "XYZQ陌生项目"}).json()["routes"] == []
        assert (
            api.get("/api/world/route", params={"q": "统计期"}).json()["routes"][0]["schema_id"] == "indicators"
        )
        assert api.get("/api/world/search", params={"q": "x" * 2001}).status_code == 422
    assert catalog.directory_for("indicators", "CPI 与 PPI", "", "") == "indicators/prices"
    assert catalog.directory_for("indicators", "居民消费价格", "附录也对比工业增加值") == path
    assert catalog.route("xCPIy") == []


def test_typed_qualifiers_disambiguate_series_and_keep_original_value(tmp_path):
    store = Store(tmp_path)
    with store.connection(True) as c:
        event = add_event(c, "两地两期价格月报", "按统计期和地区检索", [], status="paused")
        sep, old = metric(c, event, 0.7, period="2026年9月", region="东区")
        metric(c, event, 0.7, period="2026-10", region="东区")
        metric(c, event, 0.8, period="2026年9月", region="西区")
        assert world_search.read_records(c, [{"entry_id": sep}])[0]["attributes"]["period"] == "2026年9月"
        sql, args = world_search.candidate_query(
            path="indicators/prices", qualifiers={"period": "2026-09", "region": "东区"}
        )
        plan = [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql, args)]
        assert sum("term=?" in item for item in plan) == 2
        assert (
            world_search.search(c, "CPI", path="indicators", qualifiers={"period": "2026/9", "region": "东区"})[
                0
            ][0]["entry_id"]
            == sep
        )
        metric(c, event, 0.9, previous=old, period="2026年9月", region="东区")
    with TestClient(make_app(store, background=False), base_url="http://127.0.0.1") as api:

        def lookup(values):
            return api.get(
                "/api/world/search", params={"path": "indicators/prices", "qualifiers": json.dumps(values)}
            )

        assert [
            x["entry_id"]
            for x in lookup({"period": "2026-09", "region": "东区", "value": 0.9}).json()["entries"]
        ] == [sep]
        assert lookup({"period": "2026-09", "region": "东区", "value": 0.7}).json()["entries"] == []
        for value in ({"action": "召开会议"}, {"value": True}, None, []):
            assert lookup(value).status_code == 422
        assert api.get("/api/world/search", params={"qualifiers": '{"period":"2026-09"}'}).status_code == 422


def test_pinned_records_filter_and_projection(tmp_path):
    store = Store(tmp_path)
    with store.connection(True) as c:
        event = add_event(c, "物价月报", "读取统计口径", [], status="paused")
        entry, old = metric(c, event, 0)
        _, new = metric(c, event, 0.7, previous=old)
        sample = add_event(c, "样例", "用来核对隔离", [], status="paused")
        c.execute("UPDATE events SET demo=1 WHERE id=?", (sample,))
        demo, demo_version = metric(c, sample, 1.1)
        assert old != new
        by_path = world_search.directory(c, "indicators/prices")
        assert by_path["count"] == 1 and by_path["direct_count"] == 0
        assert {i["path"]: i["count"] for i in by_path["children"]} == {
            "indicators/prices/consumer": 1,
            "indicators/prices/producer": 0,
        }
    with TestClient(
        make_app(store, background=False), base_url="http://127.0.0.1", headers={"X-EvoSight": "local"}
    ) as api:
        query = {
            "q": "CPI",
            "path": "indicators/prices",
            "occurred_from": "2026-11-25",
            "occurred_to": "2026-11-25",
        }
        result = api.get("/api/world/search", params=query).json()["entries"]
        assert [(h["entry_id"], h["version_id"]) for h in result] == [(entry, new)]
        assert result[0]["text_complete"] and "attributes" not in result[0]
        assert not api.get(
            "/api/world/search", params={**query, "occurred_from": "2026-11-24", "occurred_to": "2026-11-24"}
        ).json()["entries"]
        assert api.get("/api/world/search", params={"occurred_to": "20261125"}).status_code == 422
        keys = [
            {"entry_id": entry, "version_id": old},
            {"entry_id": entry},
            {"entry_id": demo},
            {"entry_id": entry, "version_id": demo_version},
        ]
        records = api.post(
            "/api/world/records",
            json={"records": keys, "fields": ["value", "unit", "region", "action"], "evidence": True},
        ).json()["records"]
        assert [r["status"] for r in records] == ["ok", "ok", "not_found", "not_found"]
        for record, value in zip(records, [0, 0.7]):
            assert record["attributes"] == {"value": value, "unit": "%"}
            assert record["missing_fields"] == ["region"] and record["undefined_fields"] == ["action"]
            assert str(value) + "%" in record["citations"][0]["quote"]
        assert [r["version"] for r in records[:2]] == [1, 2]
    with Store(tmp_path).connection() as c:
        assert world_search.directory(c)["count"] == 1 and world_search.directory(c, demo=1)["count"] == 1


def test_only_sourced_numeric_values_can_be_written():
    fragments = [{"id": 4, "document_id": 7, "text": "同比增长 0.7%。"}]
    finding = Finding(
        section="news",
        kind="fact",
        subject="物价",
        category="indicators",
        text="同比增长",
        citations=[Citation(fragment_id=4, quote="同比增长 0.7%。")],
        attributes={"value": 7},
    )
    with pytest.raises(ValueError, match="原始数字"):
        research.check_report(Report(findings=[finding]), fragments, {}, "news")
    measured = finding.model_copy(update={"attributes": {"value": 0.7, "unit": "%"}})
    report = Report(findings=[measured, finding], answered=[9])
    result = research.check_report(report, fragments, {"observations": [{"id": 9}]}, "news")
    assert [f.attributes["value"] for f in result.findings] == [0.7]
    assert result.answered == [] and any("物价" in gap for gap in result.gaps)
    untyped = measured.model_copy(update={"attributes": {"filing_date": "2026-11-25"}})
    unsupported = measured.model_copy(update={"citations": [Citation(fragment_id=27, quote="unknown")]})
    mixed = Report(findings=[untyped, measured, unsupported])
    audited = research.check_report(mixed, fragments, {}, "news")
    assert audited.findings == [measured]
    assert "filing_date" in audited.gaps[0] and "引文" in audited.gaps[0]
    for invalid in (True, float("inf"), 10**500):
        with pytest.raises(ValueError):
            catalog.attributes("indicators", {}, {"value": invalid})
    assert catalog.attributes("opinion", {"观察数": "-1"}) == {}
    with pytest.raises(ValueError):
        catalog.attributes("indicators", {}, {"count": 7})
    with pytest.raises(ValueError):
        catalog.directory_for("events", "产品发布", "", "indicators/prices")


def test_role_can_find_and_pin_outside_the_planner_candidates(tmp_path, clock, monkeypatch):
    from evosight import workflow
    from evosight.contracts import Assignment, KnowledgeLink, KnowledgeQuery, Plan
    from evosight.db import one

    store, _, runner, worker = setup(tmp_path, ["news"])
    with store.connection(True) as c:
        origin = add_event(c, "统计留存", "已核对的宏观资料", [], status="paused")
        entry, old = metric(c, origin, 0.7)
    tracked = new_event(store)
    old_call = runner.call
    reads = []
    assignment = Assignment(
        role="news",
        question="核对预算使用的物价背景",
        target="CPI",
        time_scope="history",
        priority=4,
        done_when="核实统计期与数值",
        knowledge_query=KnowledgeQuery(
            query="CPI",
            path="indicators/prices",
            qualifiers={"period": "2026年10月"},
            fields=["period", "value"],
        ),
    )

    def role_call(role, ctx):
        if role == "plan":
            assert entry not in {w["entry_id"] for w in ctx["world_candidates"]}
            return Plan(tasks=[assignment], rationale="核对背景数据"), (3, 2)
        if role == "news":
            record = ctx["world_records"][0]
            reads.append(record["version_id"])
            assert record["attributes"] == {"value": 0.7, "period": "2026-10"}
            assert "unit" in ctx["record_schemas"]["indicators"]
            assert any("同比变化 0.7%" in f["text"] for f in ctx["fragments"])
            return Report(
                use_knowledge=[
                    KnowledgeLink(entry_id=entry, version_id=old, section="background", reason="预算物价口径的既有依据")
                ]
            ), (5, 4)
        return old_call(role, ctx)

    monkeypatch.setattr(runner, "call", role_call)
    assert worker.step()
    with store.connection(True) as c:
        assert reads == [old]
        used = workflow.knowledge(c, tracked["id"])["sections"]["background"]
        assert [(r["world_id"], r["index_reason"]) for r in used] == [(entry, "预算物价口径的既有依据")]
        metric(c, origin, 1.2, previous=old)
        r = require(c, "tracking_rounds", tracked["round"])
        task = one(c, "SELECT * FROM research_tasks WHERE round_id=?", (r["id"],))
        frozen = research.task_knowledge(c, r, task, assignment.model_dump(), {"use_knowledge": []})
        assert frozen["world_records"][0]["version_id"] == old
        assert frozen["world_records"][0]["attributes"]["value"] == 0.7


def test_same_quote_can_fill_missing_fields_once(tmp_path):
    store = Store(tmp_path)
    with store.connection(True) as c:
        event = add_event(c, "旧资料", "补入可核对字段", [], status="paused")
        entry, old = metric(c, event, 0)
        quote = "2026-11-25 23:59:59 UTC 公布十月份居民消费价格同比变化 0%。"
        record = world_search.read_records(c, [{"entry_id": entry}], evidence=True)[0]
        fragment_id = record["citations"][0]["fragment_id"]
        parts = materials.fragments_for(c, record["refs"])
        finding = Finding(
            section="news",
            kind="fact",
            category="indicators",
            supports=old,
            citations=[Citation(fragment_id=fragment_id, quote=quote)],
            attributes={"method": "居民消费价格同比变化"},
        )
        research.check_report(Report(findings=[finding]), parts, {"world_records": [record]}, "news")
        assert (finding.subject, finding.text) == (record["subject"], quote)
        indexed = {p["id"]: p for p in parts}
        new, changed = research.claim(c, event, "news", finding, indexed)
        assert changed and new != old
        previous, current = world_search.read_records(
            c, [{"entry_id": entry, "version_id": old}, {"entry_id": entry}]
        )
        assert previous["evidence_count"] == current["evidence_count"] == 1
        assert "method" not in previous["attributes"] and current["attributes"]["method"] == "居民消费价格同比变化"
        assert research.claim(c, event, "news", finding, indexed) == (new, False)


def test_conflicting_support_preserves_other_results_and_raw_report(tmp_path):
    store = Store(tmp_path)
    with store.connection(True) as c:
        event = add_event(c, "价格月报", "核对统计口径", [], status="paused")
        entry, original = metric(c, event, 0)
        record = world_search.read_records(c, [{"entry_id": entry}], evidence=True)[0]
        citation = Citation(fragment_id=record["citations"][0]["fragment_id"], quote=record["text"])
        conflict = Finding(
            section="news", kind="fact", supports=original, citations=[citation], attributes={"unit": "个百分点"}
        )
        independent = Finding(
            section="news", kind="fact", subject="原文使用的单位", text="公告以百分比公布同比变化。", citations=[citation]
        )
        raw = Report(findings=[conflict, independent]).model_dump_json()
        accepted = research.validated_task_report(c, {"result": raw})
        assert accepted.findings == [independent]
        assert len(accepted.gaps) == 1 and "unit" in accepted.gaps[0]
        assert Report.model_validate_json(raw).findings[0] == conflict
        assert world_search.read_records(c, [{"entry_id": entry}])[0]["attributes"]["unit"] == "%"


def test_whole_job_retry_reuses_pinned_role_read_and_passes_validation_feedback(tmp_path, clock, monkeypatch):
    from datetime import timedelta
    from evosight import workflow
    from evosight.contracts import Assignment, KnowledgeQuery, Plan
    from evosight.db import one

    store, _, runner, worker = setup(tmp_path, ["news"])
    with store.connection(True) as c:
        origin = add_event(c, "资料留存", "对比口径", [], status="paused")
        entry, old = metric(c, origin, 0)
    tracked = new_event(store)
    original = runner.call
    seen = []

    def model(role, ctx):
        if role == "plan":
            return Plan(
                rationale="核对物价背景",
                tasks=[
                    Assignment(
                        role="news",
                        question="核验统计",
                        target="物价",
                        time_scope="history",
                        done_when="引用指标",
                        priority=3,
                        knowledge_query=KnowledgeQuery(query="CPI"),
                    )
                ],
            ), (2, 1)
        if role == "news":
            rec = ctx["world_records"][0]
            seen.append(rec["version_id"])
            frag = next(p for p in ctx["fragments"] if "同比变化 0%" in p["text"])
            item = Finding(
                section="news",
                kind="fact",
                supports=rec["version_id"],
                text="附加一段不同的结论" if len(seen) == 1 else "",
                citations=[Citation(fragment_id=frag["id"], quote="居民消费价格同比变化 0%")],
                attributes={"method": "居民消费价格同比变化"},
            )
            if len(seen) == 2:
                assert "省略 subject" in ctx["上次校验反馈"]
            return Report(findings=[item]), (4, 2)
        return original(role, ctx)

    monkeypatch.setattr(runner, "call", model)
    assert worker.step()
    with store.connection() as c:
        assert (
            one(c, "SELECT state FROM research_tasks WHERE round_id=?", (tracked["round"],))["state"]
            == "failed"
        )
    clock.dt += timedelta(seconds=50)
    assert worker.step()
    with store.connection() as c:
        assert seen == [old, old] and workflow.latest(c, tracked["id"])
        assert world_search.read_records(c, [{"entry_id": entry}])[0]["attributes"]["method"] == "居民消费价格同比变化"
