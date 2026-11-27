"""Measure retrieval on the archived pilot corpus and a separate synthetic catalog."""
import argparse
from contextlib import closing
from datetime import date, timedelta
import json
from math import ceil
from pathlib import Path
import sqlite3
from statistics import median
import sys
from time import perf_counter

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project))
from evosight import world, world_catalog as catalog, world_search as search
from evosight.db import Store, add_event, utc

# Labels identify the specific answer in the archived corpus by topic, independently of search scores.
CASES = [
    ("CPI", "居民消费价格月度数据"),
    ("居民消费价格同比", "居民消费价格月度数据"),
    ("规模以上工业增加值", "规模以上工业增加值月度数据"),
    ("工业增加值月度数据", "规模以上工业增加值月度数据"),
    ("v3.6.0-rc.16", "v3.6.0-rc.16"),
    ("v3.6.0-rc.10", "v3.6.0-rc.10"),
    ("Vue 3.6 后续正式版计划", "Vue 3.6 后续正式版计划"),
    ("HomeView 开售地区", "Apple HomeView 官方开售日期、渠道与地区限定"),
    ("HomeView 显示屏", "Apple HomeView 新增显示屏与功能描述"),
    ("国家统计局 国民经济 综合稿", "国家统计局2026年10月份国民经济综合稿"),
    ("PROLED XDR Display", "苹果发布配备 PROLED XDR Display 的新款 MacBook Pro"),
    ("HomeView 产品有什么功能", "Apple HomeView 产品属性与功能描述"),
]
HOLDOUT = [
    ("Ｖ３．６．０－ＲＣ．１３", "v3.6.0-rc.13"),
    ("v3.6.0-rc.12", "v3.6.0-rc.12"),
    ("CPI 月度数据", "居民消费价格月度数据"),
    ("HomeView 购买渠道", "Apple HomeView 官方可用日期与购买渠道"),
    ("Apple HomeView 产品属性", "Apple HomeView 产品属性与功能描述"),
    ("2025年同月稿", "目标稿与易混淆稿件的区分"),
    ("rc.16 具体修复", "rc.16 变更记录的证据状态"),
    ("工业增加值增长", "规模以上工业增加值月度数据"),
]


def timing(call, n=80):
    call()
    times = []
    for _ in range(n):
        start = perf_counter()
        call()
        times.append((perf_counter() - start) * 1000)
    return {
        "samples": n,
        "median_ms": round(median(times), 3),
        "p95_ms": round(sorted(times)[ceil(0.95 * n) - 1], 3),
    }


def measure_pilots(source, base):
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original, closing(
        sqlite3.connect(base / "evosight.sqlite3")
    ) as dest:
        original.backup(dest)
    with Store(base).connection() as c:
        labels = dict(
            c.execute(
                "SELECT k.subject,w.id FROM world_entries w JOIN claims k ON k.id=w.head_id WHERE w.demo=0"
            )
        )

        def evaluate(cases):
            rows = []
            for query, subject in cases:
                target = labels[subject]
                hits = [h["entry_id"] for h in search.search(c, query, limit=3)[0]]
                rank = hits.index(target) + 1 if target in hits else 0
                rows.append(
                    {
                        "q": query,
                        "expected_subject": subject,
                        "expected_id": target,
                        "top3": hits,
                        "rank": rank,
                    }
                )
            return {
                "queries": rows,
                "hit_at_3": sum(bool(r["rank"]) for r in rows) / len(rows),
                "mrr_at_3": sum(1 / r["rank"] for r in rows if r["rank"]) / len(rows),
            }

        negatives = {
            q: [h["entry_id"] for h in search.search(c, q, path=p, limit=3)[0]]
            for q, p in (("PPI", "indicators/prices/producer"), ("KQZ-5588", ""))
        }
        return {
            "entries": len(labels),
            **evaluate(CASES),
            "holdout": evaluate(HOLDOUT),
            "negative_cases": negatives,
        }


def measure_scale(base, count):
    paths = list(catalog.TOPICS)
    store = Store(base)
    with store.connection(True) as c:
        eid = add_event(c, "检索负载样本", "固定规模与查询分布", [], status="paused")
        for i in range(1, count + 1):
            path = paths[(i - 1) % len(paths)]
            category = path.split("/")[0]
            label = catalog.PATHS[path][0]
            subject = f"{label} 样本 EV-{i:06d}"
            day = (date(2025, 1, 1) + timedelta(days=i % 365)).isoformat()
            ident = c.execute(
                """INSERT INTO claims(event_id,role,section,kind,subject,text,occurred_at,precision,reason,
                       refs,citations,identity,created_at) VALUES(?,'news','news','fact',?,?,?,'date','','[]','[]',?,?)""",
                (eid, subject, subject + " 用于测量检索耗时。", day, str(i), day + "T00:00:00Z"),
            ).lastrowid
            attributes = {"period": day[:7], "region": "地区" + str(i % 3)} if category == "indicators" else {}
            world.register(c, ident, category=category, path=path, attributes=attributes)
        c.execute("ANALYZE")
    with store.connection() as c:
        leaf = "indicators/prices/consumer"
        mid = count // 2
        keys = [{"entry_id": mid, "version_id": mid}, {"entry_id": count, "version_id": count}]
        cases = {
            "exact_code": (f"EV-{mid:06d}", {}),
            "cpi": ("CPI", {}),
            "leaf": ("", {"path": leaf}),
            "qualified_indicator": ("", {"path": leaf, "qualifiers": {"period": "2025年3月", "region": "地区1"}}),
            "date_range": ("", {"occurred_from": "2025-03-01", "occurred_to": "2025-03-02"}),
        }
        measurements = {}
        for name, (text, filters) in cases.items():
            query, args = search.candidate_query(text, **filters)
            order = "score DESC," if text else ""
            plan = [
                r[3]
                for r in c.execute(
                    "EXPLAIN QUERY PLAN "
                    + query
                    + " ORDER BY "
                    + order
                    + "m.recorded_at DESC,m.entry_id DESC LIMIT 8",
                    args,
                )
            ]
            measurements[name] = {**timing(lambda: search.search(c, text, **filters)), "plan": plan}
        measurements["batch_two_versions"] = timing(
            lambda: search.read_records(c, keys, ["value", "period"], evidence=True)
        )
        measurements["schema_route"] = timing(lambda: catalog.route("CPI 的统计期与单位"))
        measurements["directory_counts"] = timing(lambda: search.directory(c, "indicators"))
        return {"entries": count, "sqlite_version": sqlite3.sqlite_version, "queries": measurements}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New directory for isolated databases and measurement JSON")
    parser.add_argument(
        "--pilots", type=Path, help="Read-only source containing the three documented pilot cases"
    )
    parser.add_argument("--entries", type=int, default=10000)
    args = parser.parse_args()
    if not 0 <= args.entries <= 100000 or (not args.pilots and not args.entries):
        parser.error("select a pilot database or 1 to 100,000 synthetic entries")
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"measured_at": utc()}
    if args.pilots:
        pilots = args.output / "pilots"
        pilots.mkdir()
        report["pilots"] = measure_pilots(args.pilots, pilots)
        print("pilot ranks:", [(x["q"], x["rank"]) for x in report["pilots"]["queries"]], flush=True)
    if args.entries:
        report["scale"] = measure_scale(args.output / "scale", args.entries)
    (args.output / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(args.output / "metrics.json", flush=True)
