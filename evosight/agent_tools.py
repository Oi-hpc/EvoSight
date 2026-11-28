"""The evidence files and native tools available to one Codex turn."""
import json
from functools import lru_cache
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .db import dump, rows


def frozen(role, context):
    return role == "verify" or bool(context.get("独立复核")) or context.get("basis") == "original"


def live_web(role, context):
    # Publication and forecast registration sign a specific archive. Collection
    # and current conversations have a return path for newly discovered URLs.
    return role not in ("study", "forecast", "skill_draft") and not (
        frozen(role, context) or context.get("demo") or context.get("replay")
    )


def archive_ids(c, context):
    if context.get("basis") == "original":
        return context.get("forecast_run", {}).get("input", {}).get("archive_fragment_ids", [])
    if "archive_fragment_ids" in context:
        return context["archive_fragment_ids"]
    passages = [
        f["id"]
        for key in ("fragments", "evidence_fragments", "交付可引用原文", "people_evidence")
        for f in context.get(key, [])
        if "id" in f
    ]
    if context.get("独立复核"):
        return list(dict.fromkeys(passages))
    docs = [d["id"] for d in context.get("evidence", []) if "id" in d]
    docs.extend(int(k[1:]) for k in context.get("sources", {}) if re.fullmatch(r"D\d+", k))
    if docs:
        passages.extend(
            r[0]
            for r in c.execute(
                """SELECT f.id FROM fragments f JOIN evidence_assets a ON a.id=f.asset_id
               WHERE a.document_id IN (SELECT value FROM json_each(?)) AND (? IS NULL OR a.created_at<=?)
               ORDER BY f.id""",
                (dump(docs), context.get("asked_at"), context.get("asked_at")),
            )
        )
    return list(dict.fromkeys(passages))


def read_archive(c, ids):
    if not ids:
        return []
    return rows(
        c,
        """SELECT f.id,a.document_id,d.title,d.url,f.locator,f.text
                      FROM fragments f JOIN evidence_assets a ON a.id=f.asset_id
                      JOIN documents d ON d.id=a.document_id
                      WHERE f.id IN (SELECT value FROM json_each(?)) ORDER BY a.document_id,f.id""",
        (dump(ids),),
    )


def write_materials(folder, context, archive):
    folder.mkdir()
    (folder / "context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    documents = {}
    for f in archive:
        doc = f["document_id"]
        parts = documents.setdefault(doc, [f'D{doc}  {f["title"]}', f["url"]])
        parts.append(f'\n[fragment_id={f["id"]}] {f["locator"]}\n{f["text"]}')
    for doc, parts in documents.items():
        (folder / f"D{doc}.txt").write_text("\n".join(parts), encoding="utf-8")


@lru_cache(maxsize=4)
def scoped_shell(executable):
    if os.name != "nt":
        return True
    # Explicit MXC refuses to start on unsupported hosts. The legacy Windows
    # sandbox account can still read files granted to all local users.
    with tempfile.TemporaryDirectory(prefix="evosight-scope-") as tmp:
        root = Path(tmp)
        current = root / "turn"
        current.mkdir()
        (current / "inside.txt").write_text("fixture", encoding="ascii")
        (root / "outside.txt").write_text("fixture", encoding="ascii")
        command = [
            executable,
            "-c",
            'windows.sandbox="mxc"',
            "sandbox",
            "-C",
            str(current),
            "-P",
            "evosight",
            "-c",
            'permissions.evosight={filesystem={":minimal"="read",":workspace_roots"="read"},network={enabled=false}}',
            "--",
            str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"),
            "/d",
            "/c",
            r"type inside.txt >nul && (type ..\outside.txt >nul 2>nul && exit /b 1 || exit /b 0)",
        ]
        try:
            return (
                subprocess.run(
                    command,
                    cwd=current,
                    capture_output=True,
                    timeout=25,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            return False


def codex_options(cwd: Path, materials: Path, web: bool, shell=True, *, executable=None):
    # The built-in read-only preset grants machine-wide reads. Name the two research
    # directories explicitly so a resumed session cannot inherit another turn's files.
    paths = [":minimal", str(cwd.resolve()), str(materials.resolve())]
    if shell and executable:
        binary = Path(executable).resolve()
        if binary.is_file():
            paths.append(str(binary))
    filesystem = ",".join(json.dumps(path, ensure_ascii=False) + '="read"' for path in paths)
    options = [
        "--enable" if shell else "--disable",
        "shell_tool",
        "-c",
        'default_permissions="evosight"',
        "-c",
        "permissions.evosight={filesystem={" + filesystem + "},network={enabled=false}}",
        "-c",
        'shell_environment_policy={inherit="core",include_only=["PATH","PATHEXT","SYSTEMROOT","WINDIR","COMSPEC","LANG","LC_ALL","LANGUAGE"]}',
        "-c",
        'web_search="live"' if web else 'web_search="disabled"',
        "-c",
        "project_doc_max_bytes=0",
    ]
    if shell and os.name == "nt":
        options.extend(["-c", 'windows.sandbox="mxc"'])
    for feature in (
        "unified_exec",
        "apps",
        "plugins",
        "hooks",
        "multi_agent",
        "multi_agent_v2",
        "memories",
        "browser_use",
        "browser_use_external",
        "in_app_browser",
        "in_app_local_automation",
        "image_generation",
        "view_image",
        "goals",
        "request_permissions_tool",
    ):
        options.extend(["--disable", feature])
    return options
