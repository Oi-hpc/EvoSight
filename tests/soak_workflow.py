"""Wall-clock endurance run. Start with python tests/soak_workflow.py NEW_DIRECTORY.

Only the publisher transport and model replies are deterministic. The HTTP server,
worker, SQLite, watcher, deadlines, process restart and scheduler run normally.
--smoke uses separate data and does not qualify as a 24-hour result.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html import escape
import json
import math
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from evosight import agents, contracts, feeds, materials
from evosight.api import make_app
from evosight.db import Store, one, utc

ORIGIN = "https://transit.example.org"
TITLES = ["海岚市域线正式开放", "海岚市域线运营验收"]
DECISION = "主管单位正式确认市域线开放载客"
ANNOUNCEMENT = "海岚市域线公布验收安排"
OPENER = build_opener(ProxyHandler({}))


def stamp(epoch):
    return utc(datetime.fromtimestamp(epoch, timezone.utc))


def save(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class Publisher(materials.Acquisition):
    def __init__(self, start, smoke, now=time.time):
        self.start = start
        self.now = now
        self.smoke = smoke
        self.decision = start + (80 if smoke else 150)
        super().__init__(download=self.read, search=lambda query, limit: [("海岚市域线主管单位公告", ORIGIN + "/")])

    def releases(self):
        rows = [(self.start - 300, "plan", ANNOUNCEMENT)]
        if self.now() >= self.decision:
            rows.append((self.decision, "open", DECISION))
        if self.now() >= self.start + 21590:
            rows.append((self.start + 21590, "access", "海岚市域线新设无障碍通道正式生效"))
        return rows

    def read(self, url, cached=None, **kwargs):
        if url == ORIGIN + "/":
            return feeds.Resource(
                b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed"></head></html>',
                url,
                "text/html",
            )
        releases = self.releases()
        if url == ORIGIN + "/feed":
            if not self.smoke and 20700 <= self.now() - self.start < 22200:
                raise ValueError("验收样本计划内的发布方中断")
            etag = str(len(releases))
            if cached and cached.get("etag") == etag:
                return feeds.Resource(b"", url, "", etag, None, True)
            entries = []
            for ts, key, title in reversed(releases):
                ts = stamp(ts)
                entries.append(
                    f"<item><guid>{ORIGIN}/{key}</guid><title>{title}</title><link>{ORIGIN}/{key}</link><description>{title}，公告时间为 {ts}。</description><pubDate>{ts}</pubDate></item>"
                )
            return feeds.Resource(
                ("<rss version='2.0'><channel>" + "".join(entries) + "</channel></rss>").encode(),
                url,
                "application/rss+xml",
                etag,
            )
        ts, _, title = next(row for row in releases if url == ORIGIN + "/" + row[1])
        ts = stamp(ts)
        body = f'<html><head><meta property="article:published_time" content="{ts}"></head><body><article><h1>{escape(title)}</h1><p>{title}，公告时间为 {ts}。</p><p>海岚主管单位在本公告中说明运营条件与适用范围。</p></article></body></html>'
        return feeds.Resource(body.encode(), url, "text/html; charset=utf-8")


class ModelReplay:
    mode = "test"

    def __init__(self, publisher):
        self.publisher = publisher

    def profile(self, role):
        return ("acceptance/replay-" + role, "fixture")

    def call(self, role, ctx):
        if role == "source_scout":
            value = agents.SourceScout(publisher_urls=[ORIGIN + "/feed"], queries=[])
        elif role == "plan":
            value = contracts.Plan(
                rationale="查找主管单位的原始公告",
                tasks=[
                    contracts.Assignment(
                        role="news",
                        question="主管单位公布了什么",
                        target="市域线",
                        time_scope="window",
                        priority=5,
                        done_when="找到公告原文",
                    )
                ],
            )
        elif role == "news":
            f = next((f for f in ctx["fragments"] if "公告时间为 " in f["text"]), ctx["fragments"][0])
            quote = f["text"][:180]
            value = contracts.Report(
                findings=[
                    contracts.Finding(
                        section="news",
                        kind="reported",
                        subject="市域线公告",
                        text=quote,
                        citations=[contracts.Citation(fragment_id=f["id"], quote=quote)],
                    )
                ]
            )
        else:
            docs = ctx["evidence"]
            ids = [d["id"] for d in docs[:1]]
            hit = next((d for d in docs if DECISION in d["summary"]), None)
            if role == "study":
                value = agents.Analysis(
                    fact="",
                    judgment=DECISION if hit else ANNOUNCEMENT,
                    watch="",
                    evidence_ids=[hit["id"]] if hit else ids,
                    items=[],
                    discoveries=[],
                )
            elif role == "forecast":
                value = agents.Forecast(
                    action="hold" if hit or ctx["已有预测"] else "create",
                    prediction_id=0,
                    question="截止前主管单位是否确认开放载客？",
                    deadline=stamp(self.publisher.start + (170 if self.publisher.smoke else 7200)),
                    rule="主管单位原站正式确认开放载客",
                    authority=ORIGIN,
                    p=65,
                    reason="已公布验收安排",
                    evidence_ids=ids,
                )
            elif role == "verify":
                value = agents.Verdict(
                    outcome="yes" if hit else "pending",
                    public_at=stamp(self.publisher.decision) if hit else "",
                    reason="核查预登记的主管单位原文",
                    lesson="区分验收安排与正式开放",
                    evidence_ids=[hit["id"]] if hit else [],
                )
            else:
                raise AssertionError(role)
        return value, (15, 10)


def serve(directory, state):
    pub = Publisher(state["start"], state["smoke"])
    app = make_app(Store(directory / "data"), ModelReplay(pub), acquisition=pub)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        config = uvicorn.Config(app, log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        save(directory / "socket.json", {"port": sock.getsockname()[1]})
        server.run(sockets=[sock])


def http(port, path, data=None, method=None):
    body = None if data is None else json.dumps(data).encode()
    req = Request(
        f"http://127.0.0.1:{port}/api/" + path,
        body,
        method=method,
        headers={"X-EvoSight": "local", "Content-Type": "application/json"},
    )
    with OPENER.open(req, timeout=8) as res:
        return json.load(res)


def start_server(directory):
    socket_file = directory / "socket.json"
    socket_file.unlink(missing_ok=True)
    with (directory / "server.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(directory), "--serve"], stdout=log, stderr=log
        )
    for _ in range(150):
        if process.poll() is not None:
            raise RuntimeError("服务启动失败，查看 server.log")
        if socket_file.exists():
            port = json.loads(socket_file.read_text())["port"]
            try:
                http(port, "home")
                return process, port
            except OSError:
                pass
        time.sleep(0.2)
    process.terminate()
    process.wait(timeout=20)
    raise RuntimeError("服务启动超时")


def memory_group():
    path = Path("/proc/self/cgroup")
    if not path.exists():
        return {}
    for entry in path.read_text().splitlines():
        ident, controllers, relative = entry.split(":", 2)
        if "memory" in controllers.split(","):
            root = Path("/sys/fs/cgroup/memory") / relative.lstrip("/")
            files = {
                "cgroup_peak_bytes": "memory.max_usage_in_bytes",
                "cgroup_limit_bytes": "memory.limit_in_bytes",
                "cgroup_charge_failures": "memory.failcnt",
            }
        elif ident == "0" and not controllers:
            root = Path("/sys/fs/cgroup") / relative.lstrip("/")
            files = {"cgroup_peak_bytes": "memory.peak", "cgroup_limit_bytes": "memory.max"}
        else:
            continue
        result = {}
        for name, filename in files.items():
            loc = root / filename
            if loc.is_file() and (raw := loc.read_text().strip()).isdigit():
                result[name] = int(raw)
        for loc in (root / "memory.events", root / "memory.oom_control"):
            if loc.is_file():
                counters = dict(line.split() for line in loc.read_text().splitlines())
                if "oom_kill" in counters:
                    result["cgroup_oom_kills"] = int(counters["oom_kill"])
        return result
    return {}


def metrics(store, pid):
    with store.connection() as c:
        result = {
            key: c.execute(sql).fetchone()[0]
            for key, sql in {
                "snapshots": "SELECT count(*) FROM knowledge_snapshots",
                "world_entries": "SELECT count(*) FROM world_entries WHERE demo=0",
                "world_versions": "SELECT count(*) FROM world_versions WHERE demo=0 AND claim_id=canonical_id",
                "missing_typed_versions": """SELECT count(*) FROM world_versions v LEFT JOIN world_record_data d ON d.version_id=v.claim_id
                    WHERE d.version_id IS NULL""",
                "missing_catalog_heads": """SELECT count(*) FROM world_entries w LEFT JOIN world_catalog_members m ON m.entry_id=w.id
                    LEFT JOIN world_record_data d ON d.version_id=w.head_id WHERE m.entry_id IS NULL OR m.head_id!=w.head_id
                    OR w.category!=d.schema_id OR m.path!=d.path""",
                "shared_world_entries": """SELECT count(*) FROM (
                    SELECT v.entry_id FROM snapshot_claims s JOIN knowledge_snapshots n ON n.id=s.snapshot_id
                    JOIN world_versions v ON v.claim_id=s.claim_id WHERE v.demo=0
                    AND n.id=(SELECT max(id) FROM knowledge_snapshots WHERE event_id=n.event_id)
                    GROUP BY v.entry_id HAVING count(DISTINCT n.event_id)>1)""",
                "unresolved_world_references": """SELECT count(*) FROM snapshot_claims s
                    LEFT JOIN world_versions v ON v.claim_id=s.claim_id LEFT JOIN world_entries w ON w.id=v.entry_id
                    WHERE w.id IS NULL OR s.index_section='' OR s.index_reason=''""",
                "stale_world_references": """SELECT count(*) FROM snapshot_claims s JOIN knowledge_snapshots n ON n.id=s.snapshot_id
                    JOIN world_versions v ON v.claim_id=s.claim_id JOIN world_entries w ON w.id=v.entry_id
                    WHERE v.canonical_id!=w.head_id AND n.id=(SELECT max(id) FROM knowledge_snapshots WHERE event_id=n.event_id)""",
                "scheduled": "SELECT count(*) FROM tracking_rounds WHERE request_key LIKE '%/slot/%' AND state IN ('published','unchanged','recovered')",
                "signals": "SELECT count(*) FROM watch_signals WHERE round_id IS NOT NULL AND action='full'",
                "recoveries": "SELECT count(*) FROM round_sources WHERE state='recovered'",
                "settlements": "SELECT count(*) FROM resolutions WHERE outcome='yes'",
                "archived_settlements": "SELECT count(*) FROM resolutions r JOIN predictions p ON r.prediction_id=p.id JOIN events e ON e.id=p.event_id WHERE e.status='archived' AND r.outcome='yes'",
                "duplicate_settlements": "SELECT count(*) FROM (SELECT prediction_id FROM resolutions WHERE outcome IN ('yes','no') GROUP BY prediction_id HAVING count(*)>1)",
                "backlog": "SELECT count(*) FROM jobs WHERE status IN ('queued','running','deferred')",
                "failed_jobs": "SELECT count(*) FROM jobs WHERE status='failed'",
                "unfinished_roles": "SELECT count(*) FROM research_tasks t JOIN tracking_rounds r ON t.round_id=r.id WHERE r.state!='superseded' AND t.state!='done'",
                "unresolved_sources": "SELECT count(*) FROM round_sources WHERE state='missing'",
                "max_queue_wait_s": "SELECT coalesce(max(unixepoch(started_at)-unixepoch(created_at)),0) FROM jobs",
                "calls": "SELECT count(*) FROM runs",
            }.items()
        }
        sig = c.execute(
            """SELECT min(unixepoch(s.seen_at)-unixepoch(d.published_at)) FROM watch_signals s JOIN documents d ON d.id=s.document_id WHERE d.title=? AND s.action='full'""",
            (DECISION,),
        ).fetchone()[0]
        result["first_signal_delay_s"] = sig
    proc = Path(f"/proc/{pid}/status")
    if proc.exists():
        for line in proc.read_text().splitlines():
            if line.startswith("VmHWM:") or line.startswith("VmRSS:"):
                result[line.split(":")[0] + "_KiB"] = int(line.split()[1])
    result.update(memory_group())
    return result


def verify_backup(store, directory):
    import hashlib
    from contextlib import closing

    with store.connection() as original, closing(
        sqlite3.connect(directory / "restore-check.sqlite3")
    ) as copy:
        original.backup(copy)
        tables = [
            r[0]
            for r in original.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        counts_match = all(
            original.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            == copy.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in tables
        )
        return {
            "tables": len(tables),
            "counts_match": counts_match,
            "integrity": copy.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_keys": list(copy.execute("PRAGMA foreign_key_check")),
            "blob_digests_match": all(
                hashlib.sha256(content).hexdigest() == digest
                for digest, content in copy.execute("SELECT digest,content FROM evidence_blobs")
            ),
        }


def drive(directory, state):
    store = Store(directory / "data")
    # A service restart must recover the same database, IDs, reading cursor and work.
    process, port = start_server(directory)
    state["launches"] = state.get("launches", 0) + 1
    smoke = state["smoke"]
    span = 240 if smoke else 86400
    end = state["start"] + span
    try:
        while True:
            elapsed = int(time.time() - state["start"])
            now = time.perf_counter()
            home = http(port, "home")
            latency = round((time.perf_counter() - now) * 1000, 2)
            by_title = {c["title"]: c for c in home["cards"]}
            for title in TITLES:
                if title not in by_title:
                    http(port, "events", {"title": title, "scope": "追踪海岚市域线的验收、开放公告与实际载客"})
            primary = by_title.get(TITLES[0])
            secondary = by_title.get(TITLES[1])
            if secondary and secondary["predictions"] and secondary["status"] == "active":
                http(port, f"events/{secondary['id']}/status", {"status": "archived"})
            if primary and primary["predictions"] and "read" not in state:
                http(port, f"events/{primary['id']}/read", {"token": primary["token"]})
                state["read"] = primary["upper"]
            if primary and elapsed >= (24 if smoke else 900) and not state.get("edit"):
                goal = http(port, f"events/{primary['id']}/knowledge")["goal"]
                if goal["version"] == 1:
                    http(
                        port,
                        f"events/{primary['id']}",
                        {"expected_version": 1, "scope": "核对海岚市域线正式载客时间与无障碍通道"},
                        method="PATCH",
                    )
                state["edit"] = True
            if state.get("edit") and elapsed >= (44 if smoke else 1200) and not state.get("restart"):
                process.kill()
                process.wait(timeout=20)
                process, port = start_server(directory)
                state["restart"] = True
                state["launches"] += 1
            sample = {"at": utc(), "elapsed_s": elapsed, "home_ms": latency, **metrics(store, process.pid)}
            with (directory / "metrics.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(sample) + "\n")
            save(directory / "state.json", state)
            save(
                directory / "status.json",
                {
                    **sample,
                    "mode": "smoke" if smoke else "24h-replay",
                    "due": stamp(end),
                    "service_restarted": bool(state.get("restart")),
                },
            )
            if time.time() >= end and (
                (smoke or sample["scheduled"] >= 4) and sample["backlog"] == 0 or time.time() >= end + 240
            ):
                break
            time.sleep(2 if smoke else 30)
        with store.connection() as c:
            seen = one(c, "SELECT seen FROM reads WHERE event_id=?", (primary["id"],))
            goal = one(
                c, "SELECT max(version) AS version FROM event_goals WHERE event_id=?", (primary["id"],)
            )
            slots = [
                dict(row)
                for row in c.execute(
                    "SELECT window_start,window_end,state FROM tracking_rounds WHERE event_id=? AND request_key LIKE '%/slot/%' ORDER BY window_end",
                    (primary["id"],),
                )
            ]
        samples = [json.loads(s) for s in (directory / "metrics.jsonl").read_text().splitlines()]
        latencies = sorted(s["home_ms"] for s in samples)
        peak = max(s.get("VmHWM_KiB", 0) for s in samples)
        backup = verify_backup(store, directory)
        checks = {
            "wall_clock": elapsed >= span,
            "shared_knowledge": sample["shared_world_entries"] >= 1,
            "typed_catalog": sample["missing_typed_versions"] == 0 and sample["missing_catalog_heads"] == 0,
            "index_integrity": sample["unresolved_world_references"] == 0
            and sample["stale_world_references"] == 0,
            "major_news": sample["first_signal_delay_s"] is not None
            and 0 <= sample["first_signal_delay_s"] <= (120 if smoke else 660),
            "edit_and_restart": bool(state.get("restart"))
            and goal["version"] == 2
            and seen
            and seen["seen"] == state["read"],
            "archived_deadlines": sample["archived_settlements"] == 1,
            "settlement_uniqueness": sample["settlements"] == 2 and sample["duplicate_settlements"] == 0,
            "source_gap_recovered": smoke
            or (sample["recoveries"] >= 1 and sample["unresolved_sources"] == 0),
            "queue_drained": sample["backlog"] == 0
            and sample["failed_jobs"] == 0
            and sample["unfinished_roles"] == 0,
            "restored_evidence": backup["counts_match"]
            and backup["blob_digests_match"]
            and backup["integrity"] == "ok"
            and not backup["foreign_keys"],
        }
        checks["process_recovery"] = (
            state["launches"] == 2 and max(s.get("cgroup_oom_kills", 0) for s in samples) == 0
        )
        if smoke:
            checks.pop("source_gap_recovered")
        else:
            checks["periodic_slots"] = (
                sample["scheduled"] >= 4
                and len(slots) >= 4
                and all(
                    (
                        datetime.fromisoformat(s["window_end"]) - datetime.fromisoformat(s["window_start"])
                    ).total_seconds()
                    == 21600
                    for s in slots
                )
                and all(a["window_end"] == b["window_start"] for a, b in zip(slots, slots[1:]))
            )
        report = {
            "passed": all(checks.values()),
            "mode": "smoke" if smoke else "24h-replay",
            "start": stamp(state["start"]),
            "end": utc(),
            "sample_count": len(samples),
            "home_p95_ms": latencies[math.ceil(0.95 * len(latencies)) - 1],
            "peak_service_kib": peak,
            "slots": slots,
            "checks": checks,
            "backup": backup,
            **sample,
        }
        save(directory / "final.json", report)
        return report["passed"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "state.json"
    if marker.exists():
        state = json.loads(marker.read_text(encoding="utf-8"))
        if not args.serve and state["smoke"] != args.smoke:
            parser.error("烟测与长稳使用不同目录")
    else:
        if args.serve or any(directory.iterdir()):
            parser.error("请使用新的独立验收目录")
        state = {"start": int(time.time()), "smoke": args.smoke}
        save(marker, state)
        if args.smoke:
            with Store(directory / "data").connection(True) as c:
                c.execute("UPDATE settings SET value='1' WHERE key='watch_minutes'")
    if args.serve:
        serve(directory, state)
    elif (directory / "final.json").exists():
        report = json.loads((directory / "final.json").read_text(encoding="utf-8"))
        print("该目录已完成验收", report["end"], flush=True)
        raise SystemExit(0 if report["passed"] else 2)
    else:
        raise SystemExit(0 if drive(directory, state) else 2)
