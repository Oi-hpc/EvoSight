"""Bounded local OCR, subtitle and keyframe extraction for publisher attachments."""
import csv
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def tool(name):
    return os.environ.get("EVOSIGHT_" + name.upper().replace("-", "_")) or shutil.which(name)


def run(args, timeout=45):
    return subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def ocr(path, url, prefix="", suffix=""):
    binary = tool("tesseract")
    if not binary:
        return []
    langs = os.environ.get("EVOSIGHT_OCR_LANG", "eng")
    out = run([binary, str(path), "stdout", "-l", langs, "-c", "tessedit_create_tsv=1"]).decode(
        "utf-8", "replace"
    )
    lines = {}
    for word in csv.DictReader(io.StringIO(out), delimiter="\t"):
        if not word.get("text", "").strip() or float(word.get("conf", -1)) < 45:
            continue
        key = (word["block_num"], word["par_num"], word["line_num"])
        lines.setdefault(key, []).append(word)
    parts = []
    for words in lines.values():
        x = min(int(w["left"]) for w in words)
        y = min(int(w["top"]) for w in words)
        width = max(int(w["left"]) + int(w["width"]) for w in words) - x
        height = max(int(w["top"]) + int(w["height"]) for w in words) - y
        box = f"{x},{y},{width},{height}"
        text = re.sub(r"(?<=[\u3400-\u9fff]) (?=[\u3400-\u9fff])", "", " ".join(w["text"] for w in words))
        parts.append(
            (
                f"{prefix}图像区域 {box}",
                text,
                url + (suffix or "#xywh=pixel:" + box),
            )
        )
    return parts[:80]


def scanned_pages(resource, page_numbers):
    convert = tool("pdftoppm")
    if not convert or not tool("tesseract"):
        return []
    with tempfile.TemporaryDirectory(prefix="evosight-ocr-") as tmp:
        source = Path(tmp) / "source.pdf"
        source.write_bytes(resource.body)
        prefix = Path(tmp) / "page"
        parts = []
        for i in page_numbers[:4]:
            run(
                [
                    convert,
                    "-f",
                    str(i),
                    "-l",
                    str(i),
                    "-scale-to",
                    "1500",
                    "-singlefile",
                    "-png",
                    str(source),
                    str(prefix),
                ]
            )
            parts.extend(ocr(prefix.with_suffix(".png"), resource.url, f"第 {i} 页 ", "#page=" + str(i)))
        return parts


def extract_media(resource, kind):
    from .materials import Extract, caption_parts

    if kind == "auto":
        kind = "image" if resource.mime.startswith("image/") else "media"
    result = Extract(kind)
    with tempfile.TemporaryDirectory(prefix="evosight-media-") as tmp:
        source = Path(tmp) / "original"
        source.write_bytes(resource.body)
        if kind == "image":
            result.parts = ocr(source, resource.url)
        elif ffmpeg := tool("ffmpeg"):
            wav = Path(tmp) / "audio.wav"
            whisper = tool("whisper-cli")
            model = os.environ.get("EVOSIGHT_WHISPER_MODEL")
            media_input = [
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                "mov,matroska,webm,ogg,wav,mp3,flac,aac,avi,mpeg,mpegts,asf",
                "-threads",
                "1",
                "-i",
                str(source),
            ]
            if whisper and model:
                out = Path(tmp) / "words"
                lang = os.environ.get("EVOSIGHT_ASR_LANG", "auto")
                try:
                    run(
                        [
                            ffmpeg,
                            "-nostdin",
                            "-v",
                            "error",
                            *media_input,
                            "-t",
                            "180",
                            "-ar",
                            "16000",
                            "-ac",
                            "1",
                            str(wav),
                        ]
                    )
                    run([whisper, "-m", model, "-l", lang, "-f", str(wav), "-osrt", "-of", str(out)], 120)
                    captions = caption_parts(out.with_suffix(".srt").read_bytes(), resource.url)
                    result.parts = [
                        (label.replace("字幕", "自动转写", 1), text, link) for label, text, link in captions
                    ]
                except (OSError, subprocess.SubprocessError):
                    result.detail = "未取得可复核的音轨或转写"
            if not result.parts and not resource.mime.startswith("audio/") and tool("tesseract"):
                for second in (0, 15, 45):
                    image = Path(tmp) / f"frame-{second}.png"
                    run(
                        [
                            ffmpeg,
                            "-nostdin",
                            "-v",
                            "error",
                            "-ss",
                            str(second),
                            *media_input,
                            "-frames:v",
                            "1",
                            "-vf",
                            "scale=960:-2",
                            str(image),
                        ]
                    )
                    if image.exists():
                        result.parts.extend(ocr(image, resource.url, f"{second} 秒 ", "#t=" + str(second)))
    if not result.parts:
        result.detail = (result.detail + "。" if result.detail else "") + "保留发布方原件，暂无可核实的文字或字幕片段"
    return result
