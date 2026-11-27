"""Admission limits for model subprocesses. CPU and memory include the service's cgroup."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys

from .db import config


MIB = 1024 * 1024
MAX_AGENTS = 4
RESERVE_MIB = 320
AGENT_MIB = 320


def _read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return ""


def _positive(value):
    try:
        n = int(value)
        return n if 0 < n < 1 << 60 else None
    except (ValueError, TypeError):
        return None


def linux_limits(cpu, total, available):
    """Walk the process cgroup and its parents. A parent can have the tighter limit."""
    root = Path("/sys/fs/cgroup")
    groups = _read("/proc/self/cgroup").splitlines()
    for line in groups:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        _, controls, path = fields
        controllers = controls.split(",")
        roots = (
            [root]
            if not controls
            else (
                [root / "memory"]
                if "memory" in controllers
                else [root / "cpu", root / "cpu,cpuacct", root / "cpuacct,cpu"]
                if "cpu" in controllers
                else []
            )
        )
        for base in roots:
            if not base.is_dir():
                continue
            rel = Path(path.lstrip("/"))
            current = base / rel if ".." not in rel.parts and (base / rel).is_dir() else base
            while True:
                if not controls or "memory" in controllers:
                    limit = _positive(
                        _read(current / ("memory.max" if not controls else "memory.limit_in_bytes"))
                    )
                    used = _read(current / ("memory.current" if not controls else "memory.usage_in_bytes"))
                    if limit and used.isdigit():
                        total = min(total, limit)
                        available = min(available, max(0, limit - int(used)))
                if not controls or "cpu" in controllers:
                    values = (
                        _read(current / "cpu.max").split()
                        if not controls
                        else [_read(current / "cpu.cfs_quota_us"), _read(current / "cpu.cfs_period_us")]
                    )
                    if len(values) == 2 and _positive(values[0]) and _positive(values[1]):
                        cpu = min(cpu, max(1, int(values[0]) // int(values[1])))
                if current == base:
                    break
                current = current.parent
    return cpu, total, available


def host():
    cpu = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
    total = available = 0
    loaded = False
    if sys.platform.startswith("linux"):
        fields = {}
        for line in _read("/proc/meminfo").splitlines():
            key, _, value = line.partition(":")
            if key in ("MemTotal", "MemAvailable", "MemFree") and value.split():
                fields[key] = int(value.split()[0]) * 1024
        total = fields.get("MemTotal", 0)
        available = fields.get("MemAvailable", fields.get("MemFree", 0))
        cpu, total, available = linux_limits(cpu, total, available)
        try:
            loaded = os.getloadavg()[0] > 1.25 * (os.cpu_count() or cpu)
        except OSError:
            pass
    elif os.name == "nt":

        class Status(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("load", ctypes.c_ulong),
                *[
                    (name, ctypes.c_ulonglong)
                    for name in (
                        "total",
                        "available",
                        "page",
                        "available_page",
                        "virtual",
                        "available_virtual",
                        "extended",
                    )
                ],
            ]

        data = Status()
        data.length = ctypes.sizeof(data)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(data)):
            total, available = data.total, data.available
    return cpu, total // MIB, available // MIB, loaded


class ResourcePressure(RuntimeError):
    pass


def capacity(c):
    configured = min(MAX_AGENTS, max(1, config(c)["max_agent_concurrency"]))
    cpu, total, available, loaded = host()
    active = c.execute(
        """SELECT count(*) FROM runs r JOIN jobs j ON j.id=r.job_id
                                  WHERE r.status='running' AND j.status='running'"""
    ).fetchone()[0]
    if total:
        memory = max(0, (available - RESERVE_MIB) // AGENT_MIB)
        hardware = max(0, (total - RESERVE_MIB) // AGENT_MIB)
    else:
        memory = hardware = 1
    effective = min(configured, cpu, hardware, memory, 1 if loaded else configured)
    return {
        "configured": configured,
        "effective": effective,
        "active": active,
        "cpu": cpu,
        "available_mib": available if total else None,
        "memory_limit_mib": total or None,
        "reserve_mib": RESERVE_MIB,
        "per_agent_mib": AGENT_MIB,
        "cpu_busy": loaded,
    }
