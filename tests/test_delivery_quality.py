"""Regressions for archived quotations, the delivered report and searchable provenance."""
import json

import pytest

from evosight import cards, db, delivery, feeds, grounding, materials, research, workflow, world, world_search
from evosight.contracts import Citation, DeliveryAnswer, Finding, Plan, Report, ResearchLookup
from test_workflow import clock, new_event, setup


def test_invalid_optional_lookup_does_not_discard_the_research_plan():
    task = dict(
        role="news", question="哪天完成交割", target="公告", time_scope="window", done_when="取得交割公告", priority=3
    )
    plan = Plan.model_validate(
        dict(
            rationale="核对公告",
            tasks=[
                {**task, "knowledge_query": {"path": "unknown/path"}},
                {**task, "knowledge_query": {"path": "events"}},
            ],
            checklist=["实际交割日期"],
        )
    )
    assert plan.tasks[0].knowledge_query is None and plan.tasks[1].knowledge_query.path == "events"
    assert len(plan.postponed) == 1 and plan.checklist == ["实际交割日期"]


def test_unknown_observation_id_does_not_reject_grounded_findings():
    quote = "The issuer confirmed the closing."
    finding = Finding(
        section="news",
        kind="fact",
        subject="交割",
        text="发布方已确认交割。",
        citations=[Citation(fragment_id=1, quote=quote)],
    )
    report = Report(findings=[finding], answered=[7, 99])
    rejected = []
    research.check_report(
        report, [{"id": 1, "document_id": 1, "text": quote}], {"observations": [{"id": 7}]},
        "news", rejections=rejected,
    )
    assert report.findings == [finding] and report.answered == [7]
    assert rejected == [("观察项", "已忽略不属于本轮的观察项 ID 99")]


def test_quote_recovers_exact_source_and_only_a_unique_visible_paragraph():
    body = "reported shares " * 80 + "“Tranche B”\nclosed at 87.64."
    fragments = {
        1: {"document_id": 3, "text": "An earlier paragraph."},
        2: {"document_id": 3, "text": body},
        3: {"document_id": 9, "text": body},
    }
    excerpt = "…" + body.replace("“", '"').replace("”", '"').replace("\n", " ") + "…"
    found = grounding.locate(Citation(fragment_id=1, quote=excerpt), fragments)
    assert found == Citation(fragment_id=2, quote=body)
    assert grounding.verbatim("leading   space", "  ") is None
    unrelated = {1: fragments[1], 3: fragments[3]}
    for quote, visible in [
        (excerpt, unrelated),
        (excerpt, {**fragments, 4: fragments[2]}),
        ("closed at 87.46", fragments),
        ("reported shares … closed", fragments),
        ("did not close at 87.64", fragments),
    ]:
        with pytest.raises(ValueError, match="定位"):
            grounding.locate(Citation(fragment_id=1, quote=quote), visible)


@pytest.mark.parametrize("field", ["$.attributes['value']", "/attributes/value", "attributes.数值", "value"])
def test_field_paths_and_list_input_keep_the_verified_fact(field):
    ref = {"fragment_id": 1, "quote": "close 87.64"}
    finding = Finding.model_validate(
        dict(
            section="news",
            kind="fact",
            category="indicators",
            subject="收盘",
            text="收盘为87.64",
            citations=[ref],
            attributes={"value": 87.64, "format": "PDF"},
            field_citations=[
                {"field": field, **ref},
                {"field": "format", "fragment_id": 1, "quote": "unseen text"},
            ],
        )
    )
    rejected = []
    research.check_report(
        Report(findings=[finding]), [{"id": 1, "text": ref["quote"]}], {}, "news", rejections=rejected
    )
    assert finding.field_citations == {"attributes.value": Citation(**ref)}
    assert finding.attributes == {"value": 87.64} and rejected


def test_report_distinguishes_omitted_unknown_and_invalid_numeric_answer():
    plan = {"checklist": ["实际私募股数", "第二笔股数", "结算日期", "风险结论", "是否有正文"]}
    archive = {10: {"id": 10, "document_id": 2, "text": "The sale of 12,000,000 shares closed."}}
    ref = Citation(fragment_id=10, quote=archive[10]["text"])

    def answer(i, text, state="confirmed"):
        return DeliveryAnswer(
            check_id=i, state=state, answer=text, citations=[ref] if state == "confirmed" else []
        )

    verified = delivery.inspect(
        plan,
        [
            answer(1, "已交割1200万股"),
            answer(2, "已交割1201万股"),
            answer(3, "正等待书面通知", "unknown"),
            answer(5, "已经确认"),
            answer(5, "正文可核"),
        ],
        archive,
    )
    assert [r["state"] for r in verified] == ["confirmed", "rejected", "unknown", "missing", "rejected"]
    assert verified[0]["citations"][0]["quote"] == ref.quote
    assert "数值" in verified[1]["reason"]
    assert delivery.inspect(plan, [answer(1, "已查公告")], archive)[0]["state"] == "rejected"
    originals = ["The close was 87.64.", "The offering price was $83.00 per share."]
    assert delivery.supported_quantities("收盘87.64美元，发行价83.00美元，差额4.64美元，溢价率约5.59%。", originals)
    assert not delivery.supported_quantities("收盘87.64美元，发行价83.00美元，差额4.65美元。", originals)
    assert not delivery.supported_quantities("变化为负3.25%。", ["The change was +3.25 percent."])
    assert delivery.supported_quantities("总额为$26 billion。", ["gross proceeds of $26 billion"])


def test_sibling_sentence_and_prefixed_facet_use_same_archived_paragraph():
    source = [
        {"id": 11, "document_id": 4, "text": "The price was $83.00. The conversion threshold is $83.00."}
    ]
    primary = Citation(fragment_id=11, quote="The price was $83.00.")
    facet = Citation(fragment_id=11, quote="conversion threshold is $83.00")
    item = Finding(
        section="news",
        kind="fact",
        subject="转换条件",
        text="价格为83.00美元",
        category="events",
        facets={"阈值": "83.00美元"},
        attributes={"自定义发行价": 83.0},
        citations=[primary],
        field_citations={"facets.阈值": facet, "attributes.自定义发行价": primary},
    )
    rejected = []
    research.check_report(Report(findings=[item]), source, {}, "dependencies", rejections=rejected)
    assert item.field_citations == {"facets.阈值": facet} and item.attributes == {}
    assert rejected and "未注册此属性" in rejected[0][1]
    item.attributes = {"自定义发行价": 86.0}
    item.text = "价格为86.00美元"
    with pytest.raises(ValueError, match="数值"):
        research.check_report(Report(findings=[item]), source, {}, "dependencies")


def test_typed_publisher_refresh_matches_the_same_archived_day(tmp_path):
    store = db.Store(tmp_path)
    url = "https://stockanalysis.com/stocks/example/history/"
    quote = "Trading day 2026-11-23. Close 87.64."
    with store.connection(True) as c:
        event = db.add_event(c, "收盘记录", "核对单个交易日", [])
        doc = feeds.ingest(
            c,
            event,
            url,
            {"url": url, "title": "日线", "summary": quote, "published_at": "2026-11-23T21:00:00Z"},
        )
        materials.summary_fragment(c, db.require(c, "documents", doc))
        f = materials.fragments_for(c, [doc])[0]
        draft = Finding(
            section="news",
            kind="fact",
            category="indicators",
            subject="日线收盘",
            text="收盘87.64美元",
            citations=[Citation(fragment_id=f["id"], quote=quote)],
            attributes={"value": 87.64},
        )
        version, _ = research.claim(c, event, "news", draft, {f["id"]: f})
        entry = world.version(c, version)["entry_id"]

        def record(text):
            return materials.save_asset(
                c,
                doc,
                url,
                "daily_prices",
                text.encode(),
                "text/plain",
                materials.Extract("daily_prices", parts=[("日线档案", text, url)]),
                extractor="publisher-record-v1",
            )

        record("Trading day 2026-11-20. Close 88.91.")
        assert "publisher" not in world.detail(c, entry)["attributes"]
        asset = record(quote)
        detail = world.detail(c, entry)
        assert detail["attributes"]["publisher"] == "Stock Analysis"
        assert detail["card_evidence"]["publisher"][0]["asset_id"] == asset


def test_intermediate_finding_reaches_publication_and_repair_is_bounded(tmp_path, clock):
    store, _, runner, worker = setup(tmp_path, ["news"])
    info = new_event(store)
    normal = runner.call
    plan_questions = ["谁发布验收方案", "验收方案日期", "实际通车时间", "检索缺口"]
    captured = {}

    def call(role, ctx):
        proposal, usage = normal(role, ctx)
        if role == "plan":
            proposal.checklist = plan_questions
        elif role == "news":
            if not ctx["工具回执"]:
                quote = proposal.findings[0].citations[0]
                proposal.findings[0].text = "中间找到公告的发布机构"
                proposal.lookup = ResearchLookup(
                    tool="read", query="重读原文", document_ids=[ctx["fragments"][0]["document_id"]]
                )
                captured["ref"] = quote
            else:
                proposal.findings = []
        elif role == "study":
            assert not ctx["本轮研究"][0]["findings"]
            assert any(lead["text"] == "中间找到公告的发布机构" for lead in ctx["中间调查线索"])
            ref = captured["ref"]
            assert any(f["id"] == ref.fragment_id and ref.quote in f["text"] for f in ctx["交付可引用原文"])
            if "交付前复核" in ctx:
                assert [r["position"] for r in ctx["交付前复核"]["待修项"]] == [2, 3, 4]
                proposal.delivery = [
                    DeliveryAnswer(
                        check_id=3,
                        state="confirmed",
                        answer="已正式通车",
                        citations=[Citation(fragment_id=ref.fragment_id, quote="未公开的虚构原文")],
                    ),
                    DeliveryAnswer(check_id=4, state="unknown", answer="尚缺正式通车公告"),
                ]
            else:
                proposal.delivery = [
                    DeliveryAnswer(check_id=1, state="confirmed", answer="海岚交通局公布了方案", citations=[ref])
                ]
        return proposal, usage

    runner.call = call
    assert worker.step()
    with store.connection() as c:
        snapshot = workflow.latest(c, info["id"])
        assert snapshot and runner.calls["study"] == 2
        result = workflow.knowledge(c, info["id"], revision=snapshot["id"])["delivery"]
        assert [r["state"] for r in result] == ["confirmed", "missing", "rejected", "unknown"]
        assert result[0]["citations"][0]["quote"] == captured["ref"].quote
        assert result[1]["answer"] == result[2]["answer"] == ""
        assert "定位" in result[2]["reason"]
        assert c.execute("SELECT count(*) FROM delivery_answers").fetchone()[0] == 2


def test_cards_are_searchable_only_with_archive_or_proposal_evidence(tmp_path):
    store = db.Store(tmp_path)
    url = "https://filings.example.org/close"
    statement = "The issuer announced the closing of the public offering."
    with store.connection(True) as c:
        event = db.add_event(c, "发行进度", "核对公告", [])
        doc = feeds.ingest(
            c,
            event,
            url,
            {"url": url, "title": "交割", "summary": statement, "published_at": "2026-11-20T15:00:00Z"},
        )

        def capture(metadata):
            header = json.dumps(metadata)
            raw = f'<html><head><script type="application/ld+json">{header}</script></head><body>{statement}</body></html>'.encode()
            materials.save_asset(
                c,
                doc,
                url,
                "html",
                raw,
                "text/html",
                materials.Extract("html", parts=[("段落", statement, url)]),
            )

        capture({})
        old = materials.fragments_for(c, [doc])[0]
        capture({"@type": "NewsArticle", "publisher": {"name": "不可归属"}, "url": url + "/other"})
        capture({"@type": "NewsArticle", "publisher": {"name": "发行方"}, "url": url})
        find = Finding(
            section="news",
            kind="fact",
            category="events",
            subject="发行交割",
            text="发行方公布交割",
            attributes={"publisher": "模型猜测", "source": url, "format": "网页正文", "stage": "已交割"},
            citations=[Citation(fragment_id=old["id"], quote=statement)],
        )
        version, _ = research.claim(c, event, "news", find, {old["id"]: old})
        entry = world.version(c, version)["entry_id"]
        read = world_search.read_records(c, [{"entry_id": entry}], evidence=True)[0]
        expected = {"publisher": "发行方", "source": url, "format": "网页正文", "stage": "已交割"}
        assert read["attributes"] == expected
        assert read["card_evidence"]["publisher"][0]["method"] == "archive"
        assert read["card_evidence"]["stage"][0]["quote"] == statement
        assert (
            world_search.search(c, path="events", qualifiers={"publisher": "发行方", "stage": "已交割"})[0][0][
                "entry_id"
            ]
            == entry
        )
        assert not world_search.search(c, path="events", qualifiers={"publisher": "模型猜测"})[0]
        cits = [{"document_id": doc, "fragment_id": old["id"], "quote": statement, "locator": "段落"}]
        checked, _ = cards.verify(
            c, cits, {"source": "https://unknown.example/report", "format": "PDF", "stage": "已通过"}, []
        )
        assert checked == {"publisher": "发行方"}
        capture({"@type": "NewsArticle", "publisher": {"name": "另一署名"}, "url": url})
        assert cards.verify(c, cits, {}, [])[0] == {}
        assert not world_search.search(c, path="events", qualifiers={"publisher": "发行方"})[0]
        assert not cards.supported("stage", "已交割", {}, "交割尚未完成，预计交割将在明日发生。")
        assert not cards.supported("format", "PDF", {"kind": "html"}, "PDF not available.")
