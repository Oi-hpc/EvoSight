import argparse
import json
from pathlib import Path

from .agents import Runner
from .db import Store, add_event, enqueue, one, rows
from .demo import seed
from .worker import Worker


def main():
    parser = argparse.ArgumentParser(description="EvoSight 本地事件研究工作台")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动本地工作台与单 worker")
    serve.add_argument("--mode", choices=["live", "test"], default="live")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--data", default="data")
    serve.add_argument("--demo", action="store_true", help="添加两条明确标记的虚构演示事件")
    smoke = commands.add_parser("smoke", help="用真实公开来源和低推理强度执行一轮试跑")
    smoke.add_argument("--data", default=".work/smoke", help="指定一个无其他任务的试跑数据库目录")
    check = commands.add_parser("check-feed", help="验证公开 RSS/Atom 的条目和时间字段")
    check.add_argument("url")
    check.add_argument("--recheck", action="store_true", help="再请求一次以验证 HTTP 条件请求")
    trial = commands.add_parser("workflow-trial", help="通过低强度真实模型和三个公开案例验证工作流")
    trial.add_argument("--data", default=".work/workflow-trial")
    trial.add_argument("--case", choices=["all", "software", "public", "industry"], default="all")
    args = parser.parse_args()
    if args.command == "workflow-trial":
        from .trial import CASES, run_trials

        if not run_trials(args.data, list(CASES) if args.case == "all" else [args.case]):
            raise SystemExit(1)
        return
    if args.command == "check-feed":
        from .feeds import poll

        try:
            page = poll(args.url)
            result = {
                "entries": len(page.entries),
                "exact_published": sum(bool(e["published_at"]) for e in page.entries),
                "imprecise_published": sum(bool(e.get("published_label")) for e in page.entries),
                "source_updated": sum(bool(e.get("source_updated_at")) for e in page.entries),
                "etag": bool(page.etag),
                "last_modified": bool(page.modified),
            }
            if args.recheck:
                repeated = poll(
                    args.url, {"resource": page.resource, "etag": page.etag, "modified": page.modified}
                )
                result["second_http_status"] = 304 if repeated.unchanged else 200
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (OSError, ValueError) as exc:
            parser.exit(1, f"订阅检查失败 · {exc}\n")
        return
    store = Store(args.data)
    if args.command == "serve":
        import uvicorn
        from .api import make_app

        if args.demo:
            with store.connection(True) as c:
                seed(c)
        uvicorn.run(make_app(store, Runner(args.mode)), host="127.0.0.1", port=args.port, log_level="warning")
        return
    with store.connection(True) as c:
        if one(c, "SELECT 1 FROM events WHERE demo=0") or one(
            c, "SELECT 1 FROM jobs WHERE status IN ('queued','running')"
        ):
            parser.error("试跑需要没有真实事件和待运行任务的数据目录")
        ident = add_event(
            c,
            "Vue 核心版本发布观察",
            "跟踪 Vue 3.6 正式版的发布进度，判断发布候选版本提供了哪些信号，并考虑未来 30 天能否发布稳定版。",
            ["https://github.com/vuejs/core/releases.atom"],
        )
        enqueue(c, "collect", event=ident)
    worker = Worker(store, Runner("test"))
    try:
        for _ in range(4):
            if not worker.step():
                break
        with store.connection(True) as c:
            if one(c, "SELECT id FROM jobs WHERE kind='forecast' AND status='done'"):
                enqueue(c, "collect", event=ident)
        for _ in range(3):
            if not worker.step():
                break
        with store.connection() as c:
            for job in rows(c, "SELECT kind,status,info FROM jobs ORDER BY id"):
                print(job, flush=True)
            for run in rows(
                c,
                "SELECT role,model,effort,status,input_tokens,output_tokens,round(elapsed,1) AS seconds FROM runs ORDER BY id",
            ):
                print(run, flush=True)
            ok = all(
                one(c, "SELECT id FROM runs WHERE role=? AND status='done'", (role,))
                for role in ("study", "forecast")
            )
            ok = (
                ok
                and one(c, "SELECT id FROM nodes WHERE event_id=? AND kind='judgment'", (ident,))
                and not one(c, "SELECT id FROM jobs WHERE status!='done'")
            )
    finally:
        with store.connection(True) as c:
            c.execute("UPDATE events SET status='paused' WHERE id=?", (ident,))
    print("试跑数据已保存", str(Path(args.data).resolve()), flush=True)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
