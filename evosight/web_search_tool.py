"""Stdio tools for public search and a turn's authorized evidence."""
import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import re
import sys

from . import materials


TOOL = {
    "name": "search_public_web",
    "description": "搜索公开网页并返回发布页链接。一次搜一个发布方，使用简短公开关键词。需要引用时交给 EvoSight 归档。",
    "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 2, "maxLength": 250}},
        "required": ["query"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "openWorldHint": True},
}
ARCHIVE_TOOL = {
    "name": "read_archived_evidence",
    "description": "在本轮已授权的全部归档段落中检索原文。可按关键词找段落，也可按 fragment_id 回读并引用该段原文。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 250},
            "fragment_id": {"type": "integer", "minimum": 1},
            "document_id": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
}


SKILL_TOOL = {
    "name": "read_research_skill",
    "description": "读取本轮目录中已由用户确认的调查方法及其验收步骤。",
    "inputSchema": {"type": "object", "properties": {"id": {"type": "integer", "minimum": 1}},
                    "required": ["id"], "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
}


def skill_result(params, skills):
    args = params.get("arguments")
    ident = args.get("id") if isinstance(args, dict) else None
    method = next((item for item in skills if type(ident) is int and item["id"] == ident), None)
    if not method:
        return {"content": [{"type": "text", "text": "该方法不在本轮已确认目录内。"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(method, ensure_ascii=False)}]}


def archive_result(params, archive):
    args = params.get("arguments")
    args = args if isinstance(args, dict) else {}
    query, fragment_id, doc = args.get("query", ""), args.get("fragment_id"), args.get("document_id")
    if (
        not isinstance(query, str) or len(query) > 250
        or any(n is not None and (type(n) is not int or n < 1) for n in (fragment_id, doc))
        or not (query.strip() or fragment_id)
    ):
        return {"content": [{"type": "text", "text": "请提供检索词或片段 ID，可指定文档 ID。"}], "isError": True}
    query = query.strip().casefold()
    terms = materials.words(query) | set(re.findall(r"\d+(?:[.,]\d+)*", query))
    hits = []
    for fragment in archive:
        if doc is not None and fragment["document_id"] != doc:
            continue
        if fragment_id is not None:
            if fragment["id"] == fragment_id:
                hits.append(((True, 0), fragment))
            continue
        text = fragment["text"].casefold()
        score = (query in text, sum(term in text for term in terms))
        if any(score):
            hits.append((score, fragment))
    hits.sort(key=lambda hit: hit[0], reverse=True)
    results = [
        {"fragment_id": f["id"], **{key: f[key] for key in ("document_id", "title", "url", "locator", "text")}}
        for _, f in hits[:4]
    ]
    return {"content": [{"type": "text", "text": json.dumps(results, ensure_ascii=False)}]}


def search_result(params):
    arguments = params.get("arguments") or {}
    query = arguments.get("query") if isinstance(arguments, dict) else None
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 250:
        return {"content": [{"type": "text", "text": "请用 2 至 250 字的公开检索词。"}], "isError": True}
    try:
        with redirect_stdout(sys.stderr):
            hits = materials.search_web(query.strip(), 5)
    except (OSError, RuntimeError):
        return {"content": [{"type": "text", "text": "公开搜索服务暂不可用。"}], "isError": True}
    results = {
        url: {"title": label[:240], "url": url}
        for label, raw_url in hits
        if (url := materials.safe_link("", raw_url))
    }
    return {"content": [{"type": "text", "text": json.dumps(list(results.values()), ensure_ascii=False)}]}


def serve(stdin, stdout, *, web=True, archive=(), skills=()):
    available = ([TOOL] if web else []) + ([ARCHIVE_TOOL] if archive else []) + ([SKILL_TOOL] if skills else [])
    remaining = 4
    for line in stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict) or "id" not in request:
            continue
        method = request.get("method")
        params = request.get("params")
        if not isinstance(params, dict):
            params = {}
        reply = {"jsonrpc": "2.0", "id": request["id"]}
        if method == "initialize":
            reply["result"] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "evosight_web", "version": "1.0"},
            }
        elif method == "ping":
            reply["result"] = {}
        elif method == "tools/list":
            reply["result"] = {"tools": available}
        elif method == "tools/call" and web and params.get("name") == TOOL["name"]:
            if remaining:
                remaining -= 1
                reply["result"] = search_result(params)
            else:
                reply["result"] = {"content": [{"type": "text", "text": "本次调用的公开搜索次数已用完。"}], "isError": True}
        elif method == "tools/call" and archive and params.get("name") == ARCHIVE_TOOL["name"]:
            reply["result"] = archive_result(params, archive)
        elif method == "tools/call" and skills and params.get("name") == SKILL_TOOL["name"]:
            reply["result"] = skill_result(params, skills)
        else:
            reply["error"] = {"code": -32601, "message": "Method or tool not found"}
        print(json.dumps(reply), file=stdout, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-file", type=Path)
    parser.add_argument("--archive-only", action="store_true")
    parser.add_argument("--skills-file", type=Path)
    options = parser.parse_args()
    archive = json.loads(options.archive_file.read_text(encoding="utf-8")) if options.archive_file else ()
    skills = json.loads(options.skills_file.read_text(encoding="utf-8")) if options.skills_file else ()
    serve(sys.stdin.buffer, sys.stdout, web=not options.archive_only, archive=archive, skills=skills)
