#!/opt/evosight/runtime/bin/python
"""Package a committed EvoSight version and switch a Linux installation to it."""
import argparse
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from time import monotonic, sleep
from urllib.request import ProxyHandler, build_opener


ROOT = Path("/opt/evosight")
RELEASES = ROOT / "releases"
CURRENT = ROOT / "current"
PREVIOUS = ROOT / "previous"
STATE = Path("/var/lib/evosight")
DB = STATE / "data" / "evosight.sqlite3"
PYTHON = ROOT / "runtime" / "bin" / "python"
SERVICE = "evosight.service"
REVISION = "EVOSIGHT_REVISION"
PREPARED = ".prepared"


def checksum(file):
    with file.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def package(output, dist, ref):
    if not (dist / "index.html").is_file() or not (dist / "assets").is_dir():
        raise ValueError("Build the frontend before packaging")
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", ref + "^{commit}"], text=True
    ).strip()
    tree = subprocess.check_output(["git", "archive", "--format=tar", revision])
    with tarfile.open(fileobj=io.BytesIO(tree), mode="r:") as source, tarfile.open(output, "x:gz") as bundle:
        for info in source:
            bundle.addfile(info, source.extractfile(info) if info.isfile() else None)
        bundle.add(dist, arcname="frontend/dist")
        version = (revision + "\n").encode("ascii")
        entry = tarfile.TarInfo(REVISION)
        entry.mode, entry.size = 0o644, len(version)
        bundle.addfile(entry, io.BytesIO(version))
    print(f"Commit {revision}")
    print(f"SHA256 {checksum(output)}")


def prepare(bundle, expected):
    digest = checksum(bundle)
    if digest != expected.lower():
        raise ValueError("Release archive SHA256 mismatch")
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=RELEASES) as temporary:
        stage = Path(temporary)
        with tarfile.open(bundle, "r:gz") as archive:
            archive.extractall(stage, filter="data")
        revision = (stage / REVISION).read_text(encoding="ascii").strip()
        if not re.fullmatch(r"([0-9a-f]{40}|[0-9a-f]{64})", revision):
            raise ValueError("Missing or invalid commit in release archive")
        if not (stage / "frontend/dist/index.html").is_file():
            raise ValueError("Release archive omits the built frontend")
        release = RELEASES / revision
        if not release.exists():
            stage.chmod(0o755)
            (stage / ".archive-sha256").write_text(digest, encoding="ascii")
            stage.rename(release)
    stamp = release / ".archive-sha256"
    if not stamp.is_file() or stamp.read_text(encoding="ascii") != digest:
        raise ValueError(f"{revision} already has a different staged archive")
    if not (release / PREPARED).is_file():
        venv = release / ".venv"
        subprocess.run([PYTHON, "-m", "venv", venv], check=True)
        interpreter = venv / "bin" / "python"
        subprocess.run(
            [interpreter, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"],
            cwd=release, check=True,
        )
        subprocess.run([interpreter, "-c", "from evosight.api import make_app"], cwd=release, check=True)
        (release / PREPARED).touch()
    return release


def link_target(link):
    if not link.is_symlink():
        if link.exists():
            raise ValueError(f"Expected a release link at {link}")
        return None
    target = link.resolve(strict=True)
    if target.parent != RELEASES or not (target / PREPARED).is_file():
        raise ValueError(f"Unprepared release at {target}")
    return target


def point(link, target):
    pending = link.with_name(f".{link.name}-{os.getpid()}")
    pending.symlink_to(target, target_is_directory=True)
    os.replace(pending, link)


def systemctl(action, *, check=True):
    return subprocess.run(["systemctl", action, SERVICE], check=check, timeout=55)


def stop_idle():
    if systemctl("is-active", check=False).returncode:
        return
    if not DB.is_file():
        raise RuntimeError("Running workbench has no database at the expected path")
    with closing(sqlite3.connect(DB, timeout=10)) as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute(
            "SELECT EXISTS(SELECT 1 FROM jobs WHERE status='running') "
            "OR EXISTS(SELECT 1 FROM runs WHERE status='running')"
        ).fetchone()[0]:
            raise RuntimeError("Workbench is busy. Release is staged for the next idle window.")
        systemctl("stop")


def backup():
    if not DB.is_file():
        return
    folder = STATE / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    snapshot = folder / f"{name}.sqlite3"
    snapshot.touch(mode=0o600)
    with closing(sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
    print(f"SQLite snapshot {snapshot}")


def healthy(target):
    client = build_opener(ProxyHandler({}))
    deadline = monotonic() + 35
    while monotonic() < deadline:
        try:
            with client.open("http://127.0.0.1:8765/api/home", timeout=3) as response:
                if response.status == 200 and "cards" in json.load(response):
                    pid = subprocess.check_output(
                        ["systemctl", "show", "-p", "MainPID", "--value", SERVICE], text=True, timeout=5
                    ).strip()
                    if Path(f"/proc/{pid}/cwd").resolve(strict=True) == target:
                        return
        except (OSError, ValueError):
            sleep(1)
    raise RuntimeError("New workbench failed its local health check")


def activate(target):
    old = link_target(CURRENT)
    stop_idle()
    try:
        backup()
        point(CURRENT, target)
        systemctl("start")
        healthy(target)
    except Exception:
        systemctl("stop", check=False)
        if old:
            point(CURRENT, old)
            systemctl("start")
            healthy(old)
        elif CURRENT.is_symlink() and CURRENT.resolve() == target:
            CURRENT.unlink()
        raise
    if old and old != target:
        point(PREVIOUS, old)
    print(f"Active release {target.name}")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("package")
    pack.add_argument("output", type=Path)
    pack.add_argument("--dist", type=Path, default=Path("frontend/dist"))
    pack.add_argument("--ref", default="HEAD")
    apply = commands.add_parser("apply")
    apply.add_argument("archive", type=Path)
    apply.add_argument("sha256")
    commands.add_parser("rollback")
    args = cli.parse_args()
    if args.command == "package":
        package(args.output, args.dist, args.ref)
        return

    import fcntl

    os.umask(0o022)
    with (ROOT / ".release.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.command == "apply":
            activate(prepare(args.archive, args.sha256))
        else:
            previous = link_target(PREVIOUS)
            if previous is None:
                raise RuntimeError("No previous release is installed")
            activate(previous)


if __name__ == "__main__":
    main()
