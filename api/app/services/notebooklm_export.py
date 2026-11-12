"""把一条视频的现有字幕转写送到 Gemini Notebook，或导出为可手动导入的文件。"""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import BadRequestError, NotFoundError
from app.models.notebooklm import NotebookLmConnection
from app.models.user import User
from app.notebooklm.client import NotebookLmApi
from app.notebooklm.format import (
    build_markdown_sources,
    notebook_web_url,
    render_plain,
    safe_filename,
    transcript_body,
)
from app.notebooklm.oauth import exchange_auth_code, oauth_configured, refresh_access_token
from app.services.sources import get_video_readable

_NOTEBOOK_NAME = re.compile(r"^notebooks/[A-Za-z0-9_-]+$")


class NotebookLmNotConfiguredError(BadRequestError):
    code = "notebooklm_not_configured"

    def __init__(self) -> None:
        super().__init__(
            "服务器还没有配置 Gemini Notebook OAuth。"
            "管理员需要设置 NOTEBOOKLM_OAUTH_CLIENT_ID 和 NOTEBOOKLM_OAUTH_CLIENT_SECRET。"
            "在此之前可以下载 Markdown 或 TXT，再到 notebook.google.com 手动添加来源。"
        )


class NotebookLmNotConnectedError(BadRequestError):
    code = "notebooklm_not_connected"

    def __init__(self) -> None:
        super().__init__("还没有连接 Google 账号。请先到设置里连接，或下载 Markdown / TXT 手动导入。")


async def connection_status(session: AsyncSession, user_id: int) -> dict[str, Any]:
    row = await _connection(session, user_id)
    email = row.google_email if row and row.google_email else None
    return {
        "configured": oauth_configured(),
        "project_id_set": bool(get_settings().notebooklm_project_id.strip()),
        "connected": row is not None and bool(row.get_refresh_token()),
        "google_email": email,
    }


async def save_oauth_result(
    session: AsyncSession,
    *,
    user_id: int,
    token: dict[str, Any],
) -> NotebookLmConnection:
    refresh = str(token.get("refresh_token") or "")
    access = str(token.get("access_token") or "")
    row = await _connection(session, user_id)
    if row is None:
        if not refresh:
            raise BadRequestError("Google 没有返回刷新令牌，请重新连接并同意授权")
        row = NotebookLmConnection(user_id=user_id)
        session.add(row)
    if refresh:
        row.set_refresh_token(refresh)
    elif not row.get_refresh_token():
        raise BadRequestError("Google 没有返回刷新令牌，请重新连接并同意授权")
    row.set_access_token(access)
    expires_in = int(token.get("expires_in") or 3600)
    row.access_expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=expires_in)
    scope = token.get("scope")
    if isinstance(scope, str):
        row.scope = scope[:512]
    await session.commit()
    await session.refresh(row)
    return row


async def disconnect(session: AsyncSession, user_id: int) -> None:
    row = await _connection(session, user_id)
    if row is not None:
        await session.delete(row)
        await session.commit()


async def list_remote_notebooks(
    session: AsyncSession,
    *,
    user_id: int,
    page_token: str | None = None,
) -> dict[str, Any]:
    async with await open_api(session, user_id) as api:
        payload = await api.list_notebooks(page_token=page_token)
    notebooks = []
    for item in payload.get("notebooks") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        notebooks.append(
            {
                "name": name,
                "display_name": _display(item) or name,
                "url": notebook_web_url(name),
            }
        )
    next_token = payload.get("nextPageToken") or payload.get("next_page_token")
    return {
        "notebooks": notebooks,
        "next_page_token": str(next_token) if next_token else None,
    }


async def export_transcript(
    session: AsyncSession,
    *,
    viewer: User,
    video_id: int,
    target: str,
    notebook_title: str | None,
    notebook_name: str | None,
    wait_seconds: int,
) -> dict[str, Any]:
    document = await load_export_document(session, viewer_id=viewer.id, video_id=video_id)
    pieces = build_markdown_sources(
        title=document["title"],
        transcript=document["transcript"],
        source_url=document["source_url"],
        language=document["language"],
        subtitle_source=document["subtitle_source"],
    )
    if target == "existing":
        if not notebook_name or not _NOTEBOOK_NAME.fullmatch(notebook_name):
            raise BadRequestError("请选择一个已有的 Gemini Notebook")
    title = (notebook_title or document["title"] or "视频转写").strip()[:500] or "视频转写"

    async with await open_api(session, viewer.id) as api:
        if target == "new":
            created = await api.create_notebook(title)
            remote_name = str(created.get("name") or "")
            remote_title = _display(created) or title
        else:
            assert notebook_name is not None
            existing = await api.get_notebook(notebook_name)
            remote_name = str(existing.get("name") or notebook_name)
            remote_title = _display(existing) or title
        if not remote_name:
            raise BadRequestError("Gemini Notebook 没有返回笔记本编号")

        loop_time = asyncio.get_running_loop().time
        deadline = loop_time() + max(0, wait_seconds)
        sources: list[dict[str, Any]] = []
        for display_name, content in pieces:
            created_source = await api.create_text_source(
                remote_name,
                display_name=display_name,
                content=content,
            )
            source_name = str(created_source.get("name") or "")
            state = _state(created_source)
            error = _source_error(created_source)
            remaining = deadline - loop_time()
            if source_name and state not in {"ACTIVE", "FAILED"} and remaining > 0:
                polled = await api.wait_source(source_name, timeout_sec=remaining)
                state = _state(polled) or state
                error = _source_error(polled) or error
            sources.append(
                {
                    "name": source_name,
                    "display_name": display_name,
                    "state": state or "PROCESSING",
                    "error": error,
                }
            )
    return {
        "notebook_name": remote_name,
        "notebook_title": remote_title,
        "notebook_url": notebook_web_url(remote_name),
        "sources": sources,
    }


async def export_file(
    session: AsyncSession,
    *,
    viewer_id: int,
    video_id: int,
    kind: str,
) -> tuple[bytes, str, str]:
    document = await load_export_document(session, viewer_id=viewer_id, video_id=video_id)
    if kind == "txt":
        text = render_plain(title=document["title"], transcript=document["transcript"])
        media = "text/plain; charset=utf-8"
        filename = safe_filename(document["title"], "txt")
    else:
        pieces = build_markdown_sources(
            title=document["title"],
            transcript=document["transcript"],
            source_url=document["source_url"],
            language=document["language"],
            subtitle_source=document["subtitle_source"],
        )
        text = "\n\n".join(part for _, part in pieces)
        media = "text/markdown; charset=utf-8"
        filename = safe_filename(document["title"], "md")
    return text.encode("utf-8"), filename, media


async def load_export_document(
    session: AsyncSession,
    *,
    viewer_id: int,
    video_id: int,
) -> dict[str, Any]:
    from app.subtitles.store import load_subtitle

    video = await get_video_readable(session, viewer_id=viewer_id, video_id=video_id)
    subtitle, lines = await load_subtitle(session, video)
    transcript = transcript_body(lines)
    if not transcript.strip():
        if video.subtitle_status in {"pending", "processing"}:
            raise BadRequestError("字幕还在生成，完成后再导出到 NotebookLM")
        raise BadRequestError("还没有可导出的转写")
    return {
        "title": (video.title or "").strip() or "视频转写",
        "source_url": video.source_url,
        "language": subtitle.language if subtitle else None,
        "subtitle_source": subtitle.source if subtitle else None,
        "transcript": transcript,
    }


async def open_api(session: AsyncSession, user_id: int) -> NotebookLmApi:
    if not oauth_configured():
        raise NotebookLmNotConfiguredError()
    token = await ensure_access_token(session, user_id)
    return NotebookLmApi(token, project_id=get_settings().notebooklm_project_id)


async def ensure_access_token(session: AsyncSession, user_id: int) -> str:
    row = await _connection(session, user_id)
    if row is None or not row.get_refresh_token():
        raise NotebookLmNotConnectedError()
    now = datetime.now(UTC).replace(tzinfo=None)
    cached = row.get_access_token()
    if cached and row.access_expires_at and row.access_expires_at > now + timedelta(seconds=60):
        return cached
    refreshed = await refresh_access_token(row.get_refresh_token())
    access = str(refreshed["access_token"])
    row.set_access_token(access)
    expires_in = int(refreshed.get("expires_in") or 3600)
    row.access_expires_at = now + timedelta(seconds=expires_in)
    new_refresh = refreshed.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh:
        row.set_refresh_token(new_refresh)
    session.add(row)
    await session.commit()
    return access


async def finish_oauth_code(
    session: AsyncSession,
    *,
    user_id: int,
    code: str,
    redirect_uri: str,
) -> None:
    user = await session.get(User, user_id)
    if user is None or user.status != "active":
        raise NotFoundError("用户不存在或已停用")
    token = await exchange_auth_code(code=code, redirect_uri=redirect_uri)
    await save_oauth_result(session, user_id=user_id, token=token)


def with_query(path: str, **params: str) -> str:
    parts = urlsplit(path)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in params.items():
        if value:
            query[key] = value
    return urlunsplit(("", "", parts.path or "/", urlencode(query), ""))


async def _connection(session: AsyncSession, user_id: int) -> NotebookLmConnection | None:
    return (
        await session.execute(
            select(NotebookLmConnection).where(NotebookLmConnection.user_id == user_id)
        )
    ).scalar_one_or_none()


def _display(item: dict[str, Any]) -> str:
    return str(item.get("displayName") or item.get("display_name") or "")


def _state(item: dict[str, Any]) -> str:
    return str(item.get("state") or "")


def _source_error(item: dict[str, Any]) -> str | None:
    error = item.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        return message or None
    if isinstance(error, str) and error.strip():
        return error.strip()
    return None
