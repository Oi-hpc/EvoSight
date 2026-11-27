"""Compare one and two agent slots on the same host through the production queue and HTTP API.

Run once per new directory. Four replay roles each launch a short-lived 232 MiB
child to model a memory-resident external CLI waiting for a provider response.
The process and HTTP measurements are real. No provider credentials are needed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import argparse
import math
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from evosight import contracts, db, resources
from evosight.api import make_app
from evosight.worker import Worker
from soak_workflow import ModelReplay, Publisher, ORIGIN, http, memory_group, save

ROLES = ("news", "counter", "dependencies", "organizations")
CHILD_MIB = 232
HOLD_SECONDS = 1.5


class ResidentReplay(ModelReplay):
    def __init__(self, publisher):
        super().__init__(publisher)
        self.lock = threading.Lock()
        self.peak = self.active = 0
        self.roles_finished = 0

    def call(self, role, payload):
        if role == "plan":
            return contracts.Plan(
                rationale="核对公告并交叉检验",
                tasks=[
                    contracts.Assignment(
                        role=r,
                        question=f"{r} 查公告变化",
                        target="市域线",
                        time_scope="window",
                        priority=4,
                        done_when="找到可定位的原文",
                    )
                    for r in ROLES
                ],
            ), (15, 10)
        if role == "study":
            assert self.roles_finished == len(ROLES) and self.active == 0
        if role not in ROLES:
            return super().call(role, payload)
        with self.lock:
            self.active += 1
            self.peak = max(self.active, self.peak)
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys,time; data=bytearray(int(sys.argv[1])*1024*1024); time.sleep(float(sys.argv[2]))",
                    str(CHILD_MIB),
                    str(HOLD_SECONDS),
                ],
                check=True,
                timeout=15,
            )
            assert proc.returncode == 0
            return super().call("news", payload)
        finally:
            with self.lock:
                self.active -= 1
                self.roles_finished += 1


def tree_rss_mib():
    """Sum resident sets of this process and its live child processes across threads."""
    if not Path("/proc/self/task").exists():
        return None
    total, pending, seen = 0, [os.getpid()], set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            status = Path(f"/proc/{pid}/status").read_text()
            total += next(int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS:"))
            for child_list in Path(f"/proc/{pid}/task").glob("*/children"):
                try:
                    pending.extend(map(int, child_list.read_text().split()))
                except FileNotFoundError:
                    pass
        except (OSError, StopIteration):
            pass
    return round(total / 1024, 2)


@contextmanager
def serving(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        port = sock.getsockname()[1]
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.03)
            assert server.started
            yield port
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()


def one_trial(directory, setting, start):
    publisher = Publisher(start, False)
    runner = ResidentReplay(publisher)
    store = db.Store(directory)
    with store.connection(True) as c:
        c.execute("UPDATE settings SET value=? WHERE key='max_agent_concurrency'", (str(setting),))
        initial = resources.capacity(c)
        assert initial["effective"] >= 1, initial
    latencies, rss, headroom, queue_wait = [], [], [], []
    peak_active = 0
    worker = Worker(store, runner, acquisition=publisher)
    app = make_app(store, runner, background=False, acquisition=publisher)
    with serving(app) as port:
        event = http(
            port,
            "events",
            {"title": "海岚市域线正式开放", "scope": "持续核对主管单位公开的载客条件和验收进展", "feeds": [ORIGIN + "/feed"]},
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            begin = time.monotonic()

            def drain():
                for _ in range(6):
                    if not worker.step():
                        break

            processing = pool.submit(drain)
            while not processing.done():
                tick = time.monotonic()
                http(port, "home")
                latencies.append((time.monotonic() - tick) * 1000)
                resident = tree_rss_mib()
                if resident is not None:
                    rss.append(resident)
                with store.connection() as c:
                    state = resources.capacity(c)
                peak_active = max(peak_active, state["active"])
                if state["available_mib"] is not None:
                    headroom.append(state["available_mib"])
                time.sleep(0.05)
            processing.result()
            elapsed = time.monotonic() - begin
    with store.connection() as c:
        assert (
            db.one(c, "SELECT state FROM tracking_rounds WHERE event_id=?", (event["id"],))["state"]
            == "published"
        )
        assert not list(c.execute("PRAGMA foreign_key_check"))
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        queue_wait = [
            r[0]
            for r in c.execute(
                "SELECT unixepoch(started_at)-unixepoch(created_at) FROM jobs WHERE started_at IS NOT NULL"
            )
        ]
        counts = {
            key: c.execute(q).fetchone()[0]
            for key, q in {
                "jobs": "SELECT count(*) FROM jobs",
                "backlog": "SELECT count(*) FROM jobs WHERE status IN ('queued','running','deferred')",
                "failed": "SELECT count(*) FROM jobs WHERE status='failed'",
                "calls": "SELECT count(*) FROM model_receipts",
                "snapshots": "SELECT count(*) FROM knowledge_snapshots",
                "indexed_claims": "SELECT count(*) FROM snapshot_claims",
                "foreign_key_gaps": "SELECT count(*) FROM snapshot_claims s LEFT JOIN world_versions v ON v.claim_id=s.claim_id WHERE v.claim_id IS NULL",
            }.items()
        }
    return {
        "setting": setting,
        "initial_capacity": initial,
        "peak_role_calls": runner.peak,
        "peak_db_running": peak_active,
        "elapsed_s": round(elapsed, 3),
        "home_samples": len(latencies),
        "home_ms": latencies,
        "tree_peak_mib": max(rss, default=None),
        "min_available_mib": min(headroom, default=None),
        "max_queue_wait_s": max(queue_wait, default=0),
        **counts,
    }


def compare(directory):
    directory.mkdir(exist_ok=False, parents=True)
    start = time.time()
    before = memory_group()
    trials = {1: [], 2: []}
    for n, setting in enumerate((1, 2, 2, 1, 1, 2)):
        row = one_trial(directory / f"run-{n}", setting, start)
        trials[setting].append(row)
    after = memory_group()
    result = {
        "kind": "bounded subprocess replay",
        "child_mib": CHILD_MIB,
        "child_hold_s": HOLD_SECONDS,
        "cpu_count": os.cpu_count(),
        "cgroup_start": before,
        "cgroup_end": after,
        "modes": {},
    }
    for setting, trials_for_setting in trials.items():
        delays = sorted(ms for row in trials_for_setting for ms in row.pop("home_ms"))
        result["modes"][setting] = {
            "trials": trials_for_setting,
            "median_s": statistics.median(r["elapsed_s"] for r in trials_for_setting),
            "peak_agent_calls": max(r["peak_db_running"] for r in trials_for_setting),
            "tree_peak_mib": max(
                (r["tree_peak_mib"] for r in trials_for_setting if r["tree_peak_mib"] is not None),
                default=None,
            ),
            "home_p95_ms": round(delays[math.ceil(0.95 * len(delays)) - 1], 2),
            "home_samples": len(delays),
            "min_available_mib": min(
                (r["min_available_mib"] for r in trials_for_setting if r["min_available_mib"] is not None),
                default=None,
            ),
        }
    a, b = result["modes"][1], result["modes"][2]
    result["speedup"] = round(a["median_s"] / b["median_s"], 3)
    result["passed"] = (
        all(
            r["peak_role_calls"] == s
            and r["peak_db_running"] == s
            and r["backlog"] == r["failed"] == r["foreign_key_gaps"] == 0
            and r["calls"] == 7
            and r["snapshots"] == 1
            for s, group in trials.items()
            for r in group
        )
        and all(result["modes"][s]["home_p95_ms"] < 200 for s in trials)
        and result["speedup"] > 1.15
        and after.get("cgroup_oom_kills", 0) == before.get("cgroup_oom_kills", 0)
        and after.get("cgroup_charge_failures", 0) == before.get("cgroup_charge_failures", 0)
    )
    save(directory / "metrics.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "modes"}))
    return result["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    sys.exit(0 if compare(parser.parse_args().directory) else 1)
