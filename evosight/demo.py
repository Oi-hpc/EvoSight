"""Two explicitly fictional, local scenarios with a shared original source."""
from datetime import datetime, timedelta, timezone

from . import forecast
from .db import add_event, node, one, utc
from .feeds import ingest


def seed(c):
    if one(c, "SELECT 1 FROM events WHERE demo=1 LIMIT 1"):
        return
    now = datetime.now(timezone.utc)

    def ago(days=0, hours=0):
        return utc(now - timedelta(days=days, hours=hours))

    a = add_event(c, "海岚线一期试运行", "年底前，海岚线一期能否完成全线试运行？", ["https://transit.example.invalid/feed"], demo=True)
    b = add_event(c, "海岚线列车交付", "首批列车交付与线路联调能否按计划衔接？", ["https://rail.example.invalid/feed"], demo=True)
    feed_a = "https://transit.example.invalid/feed"
    feed_b = "https://rail.example.invalid/feed"

    def article(url, title, summary, at):
        return dict(
            url="https://transit.example.invalid/" + url, title=title, summary=summary, published_at=at
        )

    first = article("engineering-brief", "工程周报 · 联调窗口获批", "建设方确认首段联调窗口，末段供电验收仍在等待。", ago(12))
    second = article("test-log", "运营简报 · 两座车站通过限界复测", "本轮联调记录确认两座车站完成限界复测。末段供电验收时间尚未公布。", ago(hours=5))
    third = article("delivery", "制造周报 · 首批列车到段", "两列车已交付车辆段，完成静态检查，动态测试仍待调度。", ago(hours=3))
    dispatch = article("dispatch", "制造计划 · 首批列车进入发运窗口", "制造方确认首批两列车的发运安排，交付时间仍受线路窗口影响。", ago(13))
    d1 = ingest(c, a, feed_a, first)
    d2 = ingest(c, a, feed_a, second)
    d3 = ingest(c, b, feed_b, third)
    d4 = ingest(c, b, feed_b, dispatch)
    ingest(c, b, feed_b, second)
    sub = c.execute("INSERT INTO items(event_id,name,pinned) VALUES(?,?,1)", (a, "末段供电与联调窗口")).lastrowid
    node(
        c,
        a,
        "item",
        "末段供电与联调窗口",
        "首段联调窗口已有书面确认，末段仍等待排期。",
        watch="观察下一期工程周报。",
        refs=[d1],
        item=sub,
        at=ago(10),
    )
    node(c, a, "fact", "两座车站完成限界复测，末段供电验收仍待排期。", refs=[d2], at=ago(hours=4), occurred=second["published_at"])
    node(
        c,
        a,
        "judgment",
        "联调的主要风险移向末段供电",
        "复测通过降低了土建返工风险。能否按期开跑，仍取决于供电验收窗口。",
        watch="建设方何时公布末段送电日期？",
        refs=[d1, d2],
        at=ago(hours=4),
    )
    node(
        c,
        a,
        "item",
        "末段供电与联调窗口",
        "复测结果已提交。末段送电日期仍是联调排期的约束。",
        watch="等待送电公告与联合测试排期。",
        refs=[d2],
        item=sub,
        at=ago(hours=4),
    )
    p = forecast.new(
        c,
        a,
        "海岚线一期会在本轮计划截止前启动全线试运行吗？",
        ago(-22),
        "运营方须公告全线空载联调已启动。",
        "海岚运营公告（虚构）",
        48,
        "首段有排期，末段验收未定。",
        [d1],
        "演示研究员",
        at=ago(10),
    )
    forecast.revise(c, p, 62, "限界复测通过，减少一项前置不确定性。", [d2], "演示研究员", at=ago(hours=4))
    c.execute("UPDATE events SET pinned_prediction=? WHERE id=?", (p, a))
    node(c, b, "fact", "首批两列车已到段，静态检查完成。", refs=[d3], at=ago(hours=2), occurred=third["published_at"])
    node(
        c,
        b,
        "judgment",
        "列车交付判断维持",
        "到段数量符合计划。动态测试要等线路可用，交付进度尚不能单独推高通车概率。",
        watch="查看下一份动态调试报告。",
        refs=[d2, d3],
        at=ago(hours=2),
    )
    p2 = forecast.new(
        c,
        b,
        "首批列车是否会在约定窗口内到段？",
        ago(hours=1),
        "制造方公告首批至少两列到段即为是。",
        "海岚制造周报（虚构）",
        65,
        "周报确认发运计划。",
        [d4],
        "演示研究员",
        at=ago(10),
    )
    forecast.revise(c, p2, 72, "临近交付窗口，给发运安排更高权重。", [d4], "演示研究员", at=ago(2))
    forecast.settle(
        c,
        p2,
        "yes",
        third["published_at"],
        "制造周报记录两列车到段，满足登记的数量和时限。",
        [d3],
        "交付和动态验收是不同节点，下次继续分开登记。",
        "演示裁定",
        at=ago(hours=1),
    )
    for event in (a, b):
        c.execute(
            "UPDATE source_state SET last_success=?,last_attempt=? WHERE event_id=?",
            (ago(hours=1), ago(hours=1), event),
        )
        c.execute("UPDATE events SET last_checked=? WHERE id=?", (ago(hours=1), event))
        c.execute("UPDATE event_docs SET analyzed=1 WHERE event_id=?", (event,))
    from .workflow import migrate_existing_knowledge

    migrate_existing_knowledge(c)
