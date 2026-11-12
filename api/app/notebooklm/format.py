"""把已有字幕行整理成 Gemini Notebook 能直接当来源的文本。

行文与知识底稿使用的转写一致：`[mm:ss] 文本`，不按底稿的上下文字数截断。
超过官方文本来源上限（1_000_000 字节 / 500_000 词）时拆成多段。
"""
from __future__ import annotations

import re

# 留出标题和 JSON 封装余量，避免贴着官方 1_000_000 字节上限。
MAX_SOURCE_BYTES = 900_000
MAX_SOURCE_WORDS = 450_000

_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_CJK = re.compile(r"[\u4e00-\u9fff]")


def transcript_body(lines: list) -> str:
    """与 `draft_strategy._transcript_from_lines` 同一行格式，保留全部字幕。"""
    parts: list[str] = []
    for line in lines:
        text = (getattr(line, "text", None) or "").strip()
        if not text:
            continue
        start = getattr(line, "start_ms", None)
        if start is None:
            parts.append(text)
            continue
        try:
            sec = int(start) // 1000
            mm, ss = divmod(sec, 60)
            parts.append(f"[{mm:02d}:{ss:02d}] {text}")
        except (TypeError, ValueError):
            parts.append(text)
    return "\n".join(parts)


def render_plain(*, title: str, transcript: str) -> str:
    """纯文本回退。有标题时与底稿读取转写的包装相同。"""
    body = transcript.strip()
    title = (title or "").strip()
    if title:
        return f"视频标题：{title}\n\n字幕：\n{body}\n"
    if not body:
        return ""
    return body if body.endswith("\n") else body + "\n"


def render_markdown(
    *,
    title: str,
    transcript: str,
    source_url: str | None = None,
    language: str | None = None,
    subtitle_source: str | None = None,
) -> str:
    heading = (title or "").strip() or "视频转写"
    meta: list[str] = []
    if source_url:
        meta.append(f"- 视频：{source_url}")
    if language:
        meta.append(f"- 语言：{language}")
    if subtitle_source:
        meta.append(f"- 字幕来源：{subtitle_source}")
    blocks = [f"# {heading}"]
    if meta:
        blocks.append("\n".join(meta))
    blocks.append("## 转写")
    blocks.append(transcript.strip())
    blocks.append("本文由视频笔记本导出，可直接作为 Gemini Notebook（原 NotebookLM）的文本来源。")
    return "\n\n".join(blocks) + "\n"


def build_markdown_sources(
    *,
    title: str,
    transcript: str,
    source_url: str | None = None,
    language: str | None = None,
    subtitle_source: str | None = None,
) -> list[tuple[str, str]]:
    """返回 (显示名, markdown)。超限时按转写正文分段，每段都是独立来源。"""
    base = (title or "").strip() or "视频转写"
    pieces = split_for_notebooklm(transcript.strip()) or [""]
    total = len(pieces)
    sources: list[tuple[str, str]] = []
    for index, piece in enumerate(pieces, start=1):
        suffix = f"（{index}/{total}）" if total > 1 else ""
        heading = f"{base}{suffix}"
        document = render_markdown(
            title=heading,
            transcript=piece,
            source_url=source_url,
            language=language,
            subtitle_source=subtitle_source,
        )
        if fits_notebooklm_source(document):
            sources.append((_clip_name(heading), document))
            continue
        # 标题元数据把单段顶过上限时，再对完整文档切一刀。
        for part_index, part in enumerate(split_for_notebooklm(document), start=1):
            part_name = _clip_name(f"{heading} · {part_index}")
            sources.append((part_name, part if part.endswith("\n") else part + "\n"))
    return sources


def fits_notebooklm_source(text: str) -> bool:
    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        return False
    return estimate_words(text) <= MAX_SOURCE_WORDS


def estimate_words(text: str) -> int:
    cjk = len(_CJK.findall(text))
    rest = _CJK.sub(" ", text)
    return cjk + len(rest.split())


def split_for_notebooklm(text: str) -> list[str]:
    if text == "":
        return []
    if fits_notebooklm_source(text):
        return [text]
    atoms: list[str] = []
    for line in text.split("\n"):
        atoms.extend(_split_atom(line))
    chunks: list[str] = []
    buf: list[str] = []
    for atom in atoms:
        candidate = "\n".join([*buf, atom]) if buf else atom
        if buf and not fits_notebooklm_source(candidate):
            chunks.append("\n".join(buf))
            buf = [atom]
        else:
            buf.append(atom)
    if buf:
        chunks.append("\n".join(buf))
    return [chunk for chunk in chunks if chunk]


def safe_filename(title: str, ext: str) -> str:
    cleaned = _UNSAFE_FILENAME.sub(" ", title or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:80].strip(" .")
    stem = cleaned or "transcript"
    suffix = ext if ext.startswith(".") else f".{ext}"
    return f"{stem}{suffix}"


def notebook_web_url(resource_name: str) -> str:
    notebook_id = (resource_name or "").rstrip("/").split("/")[-1]
    return f"https://notebook.google.com/notebook/{notebook_id}"


def _clip_name(name: str) -> str:
    name = name.strip() or "视频转写"
    if len(name) <= 500:
        return name
    return name[:499] + "…"


def _split_atom(line: str) -> list[str]:
    if line == "" or fits_notebooklm_source(line):
        return [line]
    parts: list[str] = []
    remaining = line
    while remaining:
        if fits_notebooklm_source(remaining):
            parts.append(remaining)
            break
        lo, hi = 1, len(remaining)
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if fits_notebooklm_source(remaining[:mid]):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        parts.append(remaining[:best])
        remaining = remaining[best:]
    return parts
